import concurrent.futures
import io
import json
import os
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from quality_input.common import InputError
from quality_llm import LLMClient, LLMError, validate_profile
from quality_llm.protocols import extract_response, response_diagnostics


def wire(protocol, text='{"answer":1}'):
    if protocol == "openai_responses":
        return {"status": "completed", "model": "returned-model", "id": "r",
                "output": [{"type": "reasoning", "content": [{"text": "private reasoning"}]},
                           {"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {"input_tokens": 7, "output_tokens": 3}}
    if protocol == "openai_chat":
        return {"model": "returned-model", "choices": [
            {"finish_reason": "stop", "message": {"content": text}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}
    if protocol == "anthropic":
        return {"model": "returned-model", "stop_reason": "end_turn",
                "content": [{"type": "thinking", "thinking": "private reasoning"},
                            {"type": "text", "text": text}],
                "usage": {"input_tokens": 7, "output_tokens": 3}}
    return {"modelVersion": "returned-model", "candidates": [
        {"finishReason": "STOP", "content": {"parts": [
            {"thought": True, "text": "private reasoning"}, {"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3}}


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Handler(BaseHTTPRequestHandler):
    records = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.records.append((self.path, dict(self.headers), body))
        if self.path.endswith("/responses"):
            protocol = "openai_responses"
        elif self.path.endswith("/chat/completions"):
            protocol = "openai_chat"
        elif self.path.endswith("/messages"):
            protocol = "anthropic"
        else:
            protocol = "gemini"
        response = json.dumps(wire(protocol)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class LLMTests(unittest.TestCase):
    def test_all_wire_adapters_against_local_http_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        Handler.records = []
        try:
            with patch.dict(os.environ, {"TEST_LLM_KEY": "fixture-key"}):
                for protocol in ("openai_responses", "openai_chat", "anthropic", "gemini"):
                    client = LLMClient({
                        "protocol": protocol, "model": "test-model",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                        "api_key_env": "TEST_LLM_KEY", "requests_per_second": 1000,
                        "output_mode": "json_schema" if protocol.startswith("openai") else "prompt",
                    })
                    result = client.complete("Return JSON", {"input": "evidence"},
                                             {"type": "object", "properties": {}, "required": [],
                                              "additionalProperties": False})
                    self.assertEqual(json.loads(result["text"]), {"answer": 1})
                    self.assertEqual(result["returned_model"], "returned-model")
                    self.assertEqual(result["usage"]["input_tokens"], 7)
                    self.assertNotIn("private reasoning", json.dumps(result))
            self.assertEqual(len(Handler.records), 4)
            responses, chat, claude, gemini = Handler.records
            self.assertEqual(responses[2]["text"]["format"]["type"], "json_schema")
            self.assertFalse(responses[2]["store"])
            self.assertEqual(responses[2]["truncation"], "disabled")
            self.assertEqual(chat[2]["response_format"]["type"], "json_schema")
            self.assertIn("max_completion_tokens", chat[2])
            self.assertEqual(claude[1]["Anthropic-Version"], "2023-06-01")
            self.assertIn("systemInstruction", gemini[2])
            self.assertIn("/models/test-model:generateContent", gemini[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_incomplete_refused_and_reasoning_only_responses_are_not_scores(self):
        for protocol, field, value in (
            ("openai_responses", "status", "incomplete"),
            ("anthropic", "stop_reason", "max_tokens"),
        ):
            response = wire(protocol)
            response[field] = value
            with self.assertRaises(ValueError):
                extract_response(protocol, response)
        response = wire("openai_chat")
        response["choices"][0]["finish_reason"] = "length"
        with self.assertRaises(ValueError):
            extract_response("openai_chat", response)
        response = wire("gemini")
        response["candidates"][0]["finishReason"] = "MAX_TOKENS"
        with self.assertRaises(ValueError):
            extract_response("gemini", response)
        response = wire("openai_responses")
        response["output"] = response["output"][:1]
        with self.assertRaises(ValueError):
            extract_response("openai_responses", response)

    def test_transient_retry_and_auth_error_not_retried_or_exposed(self):
        profile = {"protocol": "openai_chat", "model": "test",
                   "base_url": "http://127.0.0.1/v1", "max_attempts": 2, "requests_per_second": 1000}
        calls = []

        def transient(*args):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError("http://127.0.0.1", 429, "limited", {}, io.BytesIO(b"secret"))
            return wire("openai_chat")
        clock = FakeClock()
        with patch("quality_llm.client.time.sleep", side_effect=clock.sleep), \
                patch("quality_llm.client.time.monotonic", side_effect=lambda: clock.now):
            answer = LLMClient(profile, transient).complete("JSON", {}, {})
        self.assertEqual(answer["attempts"], 2)
        calls.clear()

        def unauthorized(*args):
            calls.append(1)
            raise urllib.error.HTTPError("http://127.0.0.1", 401, "secret-message", {}, io.BytesIO(b"secret"))
        with self.assertRaises(LLMError) as caught:
            LLMClient(profile, unauthorized).complete("JSON", {}, {})
        self.assertEqual(len(calls), 1)
        self.assertNotIn("secret", str(caught.exception))

    def test_client_enforces_its_own_concurrency(self):
        active = [0, 0]
        lock = threading.Lock()

        def transport(*args):
            with lock:
                active[0] += 1
                active[1] = max(active[1], active[0])
            time.sleep(0.015)
            with lock:
                active[0] -= 1
            return wire("openai_chat")
        client = LLMClient({"protocol": "openai_chat", "model": "test",
                            "base_url": "http://127.0.0.1/v1", "max_concurrency": 2,
                            "requests_per_second": 1000}, transport)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: client.complete("JSON", {}, {}), range(8)))
        self.assertEqual(active[1], 2)
        self.assertEqual(len(results), 8)

    def test_partial_responses_saved_without_resampling_or_private_reasoning(self):
        for protocol in ("openai_responses", "openai_chat", "anthropic", "gemini"):
            with self.subTest(protocol=protocol):
                response = wire(protocol, '{"answer":')
                if protocol == "openai_responses":
                    response.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
                elif protocol == "openai_chat":
                    response["choices"][0]["finish_reason"] = "length"
                    response["choices"][0]["message"]["reasoning_content"] = "private reasoning"
                elif protocol == "anthropic":
                    response["stop_reason"] = "max_tokens"
                else:
                    response["candidates"][0]["finishReason"] = "MAX_TOKENS"
                calls = []
                def transport(*args):
                    calls.append(1)
                    return response
                with patch.dict(os.environ, {"TEST_KEY": "fixture-key"}):
                    client = LLMClient({"protocol": protocol, "model": "test", "api_key_env": "TEST_KEY"}, transport)
                    with self.assertRaises(LLMError) as caught:
                        client.complete("JSON", {}, {})
                self.assertEqual(len(calls), 1)
                error = caught.exception
                self.assertEqual(error.code, "output_truncated")
                attempt = error.diagnostics["attempts"][0]
                self.assertEqual(attempt["response"]["text"], '{"answer":')
                self.assertEqual(attempt["http_status"], 200)
                self.assertEqual(attempt["outcome"], "output_truncated")
                self.assertNotIn("private reasoning", json.dumps(error.diagnostics))

    def test_refusal_empty_malformed_and_blocked_response_diagnostics(self):
        cases = [
            ("openai_chat", {"choices": [{"finish_reason": "stop", "message": {"content": None, "refusal": "Cannot answer"}}]}, "Cannot answer"),
            ("openai_responses", {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "Cannot answer"}]}]}, "Cannot answer"),
            ("gemini", {"promptFeedback": {"blockReason": "SAFETY"}}, None),
            ("anthropic", {"content": [{"type": "thinking", "thinking": "private reasoning"}]}, None),
            ("openai_chat", {"choices": None}, None),
            ("openai_chat", [], None),
        ]
        for protocol, data, refusal in cases:
            with self.subTest(protocol=protocol, data=data):
                result = response_diagnostics(protocol, data)
                self.assertEqual(result["text"], "")
                self.assertEqual(result["refusal"], refusal)
                self.assertNotIn("private reasoning", json.dumps(result))
        self.assertEqual(response_diagnostics("gemini", cases[2][1])["incomplete_reason"], "SAFETY")

    def test_http_retries_preserve_diagnostics_and_redact_credentials(self):
        calls = []
        def transport(*args):
            calls.append(1)
            if len(calls) == 1:
                error = {"error": {"message": "Rate limited fixture-key", "code": "rate_limit", "request_headers": {"Authorization": "other-secret"}}}
                raise urllib.error.HTTPError("http://127.0.0.1", 429, "limited", {}, io.BytesIO(json.dumps(error).encode()))
            return wire("openai_chat")
        profile = {"protocol": "openai_chat", "model": "test", "api_key_env": "TEST_KEY"}
        clock = FakeClock()
        with patch.dict(os.environ, {"TEST_KEY": "fixture-key"}), \
                patch("quality_llm.client.time.sleep", side_effect=clock.sleep), \
                patch("quality_llm.client.time.monotonic", side_effect=lambda: clock.now):
            answer = LLMClient(profile, transport).complete("JSON", {}, {})
        attempts = answer["diagnostics"]["attempts"]
        self.assertEqual([a["http_status"] for a in attempts], [429, 200])
        self.assertEqual(attempts[0]["response"]["error"]["message"], "Rate limited [REDACTED]")
        self.assertNotIn("fixture-key", json.dumps(answer))
        self.assertNotIn("other-secret", json.dumps(answer))

    def test_transport_failure_has_no_invented_response(self):
        def transport(*args):
            raise TimeoutError("sensitive network exception")
        profile = {"protocol": "openai_chat", "model": "test", "base_url": "http://127.0.0.1/v1", "max_attempts": 1}
        with self.assertRaises(LLMError) as caught:
            LLMClient(profile, transport).complete("JSON", {}, {})
        attempt = caught.exception.diagnostics["attempts"][0]
        self.assertIsNone(attempt["response"])
        self.assertIsNone(attempt["http_status"])
        self.assertNotIn("sensitive", json.dumps(attempt))

    def test_unparseable_http_body_is_reported_without_leaking_wire_bytes(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"reasoning_content":"private reasoning"'
        profile = {"protocol": "openai_chat", "model": "test", "base_url": "http://127.0.0.1/v1"}
        with patch("quality_llm.client.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(LLMError) as caught:
                LLMClient(profile).complete("JSON", {}, {})
        self.assertEqual(opener.open.call_count, 1)
        attempt = caught.exception.diagnostics["attempts"][0]
        self.assertEqual(attempt["http_status"], 200)
        self.assertIn("not valid UTF-8 JSON", attempt["body_note"])
        self.assertNotIn("private reasoning", json.dumps(attempt))

    def test_profile_configuration_cannot_override_control_fields_or_expose_keys(self):
        base = {"protocol": "openai_chat", "model": "test", "base_url": "http://127.0.0.1/v1"}
        for override in (
            {"parameters": {"model": "different"}},
            {"parameters": {"tools": []}},
            {"base_url": "https://example.com/v1?api_key=secret"},
            {"base_url": "http://remote.example/v1"},
            {"api_key": "literal-secret"},
        ):
            with self.assertRaises(InputError):
                validate_profile({**base, **override})
        with patch.dict(os.environ, {}, clear=True):
            client = LLMClient({**base, "api_key_env": "UNSET_TEST_KEY"})
            with self.assertRaisesRegex(LLMError, "UNSET_TEST_KEY"):
                client.check_credentials()

    def test_chat_thinking_parameter_is_explicit_and_validated(self):
        base = {"protocol": "openai_chat", "model": "test", "base_url": "http://127.0.0.1/v1"}
        for mode in ('enabled', 'disabled'):
            profile = validate_profile({**base, 'parameters': {'thinking': {'type': mode}}})
            self.assertEqual(profile['parameters']['thinking']['type'], mode)
        for thinking in (True, None, {}, {'type': 'auto'}, {'type': 'enabled', 'messages': []}):
            with self.subTest(thinking=thinking), self.assertRaises(InputError):
                validate_profile({**base, 'parameters': {'thinking': thinking}})


if __name__ == "__main__":
    unittest.main()

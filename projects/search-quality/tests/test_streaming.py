import io
import json
import unittest
from unittest.mock import patch

from quality_input.common import InputError
from quality_llm import LLMClient, LLMError, validate_profile
from quality_llm.protocols import build_request, extract_response, response_diagnostics
from quality_llm.streaming import read_chat_stream


def event(content=None, finish=None, **fields):
    delta = {} if content is None else {"content": content}
    return ('data: ' + json.dumps({"id": "stream-id", "model": "fixture-model", "choices": [
        {"index": 0, "delta": delta, "finish_reason": finish}], **fields}, ensure_ascii=False) + '\r\n\r\n').encode()


class StreamingTests(unittest.TestCase):
    def test_collects_visible_unicode_answer_and_final_usage_without_reasoning(self):
        raw = (b': keepalive\r\n\r\n' + event(choices=[{"index": 0, "delta": {"reasoning_content": "private reasoning"}}]) +
               event('{"回答":') + event('"正确"}', 'stop') +
               event(choices=[], usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}) +
               b'data: [DONE]\r\n\r\n')
        data = read_chat_stream(io.BytesIO(raw), 10)
        answer = extract_response("openai_chat", data)
        self.assertEqual(answer["text"], '{"回答":"正确"}')
        self.assertEqual(answer["usage"]["output_tokens"], 20)
        self.assertTrue(data["stream_info"]["completed"])
        self.assertEqual(data["stream_info"]["answer_chunks"], 2)
        self.assertEqual(data["stream_info"]["reasoning_chunks"], 1)
        self.assertNotIn("private reasoning", json.dumps(data))

    def test_length_refusal_missing_done_and_malformed_events_are_not_scores(self):
        for raw in (
            event('{"partial":', 'length') + b'data: [DONE]\n\n',
            event(choices=[{"delta": {"refusal": "Cannot answer"}, "finish_reason": "stop"}]) + b'data: [DONE]\n\n',
            event('{"answer":1}', 'stop'),
            event('{"partial":') + b'data: broken JSON\n\n',
            event('{"partial":') + b'data: {"error":{"code":"Limit","message":"Limit reached"}}\n\n',
        ):
            with self.subTest(raw=raw):
                data = read_chat_stream(io.BytesIO(raw), 10)
                with self.assertRaises(ValueError):
                    extract_response("openai_chat", data)
                diagnostics = response_diagnostics("openai_chat", data)
                self.assertIn("stream_info", diagnostics)
                self.assertTrue(diagnostics["text"] or diagnostics["refusal"])

    def test_partial_stream_network_failure_is_saved_without_retry(self):
        class Interrupted(io.BytesIO):
            def readline(self, *args):
                line = super().readline(*args)
                if not line:
                    raise ConnectionResetError("sensitive network detail")
                return line
        def transport(*args):
            return read_chat_stream(Interrupted(event('{"partial":')), 10)
        profile = {"protocol": "openai_chat", "model": "fixture-model", "base_url": "http://127.0.0.1", "stream": True}
        with patch("quality_llm.client.time.sleep") as sleep:
            with self.assertRaises(LLMError) as caught:
                LLMClient(profile, transport).complete("JSON", {}, {})
        self.assertEqual(caught.exception.attempts, 1)
        sleep.assert_not_called()
        response = caught.exception.diagnostics["attempts"][0]["response"]
        self.assertEqual(response["text"], '{"partial":')
        self.assertIn("interrupted", response["stream_info"]["error"])
        self.assertNotIn("sensitive", json.dumps(response))

    def test_stream_deadline_and_size_limits(self):
        with patch("quality_llm.streaming.time.monotonic", side_effect=[0, 11]):
            data = read_chat_stream(io.BytesIO(b''), 10)
        self.assertEqual(data["stream_info"]["error_code"], "stream_total_timeout")
        data = read_chat_stream(io.BytesIO(b'data: ' + b'x' * (1024 * 1024)), 10)
        self.assertIn("size limit", data["stream_info"]["error"])

    def test_total_time_budget_does_not_stop_an_active_stream_after_120_seconds(self):
        stream = io.BytesIO(event('{"answer":1}', 'stop') + b'data: [DONE]\n\n')
        with patch("quality_llm.streaming.time.monotonic", side_effect=[0, 121, 122, 123, 124]):
            data = read_chat_stream(stream, 600)
        self.assertTrue(data["stream_info"]["completed"])
        self.assertEqual(extract_response('openai_chat', data)['text'], '{"answer":1}')

    def test_socket_idle_timeout_is_distinct_from_generation_deadline(self):
        class Stalled(io.BytesIO):
            def readline(self, *args):
                raise TimeoutError()
        data = read_chat_stream(Stalled(), 600)
        self.assertEqual(data['stream_info']['error_code'], 'stream_idle_timeout')
        self.assertEqual(data['stream_info']['events'], 0)

    def test_socket_timeout_and_stream_deadline_are_passed_separately(self):
        from unittest.mock import MagicMock
        from quality_llm.client import http_transport
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.headers = {'Content-Type': 'text/event-stream'}
        with patch('quality_llm.client.urllib.request.build_opener', return_value=opener), patch('quality_llm.client.read_chat_stream', return_value={}) as reader:
            http_transport('http://127.0.0.1', {}, {'stream': True}, 120, stream_timeout=600)
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 120)
        reader.assert_called_once_with(response, 600)
        profile = {'protocol': 'openai_chat', 'model': 'test', 'base_url': 'http://127.0.0.1', 'stream': True}
        for value in (0, 1801, '600', True):
            with self.assertRaises(InputError):
                validate_profile({**profile, 'stream_timeout_seconds': value})
        self.assertEqual(validate_profile({**profile, 'stream_timeout_seconds': 600})['stream_timeout_seconds'], 600)

    def test_explicit_stream_config_only_changes_chat_requests(self):
        profile = validate_profile({"protocol": "openai_chat", "model": "fixture-model", "api_key_env": "FIXTURE_KEY",
                                    "base_url": "https://maas.qianwenaiapi.com/compatible-mode/v1", "stream": True,
                                    "token_limit_field": "max_tokens"})
        url, headers, body = build_request(profile, "JSON", {}, {}, "fixture-key")
        self.assertEqual(url, "https://maas.qianwenaiapi.com/compatible-mode/v1/chat/completions")
        self.assertTrue(body["stream"])
        self.assertTrue(body["stream_options"]["include_usage"])
        self.assertIn("max_tokens", body)
        self.assertNotIn("catalog_models", body)
        without_usage_request = validate_profile({**profile, 'stream_include_usage': False})
        _, _, body = build_request(without_usage_request, "JSON", {}, {}, "fixture-key")
        self.assertTrue(body['stream'])
        self.assertNotIn('stream_options', body)
        for extra in ({'stream_include_usage': 'false'}, {'stream': False, 'stream_include_usage': False}):
            with self.assertRaises(InputError):
                validate_profile({**profile, **extra})
        for extra in ({"stream": "true"}, {"protocol": "anthropic", "stream": True}):
            with self.assertRaises(InputError):
                validate_profile({**profile, **extra})

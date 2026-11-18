import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import Mock
from quality_input.common import InputError

from quality_llm import LLMError
from quality_llm.models import list_models, models_endpoint


class ModelDiscoveryTests(unittest.TestCase):
    def profile(self, protocol="openai_chat", **overrides):
        return {"protocol": protocol, "model": "configured-model",
                "api_key_env": "TEST_API_KEY", **overrides}

    def test_root_versioned_and_custom_base_paths(self):
        for base, expected in (
            ("https://api.deepseek.com", "https://api.deepseek.com/v1/models"),
            ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1/models"),
            ("https://example.com/compatible/v1", "https://example.com/compatible/v1/models"),
        ):
            with self.subTest(base=base):
                self.assertEqual(models_endpoint(self.profile(base_url=base)), expected)

    def test_real_get_auth_and_http_error_without_response_body(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                calls.append((self.command, self.path, self.headers.get("Authorization")))
                if len(calls) == 1:
                    code, body = 200, {"data": [{"id": "model-fast"}, {"id": "model-pro"},
                                              {"id": "model-fast"}]}
                else:
                    code, body = 401, {"error": "private-response-and-fixture-key"}
                self.send_response(code)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            profile = self.profile(base_url=f"http://127.0.0.1:{server.server_port}")
            result = list_models(profile, "fixture-key")
            self.assertEqual([m["id"] for m in result["models"]], ["model-fast", "model-pro"])
            self.assertEqual(calls[0], ("GET", "/v1/models", "Bearer fixture-key"))
            with self.assertRaises(LLMError) as caught:
                list_models(profile, "fixture-key")
            self.assertEqual(caught.exception.code, "models_http_error")
            self.assertIn("401", str(caught.exception))
            self.assertNotIn("fixture-key", str(caught.exception))
            self.assertEqual(len(calls), 2)  # No silent retry or generation.
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_native_protocols_fetch_all_pages(self):
        anthropic = Mock(side_effect=[
            {"data": [{"id": "claude-a"}], "has_more": True, "last_id": "claude-a"},
            {"data": [{"id": "claude-b"}], "has_more": False},
        ])
        result = list_models(self.profile("anthropic"), "fixture-key", anthropic)
        self.assertEqual([m["id"] for m in result["models"]], ["claude-a", "claude-b"])
        self.assertIn("after_id=claude-a", anthropic.call_args_list[1].args[0])
        self.assertEqual(anthropic.call_args.args[1]["x-api-key"], "fixture-key")
        gemini = Mock(side_effect=[
            {"models": [{"name": "models/gemini-a", "supportedGenerationMethods": ["generateContent"]}],
             "nextPageToken": "page two"},
            {"models": [{"name": "models/embedding-a", "supportedGenerationMethods": ["embedContent"]}]},
        ])
        result = list_models(self.profile("gemini"), "fixture-key", gemini)
        self.assertEqual([m["id"] for m in result["models"]], ["gemini-a", "embedding-a"])
        self.assertEqual([m["selectable"] for m in result["models"]], [True, False])
        self.assertIn("/v1beta/models?pageSize=1000&pageToken=page+two", gemini.call_args.args[0])
        self.assertEqual(gemini.call_args.args[1]["x-goog-api-key"], "fixture-key")

    def test_incomplete_response_never_returns_partial_catalog(self):
        cases = [
            Mock(return_value={}),
            Mock(return_value={"data": [{"id": ""}]}),
            Mock(side_effect=[{"data": [{"id": "a"}], "has_more": True}, {"data": "broken"}]),
            Mock(return_value={"data": [{"id": "a"}], "has_more": True, "last_id": "a"}),
        ]
        for transport in cases:
            with self.subTest(transport=transport), self.assertRaises(LLMError) as caught:
                list_models(self.profile(), "fixture-key", transport)
            self.assertEqual(caught.exception.code, "models_invalid_response")

    def test_mock_and_missing_credentials_do_not_call_provider(self):
        transport = Mock()
        result = list_models({"protocol": "mock", "model": "local-mock"}, transport=transport)
        self.assertEqual(result["source"], "simulation")
        self.assertEqual(result["models"][0]["id"], "local-mock")
        with self.assertRaises(LLMError) as caught:
            list_models(self.profile(), transport=transport)
        self.assertEqual(caught.exception.code, "missing_credentials")
        transport.assert_not_called()

    def test_configured_catalog_is_explicit_and_does_not_probe_models_endpoint(self):
        transport = Mock(side_effect=AssertionError("This provider has no /models"))
        profile = self.profile(model="qwen3.8-flash", catalog_models=["qwen3.8-flash", "qwen3.7-plus"])
        result = list_models(profile, "fixture-key", transport)
        self.assertEqual(result["source"], "configured")
        self.assertIsNone(result["endpoint"])
        self.assertEqual([m["id"] for m in result["models"]], profile["catalog_models"])
        transport.assert_not_called()
        with self.assertRaises(LLMError):
            list_models(profile, "", transport)
        for catalog in ([], ["qwen3.8-flash", "qwen3.8-flash"], ["unknown"], [123]):
            with self.assertRaises(InputError):
                list_models({**profile, "catalog_models": catalog}, "fixture-key", transport)


if __name__ == "__main__":
    unittest.main()

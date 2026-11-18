import unittest

from frontend.diagnostics import failure_info
from quality_llm.protocols import response_failure_code


class ResponseFailureTests(unittest.TestCase):
    def test_rate_limit_diagnostics_cover_historical_and_new_records(self):
        row = {"judgment_status": "error", "error_code": "http_error", "reason": "HTTP 429"}
        self.assertEqual(failure_info(row)["code"], "provider_rate_limited")
        diagnostics = {"attempts": [{"http_status": 429}], "retry_stop_reason": "wait_budget_exhausted"}
        info = failure_info({**row, "reason": ""}, diagnostics)
        self.assertEqual(info["code"], "provider_rate_limited")
        self.assertIn("等待预算", info["description"])
        self.assertEqual(row["error_code"], "http_error")
        self.assertEqual(failure_info({**row, "reason": "HTTP 402"})["code"], "provider_payment_required")
        self.assertEqual(failure_info({**row, "reason": "", "error_code": "retry_wait_exhausted"})["code"], "retry_wait_exhausted")

    def test_failure_causes_are_distinguished_without_assuming_a_refusal(self):
        cases = [
            ({'text': 'partial', 'finish_reason': 'length'}, 'output_truncated'),
            ({'text': '', 'refusal': 'Cannot answer', 'finish_reason': 'stop'}, 'model_refused'),
            ({'text': '', 'finish_reason': 'stop'}, 'empty_response'),
            ({'text': '', 'stream_info': {'error_code': 'stream_total_timeout'}}, 'stream_total_timeout'),
            ({'text': '', 'stream_info': {'error_code': 'stream_idle_timeout'}}, 'stream_idle_timeout'),
            ({'text': '', 'stream_info': {'completed': False, 'events': 1397, 'error': 'Stream interrupted or timed out'}}, 'stream_timeout_or_interrupted'),
        ]
        for response, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(response_failure_code(response), expected)
                row = {'judgment_status': 'error', 'error_code': 'incomplete_response'}
                info = failure_info(row, {'attempts': [{'response': response}]})
                self.assertEqual(info['code'], expected)
                self.assertEqual(row['error_code'], 'incomplete_response')  # Historical exports stay unchanged.
        self.assertIsNone(failure_info({'judgment_status': 'rated'}))
        self.assertEqual(failure_info({'judgment_status': 'error', 'error_code': 'incomplete_response'})['code'], 'incomplete_response')

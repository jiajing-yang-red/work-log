import concurrent.futures
import http.client
import io
import threading
import time
import unittest
import urllib.error
from contextlib import contextmanager
from email.message import Message
from email.utils import formatdate
from unittest.mock import patch

from quality_input.common import InputError
from quality_llm import LLMClient, LLMError, validate_profile
from quality_llm.retry import backoff_seconds, retry_after_seconds
from test_llm import FakeClock, wire


BASE = {"protocol": "openai_chat", "model": "test", "base_url": "http://127.0.0.1/v1",
        "max_attempts": 4, "requests_per_second": 1000}


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("http://127.0.0.1", code, "fixture", headers, io.BytesIO(b"{}"))


@contextmanager
def fake_time():
    clock = FakeClock()
    with patch("quality_llm.client.time.sleep", side_effect=clock.sleep), \
            patch("quality_llm.client.time.monotonic", side_effect=lambda: clock.now), \
            patch("quality_llm.retry.random.uniform", side_effect=lambda low, high: (low + high) / 2):
        yield clock


class RetryTests(unittest.TestCase):
    def test_incomplete_http_response_uses_bounded_transport_retries(self):
        for exception in (http.client.IncompleteRead(b"private partial body"), http.client.BadStatusLine("bad status")):
            calls = []
            with self.subTest(exception=type(exception).__name__), fake_time() as clock:
                def transport(*args):
                    calls.append(clock.now)
                    if len(calls) == 1:
                        raise exception
                    return wire("openai_chat")
                result = LLMClient(BASE, transport).complete("JSON", {}, {})
                self.assertEqual(calls, [1000, 1003])
                record = result["diagnostics"]["attempts"][0]
                self.assertEqual(record["outcome"], "transport_error")
                self.assertEqual(record["transport_exception"], type(exception).__name__)
                self.assertNotIn("private partial body", str(result))

    def test_incomplete_http_response_exhaustion_is_a_task_failure(self):
        with fake_time():
            def transport(*args):
                raise http.client.IncompleteRead(b"")
            with self.assertRaises(LLMError) as caught:
                LLMClient({**BASE, "max_attempts": 2}, transport).complete("JSON", {}, {})
        self.assertEqual(caught.exception.code, "transport_error")
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(caught.exception.diagnostics["attempts"][-1]["retry_stop_reason"], "max_attempts")

    def test_incomplete_http_error_body_preserves_http_retry_policy(self):
        class BrokenBody(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(b"")
        for status in (401, 503):
            calls = []
            with self.subTest(status=status), fake_time():
                def transport(*args):
                    calls.append(1)
                    if len(calls) == 1:
                        raise urllib.error.HTTPError("http://127.0.0.1", status, "fixture", {}, BrokenBody())
                    return wire("openai_chat")
                client = LLMClient(BASE, transport)
                if status == 401:
                    with self.assertRaises(LLMError) as caught:
                        client.complete("JSON", {}, {})
                    self.assertEqual(caught.exception.code, "http_error")
                    self.assertEqual(caught.exception.attempts, 1)
                else:
                    result = client.complete("JSON", {}, {})
                    self.assertEqual(result["attempts"], 2)
                    self.assertEqual(result["diagnostics"]["attempts"][0]["http_status"], 503)

    def test_retry_after_seconds_dates_invalid_and_oversize(self):
        now = 1700000000
        for value, expected in ((" 12 ", 12), ("0", 0),
                                (formatdate(now + 30, usegmt=True), 30),
                                (formatdate(now - 10, usegmt=True), 0),
                                (None, None), ("-1", None), ("1.5", None),
                                ("nan", None), ("invalid", None), ("", None)):
            with self.subTest(value=value):
                self.assertEqual(retry_after_seconds(value, now), expected)
        self.assertGreater(retry_after_seconds("9" * 1000, now), 1800)

    def test_equal_jitter_grows_and_caps_without_zero_delay(self):
        with patch("quality_llm.retry.random.uniform", side_effect=lambda low, high: low) as draw:
            self.assertEqual([backoff_seconds(n, 4, 10) for n in range(1, 5)], [2, 4, 5, 5])
            self.assertEqual(draw.call_args_list[2].args, (5, 10))

    def test_four_attempts_use_growing_delays_and_preserve_failure_records(self):
        calls = []
        with fake_time() as clock:
            def transport(*args):
                calls.append(clock.now)
                raise http_error(429)
            with self.assertRaises(LLMError) as caught:
                LLMClient(BASE, transport).complete("JSON", {}, {})
        self.assertEqual(calls, [1000, 1003, 1009, 1021])
        error = caught.exception
        self.assertEqual(error.attempts, 4)
        self.assertEqual(error.diagnostics["wait_seconds"], 21)
        attempts = error.diagnostics["attempts"]
        self.assertEqual([r["http_status"] for r in attempts], [429] * 4)
        self.assertEqual([r["will_retry"] for r in attempts], [True, True, True, False])
        self.assertEqual(attempts[-1]["retry_stop_reason"], "max_attempts")

    def test_retry_after_is_a_minimum_not_truncated_by_backoff_cap(self):
        for header in ("30", formatdate(1700000030, usegmt=True)):
            calls = []
            with self.subTest(header=header), fake_time() as clock, \
                    patch("quality_llm.client.time.time", return_value=1700000000):
                def transport(*args):
                    calls.append(clock.now)
                    if len(calls) == 1:
                        raise http_error(503, header)
                    return wire("openai_chat")
                result = LLMClient({**BASE, "retry_max_seconds": 4}, transport).complete("JSON", {}, {})
                self.assertEqual(calls, [1000, 1030])
                self.assertEqual(result["diagnostics"]["attempts"][0]["retry_after_seconds"], 30)
                self.assertEqual(result["diagnostics"]["attempts"][1]["wait_before_seconds"], 30)

    def test_long_provider_wait_stops_without_early_retry_and_blocks_new_work(self):
        calls = []
        with fake_time() as clock:
            def transport(*args):
                calls.append(clock.now)
                raise http_error(429, "600")
            client = LLMClient(BASE, transport)
            with self.assertRaises(LLMError) as caught:
                client.complete("JSON", {}, {})
            self.assertEqual(caught.exception.code, "http_error")
            self.assertEqual(caught.exception.diagnostics["retry_stop_reason"], "wait_budget_exhausted")
            self.assertFalse(caught.exception.diagnostics["attempts"][0]["will_retry"])
            with self.assertRaises(LLMError) as blocked:
                client.complete("JSON", {}, {})
            self.assertEqual(blocked.exception.code, "retry_wait_exhausted")
            self.assertEqual(blocked.exception.attempts, 0)
            self.assertEqual(calls, [1000])

    def test_wait_budget_is_cumulative(self):
        with fake_time() as clock:
            def transport(*args):
                raise http_error(429)
            with self.assertRaises(LLMError) as caught:
                LLMClient({**BASE, "retry_wait_budget_seconds": 8}, transport).complete("JSON", {}, {})
            self.assertEqual(caught.exception.attempts, 2)
            self.assertEqual(clock.sleeps, [3])
            self.assertEqual(caught.exception.diagnostics["retry_stop_reason"], "wait_budget_exhausted")

    def test_retryable_statuses_and_network_errors_recover(self):
        for failure in (408, 429, 500, 502, 503, 504, "network"):
            calls = []
            with self.subTest(failure=failure), fake_time() as clock:
                def transport(*args):
                    calls.append(clock.now)
                    if len(calls) == 1:
                        if failure == "network":
                            raise TimeoutError("fixture")
                        raise http_error(failure)
                    return wire("openai_chat")
                result = LLMClient(BASE, transport).complete("JSON", {}, {})
                self.assertEqual(calls, [1000, 1003])
                self.assertEqual(result["attempts"], 2)

    def test_permanent_errors_do_not_retry_even_with_header(self):
        for status in (400, 401, 402, 403, 404, 422):
            calls = []
            with self.subTest(status=status), fake_time() as clock:
                def transport(*args):
                    calls.append(1)
                    raise http_error(status, "30")
                with self.assertRaises(LLMError) as caught:
                    LLMClient(BASE, transport).complete("JSON", {}, {})
                self.assertEqual(len(calls), 1)
                self.assertFalse(clock.sleeps)
                self.assertEqual(caught.exception.diagnostics["attempts"][0]["retry_stop_reason"], "non_retryable")

    def test_cooldown_is_shared_even_when_failed_task_has_no_attempts_left(self):
        with fake_time() as clock:
            calls = []
            def transport(*args):
                calls.append(clock.now)
                if len(calls) == 1:
                    raise http_error(429, "10")
                return wire("openai_chat")
            client = LLMClient({**BASE, "max_attempts": 1}, transport)
            with self.assertRaises(LLMError):
                client.complete("JSON", {}, {})
            # A different model/client must not inherit this client's cooldown.
            other = LLMClient(BASE, lambda *args: wire("openai_chat"))
            self.assertEqual(other.complete("JSON", {}, {})["diagnostics"]["wait_seconds"], 0)
            client.complete("JSON", {}, {})
            self.assertEqual(calls, [1000, 1010])

    def test_sleepers_recheck_extensions_and_do_not_burst_after_cooldown(self):
        client = LLMClient({**BASE, "requests_per_second": 50, "max_concurrency": 8},
                           lambda *args: wire("openai_chat"))
        first = time.monotonic() + 0.08
        client._cool_down(first)
        starts, lock = [], threading.Lock()
        def transport(*args):
            with lock:
                starts.append(time.monotonic())
            return wire("openai_chat")
        client.transport = transport
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(client.complete, "JSON", {}, {}) for _ in range(4)]
            time.sleep(0.025)
            extended = time.monotonic() + 0.12
            client._cool_down(extended)
            results = [f.result(timeout=3) for f in futures]
        self.assertEqual(len(results), 4)
        self.assertGreaterEqual(min(starts), extended - 0.001)
        self.assertTrue(all(b - a >= 0.018 for a, b in zip(sorted(starts), sorted(starts)[1:])))

    def test_invalid_retry_settings_are_rejected(self):
        for values in ({"retry_base_seconds": 0}, {"retry_max_seconds": float("nan")},
                       {"retry_wait_budget_seconds": float("inf")}, {"retry_base_seconds": True},
                       {"retry_base_seconds": 5, "retry_max_seconds": 4}):
            with self.subTest(values=values), self.assertRaises(InputError):
                validate_profile({**BASE, **values})


if __name__ == "__main__":
    unittest.main()

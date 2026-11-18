"""Offline integration tests: no external inference endpoints are contacted."""
import copy
import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from frontend.service import PROJECT, Service, prompt_content
from quality_eval import block_needs_met as block
from quality_eval.config import validate_config
from quality_eval.execution import BatchPaused, execute_task, execute_tasks
from quality_eval.recovery import DEFAULT_POLICY, accounting, diagnose, execute_with_recovery, preserve_valid, validate_policy
from quality_eval.runner import plan, run
from quality_eval.schemes import make_tasks, prompt_for
from quality_eval.storage import checked_read, checked_write
from quality_input.common import InputError, canonical, digest
from quality_input.pipeline import import_dataset, load_dataset
from quality_llm import LLMClient
from test_llm import wire
from test_evaluation_layer import valid_answer
from quality_eval.evidence import validate_output


GOOD = {"score": 3, "reason": "测试说明。", "evidence": ["fixture text"]}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = {"protocol": "openai_chat", "model": "fixture", "base_url": "http://127.0.0.1:1/v1",
                        "max_output_tokens": 100, "max_attempts": 2, "max_concurrency": 2,
                        "requests_per_second": 1000, "retry_base_seconds": .001, "retry_max_seconds": .002}
        self.config = validate_config({"models": {"m": self.profile}, "query_understanding": "direct",
                                      "experiments": [{"id": "block", "scheme": block.SCHEME, "model": "m"}],
                                      "failure_recovery": {**DEFAULT_POLICY, "max_output_tokens": 200}})
        self.payload = {"id": "row", "input_index": 0, "query": "fixture query", "title": "fixture title",
                        "snippet": "fixture text", "url": "https://example.test"}
        system = prompt_for(block.KIND, "direct")
        self.task = {"task_id": "a" * 64, "kind": block.KIND, "model_profile": "m", "prompt_hash": digest(system),
                     "payload": self.payload, "system": system, "blocked_reason": None,
                     "block_output_contract": block.COMPACT_CONTRACT}
        self.requests = []
        self.delay = patch("quality_eval.recovery.backoff_seconds", return_value=0)
        self.delay.start()

    def tearDown(self):
        self.delay.stop()
        self.temp.cleanup()

    def client(self, responses):
        queue = iter(responses)
        def transport(url, headers, body, timeout):
            self.requests.append(copy.deepcopy(body))
            value = next(queue)
            if isinstance(value, BaseException):
                raise value
            return value
        return LLMClient(self.profile, transport=transport)

    def execute(self, responses, **kwargs):
        return execute_with_recovery(self.root, self.task, self.client(responses), self.config, 1, execute_task, **kwargs)

    def good(self, score=3):
        return wire("openai_chat", canonical({**GOOD, "score": score}))

    def test_json_then_evidence_then_success_keeps_all_responses(self):
        result = self.execute([wire("openai_chat", "bad JSON"), wire("openai_chat", canonical({**GOOD, "evidence": ["invented"]})), self.good()])
        recovery = result["auto_recovery"]
        self.assertEqual(result["status"], "success")
        self.assertTrue(recovery["recovered"])
        self.assertEqual(recovery["attempt_count"], 3)
        self.assertEqual([r["cause"] for r in recovery["attempts"]], ["invalid_json", "evidence_invalid", "no_recovery_needed"])
        self.assertIn("无法解析", self.requests[1]["messages"][0]["content"])
        self.assertIn("逐字复制", self.requests[2]["messages"][0]["content"])
        for request in self.requests:
            self.assertEqual(json.loads(request["messages"][1]["content"]), self.payload)
            self.assertEqual(prompt_content(request["messages"][0]["content"], self.payload)["schema"], block.output_schema(block.COMPACT_CONTRACT))
        self.assertEqual(len(result["new_call_records"]), 3)
        self.assertEqual(sum(c["input_tokens"] for c in result["new_call_records"]), 21)
        self.assertNotEqual(result["effective_prompt_hash"], self.task["prompt_hash"])
        self.assertEqual(checked_read(self.root / recovery["attempts"][0]["path"])["call"]["text"], "bad JSON")

    def test_schema_missing_field_corrected(self):
        result = self.execute([wire("openai_chat", canonical({"score": 3})), self.good()])
        self.assertEqual(result["auto_recovery"]["attempts"][0]["action"], "correct_schema")
        self.assertIn("必填字段", self.requests[1]["messages"][0]["content"])

    def test_valid_zero_and_abstention_never_retried(self):
        for score in (0, None):
            with self.subTest(score=score):
                root = self.root / str(score)
                root.mkdir()
                result = execute_with_recovery(root, self.task, self.client([self.good(score)]), self.config, 1, execute_task)
                self.assertEqual(result["auto_recovery"]["attempt_count"], 1)
                self.assertFalse(result["auto_recovery"]["recovered"])

    def test_truncation_increases_only_per_call_limit_and_accounts_failed_usage(self):
        truncated = self.good()
        truncated["choices"][0]["finish_reason"] = "length"
        client = self.client([truncated, self.good()])
        slots, lock = client.slots, client.rate_lock
        result = execute_with_recovery(self.root, self.task, client, self.config, 1, execute_task)
        self.assertEqual([r["max_completion_tokens"] for r in self.requests], [100, 200])
        self.assertEqual(client.profile["max_output_tokens"], 100)
        self.assertIs(client.slots, slots)
        self.assertIs(client.rate_lock, lock)
        self.assertEqual(result["request"]["max_output_tokens"], 200)
        self.assertEqual(sum(c["output_tokens"] for c in result["new_call_records"]), 6)
        self.assertEqual(sum(c["completed"] for c in result["new_call_records"]), 1)

    def test_truncation_stops_at_cap(self):
        truncated = self.good()
        truncated["choices"][0]["finish_reason"] = "length"
        result = self.execute([truncated, truncated])
        self.assertEqual(result["auto_recovery"]["stop_reason"], "output_budget_cap")
        self.assertEqual(len(self.requests), 2)
        self.assertIsNone(result["judgment"])

    def test_permanent_http_and_refusal_are_not_retried(self):
        refused = self.good()
        refused["choices"][0]["message"] = {"content": None, "refusal": "refused"}
        for status in (400, 401, 402, 403, 404, 422, "refusal"):
            with self.subTest(status=status):
                root = self.root / str(status)
                root.mkdir()
                response = refused if status == "refusal" else urllib.error.HTTPError("url", status, "error", {}, io.BytesIO(b"{}"))
                before = len(self.requests)
                result = execute_with_recovery(root, self.task, self.client([response]), self.config, 1, execute_task)
                self.assertEqual(len(self.requests) - before, 1)
                self.assertEqual(result["auto_recovery"]["attempt_count"], 1)

    def test_exhausted_http_retries_are_not_reset_by_outer_recovery(self):
        errors = [urllib.error.HTTPError("url", 503, "error", {}, io.BytesIO(b"{}")) for _ in range(2)]
        result = self.execute(errors)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["auto_recovery"]["stop_reason"], "transport_retries_exhausted")
        self.assertEqual(result["new_call_records"][0]["http_attempts"], 2)

    def test_retry_after_wait_budget_is_not_bypassed(self):
        error = urllib.error.HTTPError("url", 429, "error", {"Retry-After": "3600"}, io.BytesIO(b"{}"))
        result = self.execute([error])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["response_diagnostics"]["retry_stop_reason"], "wait_budget_exhausted")
        self.assertEqual(result["auto_recovery"]["attempt_count"], 1)

    def test_empty_response_recovers_and_max_attempts_terminates(self):
        result = self.execute([wire("openai_chat", "")] * 3)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result["auto_recovery"]["stop_reason"], "max_attempts")
        self.assertEqual(result["status"], "error")

    def test_incomplete_read_does_not_abort_other_tasks_or_repeat_valid_scores(self):
        (self.root / "tasks").mkdir()
        other = {**self.task, "task_id": "b" * 64}
        self.config["runtime"]["max_workers"] = 1
        client = self.client([http.client.IncompleteRead(b""), http.client.IncompleteRead(b""), self.good()])
        outcomes = execute_tasks(self.root, [self.task, other], {"m": client}, self.config)
        self.assertEqual([r["status"] for r in outcomes], ["error", "success"])
        valid_path = self.root / "tasks" / (other["task_id"] + ".json")
        before = valid_path.read_bytes()
        resumed = execute_tasks(self.root, [self.task, other], {"m": self.client([self.good()])}, self.config, retry_failed=True)
        self.assertEqual([r["status"] for r in resumed], ["success", "success"])
        self.assertEqual(valid_path.read_bytes(), before)
        self.assertEqual(resumed[0]["sampling_attempt"], 2)

    def test_missing_input_and_oversize_never_call_model(self):
        for field, value in (("blocked_reason", "missing input"), ("system", "x" * 200001)):
            task = {**self.task, field: value, "task_id": digest(field)}
            result = execute_with_recovery(self.root, task, self.client([]), self.config, 1, execute_task)
            self.assertIn(result["status"], ("unjudgeable", "input_too_large"))
            self.assertEqual(len(self.requests), 0)

    def test_account_failure_pauses_queue_drains_and_resumes_without_rescoring_success(self):
        (self.root / "tasks").mkdir()
        tasks = [{**self.task, "task_id": digest(str(i)), "payload": {**self.payload, "id": str(i)}} for i in range(10)]
        self.config["runtime"]["max_workers"] = 2
        saved_failure = threading.Event()
        calls = []

        def transport(url, headers, body, timeout):
            item = json.loads(body["messages"][1]["content"])["id"]
            calls.append(item)
            if item == "0":
                raise urllib.error.HTTPError("url", 402, "error", {}, io.BytesIO(b"{}"))
            self.assertTrue(saved_failure.wait(5))
            return self.good()

        def progress(done, total):
            if done:
                saved_failure.set()

        with self.assertRaises(BatchPaused):
            execute_tasks(self.root, tasks, {"m": LLMClient(self.profile, transport)}, self.config, progress=progress)
        self.assertEqual(set(calls), {"0", "1"})
        self.assertEqual(checked_read(self.root / "batch_stop.json")["done"], 2)
        self.assertEqual(len(list((self.root / "tasks").glob("*.json"))), 2)
        self.assertEqual(len(list((self.root / "recovery").glob("*/cycle-*"))), 2)
        kept_path = self.root / "tasks" / (tasks[1]["task_id"] + ".json")
        kept = kept_path.read_bytes()
        resumed = execute_tasks(self.root, tasks, {"m": self.client([self.good()] * 9)}, self.config, retry_failed=True)
        self.assertEqual(len(resumed), 10)
        self.assertTrue(all(r["status"] == "success" for r in resumed))
        self.assertEqual(kept_path.read_bytes(), kept)
        self.assertEqual(len(self.requests), 9)

    def test_worker_marks_account_pause_resumable(self):
        from frontend.worker import execute as worker_execute
        (self.root / "job.json").write_text(json.dumps({"id": "fixture", "dataset_path": "unused", "limit": None}))
        with patch("frontend.worker.load_config", return_value=self.config), \
             patch("frontend.worker.plan", return_value={"experiments": {"block": {"tasks": 10}}}), \
             patch("frontend.worker.run", side_effect=BatchPaused("账户余额不足（HTTP 402）")):
            worker_execute(self.root)
        saved = json.loads((self.root / "job.json").read_text())
        self.assertEqual(saved["status"], "interrupted")
        self.assertIn("HTTP 402", saved["message"])
        self.assertFalse((self.root / "summary.json").exists())

    def test_restarts_consume_reserved_rounds_and_do_not_reset_budget(self):
        for _ in range(3):
            with self.assertRaises(KeyboardInterrupt):
                self.execute([KeyboardInterrupt()])
        result = self.execute([])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result["auto_recovery"]["attempt_count"], 3)
        self.assertEqual(result["auto_recovery"]["stop_reason"], "max_attempts")
        self.assertEqual(result["new_call_records"], [])
        self.assertEqual(result["error_code"], "recovery_interrupted")

    def test_resume_after_completed_recovery_without_task_checkpoint_is_free(self):
        first = self.execute([wire("openai_chat", "bad"), self.good()])
        resumed = self.execute([])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(first["judgment"], resumed["judgment"])
        self.assertEqual(resumed["new_call_records"], [])

    def test_legacy_policy_absent_disables_new_recovery(self):
        self.config.pop("failure_recovery")
        result = self.execute([wire("openai_chat", "bad")])
        self.assertNotIn("auto_recovery", result)
        self.assertFalse((self.root / "recovery").exists())
        self.assertNotIn("failure_recovery", validate_config(self.config))

    def test_policy_validation_rejects_unbounded_or_wrong_type(self):
        for bad in ({"max_attempts": 6}, {"enabled": 1}, {"max_output_tokens": 0}, {"unknown": True}):
            with self.subTest(bad=bad), self.assertRaises(InputError):
                validate_policy(bad)

    def test_valid_dimensions_preserved_even_when_retry_changes_them(self):
        payload = {"query_understanding": "direct", "result": {"title": "Installation", "snippet": "Install ExampleDB with this command.", "url": "https://example.test/install"}}
        old = valid_answer("legacy_url", payload, "a")
        old["dimensions"]["answerability"]["evidence"][0]["quote"] = "invalid quote"
        previous = validate_output("legacy_url", old, payload)
        self.assertEqual(previous["status"], "partial_output")
        later = valid_answer("legacy_url", payload, "b")
        later["dimensions"]["authoritative"]["value"] = False
        # Raw later output is used only for failed scopes; original valid values stay.
        latest = {"status": "success", "call": {"text": canonical(later)}}
        result = preserve_valid(previous, latest, {"kind": "legacy_url", "payload": payload}, [])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["judgment"]["dimensions"]["answerability"]["value"], 2)
        for name in ("authoritative", "noise", "freshness"):
            self.assertEqual(result["judgment"]["dimensions"][name], previous["judgment"]["dimensions"][name])
        retained = preserve_valid(previous, {"status": "invalid_output", "call": {"text": "bad"}}, {"kind": "legacy_url", "payload": payload}, [])
        self.assertEqual(retained["judgment"], previous["judgment"])
        self.assertEqual(retained["status"], "partial_output")

    def test_parallel_per_call_budgets_do_not_mutate_shared_profile(self):
        client = self.client([self.good(), self.good()])
        def complete(budget):
            return client.complete("system", {}, {}, max_output_tokens=budget)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(complete, [100, 200]))
        self.assertEqual(sorted(r["diagnostics"]["max_output_tokens"] for r in results), [100, 200])
        self.assertEqual(client.profile["max_output_tokens"], 100)

    def test_explicit_retry_crash_resumes_same_cycle(self):
        (self.root / "tasks").mkdir()
        bad = wire("openai_chat", "bad")
        first = execute_tasks(self.root, [self.task], {"m": self.client([bad] * 3)}, self.config)[0]
        self.assertEqual(first["sampling_attempt"], 1)
        with self.assertRaises(KeyboardInterrupt):
            execute_tasks(self.root, [self.task], {"m": self.client([KeyboardInterrupt()])}, self.config, retry_failed=True)
        resumed = execute_tasks(self.root, [self.task], {"m": self.client([self.good()])}, self.config)[0]
        self.assertEqual(resumed["sampling_attempt"], 2)
        self.assertEqual(resumed["status"], "success")
        self.assertEqual(resumed["auto_recovery"]["attempt_count"], 2)
        self.assertEqual(len(resumed["history"]), 1)
        self.assertIn("cycle-2", resumed["auto_recovery"]["attempts"][1]["path"])

    def test_feedback_cannot_turn_input_limit_into_success(self):
        self.config["runtime"]["max_input_chars"] = len(self.task["system"]) + len(canonical(self.payload))
        result = self.execute([wire("openai_chat", "bad")])
        self.assertEqual(result["status"], "input_too_large")
        self.assertFalse(result["auto_recovery"]["recovered"])
        self.assertEqual(len(self.requests), 1)

    def test_stream_interrupt_recovers_but_embedded_auth_stops(self):
        interrupted = self.good()
        interrupted["stream_info"] = {"completed": False, "error_code": "stream_interrupted"}
        result = self.execute([interrupted, self.good()])
        self.assertTrue(result["auto_recovery"]["recovered"])
        error = {"status": "error", "error_code": "stream_provider_error", "response_diagnostics": {
            "attempts": [{"response": {"error": {"type": "authentication_error"}}}]}}
        self.assertEqual(diagnose(error, 100, DEFAULT_POLICY, "openai_chat")["action"], "stop")

    def test_failed_duplicate_row_recovers_without_changing_valid_row(self):
        payload = {"results": [{"result_id": "a", "url": "https://a.test", "title": "same fact"},
                               {"result_id": "b", "url": "https://b.test", "title": "same fact"}]}
        row = lambda rid, target, quote: {"result_id": rid, "status": "rated", "duplicate_of": target,
                                          "reason": "fixture", "evidence": [{"path": "/results/1/title", "quote": quote}]}
        old = {"rows": [row("a", None, "same fact"), row("b", "a", "bad quote")]}
        previous = validate_output("duplicates", old, payload)
        self.assertEqual(previous["status"], "partial_output")
        latest = {"status": "success", "call": {"text": canonical({"rows": [row("a", None, "same fact"), row("b", "a", "same fact")]})}}
        result = preserve_valid(previous, latest, {"kind": "duplicates", "payload": payload}, [])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["judgment"]["rows"][0], previous["judgment"]["rows"][0])

    def test_full_run_summary_resume_plan_and_frontend_audit(self):
        source = self.root / "source.json"
        source.write_text(json.dumps([{"query": "fixture query", "results": [{"title": "fixture title", "snippet": "fixture text", "url": "https://example.test"}]}]))
        dataset = self.root / "data" / "fixture"
        dataset.parent.mkdir()
        import_dataset(source, dataset)
        output = self.root / "runs" / ("b" * 32)
        output.mkdir(parents=True)
        evaluation = output / "evaluation"
        client = self.client([wire("openai_chat", "bad"), self.good()])
        summary = run(dataset, self.config, None, evaluation, client_factory=lambda p: client)
        detail = summary["experiments"]["block"]
        self.assertEqual(detail["initial_task_status"], {"invalid_output": 1})
        self.assertEqual(detail["task_status"], {"success": 1})
        self.assertEqual(detail["new_http_attempts"], 2)
        self.assertEqual(detail["reported_new_input_tokens"], 14)
        self.assertEqual(summary["auto_recovery"]["recovered_tasks"], 1)
        planned = plan(dataset, self.config)["experiments"]["block"]
        self.assertEqual(planned["maximum_http_attempts"], 6)
        resumed = run(dataset, self.config, None, evaluation, resume=True, client_factory=lambda p: self.client([]))
        self.assertEqual(resumed["experiments"]["block"]["new_http_attempts"], 0)
        self.assertEqual(len(self.requests), 2)
        # The UI reads saved effective prompts and every failed/successful response.
        (output / "config.json").write_text(canonical(self.config))
        (output / "job.json").write_text(canonical({"id": output.name, "status": "completed", "prompt_versions": {}}))
        service = Service(PROJECT, dataset.parent, output.parent)
        try:
            task_id = next((evaluation / "tasks").glob("*.json")).stem
            prompt = service.task_prompt(output.name, task_id)
            self.assertNotEqual(prompt["effective_prompt_hash"], prompt["prompt_hash"])
            response = service.task_response(output.name, task_id)
            self.assertEqual([a["recovery_round"] for a in response["diagnostics"]["attempts"]], [1, 2])
            self.assertEqual(response["diagnostics"]["attempts"][0]["response"]["text"], "bad")
        finally:
            service.close()

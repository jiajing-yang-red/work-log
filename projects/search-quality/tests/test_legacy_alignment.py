import json
import unittest
from unittest.mock import Mock

from quality_input.common import InputError
from quality_eval.contracts import validate_answer
from quality_eval.evidence import validate_output
from quality_eval.execution import execute_task
from quality_eval.runner import flatten
from quality_eval.schemes import make_tasks, prompt_for
from quality_llm import LLMClient
from test_evaluation_layer import spec_fixture, valid_answer, measure


class LegacyAlignmentTests(unittest.TestCase):
    def payload(self, mode):
        p = {"query": {"query_text": "ExampleDB installation documentation"},
             "result": {"url": "https://example.com/docs", "snippet": "Install ExampleDB"}}
        if mode == "direct":
            p["query_understanding"] = mode
        else:
            p["query_spec"] = spec_fixture()
        return p

    def task(self, kind, payload):
        return {**{key: "fixture" for key in (
            "task_id", "experiment_id", "model_profile", "query_id", "search_run_id",
            "response_id", "dataset_version", "prompt_hash", "input_hash")},
            "scheme": "legacy_five_v1", "scheme_version": "1.1", "query_spec_hash": None,
            "result_ids": ["r1"], "kind": kind, "mode": "api_only", "repeat": 0,
            "rating_target": "api_evidence", "query_understanding": "direct",
            "payload": payload, "system": prompt_for(kind, "direct"), "blocked_reason": None}

    def test_legacy_prompts_and_schemas_remove_unknown_rating_in_both_modes(self):
        for mode in ("frozen", "direct"):
            for kind in ("legacy_url", "duplicates"):
                system = prompt_for(kind, mode)
                self.assertNotIn("unjudgeable", system)
                self.assertNotIn("无法判断", system)
            p = self.payload(mode)
            for dimension in ("answerability", "authoritative", "freshness", "noise"):
                a = valid_answer("legacy_url", p)
                a["dimensions"][dimension].update(status="unjudgeable", value=None)
                with self.subTest(mode=mode, dimension=dimension), self.assertRaises(InputError):
                    validate_answer("legacy_url", a, p)
        p = {"results": [{"result_id": "r1", "rank": 1, "url": "https://example.com"}]}
        a = {"rows": [{"result_id": "r1", "status": "unjudgeable", "duplicate_of": None,
                       "reason": "No decision", "evidence": []}]}
        with self.assertRaises(InputError):
            validate_answer("duplicates", a, p)
        # Google keeps its independent missing-evidence policy.
        self.assertIn("unjudgeable", prompt_for("google_nm", "direct"))
        self.assertIn("unjudgeable", prompt_for("google_pq"))

    def test_all_five_answerability_grades_remain_available(self):
        for mode in ("frozen", "direct"):
            p = self.payload(mode)
            for grade in (-1, 0, 1, 2, 3):
                a = valid_answer("legacy_url", p)
                a["dimensions"]["answerability"]["value"] = grade
                a["dimensions"]["noise"]["value"] = grade <= 0
                with self.subTest(mode=mode, grade=grade):
                    self.assertEqual(validate_output("legacy_url", a, p)["status"], "success")

    def test_incomplete_or_contradictory_high_scores_are_rejected(self):
        for mode in ("frozen", "direct"):
            p = self.payload(mode)
            for grade in (2, 3):
                for change in ("partial", "contradicted", "unknown", "temporal_failure", "freshness_failure"):
                    a = valid_answer("legacy_url", p)
                    a["dimensions"]["answerability"]["value"] = grade
                    if change == "partial":
                        a["requirements"][0]["status"] = "partial"
                    elif change in ("contradicted", "unknown"):
                        a["reliability"] = change
                    elif change == "temporal_failure":
                        a["temporal_fit"] = "fails"
                    else:
                        temporal = a["query_assessment"] if mode == "direct" else p["query_spec"]
                        temporal.update(freshness_applicable=True, freshness_type="F3")
                        a["dimensions"]["freshness"] = measure(False)
                    with self.subTest(mode=mode, grade=grade, change=change), self.assertRaises(InputError):
                        validate_output("legacy_url", a, p)
                    p = self.payload(mode)
            a = valid_answer("legacy_url", p)
            a["dimensions"]["noise"]["value"] = True
            with self.assertRaises(InputError):
                validate_answer("legacy_url", a, p)

    def test_f1_through_f4_cannot_be_not_applicable(self):
        for mode in ("frozen", "direct"):
            for ftype in ("F1", "F2", "F3", "F4"):
                p = self.payload(mode)
                a = valid_answer("legacy_url", p)
                temporal = a["query_assessment"] if mode == "direct" else p["query_spec"]
                temporal["freshness_type"] = ftype
                with self.subTest(mode=mode, ftype=ftype), self.assertRaises(InputError):
                    validate_answer("legacy_url", a, p)
                temporal["freshness_applicable"] = True
                a["temporal_fit"] = "meets"
                a["dimensions"]["freshness"] = measure(True)
                validate_answer("legacy_url", a, p)

    def test_empty_evidence_zero_and_noise_need_no_invented_quote(self):
        for mode in ("frozen", "direct"):
            p = self.payload(mode)
            p["result"]["snippet"] = ""
            a = valid_answer("legacy_url", p)
            a["authority_tier"] = "S5"
            for name, value in (("answerability", 0), ("authoritative", False), ("noise", True)):
                a["dimensions"][name].update(value=value, evidence=[], reason="Returned content is empty.")
            a["requirements"][0]["status"] = "not_demonstrated"
            self.assertEqual(validate_output("legacy_url", a, p)["status"], "success")
            a["dimensions"]["answerability"]["value"] = -1
            with self.assertRaises(InputError):
                validate_answer("legacy_url", a, p)

    def test_unrelated_result_can_be_zero_without_evaluation_time(self):
        for mode in ("frozen", "direct"):
            p = self.payload(mode)
            p["query"]["query_text"] = "Today's gold price"
            a = valid_answer("legacy_url", p)
            temporal = a["query_assessment"] if mode == "direct" else p["query_spec"]
            temporal.update(freshness_applicable=True, freshness_type="F1")
            a["dimensions"]["answerability"]["value"] = 0
            a["dimensions"]["noise"]["value"] = True
            a["dimensions"]["freshness"] = measure(False)
            a["temporal_fit"] = "unknown"
            self.assertEqual(validate_output("legacy_url", a, p)["status"], "success")

    def test_input_failure_and_input_limit_are_errors_without_scores(self):
        client = Mock(profile={"protocol": "openai_chat"})
        for kind in ("legacy_url", "duplicates"):
            task = self.task(kind, self.payload("direct"))
            task["blocked_reason"] = "page_snapshot_missing"
            outcome = execute_task(task, client, 100000, [])
            self.assertEqual(outcome["status"], "error")
            self.assertEqual(outcome["error_code"], "input_unavailable")
            for row in flatten([outcome]):
                self.assertEqual(row["judgment_status"], "error")
                self.assertIsNone(row["value"])
            task["blocked_reason"] = None
            outcome = execute_task(task, client, 1, [])
            self.assertEqual(outcome["status"], "input_too_large")
            self.assertTrue(all(r["judgment_status"] == "error" for r in flatten([outcome])))
        client.complete.assert_not_called()

    def test_simulation_and_real_unknown_output_cannot_fabricate_scores(self):
        p = self.payload("direct")
        task = self.task("legacy_url", p)
        client = LLMClient({"protocol": "mock", "model": "fixture"})
        outcome = execute_task(task, client, 100000, [])
        self.assertEqual(outcome["status"], "success")
        rows = flatten([outcome])
        self.assertTrue(all(r["value"] is None for r in rows))
        self.assertEqual({r["judgment_status"] for r in rows}, {"simulated", "not_applicable"})
        client = Mock(profile={"protocol": "openai_chat"})
        answer = valid_answer("legacy_url", p)
        answer["dimensions"]["answerability"].update(status="unjudgeable", value=None)
        client.complete.return_value = {"text": json.dumps(answer)}
        outcome = execute_task(task, client, 100000, [])
        self.assertEqual(outcome["status"], "invalid_output")
        self.assertTrue(all(r["judgment_status"] == "error" and r["value"] is None for r in flatten([outcome])))

    def test_task_planning_preserves_judgable_evidence_and_google_policy(self):
        dataset = {"queries": [{"query_id": "q1", "query_text": "Today's gold price"}],
                   "manifest": {"evaluation_as_of": None, "dataset_version": "fixture"},
                   "responses": [{"query_id": "q1", "search_run_id": "s1", "response_id": "r1",
                                  "status": "ok", "results": [{"result_id": "x1", "rank": 1,
                                  "url": "https://example.com/docs", "snippet": "Install ExampleDB"}]}],
                   "page_snapshots": []}
        config = {"models": {"a": {"model": "fixture"}}, "query_understanding": "frozen",
                  "approved_authority_domains": [], "experiments": [
                      {"id": scheme, "scheme": scheme, "model": "a", "mode": "api_only", "repeats": 1}
                      for scheme in ("legacy_five_v1", "google_overview_v1")]}
        spec = spec_fixture()
        spec.update(freshness_applicable=True, freshness_type="F1", needs_clarification=True)
        specs = {"q1": {"status": "ready", "spec": spec}}
        tasks = make_tasks(dataset, config, specs, ["q1"])
        self.assertIsNone(next(t for t in tasks if t["kind"] == "legacy_url")["blocked_reason"])
        self.assertEqual(next(t for t in tasks if t["kind"] == "google_nm")["blocked_reason"], "query_requires_clarification")
        legacy = next(t for t in tasks if t["kind"] == "legacy_url")
        self.assertEqual(legacy["scheme_version"], "1.1")
        specs["q1"]["reference_answer"] = {"answer": "Fact", "source": "human", "as_of": "2026-10-20T00:00:00Z"}
        legacy = next(t for t in make_tasks(dataset, config, specs, ["q1"]) if t["kind"] == "legacy_url")
        self.assertEqual(legacy["payload"]["evaluation_as_of"], specs["q1"]["reference_answer"]["as_of"])
        config["query_understanding"] = "direct"
        for e in config["experiments"]:
            e["mode"] = "page_content"
        dataset["responses"][0]["results"][0]["page_snapshot_id"] = "p1"
        for page in ({"fetch_status": "error", "http_status": 401},
                     {"fetch_status": "ok", "extraction_status": "complete", "truncated": False, "body_text": ""}):
            dataset["page_snapshots"] = [{"page_snapshot_id": "p1", **page}]
            tasks = make_tasks(dataset, config, {}, ["q1"])
            self.assertTrue(all(t["blocked_reason"] is None for t in tasks if t["scheme"] == "legacy_five_v1"))
            self.assertTrue(all(t["blocked_reason"] for t in tasks if t["scheme"] == "google_overview_v1"))
            task = next(t for t in tasks if t["kind"] == "legacy_url")
            self.assertEqual(task["payload"]["page"]["http_status"], page.get("http_status"))


if __name__ == "__main__":
    unittest.main()

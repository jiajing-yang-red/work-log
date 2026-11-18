import copy
import json
import unittest
from unittest.mock import Mock

from quality_input.common import InputError
from quality_eval.contracts import resolve_pointer, validate_answer
from quality_eval.evidence import evidence_catalog, validate_output
from quality_eval.execution import execute_task
from quality_eval.runner import flatten
from quality_llm import LLMClient
from test_evaluation_layer import valid_answer


class EvidenceValidationTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "query_understanding": "direct", "query": {"query_text": "ExampleDB install"},
            "result": {"url": "https://example.com", "snippet": "Install ExampleDB", "metadata": {}},
        }

    def task(self, kind, payload):
        return {
            **{k: "fixture" for k in (
                "task_id", "experiment_id", "scheme", "scheme_version", "model_profile",
                "query_id", "search_run_id", "response_id", "dataset_version", "prompt_hash", "input_hash")},
            "query_spec_hash": None, "result_ids": ["r1"], "kind": kind, "mode": "api_only",
            "query_understanding": "direct", "repeat": 0, "rating_target": "api_evidence",
            "payload": payload, "system": "Fixture prompt", "blocked_reason": None,
        }

    def duplicates(self):
        payload = {"results": [
            {"result_id": "r1", "rank": 1, "snippet": "First distinct original.", "url": "https://a.test"},
            {"result_id": "r2", "rank": 2, "snippet": "Second report.", "url": "https://b.test"},
            {"result_id": "r3", "rank": 3, "snippet": "Third report.", "url": "https://c.test"},
        ]}
        answer = {"rows": [
            {"result_id": r["result_id"], "status": "rated", "duplicate_of": None,
             "reason": "Synthetic nonduplicate", "evidence": []} for r in payload["results"]
        ]}
        return payload, answer

    def test_url_alias_is_audited_without_changing_model_answer_or_call_count(self):
        answer = valid_answer("legacy_url", self.payload)
        answer["dimensions"]["authoritative"]["evidence"] = [
            {"path": "/result/metadata/url", "quote": "https://example.com"}]
        original = copy.deepcopy(answer)
        client = Mock(profile={"protocol": "openai_chat"})
        client.complete.return_value = {"text": json.dumps(answer)}
        outcome = execute_task(self.task("legacy_url", self.payload), client, 100000, [])
        self.assertEqual(outcome["status"], "success")
        self.assertEqual(outcome["raw_output_status"], "invalid_output")
        self.assertEqual(len(outcome["evidence_repairs"]), 1)
        self.assertEqual(json.loads(outcome["call"]["text"]), original)
        self.assertEqual(client.complete.call_count, 1)
        rows = flatten([outcome])
        repaired = next(r for r in rows if r["dimension"] == "authoritative")
        self.assertEqual(repaired["value"], True)
        self.assertEqual(repaired["evidence"][0]["path"], "/result/url")
        self.assertEqual(repaired["evidence_repairs"][0]["original_path"], "/result/metadata/url")
        self.assertEqual(sum(bool(r["evidence_repairs"]) for r in rows), 1)

    def test_bad_citation_only_invalidates_its_dimension(self):
        answer = valid_answer("legacy_url", self.payload)
        answer["dimensions"]["authoritative"]["evidence"][0]["quote"] = "Invented evidence"
        outcome = {**self.task("legacy_url", self.payload), "simulated": False,
                   **validate_output("legacy_url", answer, self.payload)}
        self.assertEqual(outcome["status"], "partial_output")
        rows = flatten([outcome])
        errors = [r for r in rows if r["judgment_status"] == "error"]
        self.assertEqual([r["dimension"] for r in errors], ["authoritative"])
        self.assertIsNone(errors[0]["value"])
        self.assertEqual(errors[0]["error_code"], "evidence_quote_mismatch")
        self.assertEqual(errors[0]["validation_issues"][0]["path"], "/result/snippet")
        self.assertEqual(next(r for r in rows if r["dimension"] == "answerability")["value"], 3)

    def test_incomplete_response_is_persisted_but_never_scored(self):
        client = LLMClient({"protocol": "openai_chat", "model": "test", "base_url": "http://127.0.0.1/v1"},
                           lambda *args: {"choices": [{"finish_reason": "length", "message": {"content": '{"dimensions":'}}]})
        outcome = execute_task(self.task("legacy_url", self.payload), client, 100000, [])
        self.assertEqual(outcome["status"], "error")
        self.assertIsNone(outcome["call"])
        self.assertIsNone(outcome["judgment"])
        self.assertEqual(outcome["response_diagnostics"]["attempts"][0]["response"]["text"], '{"dimensions":')
        self.assertTrue(all(row["value"] is None for row in flatten([outcome])))

    def test_invalid_json_keeps_the_actual_answer_and_finish_reason(self):
        client = LLMClient({"protocol": "openai_chat", "model": "test", "base_url": "http://127.0.0.1/v1"},
                           lambda *args: {"choices": [{"finish_reason": "stop", "message": {"content": 'not JSON <script>unsafe()</script>'}}]})
        outcome = execute_task(self.task("legacy_url", self.payload), client, 100000, [])
        self.assertEqual(outcome["error_code"], "invalid_json")
        self.assertEqual(outcome["call"]["text"], 'not JSON <script>unsafe()</script>')
        self.assertEqual(outcome["call"]["diagnostics"]["attempts"][0]["response"]["finish_reason"], "stop")

    def test_rank_index_repair_handles_last_item_and_cited_earlier_item(self):
        payload, answer = self.duplicates()
        answer["rows"][2].update(duplicate_of="r1", evidence=[
            {"path": "/results/1/snippet", "quote": "First distinct original."},
            {"path": "/results/3/snippet", "quote": "Third report."},
        ])
        outcome = validate_output("duplicates", answer, payload)
        self.assertEqual(outcome["status"], "success")
        self.assertEqual([r["corrected_path"] for r in outcome["evidence_repairs"]],
                         ["/results/0/snippet", "/results/2/snippet"])
        self.assertEqual(outcome["judgment"]["rows"][2]["duplicate_of"], "r1")

    def test_ambiguous_or_unrelated_sources_are_not_repaired(self):
        payload, answer = self.duplicates()
        payload["results"][0]["snippet"] = "Shared quote."
        payload["results"][1]["snippet"] = "Shared quote."
        answer["rows"][1]["evidence"] = [{"path": "/results/2/snippet", "quote": "Shared quote."}]
        outcome = validate_output("duplicates", answer, payload)
        self.assertEqual(outcome["status"], "partial_output")
        self.assertEqual(outcome["evidence_repairs"], [])
        self.assertEqual(outcome["judgment"]["rows"][1]["status"], "error")
        # A unique quote still cannot be silently reassigned to an unrelated result.
        payload, answer = self.duplicates()
        answer["rows"][2]["evidence"] = [{"path": "/results/1/snippet", "quote": "First distinct original."}]
        outcome = validate_output("duplicates", answer, payload)
        self.assertEqual(outcome["evidence_repairs"], [])
        self.assertEqual(outcome["judgment"]["rows"][2]["status"], "error")

    def test_duplicate_error_is_isolated_and_dependents_are_rejected(self):
        payload, answer = self.duplicates()
        answer["rows"][1]["evidence"] = [{"path": "/results/1/snippet", "quote": "Invented"}]
        outcome = validate_output("duplicates", answer, payload)
        self.assertEqual([r["status"] for r in outcome["judgment"]["rows"]], ["rated", "error", "rated"])
        answer["rows"][2].update(duplicate_of="r2", evidence=[
            {"path": "/results/2/snippet", "quote": "Third report."}])
        outcome = validate_output("duplicates", answer, payload)
        self.assertEqual(outcome["judgment"]["rows"][2]["error_code"], "duplicate_dependency_invalid")
        task = self.task("duplicates", payload)
        task["result_ids"] = ["r1", "r2", "r3"]
        rows = flatten([{**task, "simulated": False, **outcome}])
        self.assertEqual([r["judgment_status"] for r in rows], ["rated", "error", "error"])
        self.assertIsNone(rows[2]["value"])
        self.assertIsNone(rows[2]["duplicate_of"])

    def test_repair_does_not_bypass_schema_or_scoring_rules(self):
        answer = valid_answer("legacy_url", self.payload)
        answer["dimensions"]["authoritative"]["evidence"][0]["path"] = "/result/metadata/url"
        answer["dimensions"]["authoritative"]["evidence"][0]["quote"] = "https://example.com"
        answer["dimensions"]["answerability"]["value"] = 0  # noise=false contradicts this.
        with self.assertRaisesRegex(InputError, "noise=true"):
            validate_output("legacy_url", answer, self.payload)
        answer = valid_answer("legacy_url", self.payload)
        answer["dimensions"]["authoritative"]["value"] = "yes"
        with self.assertRaises(InputError):
            validate_output("legacy_url", answer, self.payload)
        payload, answer = self.duplicates()
        answer["rows"].pop()
        with self.assertRaisesRegex(InputError, "cover each"):
            validate_output("duplicates", answer, payload)

    def test_single_dimension_failure_and_json_errors_stay_errors(self):
        answer = valid_answer("google_nm", self.payload)
        answer["dimensions"]["needs_met"]["evidence"][0]["quote"] = "Invented"
        outcome = validate_output("google_nm", answer, self.payload)
        self.assertEqual(outcome["status"], "invalid_output")
        self.assertIsNone(outcome["judgment"]["dimensions"]["needs_met"]["value"])
        client = Mock(profile={"protocol": "openai_chat"})
        client.complete.return_value = {"text": "not JSON"}
        outcome = execute_task(self.task("legacy_url", self.payload), client, 100000, [])
        self.assertEqual(outcome["error_code"], "invalid_json")
        self.assertIsNone(outcome["judgment"])
        self.assertEqual(client.complete.call_count, 1)

    def test_catalog_paths_resolve_and_do_not_include_query_or_empty_fields(self):
        payload, _ = self.duplicates()
        payload["query"] = {"query_text": "Do not cite this"}
        payload["results"][0]["text"] = None
        payload["results"][0]["metadata"] = {"a/b~c": "Escaped key"}
        catalog = evidence_catalog(payload)
        self.assertTrue(all(isinstance(resolve_pointer(payload, e["path"]), str) for e in catalog))
        self.assertFalse(any("/query/" in e["path"] or e["path"].endswith(("/rank", "/result_id", "/text"))
                             for e in catalog))
        escaped = next(e for e in catalog if "~1" in e["path"])
        self.assertEqual(escaped["result_id"], "r1")
        for path in ("/results/-1/snippet", "/results/01/snippet", "/results/3/snippet"):
            with self.assertRaises(InputError):
                resolve_pointer(payload, path)


if __name__ == "__main__":
    unittest.main()

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from quality_input.common import InputError, canonical, read_json
from quality_input.pipeline import import_dataset, load_dataset
from quality_eval.config import load_config
from quality_eval.contracts import SCHEMAS, DIRECT_SCHEMAS, validate_answer, validate_spec
from quality_eval.execution import execute_task
from quality_eval.query_specs import prepare_specs
from quality_eval.runner import compare_rows, plan, run
from quality_eval.schemes import duplicate_fallback, simulated_response, make_tasks
from quality_eval.query_specs import load_specs
from quality_eval.storage import checked_read
from quality_llm.client import validate_profile


def spec_fixture():
    return {
        "intents": [{
            "id": "I1", "likelihood": "primary", "kind": "navigation",
            "goal": "Find the ExampleDB install documentation",
            "requirements": [{"id": "R1", "text": "Locate the specified documentation"}],
            "hard_constraints": [], "fully_meets_eligible": True,
            "specific_target": "ExampleDB installation documentation",
        }],
        "freshness_applicable": False, "freshness_type": "F5",
        "needs_clarification": False, "clarification_reason": "",
    }


def measure(value, path="/result/snippet", quote="Install ExampleDB"):
    return {"status": "rated", "value": value, "reason": "Synthetic test decision.",
            "evidence": [{"path": path, "quote": quote}]}


def valid_answer(kind, payload, model="a"):
    if kind == "query_spec":
        return spec_fixture()
    if kind == "duplicates":
        return duplicate_fallback(payload)
    if kind == "google_pq":
        return {"page_purpose": "Installation instructions",
                "dimensions": {"page_quality": measure("High", "/page/body_text", "Install ExampleDB")}}
    answer = {
        "selected_intent_id": "I1", "requirements": [{"id": "R1", "status": "met"}],
        "constraints": [], "temporal_fit": "not_applicable", "reliability": "adequate",
    }
    if kind == "google_nm":
        answer["dimensions"] = {"needs_met": measure("FullyM" if model == "a" else "HM")}
    else:
        answer["authority_tier"] = "S2"
        answer["dimensions"] = {
            "answerability": measure(3 if model == "a" else 2),
            "authoritative": measure(True), "noise": measure(False),
            "freshness": {"status": "not_applicable", "value": None, "reason": "No time requirement.",
                          "evidence": []},
        }
    if payload.get("query_understanding") == "direct":
        answer.pop("selected_intent_id")
        answer["query_assessment"] = {
            "interpretation": "Find the ExampleDB installation documentation.",
            "kind": "navigation", "likelihood": "primary", "specific_target": "ExampleDB installation documentation",
            "fully_meets_eligible": True, "needs_clarification": False, "freshness_applicable": False,
        }
        answer["requirements"] = [{"text": "Locate the specified documentation", "status": "met"}]
        if kind == "legacy_url":
            answer["query_assessment"] = {
                "interpretation": "Find the ExampleDB installation documentation.",
                "freshness_applicable": False, "freshness_type": "F5",
            }
    return answer


class FixtureClient:
    active = 0
    peak = 0
    calls = 0
    lock = threading.Lock()
    invalid = False

    def __init__(self, profile):
        self.profile = validate_profile(profile)

    def check_credentials(self):
        pass

    def complete(self, system, payload, schema, mock_response=None):
        kind = next(k for k, v in [*SCHEMAS.items(), *DIRECT_SCHEMAS.items()] if schema is v)
        with self.lock:
            FixtureClient.active += 1
            FixtureClient.calls += 1
            FixtureClient.peak = max(FixtureClient.peak, FixtureClient.active)
        try:
            time.sleep(0.005)
            text = "not JSON" if self.invalid else canonical(valid_answer(kind, payload, self.profile["model"]))
            return {"text": text, "usage": {"input_tokens": 10, "output_tokens": 5},
                    "returned_model": self.profile["model"], "response_id": "fixture-response",
                    "attempts": 1, "latency_seconds": 0.005, "simulated": False}
        finally:
            with self.lock:
                FixtureClient.active -= 1


class EvaluationLayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = {
            "queries": [
                {"query_id": "q1", "query_text": "ExampleDB installation documentation"},
                {"query_id": "q2", "query_text": "ExampleDB installation documentation empty"},
                {"query_id": "q3", "query_text": "ExampleDB installation documentation missing"},
            ],
            "search_runs": [{"search_run_id": "s1", "provider": "ateve", "requested_top_k": 10,
                             "collection_completed_at": "2026-10-20T00:00:00Z"}],
            "responses": [
                {"query_id": "q1", "search_run_id": "s1", "status": "ok",
                 "results": [{"rank": rank, "url": "https://example.com/install",
                              "title": "Installation", "snippet": "Install ExampleDB with this command.",
                              "source_score": 0.99} for rank in (1, 2)]},
                {"query_id": "q2", "search_run_id": "s1", "status": "empty", "results": []},
            ],
        }
        raw = self.root / "source.json"
        raw.write_text(json.dumps(source))
        self.dataset = self.root / "dataset"
        import_dataset(raw, self.dataset, use_collection_time=True)
        config = {
            "models": {name: {"protocol": "openai_chat", "base_url": "http://127.0.0.1:1/v1",
                              "model": name} for name in ("a", "b")},
            "query_model": "a",
            "experiments": [
                {"id": scheme + "-" + model, "scheme": scheme, "model": model}
                for scheme in ("legacy_five_v1", "google_overview_v1") for model in ("a", "b")
            ],
            "runtime": {"max_workers": 4},
        }
        self.config_file = self.root / "config.json"
        self.config_file.write_text(json.dumps(config))
        self.config = load_config(self.config_file)
        self.specs = self.root / "specs"
        FixtureClient.active = FixtureClient.peak = FixtureClient.calls = 0
        FixtureClient.invalid = False

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        return prepare_specs(self.dataset, self.config, self.specs, client_factory=FixtureClient)

    def test_matrix_concurrency_frozen_specs_empty_queries_and_comparison(self):
        self.prepare()
        prepared_calls = FixtureClient.calls
        summary = run(self.dataset, self.config, self.specs, self.root / "eval",
                      client_factory=FixtureClient)
        self.assertEqual(prepared_calls, 3)
        self.assertEqual(summary["status"], "completed")
        self.assertGreater(FixtureClient.peak, 1)
        self.assertLessEqual(FixtureClient.peak, 4)
        self.assertEqual(summary["task_count"], 14)
        executions = [json.loads(line) for line in (self.root / "eval/query_executions.jsonl").read_text().splitlines()]
        self.assertEqual(len(executions), 12)
        self.assertEqual({r["collection_status"] for r in executions}, {"ok", "empty", "missing_record"})
        comparison = read_json(self.root / "eval/model_comparison.json")
        self.assertFalse(comparison["human_accuracy_computed"])
        self.assertEqual(len(comparison["pairs"]), 2)
        self.assertTrue(any(pair["disagreements"] for pair in comparison["pairs"]))
        checkpoints = [checked_read(p) for p in (self.root / "eval/tasks").glob("*.json")]
        for record in checkpoints:
            if record["kind"] in ("legacy_url", "google_nm"):
                payload = canonical(record["request"]["payload"])
                self.assertNotIn('"source_score"', payload)
                self.assertNotIn('"rank"', payload)
                self.assertNotIn('"provider"', payload)
        self.assertEqual({r["mode"] for r in checkpoints if r["kind"] == "google_pq"}, {"page_content"})

    def test_missing_page_is_unjudgeable_without_call_or_api_fallback(self):
        self.prepare()
        run(self.dataset, self.config, self.specs, self.root / "eval", client_factory=FixtureClient)
        pq = [checked_read(p) for p in (self.root / "eval/tasks").glob("*.json")
              if checked_read(p)["kind"] == "google_pq"]
        self.assertEqual(len(pq), 4)
        for record in pq:
            self.assertEqual(record["status"], "unjudgeable")
            self.assertIsNone(record["call"])
            self.assertIsNone(record["request"]["payload"]["page"])
            self.assertNotIn("query", record["request"]["payload"])
            self.assertNotIn("query_spec", record["request"]["payload"])

    def test_page_mode_uses_only_independent_body_and_pq_has_no_query(self):
        self.prepare()
        dataset = load_dataset(self.dataset)
        first = dataset["responses"][0]["results"][0]
        first["page_snapshot_id"] = "p1"
        dataset["page_snapshots"] = [{
            "page_snapshot_id": "p1", "requested_url": first["url"], "final_url": first["url"],
            "body_text": "Independent full page evidence.", "fetch_status": "ok",
            "truncated": False, "extraction_status": "complete",
            "fetched_at": "2026-10-20T00:00:00Z",
        }]
        config = copy.deepcopy(self.config)
        config["experiments"] = [config["experiments"][2]]
        config["experiments"][0]["mode"] = "page_content"
        tasks = make_tasks(dataset, config, load_specs(self.specs, dataset), ["q1"])
        present = [task for task in tasks if task["result_ids"] == [first["result_id"]]]
        for task in present:
            self.assertIsNone(task["blocked_reason"])
            self.assertEqual(task["payload"]["page"]["body_text"], "Independent full page evidence.")
            self.assertNotIn(first["snippet"], canonical(task["payload"]))
            if task["kind"] == "google_pq":
                self.assertNotIn("query", task["payload"])
                self.assertNotIn("query_spec", task["payload"])
        dataset["page_snapshots"][0]["truncated"] = True
        tasks = make_tasks(dataset, config, load_specs(self.specs, dataset), ["q1"])
        self.assertTrue(all(task["blocked_reason"] for task in tasks))

    def test_resume_uses_cache_and_changed_configuration_is_rejected(self):
        self.prepare()
        first = run(self.dataset, self.config, self.specs, self.root / "eval", client_factory=FixtureClient)
        calls = FixtureClient.calls
        second = run(self.dataset, self.config, self.specs, self.root / "eval",
                     resume=True, client_factory=FixtureClient)
        self.assertEqual(FixtureClient.calls, calls)
        self.assertEqual(sum(e["cache_hits"] for e in second["experiments"].values()), first["task_count"])
        modified = copy.deepcopy(self.config)
        modified["models"]["a"]["max_output_tokens"] += 1
        with self.assertRaises(InputError):
            run(self.dataset, modified, self.specs, self.root / "eval", resume=True, client_factory=FixtureClient)

    def test_invalid_answers_are_preserved_not_zero_and_retry_is_explicit(self):
        self.prepare()
        FixtureClient.invalid = True
        first = run(self.dataset, self.config, self.specs, self.root / "eval", client_factory=FixtureClient)
        self.assertEqual(first["status"], "partial")
        calls = FixtureClient.calls
        FixtureClient.invalid = False
        cached = run(self.dataset, self.config, self.specs, self.root / "eval",
                     resume=True, client_factory=FixtureClient)
        self.assertEqual(cached["status"], "partial")
        self.assertEqual(FixtureClient.calls, calls)
        successful = run(self.dataset, self.config, self.specs, self.root / "eval",
                         resume=True, retry_failed=True, client_factory=FixtureClient)
        self.assertEqual(successful["status"], "completed")
        self.assertTrue(any(item["explicitly_resampled_tasks"] > 0 for item in successful["experiments"].values()))
        archives = list((self.root / "eval/previous_attempts").glob("*.json"))
        self.assertTrue(archives)
        self.assertEqual(checked_read(archives[0])["call"]["text"], "not JSON")
        comparison = read_json(self.root / "eval/model_comparison.json")
        self.assertTrue(all(dim["both_rated_real"] == 0 for pair in comparison["pairs"]
                            for dim in pair["dimensions"].values()))

    def test_partial_citation_failure_summary_resume_and_explicit_retry(self):
        class CitationClient(FixtureClient):
            bad_citation = True

            def complete(self, system, payload, schema, mock_response=None):
                call = super().complete(system, payload, schema, mock_response)
                answer = json.loads(call["text"])
                if self.bad_citation and "dimensions" in answer:
                    answer["dimensions"]["authoritative"]["evidence"][0]["quote"] = "Invented quote"
                call["text"] = json.dumps(answer)
                return call

        self.config["query_understanding"] = "direct"
        self.config["experiments"] = self.config["experiments"][:1]
        output = self.root / "citation-partial"
        result = run(self.dataset, self.config, None, output, client_factory=CitationClient)
        experiment = next(iter(result["experiments"].values()))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(experiment["task_status"], {"partial_output": 2, "success": 1})
        self.assertEqual(experiment["dimension_status"]["authoritative"], {"error": 2})
        self.assertEqual(experiment["dimension_status"]["answerability"], {"rated": 2})
        before = FixtureClient.calls
        CitationClient.bad_citation = False
        cached = run(self.dataset, self.config, None, output, resume=True, client_factory=CitationClient)
        self.assertEqual(cached["status"], "partial")
        self.assertEqual(FixtureClient.calls, before)
        retried = run(self.dataset, self.config, None, output, resume=True, retry_failed=True,
                      client_factory=CitationClient)
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(FixtureClient.calls - before, 2)
        self.assertEqual(len(list((output / "previous_attempts").glob("*.json"))), 2)

    def test_simulated_specs_cannot_be_sent_to_real_models(self):
        mock_config = copy.deepcopy(self.config)
        mock_config["models"]["a"] = {"protocol": "mock", "model": "simulated"}
        prepare_specs(self.dataset, mock_config, self.specs)
        with self.assertRaisesRegex(InputError, "simulated"):
            run(self.dataset, self.config, self.specs, self.root / "eval", client_factory=FixtureClient)

    def test_plan_is_read_only_and_respects_no_truncation_limit(self):
        self.prepare()
        count = FixtureClient.calls
        config = copy.deepcopy(self.config)
        config["runtime"]["max_input_chars"] = 10
        report = plan(self.dataset, config, self.specs)
        self.assertEqual(FixtureClient.calls, count)
        self.assertEqual(report["network_calls_made"], 0)
        self.assertTrue(all(item["planned_model_invocations"] == 0 for item in report["experiments"].values()))
        summary = run(self.dataset, config, self.specs, self.root / "limited", client_factory=FixtureClient)
        self.assertEqual(FixtureClient.calls, count)
        self.assertEqual(summary["status"], "partial")

    def test_repeats_are_independent_calls_not_reused_sampling(self):
        self.prepare()
        self.config["experiments"] = [self.config["experiments"][0]]
        self.config["experiments"][0]["repeats"] = 2
        before = FixtureClient.calls
        result = run(self.dataset, self.config, self.specs, self.root / "eval", client_factory=FixtureClient)
        self.assertEqual(result["task_count"], 6)
        self.assertEqual(FixtureClient.calls - before, 6)

    def test_tampered_query_specs_are_rejected(self):
        self.prepare()
        with (self.specs / "query_specs.jsonl").open("a") as f:
            f.write("\n")
        with self.assertRaisesRegex(InputError, "checksum"):
            plan(self.dataset, self.config, self.specs)

    def test_claimed_fullym_reference_and_constraint_rules(self):
        payload = {"query_spec": spec_fixture(), "result": {"url": "https://example.com",
                   "snippet": "Install ExampleDB with this command."}}
        answer = valid_answer("google_nm", payload)
        validate_answer("google_nm", answer, payload)
        cases = [
            lambda p, a: p["query_spec"]["intents"][0].update(fully_meets_eligible=False),
            lambda p, a: a["requirements"][0].update(status="partial"),
            lambda p, a: a.update(temporal_fit="unknown"),
            lambda p, a: a["dimensions"]["needs_met"]["evidence"][0].update(quote="Invented citation"),
        ]
        for mutate in cases:
            p, a = copy.deepcopy(payload), copy.deepcopy(answer)
            mutate(p, a)
            with self.assertRaises(InputError):
                validate_answer("google_nm", a, p)

    def test_legacy_grade_two_not_partial_authority_approval_and_noise(self):
        payload = {"query_spec": spec_fixture(), "result": {"url": "https://example.com",
                   "snippet": "Install ExampleDB with this command."}}
        answer = valid_answer("legacy_url", payload, "b")
        validate_answer("legacy_url", answer, payload)
        answer["requirements"][0]["status"] = "partial"
        with self.assertRaises(InputError):
            validate_answer("legacy_url", answer, payload)
        answer = valid_answer("legacy_url", payload)
        answer["authority_tier"] = "S1"
        with self.assertRaises(InputError):
            validate_answer("legacy_url", answer, payload)
        validate_answer("legacy_url", answer, payload, ["example.com"])
        answer = valid_answer("legacy_url", payload)
        answer["dimensions"]["answerability"]["value"] = 0
        with self.assertRaises(InputError):
            validate_answer("legacy_url", answer, payload)
        answer["dimensions"]["noise"]["value"] = True
        validate_answer("legacy_url", answer, payload)

    def test_bool_is_not_numeric_grade_and_unjudgeable_is_not_zero(self):
        payload = {"query_spec": spec_fixture(), "result": {"url": "https://example.com",
                   "snippet": "Install ExampleDB with this command."}}
        answer = valid_answer("legacy_url", payload)
        answer["dimensions"]["answerability"]["value"] = True
        with self.assertRaises(InputError):
            validate_answer("legacy_url", answer, payload)
        answer = simulated_response("google_nm", payload)
        answer["dimensions"]["needs_met"]["value"] = "FailsM"
        with self.assertRaises(InputError):
            validate_answer("google_nm", answer, payload)

    def test_duplicate_scope_rank_and_exact_url_rules(self):
        payload = {"results": [
            {"result_id": "a", "rank": 1, "url": "https://example.com/a"},
            {"result_id": "b", "rank": 2, "url": "https://example.com/a"},
        ]}
        answer = duplicate_fallback(payload)
        validate_answer("duplicates", answer, payload)
        for mutate in (
            lambda a: a["rows"][0].update(duplicate_of="b"),
            lambda a: a["rows"][1].update(duplicate_of=None),
            lambda a: a["rows"].pop(),
        ):
            bad = copy.deepcopy(answer)
            mutate(bad)
            with self.assertRaises(InputError):
                validate_answer("duplicates", bad, payload)

    def test_fullym_query_eligibility_not_open_research(self):
        spec = spec_fixture()
        spec["intents"][0]["kind"] = "research"
        with self.assertRaises(InputError):
            validate_spec(spec)

    def test_direct_mode_scores_without_preparation_and_retains_missing_page_status(self):
        self.config.update(query_understanding="direct", query_model=None)
        report = plan(self.dataset, self.config)
        self.assertEqual(report["query_understanding"], "direct")
        self.assertEqual(report["query_specs_available"], 0)
        self.assertEqual(FixtureClient.calls, 0)
        self.assertEqual(sum(e["planned_model_invocations"] for e in report["experiments"].values()), 10)
        summary = run(self.dataset, self.config, None, self.root / "direct", client_factory=FixtureClient)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["query_understanding"], "direct")
        self.assertEqual(FixtureClient.calls, 10)
        self.assertFalse(self.specs.exists())
        records = [checked_read(p) for p in (self.root / "direct/tasks").glob("*.json")]
        self.assertFalse(any(r["kind"] == "query_spec" for r in records))
        for r in records:
            self.assertEqual(r["query_understanding"], "direct")
            self.assertIsNone(r["query_spec_hash"])
            self.assertNotIn("query_spec", r["request"]["payload"])
            if r["kind"] in ("legacy_url", "google_nm"):
                self.assertEqual(r["status"], "success")
                self.assertIn("query_assessment", r["judgment"])
            elif r["kind"] == "google_pq":
                self.assertEqual(r["status"], "unjudgeable")
                self.assertIsNone(r["call"])
        rows = [json.loads(line) for line in (self.root / "direct/judgments.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 28)
        self.assertEqual({r["query_understanding"] for r in rows}, {"direct"})
        self.assertTrue(all(r["query_assessment"] for r in rows if r["kind"] in ("legacy_url", "google_nm")))
        comparison = read_json(self.root / "direct/model_comparison.json")
        self.assertEqual(len(comparison["pairs"]), 2)
        # Different understanding modes must not be compared even if other fields match.
        a = next(r for r in rows if r["dimension"] == "needs_met")
        b = {**a, "experiment_id": "frozen-experiment", "query_understanding": "frozen"}
        self.assertEqual(compare_rows([a, b])["pairs"], [])

    def test_direct_mode_retains_grading_citation_and_time_constraints(self):
        payload = {"query_understanding": "direct", "query": {"query_text": "ExampleDB documentation"},
                   "result": {"url": "https://example.com", "snippet": "Install ExampleDB"}}
        cases = [
            ("google_nm", lambda a: a["query_assessment"].update(kind="research")),
            ("google_nm", lambda a: a["query_assessment"].update(fully_meets_eligible=False)),
            ("google_nm", lambda a: a["query_assessment"].update(needs_clarification=True)),
            ("google_nm", lambda a: a["constraints"].append({"text": "Requested version", "status": "fail"})),
            ("google_nm", lambda a: a["requirements"][0].update(status="partial")),
            ("google_nm", lambda a: a["dimensions"]["needs_met"]["evidence"][0].update(quote="Invented")),
            ("legacy_url", lambda a: a["query_assessment"].update(freshness_applicable=True)),
            ("legacy_url", lambda a: a["requirements"][0].update(status="partial")),
            ("legacy_url", lambda a: a["dimensions"]["answerability"].update(value=0)),
        ]
        for kind in ("legacy_url", "google_nm"):
            validate_answer(kind, valid_answer(kind, payload), payload)
        for kind, mutate in cases:
            with self.subTest(kind=kind):
                answer = valid_answer(kind, payload)
                mutate(answer)
                with self.assertRaises(InputError):
                    validate_answer(kind, answer, payload)

    def test_direct_mode_is_explicit_and_does_not_silently_use_old_specs(self):
        self.prepare()
        frozen_tasks = make_tasks(load_dataset(self.dataset), self.config,
                                  load_specs(self.specs, load_dataset(self.dataset)), ["q1"])
        self.config["query_understanding"] = "direct"
        with self.assertRaisesRegex(InputError, "disabled"):
            prepare_specs(self.dataset, self.config, self.root / "unexpected", client_factory=FixtureClient)
        with self.assertRaisesRegex(InputError, "omit --specs"):
            plan(self.dataset, self.config, self.specs)
        with self.assertRaisesRegex(InputError, "omit --specs"):
            run(self.dataset, self.config, self.specs, self.root / "eval", client_factory=FixtureClient)
        direct_tasks = make_tasks(load_dataset(self.dataset), self.config, {}, ["q1"])
        self.assertFalse({t["task_id"] for t in frozen_tasks} & {t["task_id"] for t in direct_tasks})
        self.assertNotEqual(next(t["prompt_hash"] for t in frozen_tasks if t["kind"] == "legacy_url"),
                            next(t["prompt_hash"] for t in direct_tasks if t["kind"] == "legacy_url"))

    def test_config_defaults_to_frozen_and_cli_supports_direct_without_specs(self):
        from quality_eval.__main__ import main
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        self.assertEqual(self.config["query_understanding"], "frozen")
        raw = {"models": {"mock": {"protocol": "mock", "model": "simulated"}},
               "query_understanding": "direct",
               "experiments": [{"id": "direct", "scheme": "google_overview_v1", "model": "mock"}]}
        self.config_file.write_text(json.dumps(raw))
        self.assertIsNone(load_config(self.config_file)["query_model"])
        with redirect_stdout(StringIO()) as out, redirect_stderr(StringIO()):
            code = main(["run", "--dataset", str(self.dataset), "--config", str(self.config_file),
                         "--output", str(self.root / "cli-direct")])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["query_understanding"], "direct")
        raw["query_understanding"] = False
        self.config_file.write_text(json.dumps(raw))
        with self.assertRaises(InputError):
            load_config(self.config_file)


if __name__ == "__main__":
    unittest.main()

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frontend.service import PROJECT, Service
from frontend.diagnostics import failure_info
from quality_eval.calibration import LABEL_SCHEMA, compare_with_human, human_references
from quality_eval.calibrate import experiment_tasks, summarize
from quality_eval.config import load_config
from quality_eval.contracts import validate_answer
from quality_eval.runner import plan, run
from quality_eval.schemes import make_tasks
from quality_input.common import InputError, digest, write_json
from quality_input.pipeline import import_dataset, load_dataset


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_root = self.root / "data"
        self.data_root.mkdir()
        self.path = self.data_root / "fixture"
        rid = "response_" + digest(["run", "q1"])
        self.result_ids = ["result_" + digest([rid, rank]) for rank in range(1, 6)]
        source = {
            "queries": [{"query_id": "q1", "query_text": "ExampleDB installation"}],
            "search_runs": [{"search_run_id": "run", "provider": "fixture", "requested_top_k": 5,
                             "collection_completed_at": "2026-10-20T00:00:00Z"}],
            "responses": [{"query_id": "q1", "search_run_id": "run", "status": "ok", "results": [
                {"rank": rank, "url": f"https://example.com/{rank}", "title": "Install",
                 "snippet": "Install ExampleDB with this command."} for rank in range(1, 6)]}],
            "reference_labels": [{"query_id": "q1", "document_key": "url_exact_v1:" + digest(f"https://example.com/{rank}"),
                "result_id": self.result_ids[rank - 1], "grade": grade, "label_schema_version": LABEL_SCHEMA,
                "source": "human", "version": "fixture-v1", "evidence_mode": "api_only",
                "reference_scope": "pooled", "reason": "HUMAN_REASON_MUST_NOT_LEAK", "rubric_version": "not_recorded"}
                for rank, grade in enumerate((-1, 0, 1, 2, 3), 1)],
        }
        raw = self.root / "source.json"
        write_json(raw, source)
        self.assertEqual(import_dataset(raw, self.path, use_collection_time=True)["status"], "ready")
        self.data = load_dataset(self.path)
        self.refs = human_references(self.data)
        self.config = load_config(PROJECT / "examples/evaluation.mock.json")
        self.config.update(query_understanding="direct", query_model=None, experiments=[{
            "id": "answerability_only_v1-mock_a", "scheme": "answerability_only_v1",
            "model": "mock_a", "mode": "api_only", "repeats": 1}])

    def tearDown(self):
        self.temp.cleanup()

    def test_single_dimension_tasks_do_not_leak_reference_labels(self):
        self.assertEqual({r["grade"] for r in self.refs.values()}, {-1, 0, 1, 2, 3})
        tasks = make_tasks(self.data, self.config, {}, {"q1"})
        self.assertEqual(len(tasks), 5)
        self.assertEqual({t["kind"] for t in tasks}, {"answerability"})
        for task in tasks:
            self.assertIsNone(task["blocked_reason"])
            encoded = json.dumps(task["payload"])
            self.assertNotIn("HUMAN_REASON_MUST_NOT_LEAK", encoded)
            self.assertNotIn("reference_labels", encoded)
            self.assertNotIn("human_grade", encoded)
            self.assertNotIn("jev", encoded)
            self.assertIsNone(task["payload"]["reference_answer"])
        summary = plan(self.path, self.config)
        self.assertEqual(summary["experiments"]["answerability_only_v1-mock_a"]["by_kind"], {"answerability": 5})

    def test_schema_all_grades_and_evidence_requirements(self):
        payload = make_tasks(self.data, self.config, {}, {"q1"})[0]["payload"]
        for grade in (-1, 0, 1, 2, 3):
            answer = {"dimensions": {"answerability": {"status": "rated", "value": grade,
                "reason": "Fixture evidence", "evidence": [{"path": "/result/snippet", "quote": "Install ExampleDB"}]}}}
            validate_answer("answerability", answer, payload)
        for value in (True, None, 4):
            bad = copy.deepcopy(answer)
            bad["dimensions"]["answerability"]["value"] = value
            with self.assertRaises(InputError):
                validate_answer("answerability", bad, payload)
        for value in (-1, 2, 3):
            bad = copy.deepcopy(answer)
            bad["dimensions"]["answerability"].update(value=value, evidence=[])
            with self.assertRaises(InputError):
                validate_answer("answerability", bad, payload)
        bad = copy.deepcopy(answer)
        bad["dimensions"]["noise"] = bad["dimensions"]["answerability"]
        with self.assertRaises(InputError):
            validate_answer("answerability", bad, payload)

    def test_agreement_denominator_excludes_failures_simulation_and_missing(self):
        rows = []
        for i, grade in enumerate((-1, 0, 1, 2)):
            rows.append({"result_id": self.result_ids[i], "query_id": "q1", "mode": "api_only",
                         "dimension": "answerability", "experiment_id": "experiment", "repeat": 0,
                         "judgment_status": "rated", "value": grade, "simulated": False})
        rows[1]["value"] = 1
        rows[2].update(judgment_status="error", value=None)
        rows[3]["simulated"] = True
        out = compare_with_human(rows, self.refs, ["experiment"], repeats=2)
        first, second = out["experiments"]
        self.assertEqual((first["reference_count"], first["compared"], first["agreed"], first["uncompared"]), (5, 2, 1, 3))
        self.assertEqual(first["agreement"], 0.5)
        self.assertEqual(first["confusion"]["0"]["1"], 1)
        self.assertEqual(second["compared"], 0)
        self.assertIsNone(second["agreement"])
        rows[0]["mode"] = "page_content"
        self.assertEqual(compare_with_human(rows, self.refs)["experiments"][0]["compared"], 1)

    def test_duplicate_human_references_are_not_silently_selected(self):
        data = copy.deepcopy(self.data)
        data["reference_labels"].append(copy.deepcopy(data["reference_labels"][0]))
        with self.assertRaises(InputError):
            human_references(data)

    def test_prompt_experiments_keep_input_fixed_and_use_fresh_cache_keys(self):
        original = make_tasks(self.data, self.config, {}, {'q1'})
        first = experiment_tasks(self.data, self.config, {'q1'}, 'candidate one')
        second = experiment_tasks(self.data, self.config, {'q1'}, 'candidate two')
        self.assertEqual([t['payload'] for t in first], [t['payload'] for t in original])
        self.assertEqual([t['input_hash'] for t in first], [t['input_hash'] for t in second])
        self.assertTrue(all(a['task_id'] != b['task_id'] for a, b in zip(first, second)))
        self.assertNotIn('HUMAN_REASON_MUST_NOT_LEAK', json.dumps(first))

    def test_calibration_target_counts_failures_in_full_denominator(self):
        rows = [{'result_id': rid, 'query_id': 'q1', 'mode': 'api_only',
                 'dimension': 'answerability', 'experiment_id': 'experiment', 'repeat': 0,
                 'judgment_status': 'rated', 'value': ref['grade'], 'simulated': False,
                 'reason': 'model reason', 'task_id': rid}
                for rid, ref in self.refs.items()]
        rows[-1].update(judgment_status='error', value=None)
        report, differences = summarize(rows, self.refs, self.data, 'experiment')
        score = report['experiments'][0]
        self.assertEqual(score['agreement'], 1)
        self.assertEqual(score['exact_over_all'], .8)
        self.assertFalse(score['target_met'])
        self.assertEqual(len(differences), 1)
        self.assertEqual(score['by_query'], [{'query_id': 'q1', 'total': 5, 'valid': 4, 'agreed': 4}])

    def test_payment_failure_is_not_reported_as_a_bad_quality_score(self):
        error = failure_info({'judgment_status': 'error', 'reason': 'HTTP 402', 'error_code': 'http_error'})
        self.assertEqual(error['code'], 'provider_payment_required')
        self.assertIn('额度', error['label'])

    def test_target_requires_complete_coverage_and_95_percent_exact(self):
        refs = {str(i): {'query_id': 'q1', 'grade': 0} for i in range(100)}
        rows = [{'result_id': str(i), 'query_id': 'q1', 'mode': 'api_only', 'dimension': 'answerability',
                 'experiment_id': 'e', 'repeat': 0, 'judgment_status': 'rated',
                 'value': 0 if i < 95 else 1, 'simulated': False} for i in range(100)]
        score = compare_with_human(rows, refs)['experiments'][0]
        self.assertEqual(score['exact_over_all'], .95)
        self.assertTrue(score['target_met'])
        rows[-1].update(judgment_status='error', value=None)
        score = compare_with_human(rows, refs)['experiments'][0]
        self.assertEqual(score['exact_over_all'], .95)
        self.assertFalse(score['target_met'])

    def test_frontend_reference_preview_and_post_scoring_join(self):
        service = Service(data_root=self.data_root, runs_root=self.root / "runs")
        try:
            preview = service.preview_scope({"dataset": "fixture", "limit": None})
            self.assertEqual(preview["selected"]["human_labels"], 5)
            samples = service.calibration_samples({"dataset": "fixture", "query_id": "q1"})
            self.assertEqual(samples["total_references"], 5)
            self.assertEqual(samples["distribution"]["0"], 1)
            self.assertEqual(samples["items"][0]["reference"]["reason"], "HUMAN_REASON_MUST_NOT_LEAK")
            self.assertEqual(service.calibration_samples({"dataset": "fixture", "query_id": "absent"})["items"], [])
            job = "a" * 32
            root = service.runs_root / job
            root.mkdir()
            write_json(root / "job.json", {"id": job, "schemes": ["answerability_only_v1"], "models": ["mock_a"],
                "mode": "api_only", "repeats": 1, "query_understanding": "direct"})
            write_json(root / "context.json", {"queries": self.data["queries"], "responses": self.data["responses"],
                "pages": {}, "human_references": self.refs})
            run(self.path, self.config, None, root / "evaluation")
            detail = service.detail(job)
            self.assertEqual(set(detail["metrics"]), {"answerability"})
            self.assertEqual(detail["calibration"]["experiments"][0]["compared"], 0)
            result = service.results(job, {})
            self.assertEqual(result["items"][1]["human_reference"]["grade"], 0)
            self.assertIsNone(result["items"][1]["human_agreement"])
            exported = json.loads(service.download(job, "calibration.json"))
            self.assertEqual(exported["reference_count"], 5)
            real_rows = copy.deepcopy(service._rows(root))
            for row in real_rows:
                if row["result_id"] in self.result_ids[:2]:
                    row.update(simulated=False, judgment_status="rated", value=0)
            with patch.object(service, "_rows", return_value=real_rows):
                compared = service.results(job, {})["items"]
                self.assertIs(compared[0]["human_agreement"], False)
                self.assertIs(compared[1]["human_agreement"], True)
                score = service.detail(job)["calibration"]["experiments"][0]
                self.assertEqual((score["agreed"], score["compared"], score["agreement"]), (1, 2, 0.5))
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()

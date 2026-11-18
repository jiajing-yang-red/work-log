import copy
import tempfile
import unittest
from pathlib import Path

from frontend.datasets import DatasetStore
from frontend.service import PROJECT
from quality_eval.config import load_config
from quality_eval.schemes import make_tasks
from quality_input.cases import BENCHMARKS, CONDITIONS, split_case_directory
from quality_input.common import InputError, canonical, read_json, write_json
from quality_input.pipeline import load_dataset


class CaseDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "cases"
        self.source.mkdir()
        for benchmark in BENCHMARKS:
            for num in (1, 2):
                key = f"{benchmark}-{num}"
                case = {"key": key, "query": f"Question {key}", "正确答案": "GOLD_MUST_STAY_OUT"}
                for condition in CONDITIONS:
                    case[condition] = {"reply": "REPLY_MUST_STAY_OUT", "urls": [
                        {"url": "https://example.com/same", "title": "", "summary": f"{condition}\nExact text  甲"},
                        {"url": "https://example.com/same", "title": "Another snapshot", "summary": "Different text"},
                    ] if num == 1 else []}
                write_json(self.source / f"{key}.json", case)

    def tearDown(self):
        self.temp.cleanup()

    def test_fifteen_bundles_preserve_positions_evidence_and_empty_queries_without_answer_leakage(self):
        before = {p.name: p.read_bytes() for p in self.source.glob("*.json")}
        output = self.root / "converted"
        index = split_case_directory(self.source, output)
        self.assertEqual(len(index["datasets"]), 15)
        self.assertEqual({p.name: p.read_bytes() for p in self.source.glob("*.json")}, before)
        self.assertEqual(read_json(output / "datasets_index.json"), index)
        store = DatasetStore(self.root / "frontend-output")
        config = load_config(PROJECT / "examples/evaluation.mock.json")
        config.update(query_understanding="direct", query_model=None, experiments=[{
            "id": "score", "scheme": "answerability_only_v1", "model": "mock_a", "mode": "api_only", "repeats": 1}])
        result_ids = set()
        for item in index["datasets"]:
            bundle = Path(item["path"])
            data = load_dataset(bundle)
            self.assertEqual(item["counts"]["queries"], 2)
            self.assertEqual(item["counts"]["results"], 2)
            self.assertEqual(item["counts"]["response_status"], {"empty": 1, "ok": 1})
            self.assertEqual(item["issue_counts"]["MISSING_TITLE"], 1)
            self.assertEqual(item["issue_counts"]["EXACT_DUPLICATE_URL"], 1)
            self.assertEqual(data["manifest"]["time_basis"], "unspecified")
            self.assertIsNone(data["manifest"]["evaluation_as_of"])
            self.assertEqual(data["reference_labels"], [])
            self.assertEqual(data["page_snapshots"], [])
            self.assertEqual(len(data["search_runs"]), 1)
            self.assertEqual(data["search_runs"][0]["request_parameters"]["condition"], item["condition"])
            self.assertEqual({q["category"] for q in data["queries"]}, {item["benchmark"]})
            results = data["responses"][0]["results"]
            self.assertEqual([r["rank"] for r in results], [1, 2])
            self.assertEqual(results[0]["snippet"], f"{item['condition']}\nExact text  甲")
            self.assertNotEqual(results[0]["evidence_hash"], results[1]["evidence_hash"])
            for r in results:
                self.assertNotIn(r["result_id"], result_ids)
                result_ids.add(r["result_id"])
            self.assertEqual((bundle / "raw/source.json").read_bytes(), Path(item["input_path"]).read_bytes())
            raw = read_json(bundle / "raw/source.json")
            self.assertEqual(raw["case_answers"][item["benchmark"] + "-1"]["gold_answer"], "GOLD_MUST_STAY_OUT")
            self.assertEqual(raw["case_provenance"]["original_directory"], str(self.source.resolve()))
            tasks = make_tasks(data, config, {}, {q["query_id"] for q in data["queries"]})
            self.assertEqual(len(tasks), 2)
            for task in tasks:
                self.assertIsNone(task["blocked_reason"])
                self.assertIsNone(task["payload"]["reference_answer"])
                for marker in ("GOLD_MUST_STAY_OUT", "REPLY_MUST_STAY_OUT", "case_answers"):
                    self.assertNotIn(marker, canonical(task["payload"]))
            loaded = store.add({"path": str(bundle)})
            self.assertEqual(loaded["status"], "ready")
            self.assertEqual(loaded["dataset"]["name"], item["name"])
        self.assertEqual(len(store.list()), 15)
        self.assertEqual(len(DatasetStore(store.root).list()), 15)

    def test_bad_cases_are_not_silently_skipped(self):
        path = self.source / "browsecomp-1.json"
        original = read_json(path)
        variants = []
        wrong_key = copy.deepcopy(original)
        wrong_key["key"] = "browsecomp-99"
        variants.append(wrong_key)
        old_urls = copy.deepcopy(original)
        old_urls["off"]["urls"] = ["https://example.com/no-text"]
        variants.append(old_urls)
        missing = copy.deepcopy(original)
        del missing["qwen50"]
        variants.append(missing)
        for i, case in enumerate(variants):
            write_json(path, case)
            output = self.root / f"invalid-{i}"
            with self.subTest(i=i), self.assertRaises(InputError):
                split_case_directory(self.source, output)
            self.assertFalse(output.exists())

    def test_existing_output_is_never_overwritten(self):
        output = self.root / "existing"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_text("keep")
        with self.assertRaises(InputError):
            split_case_directory(self.source, output)
        self.assertEqual(marker.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()

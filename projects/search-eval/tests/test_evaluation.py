import json
import math
from pathlib import Path
import tempfile
import unittest

from search_eval.__main__ import compare, main, make_report, write_report
from search_eval.io import load_qrels, load_results
from search_eval.metrics import evaluate_query
from search_eval.report import aggregate


ROOT = Path(__file__).resolve().parents[1]


class MetricTests(unittest.TestCase):
    def test_hand_calculated_ranking(self):
        result = evaluate_query(["a", "x", "b"], {"a": 3, "b": 2, "c": 1}, [1, 3], 2)
        values = result["metrics"]
        self.assertAlmostEqual(values["Precision@3"], 2 / 3)
        self.assertEqual(values["Recall@3"], 1)
        self.assertAlmostEqual(values["F1@3"], 0.8)
        self.assertAlmostEqual(values["AP@3"], 5 / 6)
        expected = (7 + 3 / 2) / (7 + 3 / math.log2(3) + 1 / 2)
        self.assertAlmostEqual(values["NDCG@3"], expected)
        self.assertEqual(result["cutoffs"]["3"]["unjudged"], 1)
        self.assertEqual(values["Recall@1"], 0.5)

    def test_short_return_and_ap_denominator(self):
        values = evaluate_query(["a"], {"a": 3, "b": 2}, [5])["metrics"]
        self.assertEqual(values["Precision@5"], 0.2)
        self.assertEqual(values["Recall@5"], 0.5)
        self.assertEqual(values["AP@5"], 0.5)
        self.assertAlmostEqual(values["F1@5"], 2 / 7)
        self.assertAlmostEqual(values["NDCG@5"], 7 / (7 + 3 / math.log2(3)))

    def test_ideal_dcg_uses_all_judgments(self):
        value = evaluate_query(["b"], {"a": 3, "b": 2}, [1])["metrics"]["NDCG@1"]
        self.assertAlmostEqual(value, 3 / 7)

    def test_graded_ndcg_independent_of_binary_threshold(self):
        values = evaluate_query(["a"], {"a": 1}, [1], 2)["metrics"]
        self.assertEqual(values["Recall@1"], 0)
        self.assertEqual(values["AP@1"], 0)
        self.assertEqual(values["NDCG@1"], 1)

    def test_zero_and_empty_results(self):
        for judgments in ({"a": 3}, {"a": 0}, {}):
            values = evaluate_query([], judgments, [3])["metrics"]
            self.assertTrue(all(value == 0 for value in values.values()))
        values = evaluate_query(["x"], {"a": 0}, [3])["metrics"]
        self.assertTrue(all(value == 0 for value in values.values()))

    def test_binary_labels_and_linear_gain(self):
        values = evaluate_query(["b", "a"], {"a": 1, "b": 0}, [2], 1, "linear")["metrics"]
        self.assertEqual(values["AP"], 0.5)
        self.assertAlmostEqual(values["NDCG@2"], 1 / math.log2(3))

    def test_f1_macro_is_average_of_query_f1(self):
        queries = {"a": evaluate_query(["a"], {"a": 2}, [2]),
                   "b": evaluate_query(["b", "c"], {"b": 2, "c": 2, "d": 2, "e": 2}, [2])}
        summary = aggregate(queries)
        self.assertAlmostEqual(summary["F1@2"], 2 / 3)
        self.assertNotAlmostEqual(summary["F1@2"], 2 * 0.75 * 0.75 / 1.5)
        self.assertIn("MAP@2", summary)

    def test_invalid_metric_inputs(self):
        for kwargs in ({"ks": [0]}, {"ks": []}, {"threshold": 0}, {"threshold": float("nan")}, {"gain": "bad"}):
            with self.assertRaises(ValueError):
                evaluate_query(["a"], {"a": 2}, **kwargs)
        with self.assertRaises(ValueError):
            evaluate_query(["a", "a"], {"a": 2})


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.qrels = ROOT / "examples/qrels.csv"
        self.results = ROOT / "examples/results_baseline.csv"

    def write(self, name, content):
        path = self.directory / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_end_to_end_and_baseline(self):
        baseline = make_report(self.qrels, self.results, [3, 5], run_name="base")
        report = make_report(self.qrels, ROOT / "examples/results_candidate.csv", [3, 5], run_name="new")
        compare(report, baseline)
        self.assertEqual(report["comparison"]["regressions"][0]["query_id"], "q2")
        self.assertEqual(report["query_count"], 4)
        self.assertEqual(report["queries"]["q4"]["metrics"]["AP"], 0)
        self.assertEqual(report["coverage"]["3"]["unjudged"], 1)
        output = self.directory / "report"
        write_report(report, output)
        self.assertEqual(len(list(output.iterdir())), 4)
        loaded = json.loads((output / "report.json").read_text())
        self.assertEqual(loaded["summary"], report["summary"])
        self.assertIn("与基线比较", (output / "report.html").read_text())
        with self.assertRaises(ValueError):
            write_report(report, output)

    def test_json_jsonl_and_bom_csv(self):
        qrels = self.write("qrels.json", '[{"query_id":"q","doc_id":"a","relevance":1}]')
        results = self.write("results.jsonl", '{"query_id":"q","doc_id":"a","rank":1}\n')
        report = make_report(qrels, results, [1], 1)
        self.assertEqual(report["summary"]["MAP@1"], 1)
        csv_path = self.write("bom.csv", "\ufeffquery_id,doc_id,rank\nq,a,1\n")
        self.assertEqual(load_results(csv_path, {"q"}), {"q": ["a"]})

    def test_empty_results_preserve_query_denominator(self):
        results = self.write("empty.csv", "query_id,doc_id,rank\n")
        report = make_report(self.qrels, results, [3])
        self.assertEqual(report["query_count"], 4)
        self.assertTrue(all(value == 0 for value in report["summary"].values()))
        self.assertIsNone(report["coverage"]["3"]["judged_fraction_of_returned"])

    def test_bad_result_rows_rejected(self):
        for data in ("q,a,1\nq,a,2\n", "q,a,1\nq,b,1\n", "q,a,2\n", "unknown,a,1\n", "q,a,1.5\n"):
            path = self.write("bad.csv", "query_id,doc_id,rank\n" + data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                load_results(path, {"q"})

    def test_bad_qrels_rejected(self):
        for data in ("q,a,nan\n", "q,a,-1\n", "q,a,inf\n", "q,a,2\nq,a,3\n"):
            path = self.write("bad.csv", "query_id,doc_id,grade\n" + data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                load_qrels(path)

    def test_baseline_config_and_dataset_must_match(self):
        baseline = make_report(self.qrels, self.results, [3])
        different = make_report(self.qrels, self.results, [5])
        with self.assertRaises(ValueError):
            compare(different, baseline)
        different = make_report(self.qrels, self.results, [3])
        different["inputs"]["qrels_sha256"] = "changed"
        with self.assertRaises(ValueError):
            compare(different, baseline)

    def test_html_escapes_input(self):
        qrels = self.write("escape.json", json.dumps([
            {"query_id": "q", "doc_id": "<script>x</script>", "grade": 2, "query": "<img src=x onerror=alert(1)>"}]))
        results = self.write("escape-results.json", '[{"query_id":"q","doc_id":"<script>x</script>","rank":1}]')
        report = make_report(qrels, results, [1])
        write_report(report, self.directory / "escape")
        html = (self.directory / "escape/report.html").read_text()
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", html)


if __name__ == "__main__":
    unittest.main()

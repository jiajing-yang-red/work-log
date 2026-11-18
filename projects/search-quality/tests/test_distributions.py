import unittest

from frontend.distributions import answerability_distributions


class AnswerabilityDistributionTests(unittest.TestCase):
    job = {"schemes": ["answerability_only_v1"], "models": ["a"], "repeats": 1}

    def row(self, result_id, value, **extra):
        return {"result_id": result_id, "value": value, "judgment_status": "rated",
                "dimension": "answerability", "experiment_id": "answerability_only_v1-a",
                "repeat": 0, "simulated": False, **extra}

    def test_only_valid_real_scores_form_denominator(self):
        rows = [self.row(str(i), grade) for i, grade in enumerate([-1, 0, 0, 1, 2, 3.0])]
        rows += [self.row("failed", None, judgment_status="error"),
                 self.row("mock", 3, simulated=True),
                 self.row("mock-status", None, judgment_status="simulated"),
                 self.row("unknown", None, judgment_status="unjudgeable"),
                 self.row("na", None, judgment_status="not_applicable"),
                 self.row("bool", True), self.row("string", "3"), self.row("invalid", 4),
                 self.row("other", 3, dimension="needs_met")]
        group, = answerability_distributions(rows, self.job, 16)
        self.assertEqual(group["counts"], {"-1": 1, "0": 2, "1": 1, "2": 1, "3": 1})
        self.assertEqual(group["rated"], 6)
        self.assertEqual(group["excluded"], {"error": 1, "simulated": 2, "unjudgeable": 1,
                                             "not_applicable": 1, "invalid": 3})
        self.assertEqual(group["pending"], 2)
        self.assertEqual(group["rated"] + sum(group["excluded"].values()) + group["pending"], 16)

    def test_models_schemes_and_repeats_are_independent_and_retries_count_once(self):
        job = {"schemes": ["answerability_only_v1", "legacy_five_v1", "google_overview_v1"],
               "models": ["a", "b"], "repeats": 2}
        rows = [self.row("url1", None, judgment_status="error"), self.row("url1", 3),
                self.row("url1", 1, repeat=1),
                self.row("url1", 0, experiment_id="answerability_only_v1-b"),
                self.row("url1", -1, experiment_id="legacy_five_v1-a")]
        groups = {(g["experiment_id"], g["repeat"]): g for g in answerability_distributions(rows, job, 2)}
        self.assertEqual(len(groups), 8)
        for key, grade in [(('answerability_only_v1-a', 0), '3'),
                           (('answerability_only_v1-a', 1), '1'),
                           (('answerability_only_v1-b', 0), '0'),
                           (('legacy_five_v1-a', 0), '-1')]:
            self.assertEqual(groups[key]["rated"], 1)
            self.assertEqual(groups[key]["counts"][grade], 1)
            self.assertEqual(groups[key]["pending"], 1)
            self.assertEqual(groups[key]["excluded"], {})
        self.assertEqual(groups[("legacy_five_v1-b", 1)]["pending"], 2)

    def test_empty_and_failed_run_has_no_score_shares(self):
        group, = answerability_distributions([], self.job, 10)
        self.assertEqual(group["pending"], 10)
        self.assertEqual(group["rated"], 0)
        self.assertTrue(all(count == 0 for count in group["counts"].values()))
        group, = answerability_distributions([self.row("url1", None, judgment_status="error")], self.job, 10)
        self.assertEqual(group["rated"], 0)
        self.assertEqual(group["excluded"], {"error": 1})
        self.assertEqual(group["pending"], 9)
        self.assertEqual(answerability_distributions([], {**self.job, "schemes": ["google_overview_v1"]}, 10), [])


if __name__ == "__main__":
    unittest.main()

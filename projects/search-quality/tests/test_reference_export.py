import copy
import json
import tempfile
import unittest
from pathlib import Path

from frontend.datasets import DatasetStore
from frontend.service import PROJECT, Service
from quality_eval.calibration import human_references
from quality_eval.config import load_config
from quality_eval.schemes import make_tasks
from quality_input.common import canonical, write_jsonl
from quality_input.pipeline import import_dataset, load_dataset


def reference_rows():
    return [{
        "sample_id": "q1", "query": "ExampleDB installation", "rank": rank,
        "url": "https://example.com/install", "title": "Installation",
        "snippet": "Install ExampleDB with this command.", "published_at": "2026-10-01T00:00:00Z",
        "golden_answer": "GOLDEN_SECRET", "golden_answer_en": "GOLDEN_EN_SECRET",
        "golden_answer_as_of": "2026-10-20T00:00:00Z",
        "human_review_status": "graded" if rank <= 2 else "not_graded",
        "human_grade": 1 if rank <= 2 else None, "human_reason": "HUMAN_SECRET" if rank <= 2 else "",
        "human_source": "human-export-v1", "label_status": "unresolved" if rank <= 2 else "numeric",
        "jev_v10": {"sample_id": "q1", "rank": rank, "grade": None if rank <= 2 else 3,
                    "prompt_hash": "JEV_SECRET", "scored_at": "2026-10-21T00:00:00Z"},
        "jev_source": "jev_v10_snapshot", "reference_grade": 1 if rank <= 2 else 3,
        "reference_source": "human" if rank <= 2 else "jev",
    } for rank in range(1, 11)]


class ReferenceExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "references.jsonl"
        write_jsonl(self.source, reference_rows())

    def tearDown(self):
        self.temp.cleanup()

    def test_import_preserves_evidence_and_separates_labels_from_all_judge_inputs(self):
        original = self.source.read_bytes()
        output = self.root / "bundle"
        report = import_dataset(self.source, output)
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(report['counts']['reference_labels_by_source'], {'human': 2, 'jev': 8})
        data = load_dataset(output)
        self.assertEqual(data['manifest']['sources'][0]['adapter'], 'answerability_top10_reference_jsonl_v1')
        self.assertIsNone(data['manifest']['evaluation_as_of'])
        self.assertEqual(data['manifest']['time_basis'], 'unspecified')
        self.assertIsNone(data['search_runs'][0]['collection_completed_at'])
        self.assertIsNone(data['responses'][0]['retrieved_at'])
        self.assertEqual(data['search_runs'][0]['provider'], 'not_recorded')
        results = data['responses'][0]['results']
        self.assertEqual([r['rank'] for r in results], list(range(1, 11)))
        self.assertEqual(len({r['result_id'] for r in results}), 10)
        self.assertEqual(len({r['document_key'] for r in results}), 1)
        self.assertEqual([r['snippet'] for r in results], [r['snippet'] for r in reference_rows()])
        self.assertEqual(len(human_references(data)), 2)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual((output / 'raw/source.jsonl').read_bytes(), original)
        config = load_config(PROJECT / 'examples/evaluation.mock.json')
        config.update(query_understanding='direct', query_model=None, experiments=[{
            'id': 'score', 'scheme': 'answerability_only_v1', 'model': 'mock_a', 'mode': 'api_only', 'repeats': 1}])
        tasks = make_tasks(data, config, {}, {'q1'})
        self.assertEqual(len(tasks), 10)
        for task in tasks:
            self.assertIsNone(task['blocked_reason'])
            self.assertIsNone(task['payload']['reference_answer'])
            encoded = canonical(task['payload'])
            for forbidden in ('human_grade', 'reference_grade', 'reference_source', 'jev_v10',
                              'HUMAN_SECRET', 'JEV_SECRET', 'GOLDEN_SECRET', 'GOLDEN_EN_SECRET'):
                self.assertNotIn(forbidden, encoded)

    def test_missing_collection_time_stays_explicit_and_never_uses_label_or_publication_time(self):
        blocked = import_dataset(self.source, self.root / 'blocked', use_collection_time=True)
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['counts']['results'], 10)
        self.assertNotIn('IMPORT_FAILED', blocked['issue_counts'])
        self.assertIn('EVALUATION_TIME_UNAVAILABLE', blocked['issue_counts'])
        explicit = self.root / 'explicit'
        self.assertEqual(import_dataset(self.source, explicit,
            evaluation_as_of='2026-10-22T08:00:00+08:00')['status'], 'ready')
        self.assertEqual(load_dataset(explicit)['manifest']['evaluation_as_of'], '2026-10-22T00:00:00Z')

    def test_malformed_exports_block_without_dropping_or_relabeling_rows(self):
        cases = []
        for updates in ({'reference_grade': 3}, {'reference_grade': True}, {'reference_source': 'model'},
                        {'human_review_status': 'not_graded'}, {'query': ''}):
            rows = reference_rows()
            rows[0].update(updates)
            cases.append(rows)
        for updates in ({'label_status': 'unresolved'}, {'reference_grade': 2},
                        {'human_review_status': 'graded', 'human_grade': 0},
                        {'jev_v10': {'sample_id': 'other', 'rank': 3, 'grade': 3}}):
            rows = reference_rows()
            rows[2].update(updates)
            cases.append(rows)
        rows = reference_rows()
        rows[1]['query'] = 'Conflicting query'
        cases.extend([rows, reference_rows()[:-1], reference_rows() + [reference_rows()[0]],
                      reference_rows() + [{'query_id': 'other'}]])
        for index, rows in enumerate(cases):
            with self.subTest(index=index):
                write_jsonl(self.source, rows)
                report = import_dataset(self.source, self.root / str(index))
                self.assertEqual(report['status'], 'blocked')
                self.assertIn('IMPORT_FAILED', report['issue_counts'])

    def test_dashboard_import_and_human_preview_count_only_human_labels(self):
        data_root = self.root / 'data'
        store = DatasetStore(data_root)
        loaded = store.add({'path': str(self.source), 'time_policy': 'unspecified', 'name': 'Mixed references'})
        self.assertEqual(loaded['status'], 'ready')
        item = loaded['dataset']
        self.assertEqual((item['queries'], item['results'], item['reference_labels']), (1, 10, 10))
        self.assertEqual(item['reference_labels_by_source'], {'human': 2, 'jev': 8})
        service = Service(data_root=data_root, runs_root=self.root / 'runs')
        try:
            preview = service.preview_scope({'dataset': item['id'], 'limit': None})
            self.assertEqual(preview['selected']['human_labels'], 2)
            self.assertEqual(preview['selected']['results'], 10)
            references = service.calibration_samples({'dataset': item['id']})
            self.assertEqual(references['total_references'], 2)
            self.assertTrue(all(r['reference']['source'] == 'human' for r in references['items']))
        finally:
            service.close()

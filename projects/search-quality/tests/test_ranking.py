import copy
import math
import unittest

from quality_input.common import InputError
from quality_metrics.ranking import build_rankings, compare_groups, metrics


class RankingTests(unittest.TestCase):
    def report(self, scores, *, name='a', urls=None, status='ok', repeat=0):
        results = [{'result_id': f'{name}-{i}', 'rank': i+1,
                    'url': urls[i] if urls else f'https://example.org/{i}',
                    'title': 'title', 'snippet': 'same evidence'} for i in range(len(scores))]
        rows = [{'result_id': r['result_id'], 'query_id': 'q1', 'search_run_id': name,
                 'dimension': 'answerability', 'experiment_id': 'answerability_only_v1-model',
                 'repeat': repeat, 'judgment_status': 'error' if s is None else 'rated',
                 'value': s, 'simulated': False, 'model_id': 'test-model', 'prompt_hash': 'prompt-v1',
                 'scheme_version': '1.1', 'mode': 'api_only', 'query_understanding': 'direct',
                 'query_spec_hash': None} for r, s in zip(results, scores)]
        context = {'queries': [{'query_id': 'q1', 'query_text': 'question'}],
                   'responses': [{'query_id': 'q1', 'search_run_id': name, 'status': status, 'results': results}]}
        job = {'id': name, 'dataset_path': '/example/'+name, 'simulated': False}
        config = {'models': {'model': {'protocol': 'openai_chat', 'model': 'test-model'}},
                  'query_understanding': 'direct', 'experiments': [{'id': 'answerability_only_v1-model',
                   'model': 'model', 'scheme': 'answerability_only_v1', 'mode': 'api_only', 'repeats': repeat+1}]}
        return rows, context, job, config

    def group(self, *args, **kwargs):
        return build_rankings(*self.report(*args, **kwargs))['groups'][0]

    def test_known_discount_example_and_first_answer(self):
        a=metrics([3,0,0,0,0,3,0,0,0,0])
        self.assertAlmostEqual(a['ndcg_at_10'], (1+1/math.log2(7))/(1+1/math.log2(3)))
        self.assertEqual(a['mrr_at_10'], 1)
        self.assertEqual(metrics([3,3,0])['ndcg_at_10'], 1)
        m=metrics([0,2,3])
        self.assertEqual(m['mrr_at_10'], 1/3)
        self.assertEqual(m['success_at_1'], 0)
        self.assertEqual(m['success_at_3'], 1)

    def test_zero_negative_short_and_beyond_cutoff(self):
        for s in ([], [0]*10, [-1,0,0], [0]*10+[3]):
            self.assertTrue(all(v==0 for v in metrics(s).values()))
        self.assertEqual(metrics([3])['success_at_10'], 1)
        self.assertEqual(metrics([2])['success_at_10'], 0)

    def test_failed_missing_simulated_and_boolean_excluded_without_compressing(self):
        for bad in (None, True, '3', 4):
            group=self.group([bad,3])
            self.assertEqual(group['eligible_queries'],0)
            self.assertIsNone(group['metrics']['mrr_at_10'])
        rows,context,job,config=self.report([0,3])
        group=build_rankings(rows[1:],context,job,config)['groups'][0]
        self.assertEqual(group['excluded'],{'pending_scores':1})
        job['simulated']=True
        self.assertEqual(build_rankings(rows,context,job,config)['groups'][0]['excluded'],{'simulated':1})

    def test_empty_is_zero_but_failed_collection_is_excluded(self):
        group=self.group([],status='empty')
        self.assertEqual(group['eligible_queries'],1)
        self.assertEqual(group['empty_queries'],1)
        self.assertEqual(group['metrics']['ndcg_at_10'],0)
        group=self.group([],status='error')
        self.assertEqual(group['eligible_queries'],0)

    def test_duplicates_keep_slots_and_do_not_add_gain(self):
        group=self.group([0,3,3],urls=['a','a','b'])
        self.assertEqual(group['queries'][0]['scores'],[0,0,3])
        self.assertEqual(group['metrics']['mrr_at_10'],1/3)
        self.assertEqual(group['duplicate_slots'],1)

    def test_repeats_search_runs_and_retries_separate(self):
        rows,context,job,config=self.report([3],repeat=1)
        rows=[{**rows[0],'value':None,'judgment_status':'error'},*rows]
        context['responses'].append({**context['responses'][0], 'search_run_id':'b', 'results':[],'status':'empty'})
        groups=build_rankings(rows,context,job,config)['groups']
        self.assertEqual(len(groups),4)
        self.assertEqual(groups[0]['eligible_queries'],0)
        self.assertEqual(groups[2]['metrics']['mrr_at_10'],1)
        self.assertEqual(groups[3]['metrics']['mrr_at_10'],0)

    def test_pooled_ideal_and_conflicts_use_fixed_baseline(self):
        baseline=self.group([0,3],name='baseline',urls=['a','b'])
        current=self.group([3,3],name='current',urls=['a','c'])
        r=compare_groups(current,baseline)
        self.assertEqual(r['label_conflicts'],1)
        self.assertEqual(r['shared_labels'],1)
        self.assertEqual(r['current']['mrr_at_10'],.5)
        self.assertAlmostEqual(r['current']['ndcg_at_10'], (7/math.log2(3))/(7+7/math.log2(3)))
        self.assertEqual(current['queries'][0]['results'][0]['grade'],3)  # no mutation

    def test_compare_excludes_changed_evidence_failed_scores_and_other_queries(self):
        baseline=self.group([3])
        current=self.group([3],name='b')
        current['queries'][0]['results'][0]['evidence_hash']='changed'
        r=compare_groups(current,baseline)
        self.assertEqual(r['excluded'],{'evidence_changed':1})
        self.assertIsNone(r['current']['ndcg_at_10'])
        current=self.group([None],name='b')
        self.assertEqual(compare_groups(current,baseline)['excluded'],{'incomplete_or_invalid':1})
        current=self.group([3],name='b')
        current['queries'][0]['query_id']='q2'
        r=compare_groups(current,baseline)
        self.assertEqual(r['common_queries'],0)
        self.assertEqual(r['unmatched_current'],1)

    def test_comparison_rejects_different_rubrics_and_query_specs(self):
        a=self.group([3]);b=copy.deepcopy(a);b['signature']='different'
        with self.assertRaises(InputError):compare_groups(a,b)
        b=copy.deepcopy(a);b['queries'][0]['results'][0]['query_spec_hash']='changed'
        self.assertEqual(compare_groups(a,b)['excluded'],{'query_spec_changed':1})

    def test_top10_not_score_sorted_and_rank_gaps_rejected(self):
        rows,context,job,config=self.report([0]*10+[None])
        self.assertEqual(build_rankings(rows,context,job,config)['groups'][0]['eligible_queries'],1)
        context['responses'][0]['results'][0]['rank']=20
        self.assertEqual(build_rankings(rows,context,job,config)['groups'][0]['excluded'],{'invalid_ranks':1})

    def test_old_context_infers_search_run_and_assigns_empty_query(self):
        rows,context,job,config=self.report([3])
        context['responses'][0].pop('search_run_id')
        context['queries'].append({'query_id':'q2','query_text':'empty'})
        context['responses'].append({'query_id':'q2','status':'empty','results':[]})
        g=build_rankings(rows,context,job,config)['groups'][0]
        self.assertEqual(g['query_total'],2)
        self.assertEqual(g['metrics']['success_at_1'],.5)


if __name__ == '__main__':
    unittest.main()

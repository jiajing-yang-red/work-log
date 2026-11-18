"""Reproducible answerability prompt experiments; labels are joined after calls.

python3 -m quality_eval.calibrate --help
Credentials are read from the project's .env without printing or persisting them.
"""

import argparse
import copy
import os
from collections import Counter
from pathlib import Path

from frontend.service import PROJECT, read_credentials
from quality_input.common import InputError, digest, file_digest, read_json, write_json, write_jsonl
from quality_input.pipeline import load_dataset
from quality_llm import LLMClient, validate_profile
from .calibration import GRADES, compare_with_human, human_references, valid_grade
from .config import load_config
from .contracts import schema_for
from .execution import execute_tasks
from .runner import flatten
from .schemes import make_tasks
from .storage import run_directory
from quality_input.common import canonical


def experiment_tasks(dataset, config, query_ids, system):
    tasks = make_tasks(dataset, config, {}, query_ids)
    for task in tasks:
        original = task['task_id']
        task['system'] = system
        task['prompt_hash'] = digest(system)
        task['task_id'] = digest({'base_task_id': original, 'prompt_hash': task['prompt_hash']})
    return tasks


def summarize(rows, refs, dataset, experiment_id, repeats=1):
    report = compare_with_human(rows, refs, [experiment_id], repeats)
    source = {r['result_id']: r for response in dataset['responses'] for r in response['results']}
    queries = {q['query_id']: q['query_text'] for q in dataset['queries']}
    for e in report['experiments']:
        # Unlike valid-only agreement, this target cannot improve by failing hard cases.
        e['exact_over_all'] = e['agreed'] / e['reference_count'] if e['reference_count'] else None
        e['target_met'] = e['coverage'] == 1 and e['exact_over_all'] >= .95
        e['macro_recall_present_grades'] = sum(
            e['by_human_grade'][str(g)]['agreed'] / sum(r['grade'] == g for r in refs.values())
            for g in GRADES if any(r['grade'] == g for r in refs.values())
        ) / len(set(r['grade'] for r in refs.values())) if refs else None
        e['by_query'] = []
        for qid in sorted({r['query_id'] for r in refs.values()}):
            subset = [r for r in rows if r['query_id'] == qid and r['repeat'] == e['repeat']]
            valid = [r for r in subset if r['judgment_status'] == 'rated' and not r['simulated'] and valid_grade(r['value'])]
            e['by_query'].append({'query_id': qid, 'total': sum(v['query_id'] == qid for v in refs.values()),
                                  'valid': len(valid), 'agreed': sum(r['value'] == refs[r['result_id']]['grade'] for r in valid)})
    disagreements = []
    for row in rows:
        ref = refs.get(row['result_id'])
        if not ref:
            continue
        if row['judgment_status'] != 'rated' or row['simulated'] or row['value'] != ref['grade']:
            src = source[row['result_id']]
            disagreements.append({'query_id': row['query_id'], 'query': queries[row['query_id']],
                'result_id': row['result_id'], 'rank': src['rank'], 'title': src['title'], 'url': src['url'],
                'human_grade': ref['grade'], 'human_reason': ref['reason'], 'model_grade': row['value'],
                'model_reason': row['reason'], 'status': row['judgment_status'], 'repeat': row['repeat'],
                'task_id': row['task_id']})
    return report, sorted(disagreements, key=lambda r: (r['query_id'], r['rank'], r['repeat']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--protocol', required=True, help='Frozen grouped split JSON')
    parser.add_argument('--config', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--model')
    parser.add_argument('--prompt', required=True, help='Rules text without output schema')
    parser.add_argument('--split', choices=['development', 'validation', 'all'], required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 1 <= args.workers <= 8:
        raise InputError('repeats must be 1..10; workers must be 1..8')
    dataset = load_dataset(args.dataset)
    protocol = read_json(args.protocol)
    if file_digest(Path(args.dataset) / 'manifest.json') != protocol['dataset_manifest_sha256']:
        raise InputError('Calibration dataset changed')
    dev, val = protocol['development_query_ids'], protocol['validation_query_ids']
    if set(dev) & set(val) or set(dev + val) != {q['query_id'] for q in dataset['queries']}:
        raise InputError('Split must cover all queries exactly without overlap')
    ids = dev + val if args.split == 'all' else protocol[args.split + '_query_ids']
    config = load_config(args.config)
    profile = copy.deepcopy(config['models'][args.profile])
    if args.model:
        profile['model'] = args.model
    profile.update(parameters={**profile['parameters'], 'temperature': 0}, max_concurrency=args.workers)
    profile = validate_profile(profile)
    config = {'models': {args.profile: profile}, 'query_understanding': 'direct', 'query_model': None,
        'experiments': [{'id': 'calibration', 'scheme': 'answerability_only_v1', 'model': args.profile,
                         'mode': 'api_only', 'repeats': args.repeats}],
        'runtime': {'max_workers': args.workers, 'max_input_chars': 180000}, 'approved_authority_domains': []}
    os.environ.update(read_credentials(PROJECT / '.env', {profile['api_key_env']}))
    client = LLMClient(profile)
    client.check_credentials()
    rules = Path(args.prompt).read_text(encoding='utf-8')
    system = rules + '\nOutput JSON schema:\n' + canonical(schema_for('answerability', 'direct'))
    tasks = experiment_tasks(dataset, config, ids, system)
    identity = {'dataset_version': dataset['manifest']['dataset_version'], 'protocol': protocol,
        'split': args.split, 'config': config, 'prompt_hash': digest(system), 'task_ids': [t['task_id'] for t in tasks]}
    with run_directory(args.output, identity, args.resume) as root:
        write_json(root / 'config.json', config)
        (root / 'prompt.txt').write_text(rules, encoding='utf-8')
        def progress(done, total):
            if done % 10 == 0 or done == total:
                print(f'{done}/{total}', flush=True)
        outcomes = execute_tasks(root, tasks, {args.profile: client}, config, progress=progress)
        rows = flatten(outcomes)
        write_jsonl(root / 'judgments.jsonl', rows)
        # Labels are accessed only after model calls are complete.
        refs = {rid: ref for rid, ref in human_references(dataset).items() if ref['query_id'] in ids}
        report, differences = summarize(rows, refs, dataset, 'calibration', args.repeats)
        report.update(split=args.split, prompt_hash=digest(system), model=profile['model'],
            task_status=dict(Counter(o['status'] for o in outcomes)),
            usage={'input_tokens': sum((o.get('call') or {}).get('usage', {}).get('input_tokens') or 0 for o in outcomes),
                   'output_tokens': sum((o.get('call') or {}).get('usage', {}).get('output_tokens') or 0 for o in outcomes)})
        write_json(root / 'report.json', report)
        write_jsonl(root / 'disagreements.jsonl', differences)
        print(canonical({'split': args.split, 'model': profile['model'], 'status': report['task_status'],
            'scores': [{k: e[k] for k in ('repeat', 'agreed', 'compared', 'reference_count', 'exact_over_all', 'target_met')}
                       for e in report['experiments']]}), flush=True)


if __name__ == '__main__':
    main()

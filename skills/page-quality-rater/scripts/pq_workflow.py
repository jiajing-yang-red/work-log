#!/usr/bin/env python3
"""Prepare evidence packets and validate/merge PQ decisions. No model or network calls."""
import argparse
import copy
import datetime as dt
import hashlib
import html
import json
from pathlib import Path
import shutil

RATINGS={'Lowest':0,'Lowest+':0.5,'Low':1,'Low+':1.5,'Medium':2,'Medium+':2.5,'High':3,'High+':3.5,'Highest':4}
CHECKS=('page_identity','main_content','page_experience','site_creator','reputation','lowest_screen')


def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def sha(raw):return hashlib.sha256(raw if isinstance(raw,bytes) else raw.encode()).hexdigest()
def write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.replace(path)
def read(path):
    path=Path(path);text=path.read_text(encoding='utf-8-sig')
    return [json.loads(line) for line in text.split('\n') if line.strip()] if path.suffix=='.jsonl' else json.loads(text)
def at(data,path):
    for key in path:data=data[key]
    return data
def paths(data,rows_key=None,results_key=None,top_k=None):
    root=rows_key.split('.') if rows_key else [];rows=at(data,root)
    result=[root+[i] for i in range(len(rows))] if isinstance(rows,list) else [root]
    if results_key:
        expanded=[]
        for path in result:
            p=path+results_key.split('.');values=at(data,p)
            if not isinstance(values,list):raise ValueError('results-key must select arrays')
            expanded.extend(p+[i] for i in range(min(len(values),top_k or len(values))))
        result=expanded
    elif top_k:raise ValueError('top-k requires results-key')
    if any(not isinstance(at(data,p),dict) for p in result):raise ValueError('Selected records must be objects')
    return result


def fetch_material(row,root):
    f=row.get('page_content_fetch',row)
    if not isinstance(f,dict):raise ValueError('Fetch field must be an object')
    body=f.get('page_content',f.get('content','')) or ''
    if not isinstance(body,str):raise ValueError('Body must be text')
    origin=f.get('content_file');file=None
    if origin:
        file=(root/origin).resolve()
        if file.is_file():
            disk=file.read_text(encoding='utf-8')
            if body and disk.strip()!=body.strip():raise ValueError('Body and content_file differ: '+str(file))
            body=disk
    i=f.get('selected_attempt');attempts=f.get('attempts',[])
    a=attempts[i] if isinstance(i,int) and 0<=i<len(attempts) else {}
    return body,{'status':f.get('status','unknown'),'provider':f.get('selected_provider',f.get('provider')),
        'title':f.get('page_title',f.get('title')),'reason':f.get('selected_reason',f.get('reason')),
        'observed_at':a.get('checked_at') or f.get('checked_at') or f.get('fetched_at'),
        'final_url':f.get('resolved_content_url') or f.get('final_url'),
        'retrieval_scope':f.get('retrieval_scope'),'body_sha256':sha(body),
        'original_body_file':str(file) if file and file.is_file() else None,
        'raw_file':str((root/f['raw_file']).resolve()) if f.get('raw_file') else None,
        'notes':f.get('selected_notes',f.get('notes',[]))}


def prepare(args):
    src=Path(args.input).resolve();out=Path(args.output).resolve();source=read(src)
    pointers=paths(source,args.rows_key,args.results_key,args.top_k)
    spec={'input_path':str(src),'source_sha256':sha(src.read_bytes()),'rows_key':args.rows_key,
          'results_key':args.results_key,'top_k':args.top_k,'field':args.field,
          'evidence_root':str(Path(args.evidence_root).resolve()) if args.evidence_root else str(src.parent)}
    manifest=out/'manifest.json'
    if manifest.exists():
        old=read(manifest)
        if old['input_spec']!=spec:raise ValueError('Input/selection changed; use a new output directory')
        print(json.dumps({'resumed':True,'cases':len(old['cases']),'occurrences':len(old['mapping'])}));return
    if out.exists() and any(out.iterdir()):raise ValueError('Output is not empty; choose a new run directory')
    pending=[];mapping=[];cases={}
    for sequence,pointer in enumerate(pointers,1):
        row=at(source,pointer)
        if args.field in row:raise ValueError('Output field exists; choose --field')
        url=row.get('url',row.get('requested_url'))
        if not isinstance(url,str) or not url.startswith(('https://','http://')):raise ValueError('Missing/invalid URL at '+str(pointer))
        body,f=fetch_material(row,Path(spec['evidence_root']))
        case_id='pq-'+sha(json.dumps([url,f],sort_keys=True,ensure_ascii=False))[:20]
        mapping.append({'pointer':pointer,'id':row.get('id',sequence),'case_id':case_id})
        if case_id in cases:continue
        cases[case_id]=url
        packet={'case_id':case_id,'url':url,'fetch':f,'body_file':'body.txt' if body else None,
            'route':'undecided','material_gaps':None,
            'checks':{k:{'state':'missing','note':'','evidence_ids':[]} for k in CHECKS},
            'evidence':[{'id':'fetch-record','source':str(src)+'#'+str(pointer),'kind':'tool_error' if f['status'] in ('unresolved','pending') else 'page',
                         'observed_at':f['observed_at'],'observation':'Imported fetch record; not yet reviewed for PQ.',
                         'artifact':f['original_body_file'] or f['raw_file']}]}
        pending.append((case_id,body,packet))
    out.mkdir(parents=True,exist_ok=True)
    skill=Path(__file__).resolve().parents[1]
    shutil.copy2(skill/'SKILL.md',out/'SKILL.md')
    shutil.copytree(skill/'references',out/'references')
    rules={str(p.relative_to(out)):sha(p.read_bytes()) for p in [out/'SKILL.md',*(out/'references').glob('*.md')]}
    for case_id,body,packet in pending:
        folder=out/'cases'/case_id;folder.mkdir(parents=True,exist_ok=True)
        if body:(folder/'body.txt').write_text(body,encoding='utf-8')
        write(folder/'packet.json',packet)
    write(out/'source.json',source)
    version=next(line.split(':',1)[1].strip().strip('"') for line in (skill/'SKILL.md').read_text().splitlines() if line.strip().startswith('version:'))
    write(manifest,{'input_spec':spec,'source_copy_sha256':sha((out/'source.json').read_bytes()),'created_at':now(),
                    'skill_version':version,'rating_mode':'page_quality','task_context':'standalone_pq','rule_hashes':rules,'cases':cases,'mapping':mapping})
    write(out/'pending.json',[{'case_id':i,'url':u,'packet':f'cases/{i}/packet.json'} for i,u in cases.items()])
    print(json.dumps({'prepared_cases':len(cases),'occurrences':len(mapping),'output':str(out)},ensure_ascii=False))


def validate(packet,rating):
    if rating.get('id')!=packet['case_id'] or rating.get('url')!=packet['url']:raise ValueError('Case/URL mismatch')
    if not str(rating.get('reason','')).strip():raise ValueError('Missing reason')
    status=rating.get('status');label=rating.get('rating');score=rating.get('score')
    if status not in ('rated','not_rated','insufficient_evidence'):raise ValueError('Invalid status')
    if status=='rated':
        if label not in RATINGS or isinstance(score,bool) or score!=RATINGS[label]:raise ValueError('Rating/score mismatch')
    elif label is not None or score is not None:raise ValueError('Abstentions require null rating/score')
    evid=packet.get('evidence',[]);ids=[x['id'] for x in evid]
    if len(set(ids))!=len(ids):raise ValueError('Duplicate evidence IDs')
    if any(not all(x.get(k) for k in ('source','kind','observation','observed_at')) for x in evid):raise ValueError('Incomplete evidence metadata')
    sources={x['source'] for x in evid};evidence_by_id={x['id']:x for x in evid}
    if not rating.get('evidence'):raise ValueError('Result requires evidence')
    for item in rating['evidence']:
        if item.get('source') not in sources or not item.get('observation'):raise ValueError('Unregistered/empty evidence citation')
    route=packet.get('route');gaps=packet.get('material_gaps')
    expected={'normal':'rated','safety_stop':'rated','precheck_exit':'not_rated','insufficient_evidence':'insufficient_evidence'}
    if expected.get(route)!=status:raise ValueError('Route/status mismatch')
    if not isinstance(gaps,list):raise ValueError('Material gaps have not been reviewed')
    if route=='insufficient_evidence' and not gaps:raise ValueError('Explain the material evidence gaps')
    required=CHECKS if route=='normal' else ('lowest_screen',) if route=='safety_stop' else ('page_identity',) if route=='precheck_exit' else ()
    if status=='rated' and gaps:raise ValueError('Cannot rate with unresolved material gaps')
    if route=='safety_stop' and label!='Lowest':raise ValueError('Safety stop must be evidence-backed Lowest')
    for name in required:
        check=packet['checks'][name];state=check.get('state');refs=check.get('evidence_ids',[])
        if state not in ('done','not_required') or not check.get('note'):raise ValueError('Incomplete check: '+name)
        if state=='not_required' and name!='page_experience':raise ValueError('Required check cannot be waived: '+name)
        if state=='done' and (not refs or any(i not in evidence_by_id for i in refs)):raise ValueError('Check lacks registered evidence: '+name)
    return True


def finalize(args):
    out=Path(args.output).resolve();m=read(out/'manifest.json');source=read(out/'source.json')
    if sha((out/'source.json').read_bytes())!=m['source_copy_sha256']:raise ValueError('Saved source changed')
    for name,value in m['rule_hashes'].items():
        if sha((out/name).read_bytes())!=value:raise ValueError('Rule snapshot changed')
    rows=read(args.ratings)
    if not isinstance(rows,list):raise ValueError('Ratings must be a list or JSONL')
    by_id={r['id']:r for r in rows}
    if len(by_id)!=len(rows) or set(by_id)!=set(m['cases']):raise ValueError('Need exactly one result for every case; no extras')
    packet_hashes={}
    for case_id in m['cases']:
        folder=out/'cases'/case_id;packet=read(folder/'packet.json')
        if packet.get('case_id')!=case_id or packet.get('url')!=m['cases'][case_id]:raise ValueError('Packet identity changed')
        if packet.get('body_file') and sha((folder/packet['body_file']).read_bytes())!=packet['fetch']['body_sha256']:raise ValueError('Body changed')
        validate(packet,by_id[case_id]);packet_hashes[case_id]=sha((folder/'packet.json').read_bytes())
    enriched=copy.deepcopy(source);results=[];field=m['input_spec']['field']
    for entry in m['mapping']:
        r=copy.deepcopy(by_id[entry['case_id']]);r.update(id=entry['id'],case_id=entry['case_id'])
        at(enriched,entry['pointer'])[field]=r;results.append(r)
    stripped=copy.deepcopy(enriched)
    for entry in m['mapping']:del at(stripped,entry['pointer'])[field]
    if stripped!=source:raise ValueError('Original fields changed')
    write(out/'enriched.json',enriched)
    with (out/'results.jsonl').open('w',encoding='utf-8') as f:
        for r in results:f.write(json.dumps(r,ensure_ascii=True)+'\n')
    summary={'skill_version':m['skill_version'],'rating_mode':'page_quality','task_context':m.get('task_context','standalone_pq'),'finalized_at':now(),
        'unique_evidence_cases':len(rows),'input_occurrences':len(results),
        'case_status_counts':{s:sum(r['status']==s for r in rows) for s in ('rated','not_rated','insufficient_evidence')},
        'case_rating_counts':{label:sum(r['status']=='rated' and r['rating']==label for r in rows) for label in RATINGS},
        'all_original_fields_preserved':True,'evidence_packet_hashes':packet_hashes,
        'validation_scope':'Structural consistency only; not proof of rating accuracy or evidence truth.'}
    write(out/'summary.json',summary)
    write(out/'pending.json',[])
    e=html.escape;table=[]
    for r in rows:
        links=''.join('<li><a href="'+e(x['source'],quote=True)+'">来源</a> '+e(x['observation'])+'</li>' for x in r['evidence'])
        table.append('<tr><td>'+e(r['url'])+'</td><td>'+e(str(r['rating'] or r['status']))+'</td><td>'+e(r['reason'])+'</td><td><ul>'+links+'</ul><a href="cases/'+r['id']+'/packet.json">证据包</a></td></tr>')
    (out/'report.html').write_text('<!doctype html><meta charset="utf-8"><title>PQ 评分结果</title><style>body{font:15px/1.7 system-ui;margin:32px;color:#172c40}td,th{border:1px solid #ddd;padding:12px;text-align:left;vertical-align:top}table{border-collapse:collapse;width:100%}td:first-child{max-width:320px;overflow-wrap:anywhere}pre{white-space:pre-wrap}</style><h1>PQ 评分结果</h1><p>按实际页面证据评分；缺证据不计入最低档。结构校验不代表准确率验证。</p><pre>'+e(json.dumps({k:v for k,v in summary.items() if k!='evidence_packet_hashes'},ensure_ascii=False,indent=2))+'</pre><p><a href="enriched.json">原字段＋评分</a> · <a href="results.jsonl">逐条结果</a></p><table><tr><th>URL</th><th>结果</th><th>理由</th><th>证据</th></tr>'+''.join(table)+'</table>',encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k!='evidence_packet_hashes'},ensure_ascii=False))


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('prepare');a.add_argument('--input',required=True);a.add_argument('--output',required=True)
    a.add_argument('--rows-key');a.add_argument('--results-key');a.add_argument('--top-k',type=int)
    a.add_argument('--field',default='page_quality');a.add_argument('--evidence-root')
    a=sub.add_parser('finalize');a.add_argument('--output',required=True);a.add_argument('--ratings',required=True)
    args=p.parse_args()
    if getattr(args,'top_k',None) is not None and args.top_k<1:p.error('top-k must be positive')
    (prepare if args.command=='prepare' else finalize)(args)


if __name__=='__main__':main()

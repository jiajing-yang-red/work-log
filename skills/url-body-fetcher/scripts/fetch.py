#!/usr/bin/env python3
"""One resumable fetch stage per invocation; source data are never modified."""
import argparse
import concurrent.futures as cf
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import os
import random
import re
import shlex
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
from filelock import FileLock
import content_core as c

FETCH_ID = 'gd_m6gjtfmeh43we6cqc'
KEYS = {'deepseek':'DEEPSEEK_API_KEY', 'brightdata':'BRIGHT_DATA_API_KEY',
        'platform':'BRIGHT_DATA_API_KEY', 'firecrawl':'FIRECRAWL_API_KEY', 'jina':'JINA_API_KEY'}


def platform_dataset(url):
    h, p = c.host(url), urlsplit(url).path
    if (h=='reddit.com' or h.endswith('.reddit.com')) and '/comments/' in p: return 'gd_lvz8ah06191smkebj4'
    if h in ('youtube.com','m.youtube.com','youtu.be'):
        return 'gd_lk56epmy2i5g7lzu0k' if c.kind(url)=='video' else 'gd_lk538t2k2p1k3oos71'
    if h in ('facebook.com','m.facebook.com') and '/posts/' in p: return 'gd_lyclm1571iy3mv57zw'
    if h=='instagram.com':
        if '/p/' in p: return 'gd_lk5ns7kz21pck8jpis'
        if len(p.strip('/').split('/'))==1: return 'gd_l1vikfch901nx3by4'
    if h=='linkedin.com' and '/company/' in p: return 'gd_l1vikfnt1wgvvqz95w'
    if h=='linkedin.com' and '/posts/' in p: return 'gd_lyy3tktm25m4avu764'
    if h in ('x.com','twitter.com') and '/status/' in p: return 'gd_lwxkxvnf1cynvib9co'
    if h=='quora.com' and len(p)>2: return 'gd_lvz1rbj81afv3m6n5y'
    return None


class Runner:
    def __init__(self, args, urls):
        self.args, self.out, self.urls = args, Path(args.output), urls
        self.env = dict(os.environ)
        if args.env_file:
            for line in Path(args.env_file).read_text().splitlines():
                line = line.strip().removeprefix('export ')
                key, sep, value = line.partition('=')
                if sep and key.strip() in set(KEYS.values()):
                    parsed = shlex.split(value, comments=True)
                    if parsed: self.env[key.strip()] = parsed[0]
        self.secrets = [self.env[k] for k in set(KEYS.values()) if self.env.get(k)]
        self.lock = threading.Lock(); self.rate_lock = threading.Lock()
        self.next_request = 0.0; self.next_host = {}; self.stop = threading.Event()
        for sub in ('records','content','raw','jobs','schemas'): (self.out/sub).mkdir(exist_ok=True)

    def clean(self, value):
        text = str(value)
        for key in self.secrets: text = text.replace(key, '[REDACTED]')
        return text

    def record(self, uid):
        path = self.out/'records'/f'{uid}.json'
        return json.loads(path.read_text()) if path.exists() else {**self.urls[uid], 'attempts': []}

    def attempted(self, uid, label):
        values = [a for a in self.record(uid)['attempts'] if a['provider']==label]
        if not values: return False
        return not (self.args.retry_failed and values[-1]['status']!='content_obtained')

    def pending_ids(self):
        return {uid for p in (self.out/'jobs').glob('*.json') for j in [json.loads(p.read_text())]
                if j['state'] not in ('processed','failed','rejected') for uid in j['url_ids']}

    def candidates(self, label):
        pending = self.pending_ids(); result=[]
        for uid, row in self.urls.items():
            best = c.selected(self.record(uid))
            if uid in pending or best and best['status']=='content_obtained': continue
            if not c.valid_url(row['url']) or not self.args.allow_private and not c.public_url(row['url']): continue
            if label=='platform':
                ds = platform_dataset(row['url'])
                if not ds: continue
                provider = 'brightdata_'+ds
            else: provider = label
            if not self.attempted(uid, provider): result.append(uid)
        return result

    def add(self, uid, attempt, text=''):
        with self.lock:
            rec = self.record(uid)
            aid = attempt.get('attempt_id') or f'{attempt["provider"]}-{len(rec["attempts"])+1}'
            if any(a['attempt_id']==aid for a in rec['attempts']): return
            text = self.clean(text)
            attempt.update(attempt_id=aid, checked_at=c.now(), content_chars=len(text))
            if text:
                relative = f'content/{uid}-{aid}.txt'
                (self.out/relative).write_text(text, encoding='utf-8')
                attempt['content_file'] = relative
            # Never serialize request headers or account credentials.
            attempt = json.loads(self.clean(json.dumps(attempt, ensure_ascii=False)))
            rec['attempts'].append(attempt)
            c.save(self.out/'records'/f'{uid}.json', rec)

    def gate(self, url):
        while True:
            with self.rate_lock:
                t = time.monotonic(); h=c.host(url)
                delay = max(self.next_request, self.next_host.get(h,0))-t
                if delay<=0:
                    self.next_request=t+1/self.args.qps
                    self.next_host[h]=t+self.args.domain_interval
                    return
            time.sleep(min(delay,0.5))

    def request(self, method, url, *, provider, retry=True, **kwargs):
        history=[]
        for n in range(self.args.retries+1 if retry else 1):
            self.gate(url)
            session=requests.Session();session.trust_env=False
            proxies = {} if self.args.no_proxy else {'http':self.args.proxy,'https':self.args.proxy} if self.args.proxy else requests.utils.get_environ_proxies(url)
            try:
                response=session.request(method,url,timeout=(10,self.args.timeout),proxies=proxies,**kwargs)
                history.append({'http_status':response.status_code,'at':c.now()})
            except requests.RequestException as exc:
                history.append({'error':self.clean(exc)[:500],'at':c.now()})
                if n==self.args.retries or not retry: raise
                time.sleep(min(2**n+random.random(),30));continue
            finally:
                session.close()
            if provider!='direct' and (response.status_code in (401,402) or 'Customer is not active' in response.text[:200]):
                self.stop.set()
            if response.status_code==429:
                try: retry_after=float(response.headers.get('Retry-After',0))
                except ValueError:
                    try:
                        retry_date=parsedate_to_datetime(response.headers['Retry-After'])
                        if retry_date.tzinfo is None:retry_date=retry_date.replace(tzinfo=dt.timezone.utc)
                        retry_after=max(0,retry_date.timestamp()-time.time())
                    except (ValueError,TypeError,KeyError,OverflowError):retry_after=60
                quota_error=any(s in response.text.lower() for s in ('insufficient credit','credit limit','credits exceeded','quota exceeded','free limit'))
                if retry_after>60 or quota_error or n==self.args.retries:
                    self.stop.set()
                    c.save(self.out/f'{provider}-pause.json',{'observed_at':c.now(),'http_status':429,
                        'retry_not_before_epoch':time.time()+max(retry_after,60),'reason':'quota_or_retry_limit' if quota_error else 'provider_retry_after'})
                    return response,history
            else: retry_after=0
            if response.status_code not in (408,429,500,502,503,504) or n==self.args.retries or not retry:
                return response,history
            time.sleep(max(retry_after,min(2**n+random.random(),30)))
        return response,history

    def raw(self, relative, value):
        path=self.out/relative
        if isinstance(value,bytes):
            # API credentials cannot originate from a binary target PDF.
            for secret in self.secrets: value=value.replace(secret.encode(),b'[REDACTED]')
            path.write_bytes(value)
        else: path.write_text(self.clean(value),encoding='utf-8')
        return relative

    def sync(self, uid):
        if self.stop.is_set(): return
        provider=self.args.provider;url=self.urls[uid]['url']
        ordinal=len(self.record(uid)['attempts'])+1; prefix=f'{uid}-{provider}-{ordinal}'
        text='';a={'provider':provider,'retrieval_scope':'requested_url_page','content_format':'text'}
        try:
            flags=[];target_status=None;final=None;title='';error=None
            if provider=='direct':
                target=url
                # Inspect every redirect before sending the next request; no local URLs by default.
                for redirects in range(11):
                    if not self.args.allow_private and not c.public_url(target): raise ValueError('Nonpublic redirect rejected')
                    if not self.args.allow_private:
                        import socket,ipaddress
                        addresses=socket.getaddrinfo(urlsplit(target).hostname,None)
                        if any(not ipaddress.ip_address(v[4][0]).is_global for v in addresses):
                            raise ValueError('Target hostname resolves to a nonpublic address')
                    response,history=self.request('GET',target,provider=provider,allow_redirects=False,stream=True)
                    if response.is_redirect:
                        from urllib.parse import urljoin
                        target=urljoin(target,response.headers['Location']);response.close();continue
                    break
                else: raise ValueError('Redirect limit exceeded')
                body=bytearray();truncated=False
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body)>self.args.max_bytes: truncated=True;del body[self.args.max_bytes:];break
                response.close();body=bytes(body)
                a['raw_file']=self.raw('raw/'+prefix+'.body',body)
                a['http_status']=response.status_code;a['http_tries']=history
                target_status=response.status_code;final=target
                if target_status<400:
                    title,text,method,flags=c.extract_bytes(body,response.headers.get('Content-Type',''),url)
                    a['extraction']=method
                if truncated: flags.append('download_truncated_at_byte_limit')
            else:
                key=self.env.get(KEYS[provider]);headers={'Authorization':'Bearer '+key} if key else {}
                if provider=='deepseek':
                    response,history=self.request('POST','https://api.deepseek.com/v1/tools/fetch',provider=provider,headers=headers,json={'url':url})
                elif provider=='firecrawl':
                    response,history=self.request('POST','https://api.firecrawl.dev/v2/scrape',provider=provider,headers=headers,
                        json={'url':url,'formats':['markdown'],'onlyMainContent':True,'timeout':min(self.args.timeout*1000,60000)})
                elif provider=='jina':
                    response,history=self.request('GET','https://r.jina.ai/'+url,provider=provider,headers={**headers,'X-No-Cache':'true'})
                else: raise ValueError('Unsupported synchronous provider')
                a['raw_file']=self.raw('raw/'+prefix+'.txt',response.text)
                a.update(http_status=response.status_code,http_tries=history,content_format='markdown')
                if response.status_code!=200: error=f'{provider} API HTTP {response.status_code}: '+response.text[:400]
                elif provider=='jina':
                    text=response.text
                    m=re.search(r'^Title:\s*(.*)',text,re.M);title=m[1] if m else ''
                    # URL Source is the requested URL, not evidence of the final redirect target.
                else:
                    payload=response.json()
                    if provider=='deepseek':title=payload.get('title','');text=payload.get('content','')
                    else:
                        data=payload.get('data') or {};meta=data.get('metadata') or {}
                        title=meta.get('title','');text=data.get('markdown','');final=meta.get('url');target_status=meta.get('statusCode')
                        error=payload.get('error') or meta.get('error')
                        if payload.get('warning'):flags.append(str(payload['warning']))
                if not isinstance(text,str):raise ValueError('Provider content is not a string')
            a.update(title=title or '',final_url=final,target_http_status=target_status)
            a['status'],a['reason'],a['notes']=c.assess(url,text,title,target_status,error,final,flags)
            if error:a['error']=error
            if 'Markdown Content:' in text: text=text.split('Markdown Content:',1)[1].strip()
        except Exception as exc:
            a.update(status='unresolved',reason=c.error_reason(str(exc)),error=self.clean(exc)[:700],notes=[])
        self.add(uid,a,text)

    def bd_headers(self):
        return {'Authorization':'Bearer '+self.env['BRIGHT_DATA_API_KEY']}

    def bd_call(self, method, suffix, **kwargs):
        return self.request(method,'https://api.brightdata.com/datasets/v3/'+suffix,
            provider='brightdata',headers=self.bd_headers(),retry=method=='GET',**kwargs)[0]

    def check_schema(self,dataset):
        path=self.out/'schemas'/f'{dataset}.json'
        if not path.exists():
            response,_=self.request('GET','https://docs.brightdata.com/scrapers/'+dataset+'.json',provider='schema')
            response.raise_for_status();c.save(path,response.json())
        schema=json.loads(path.read_text())
        required=[f['name'] for f in schema['collection_methods']['collect_by_url']['input_schema'] if f.get('required')]
        if required!=['url']:raise ValueError('Platform schema needs additional inputs: '+str(required))

    def process_snapshot(self,path,job,data):
        label=job['provider'];raw_file='raw/'+path.stem+'.json'
        self.raw(raw_file,json.dumps(data,ensure_ascii=False))
        if not isinstance(data,list):raise ValueError('Snapshot must be a list')
        by_url={}
        for item in data:
            inp=item.get('input') or {};url=inp.get('url') if isinstance(inp,dict) else None
            url=url or item.get('url')
            if url:by_url.setdefault(url,[]).append(item)
        for uid in job['url_ids']:
            url=self.urls[uid]['url'];items=by_url.get(url,[]);text='';title='';final=None;flags=[];primary=False
            structured=job['dataset_id']!=FETCH_ID
            error=None
            if not items:error='response_not_matched_to_exact_input_url'
            else:
                good=[x for x in items if not (x.get('error') or x.get('error_code'))]
                if not good:error=str(items[0].get('error') or items[0].get('error_code'))
                elif structured:
                    parts=[]
                    for item in good:
                        value,has_primary=c.structured_text(item);parts.append(value);primary|=has_primary
                    text='\n\n'.join(dict.fromkeys(parts));item=good[0]
                    title=item.get('title') or item.get('name') or '';final=item.get('url')
                else:
                    item=max(good,key=lambda x:len(x.get('markdown') or x.get('html2text') or ''))
                    text=item.get('markdown') or item.get('html2text') or '';title=item.get('page_title') or '';final=item.get('final_url')
                    if not text and item.get('page_html'):
                        extracted_title,text,_,flags=c.extract_bytes(item['page_html'].encode(),'text/html',url)
                        title=title or extracted_title
            status,reason,notes=c.assess(url,text,title,error=error,final_url=final,flags=flags,structured=structured,primary_text=primary)
            self.add(uid,{'provider':label,'attempt_id':path.stem,'status':status,'reason':reason,'notes':notes,
                'title':title,'final_url':final,'primary_text':primary,'http_status':200,'error':error,
                'raw_file':raw_file,'snapshot_id':job['snapshot_id'],'matched_records':len(items),
                'retrieval_scope':'platform_content_api' if structured else 'requested_url_page',
                'content_format':'text' if structured else 'markdown'},text)
        job['state']='processed';job['processed_at']=c.now();c.save(path,job)

    def poll_job(self,path):
        job=json.loads(path.read_text())
        if not job.get('snapshot_id') or job['state'] in ('processed','rejected','failed'):return
        try:
            response=self.bd_call('GET','progress/'+job['snapshot_id'])
            response.raise_for_status();progress=response.json();job['progress']=progress
            job['updated_at']=c.now();c.save(path,job)
            if progress.get('status')=='ready':
                response=self.bd_call('GET','snapshot/'+job['snapshot_id'],params={'format':'json'})
                response.raise_for_status();self.process_snapshot(path,job,response.json())
            elif progress.get('status') in ('failed','canceled'):
                for uid in job['url_ids']:
                    self.add(uid,{'provider':job['provider'],'attempt_id':path.stem,'status':'unresolved',
                        'reason':'provider_job_failed','notes':[self.clean(progress)],'snapshot_id':job['snapshot_id']})
                job['state']='failed';c.save(path,job)
        except Exception as exc:
            job['last_error']=self.clean(exc)[:700];c.save(path,job)

    def submit(self,ids,dataset):
        label='brightdata' if dataset==FETCH_ID else 'brightdata_'+dataset
        versions=[len(self.record(uid)['attempts']) for uid in ids]
        job_id=label+'-'+c.digest(json.dumps([ids,versions]))[:20];path=self.out/'jobs'/f'{job_id}.json'
        if path.exists():return
        job={'state':'submission_started','provider':label,'dataset_id':dataset,'url_ids':ids,'started_at':c.now()}
        c.save(path,job)  # checkpoint before POST: an unknown outcome is never automatically submitted again
        try:
            response=self.bd_call('POST','trigger',params={'dataset_id':dataset,'include_errors':'true','format':'json'},
                json=[{'url':self.urls[uid]['url']} for uid in ids])
            try:payload=response.json()
            except ValueError:payload={'error':response.text[:700]}
            job['submission_http_status']=response.status_code
            job['submission_response']=json.loads(self.clean(json.dumps(payload)))
            if isinstance(payload,dict) and payload.get('snapshot_id'):
                job.update(state='running',snapshot_id=payload['snapshot_id'])
            elif response.status_code in (400,401,402,403,404,422,429):
                job['state']='rejected'
                if response.status_code in (401,402,403,429):self.stop.set()
                for uid in ids:self.add(uid,{'provider':label,'attempt_id':job_id,'status':'unresolved','reason':'provider_job_rejected',
                    'http_status':response.status_code,'notes':[self.clean(payload)],'error':self.clean(payload)})
            else:job['state']='submission_unknown'
        except Exception as exc:
            job.update(state='submission_unknown',last_error=self.clean(exc)[:700])
        c.save(path,job)

    def brightdata(self):
        # Resume old snapshots before considering new URL batches.
        pending=[p for p in (self.out/'jobs').glob('*.json') if json.loads(p.read_text())['state']=='running']
        with cf.ThreadPoolExecutor(max_workers=min(self.args.workers,4)) as pool:list(pool.map(self.poll_job,pending))
        groups={}
        for uid in self.candidates(self.args.provider):
            ds=platform_dataset(self.urls[uid]['url']) if self.args.provider=='platform' else FETCH_ID
            groups.setdefault(ds,[]).append(uid)
        slots=max(0,self.args.max_new_jobs-sum(json.loads(p.read_text())['state'] in ('running','submission_unknown','submission_started') for p in (self.out/'jobs').glob('*.json')))
        for ds,ids in groups.items():
            if self.args.provider=='platform':self.check_schema(ds)
            for start in range(0,len(ids),self.args.batch_size):
                if not slots or self.stop.is_set():break
                self.submit(ids[start:start+self.args.batch_size],ds);slots-=1
        deadline=time.monotonic()+self.args.poll_budget
        while time.monotonic()<deadline and not self.stop.is_set():
            pending=[p for p in (self.out/'jobs').glob('*.json') if json.loads(p.read_text())['state']=='running']
            if not pending:break
            with cf.ThreadPoolExecutor(max_workers=min(self.args.workers,4)) as pool:list(pool.map(self.poll_job,pending))
            time.sleep(min(self.args.poll_interval,max(0,deadline-time.monotonic())))


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    source=p.add_mutually_exclusive_group(required=True)
    source.add_argument('--input');source.add_argument('--url',action='append')
    p.add_argument('--output',required=True);p.add_argument('--provider',choices=['direct','deepseek','brightdata','platform','firecrawl','jina','import','export'],default='direct')
    p.add_argument('--rows-key');p.add_argument('--results-key');p.add_argument('--url-key',default='url')
    p.add_argument('--top-k',type=int);p.add_argument('--field',default='page_content_fetch')
    p.add_argument('--env-file');p.add_argument('--proxy');p.add_argument('--no-proxy',action='store_true')
    p.add_argument('--workers',type=int,default=8);p.add_argument('--qps',type=float,default=4)
    p.add_argument('--domain-interval',type=float,default=0.5);p.add_argument('--timeout',type=int,default=60)
    p.add_argument('--retries',type=int,default=2);p.add_argument('--batch-size',type=int,default=50)
    p.add_argument('--max-new-jobs',type=int,default=4);p.add_argument('--poll-budget',type=int,default=45)
    p.add_argument('--poll-interval',type=int,default=10);p.add_argument('--max-bytes',type=int,default=20000000)
    p.add_argument('--retry-failed',action='store_true');p.add_argument('--allow-private',action='store_true')
    p.add_argument('--dry-run',action='store_true');p.add_argument('--review-file');p.add_argument('--import-file')
    return p


def main():
    args=parser().parse_args()
    if args.workers<1 or args.qps<=0 or args.domain_interval<0 or args.retries<0 or args.batch_size<1 or args.max_new_jobs<0 or args.timeout<1 or args.max_bytes<1 or args.top_k is not None and args.top_k<1:
        raise SystemExit('Invalid numeric option')
    source,urls,spec=c.load_input(args)
    if args.dry_run:
        print(json.dumps({'unique_urls':len(urls),'occurrences':sum(len(x['occurrences']) for x in urls.values()),
            'invalid_or_nonpublic':sum(not c.valid_url(x['url']) or not args.allow_private and not c.public_url(x['url']) for x in urls.values()),
            'provider':args.provider,'top_k_uses_source_array_order':True},ensure_ascii=False));return
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    with FileLock(str(out/'.fetch.lock'),timeout=0):
        manifest=out/'manifest.json'
        if manifest.exists() and json.loads(manifest.read_text())['input_spec']!=spec:
            raise SystemExit('Input or selection changed. Use a new output directory to avoid mixing runs.')
        if not manifest.exists():c.save(manifest,{'input_spec':spec,'created_at':c.now(),'unique_urls':len(urls)})
        runner=Runner(args,urls)
        for uid,row in urls.items():
            if (not c.valid_url(row['url']) or not args.allow_private and not c.public_url(row['url'])) and not runner.record(uid)['attempts']:
                runner.add(uid,{'provider':'input_validation','status':'unresolved','reason':'invalid_or_nonpublic_url','notes':[]})
        if args.review_file:
            if args.provider!='export':raise SystemExit('--review-file requires --provider export')
            reviews=json.loads(Path(args.review_file).read_text())
            for review in reviews:
                if review['status'] not in c.STATUSES or not review.get('evidence_note'):raise ValueError('Review needs a valid status and evidence_note')
                uid=c.digest(review['url']);rec=runner.record(uid)
                a=next(a for a in rec['attempts'] if a['attempt_id']==review['attempt_id'])
                if review['status']!='unresolved' and not a.get('content_file'):raise ValueError('Cannot approve content that was not saved')
                a.setdefault('before_manual_review',{k:a[k] for k in ('status','reason','notes')})
                a['manual_review']={**review,'reviewed_at':c.now()};a.update(status=review['status'],reason=review['reason'],notes=[review['evidence_note']])
                c.save(out/'records'/f'{uid}.json',rec)
        elif args.provider=='import':
            if not args.import_file:raise SystemExit('--provider import requires --import-file')
            import_path=Path(args.import_file)
            for item in json.loads(import_path.read_text()):
                uid=c.digest(item['url'])
                if uid not in urls:raise ValueError('Imported URL is not in the selected input')
                text_path=import_path.parent/item['text_file']
                text=text_path.read_text(encoding='utf-8')
                scope=item.get('retrieval_scope','requested_url_page')
                flags=['document_copy_identity_requires_review'] if scope=='document_copy_not_original_url' else []
                status,reason,notes=c.assess(item['url'],text,item.get('title',''),
                    target_status=item.get('target_http_status'),final_url=item.get('source_url'),flags=flags)
                a={'provider':item['provider'],'status':status,'reason':reason,'notes':notes,
                    'title':item.get('title',''),'final_url':item.get('source_url'),
                    'retrieval_scope':scope,'content_format':item.get('content_format','text')}
                if item.get('raw_file'):
                    raw_path=import_path.parent/item['raw_file']
                    a['raw_file']=runner.raw(f'raw/{uid}-import-{len(runner.record(uid)["attempts"])+1}.body',raw_path.read_bytes())
                runner.add(uid,a,text)
        elif args.provider!='export':
            key_name=KEYS.get(args.provider)
            if key_name and args.provider!='jina' and not runner.env.get(key_name):
                raise SystemExit('Missing '+key_name+'; configure it locally or choose an available provider.')
            pause_file=out/f'{"brightdata" if args.provider=="platform" else args.provider}-pause.json'
            if pause_file.exists() and json.loads(pause_file.read_text()).get('retry_not_before_epoch',0)>time.time():
                raise SystemExit('Provider Retry-After is still active; resume after the saved time.')
            if args.provider in ('brightdata','platform'):runner.brightdata()
            else:
                if args.provider=='jina' and not runner.env.get('JINA_API_KEY'):args.qps=min(args.qps,0.3)
                with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:list(pool.map(runner.sync,runner.candidates(args.provider)))
        summary=c.export(out,source,urls,args.field,{
            'eligible_for_requested_provider':len(runner.candidates(args.provider)) if args.provider not in ('export','import') else None,
            'provider_stopped':runner.stop.is_set()})
        print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':
    main()

"""Offline regressions: run with python -m unittest discover -s scripts -p test_fetch.py."""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests
import content_core as c
import fetch

BODY = ('This article explains a practical method for extracting useful page text. '
        'The document contains the original observations, implementation details and results. ')*10


class Screening(unittest.TestCase):
    def test_structured_post_discussing_404_is_not_error_page(self):
        text,primary=c.structured_text({'post_text':'I just started learning Django and get Page not found (404). '+BODY})
        self.assertTrue(primary)
        self.assertEqual(c.assess('https://www.facebook.com/groups/g/posts/1/',text,'Page not found',structured=True,primary_text=primary)[0],'content_obtained')

    def test_actual_errors_and_pdf_stream_are_not_body(self):
        for text,kwargs in [('%PDF-1.7 binary stream',{}),('Something went wrong. Wait a moment and try again.',{}),
                             (BODY,{'target_status':404}),('Warning: Target URL returned error 403: Forbidden\nMarkdown Content:\n'+BODY,{})]:
            with self.subTest(text=text[:30]):self.assertEqual(c.assess('https://example.org/a',text,**kwargs)[0],'unresolved')

    def test_partial_and_redirects(self):
        for url,kwargs in [('https://youtube.com/watch?v=abc',{}),('https://example.org/a',{'final_url':'https://other.org/a'}),
                           ('https://example.org/a',{'final_url':'https://example.org/'}),('https://example.org/a',{'flags':['download_truncated_at_byte_limit']})]:
            with self.subTest(url=url,kwargs=kwargs):self.assertEqual(c.assess(url,BODY,**kwargs)[0],'partial')
        self.assertEqual(c.assess('https://example.org/a',BODY,final_url='https://example.org/login')[0],'unresolved')

    def test_title_only_and_private_input(self):
        text,primary=c.structured_text({'title':BODY,'likes':30})
        self.assertFalse(primary)
        self.assertEqual(c.assess('https://example.org/a',text,structured=True,primary_text=primary)[0],'partial')
        for url in ['http://127.0.0.1/a','http://[::1]/','http://localhost/','http://10.0.0.1/']:
            self.assertFalse(c.public_url(url))
        for url in ['file:///tmp/a','https://user:pass@example.org/a','https://example.org/a b']:
            self.assertFalse(c.valid_url(url))
        self.assertIsNone(fetch.platform_dataset('https://notreddit.com/comments/a'))


class AsyncJobs(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.out=Path(self.tmp.name)
        self.args=fetch.parser().parse_args(['--url','https://example.org/a','--url','https://example.org/b','--output',str(self.out),'--provider','brightdata','--poll-budget','0'])
        self.source,self.urls,_=c.load_input(self.args)
        self.runner=fetch.Runner(self.args,self.urls)
        self.ids=list(self.urls)

    def tearDown(self):self.tmp.cleanup()

    def test_unknown_submission_is_checkpointed_and_not_repeated(self):
        with patch.object(self.runner,'bd_call',side_effect=requests.Timeout('unknown after POST')) as call:
            self.runner.submit(self.ids,fetch.FETCH_ID)
            self.runner.submit(self.ids,fetch.FETCH_ID)
            self.assertEqual(call.call_count,1)
        job=json.loads(next((self.out/'jobs').glob('*.json')).read_text())
        self.assertEqual(job['state'],'submission_unknown')
        self.assertEqual(self.runner.candidates('brightdata'),[])
        summary=c.export(self.out,self.source,self.urls,self.args.field)
        self.assertEqual(summary['pending_jobs'],1)
        self.assertEqual(summary['status_counts']['pending'],2)

    def test_out_of_order_snapshot_exact_match_and_item_error(self):
        job={'state':'running','provider':'brightdata','dataset_id':fetch.FETCH_ID,'url_ids':self.ids,'snapshot_id':'s1'}
        path=self.out/'jobs'/'test-job.json';c.save(path,job)
        data=[{'input':{'url':'https://example.org/b'},'error':'parse error'},
              {'input':{'url':'https://example.org/a'},'markdown':BODY,'final_url':'https://example.org/a'}]
        self.runner.process_snapshot(path,job,data)
        self.assertEqual(c.selected(self.runner.record(self.ids[0]))['status'],'content_obtained')
        self.assertEqual(c.selected(self.runner.record(self.ids[1]))['reason'],'provider_parse_error')
        self.runner.process_snapshot(path,job,data)
        self.assertTrue(all(len(self.runner.record(uid)['attempts'])==1 for uid in self.ids))
        self.assertEqual(json.loads(path.read_text())['state'],'processed')

    def test_secret_redaction_in_raw_text_and_records(self):
        self.runner.secrets=['fixture-secret-value']
        self.runner.raw('raw/test.txt','error fixture-secret-value')
        self.runner.add(self.ids[0],{'provider':'fixture','status':'unresolved','reason':'error','notes':['fixture-secret-value']},'fixture-secret-value')
        for file in self.out.rglob('*'):
            if file.is_file():self.assertNotIn('fixture-secret-value',file.read_text())

    def test_retry_after_date_pauses_without_sleeping(self):
        response=requests.Response();response.status_code=429;response._content=b'{"error":"rate limited"}'
        response.headers['Retry-After']='Sat, 01 Jan 2050 00:00:00 GMT'
        with patch('fetch.requests.Session') as session:
            session.return_value.request.return_value=response
            self.runner.request('GET','https://example.org/api',provider='fixture')
            self.assertEqual(session.return_value.request.call_count,1)
        self.assertTrue(self.runner.stop.is_set())
        self.assertGreater(json.loads((self.out/'fixture-pause.json').read_text())['retry_not_before_epoch'],2500000000)


class EndToEnd(unittest.TestCase):
    def test_local_fetch_dedup_resume_preservation_and_review(self):
        hits=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(404 if self.path=='/gone' else 200)
                self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers()
                body='<h1>Page not found</h1>' if self.path=='/gone' else '<article><h1>Test article</h1><p>'+BODY+'</p></article>'
                self.wfile.write(('<html><head><title>Test article</title></head><body>'+body+'</body></html>').encode())
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);url='http://127.0.0.1:'+str(server.server_port)
                source={'version':1,'queries':[{'query':'q1','results':[{'url':url+'/a','x':{'keep':True}},{'url':url+'/a','rank':2},{'url':url+'/not-selected'}]},
                                              {'query':'q2','results':[{'url':url+'/gone','rank':1},{'title':'missing url'}]}]}
                input_file=root/'input.json';c.save(input_file,source);original=input_file.read_bytes()
                out=root/'out'
                base=[sys.executable,str(Path(fetch.__file__)),'--input',str(input_file),'--rows-key','queries','--results-key','results','--top-k','2','--output',str(out),'--allow-private','--no-proxy','--qps','100','--domain-interval','0']
                dry=subprocess.run(base+['--dry-run'],capture_output=True,text=True,check=True)
                self.assertEqual(json.loads(dry.stdout)['occurrences'],4);self.assertFalse(out.exists())
                subprocess.run(base,capture_output=True,text=True,check=True)
                self.assertCountEqual(hits,['/a','/gone'])
                summary=json.loads((out/'summary.json').read_text());self.assertEqual(summary['status_counts']['unresolved'],2)
                enriched=json.loads((out/'enriched.json').read_text());stripped=copy.deepcopy(enriched)
                for q in stripped['queries']:
                    for row in q['results'][:2]:row.pop('page_content_fetch')
                self.assertEqual(stripped,source);self.assertEqual(input_file.read_bytes(),original)
                self.assertNotIn('page_content_fetch',enriched['queries'][0]['results'][2])
                subprocess.run(base,capture_output=True,text=True,check=True);self.assertCountEqual(hits,['/a','/gone'])
                review=root/'review.json';c.save(review,[{'url':url+'/a','attempt_id':'direct-1','status':'partial','reason':'test_review','evidence_note':'Fixture review preserves previous screening.'}])
                subprocess.run(base+['--provider','export','--review-file',str(review)],capture_output=True,text=True,check=True)
                summary=json.loads((out/'summary.json').read_text());self.assertEqual(summary['manually_reviewed_selected_urls'],1)
                self.assertFalse(summary['automatic_screen_only']);self.assertCountEqual(hits,['/a','/gone'])
                input_file.write_text(json.dumps({**source,'version':2}))
                changed=subprocess.run(base,capture_output=True,text=True);self.assertNotEqual(changed.returncode,0)
                self.assertIn('Input or selection changed',changed.stderr)
        finally:server.shutdown();server.server_close();thread.join()


if __name__=='__main__':unittest.main()

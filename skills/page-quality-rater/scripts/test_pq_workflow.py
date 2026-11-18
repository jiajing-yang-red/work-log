import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import pq_workflow as pq


def fixture():
    p={'case_id':'pq-test','url':'https://example.org/a','route':'normal','material_gaps':[],
       'evidence':[{'id':'e1','source':'https://example.org/a','kind':'page','observed_at':'2026-01-01T00:00:00Z','observation':'Fixture evidence.'}],
       'checks':{k:{'state':'done','note':'Fixture review.','evidence_ids':['e1']} for k in pq.CHECKS}}
    r={'id':p['case_id'],'url':p['url'],'status':'rated','rating':'Medium','score':2,'reason':'Fixture: meets its purpose.',
       'evidence':[{'source':'https://example.org/a','observation':'Fixture.'}]}
    return p,r


class Validation(unittest.TestCase):
    def test_rated_and_abstention_contract(self):
        p,r=fixture();self.assertTrue(pq.validate(p,r))
        r['score']=0
        with self.assertRaises(ValueError):pq.validate(p,r)
        p['route']='insufficient_evidence';p['material_gaps']=['Missing main content.']
        r.update(status='insufficient_evidence',rating=None,score=None)
        self.assertTrue(pq.validate(p,r))
        r['score']=0
        with self.assertRaises(ValueError):pq.validate(p,r)

    def test_missing_reputation_and_material_gap_block_ratings(self):
        for mode in ('reputation','gap','citation'):
            p,r=fixture()
            if mode=='reputation':p['checks']['reputation']['state']='missing'
            elif mode=='gap':p['material_gaps']=['Identity not verified']
            else:r['evidence'][0]['source']='https://unread.example/a'
            with self.subTest(mode=mode),self.assertRaises(ValueError):pq.validate(p,r)

    def test_safe_lowest_can_stop_before_background_research(self):
        p,r=fixture();p['route']='safety_stop';r.update(rating='Lowest',score=0)
        p['checks']['reputation']['state']='missing'
        self.assertTrue(pq.validate(p,r))
        r.update(rating='Low',score=1)
        with self.assertRaises(ValueError):pq.validate(p,r)

    def test_body_conflict_is_not_silently_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'body.txt').write_text(' original body \n')
            body,_=pq.fetch_material({'content':'original body','content_file':'body.txt'},root)
            self.assertEqual(body,' original body \n')
            with self.assertRaises(ValueError):pq.fetch_material({'content':'different body','content_file':'body.txt'},root)

    def test_cli_preserves_nested_data_duplicates_order_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'run';src=root/'in.json';ratings=root/'ratings.json'
            f={'status':'content_obtained','content':'original body','checked_at':'2026-01-01T00:00:00Z'}
            source={'extra':{'keep':True},'queries':[{'query':'query not passed to rater','results':[
                {'id':'a','url':'https://example.org/a','page_content_fetch':f,'old_rating':4},
                {'id':'b','url':'https://example.org/a','page_content_fetch':f},
                {'id':'c','url':'https://example.org/c'}]}]}
            pq.write(src,source)
            cmd=[sys.executable,pq.__file__,'prepare','--input',str(src),'--output',str(out),'--rows-key','queries','--results-key','results','--top-k','2']
            subprocess.run(cmd,check=True,capture_output=True)
            m=pq.read(out/'manifest.json');self.assertEqual(len(m['cases']),1)
            self.assertEqual(m['task_context'],'standalone_pq')
            case_id=next(iter(m['cases']));path=out/'cases'/case_id/'packet.json';packet=pq.read(path)
            self.assertNotIn('old_rating',json.dumps(packet));self.assertNotIn('query not passed',json.dumps(packet))
            packet['route']='insufficient_evidence';packet['material_gaps']=['Fixture body requires more evidence.'];pq.write(path,packet)
            subprocess.run(cmd,check=True,capture_output=True);self.assertEqual(pq.read(path),packet)
            pq.write(ratings,[{'id':case_id,'url':packet['url'],'status':'insufficient_evidence','rating':None,'score':None,'reason':'Fixture missing evidence.',
                               'evidence':[{'source':packet['evidence'][0]['source'],'observation':'Body only.'}]}])
            finalize=[sys.executable,pq.__file__,'finalize','--output',str(out),'--ratings',str(ratings)]
            subprocess.run(finalize,check=True,capture_output=True)
            self.assertEqual(pq.read(out/'summary.json')['task_context'],'standalone_pq')
            enriched=pq.read(out/'enriched.json');rs=enriched['queries'][0]['results']
            self.assertEqual([r['id'] for r in rs],['a','b','c']);self.assertEqual(rs[0]['page_quality']['id'],'a');self.assertEqual(rs[1]['page_quality']['id'],'b')
            self.assertNotIn('page_quality',rs[2]);self.assertEqual(rs[0]['old_rating'],4)
            for row in rs[:2]:del row['page_quality']
            self.assertEqual(enriched,source)
            packet['url']='https://unrelated.example/a';pq.write(path,packet)
            result=subprocess.run(finalize,capture_output=True,text=True);self.assertNotEqual(result.returncode,0);self.assertIn('Packet identity changed',result.stderr)


if __name__=='__main__':unittest.main()

import copy
import json
import math
import os
import tempfile
import unittest
from unittest.mock import patch
from scripts.evaluator.common import ROOT, read_json, git
from scripts.evaluator.collect import blob, collect, digest
from scripts.evaluator.validate import validate_report
from scripts.evaluator.model import configuration, assess
from scripts.evaluator.run import run
from scripts.evaluator.render import render

class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.rubric=read_json(ROOT/'evaluation/rubric.json')
        self.schema=read_json(ROOT/'evaluation/schemas/report.json')
        self.report={'summary':'Контрольный ответ','criteria':[{'id':c['id'],'score':None,'evidence_ids':[],
                     'reason':'Нет данных','improvement':'Показать доказательство'} for c in self.rubric['criteria']],
                     'blockers':[],'improvements':[],'questions':[],'limitations':[]}
        self.evidence=[{'id':'code','status':'available'}]
    def validate(self):
        return validate_report(self.report,self.schema,self.rubric,self.evidence)
    def test_rubric_total(self):
        self.assertEqual(sum(c['max'] for c in self.rubric['criteria']),100)
        self.assertEqual(len(self.rubric['criteria']),22)
    def test_unknown_is_not_zero(self):
        self.assertEqual(self.validate(),{'earned':0,'assessed_max':0,'unassessed_max':100})
        self.report['criteria'][0].update(score=0,evidence_ids=['code'])
        self.assertEqual(self.validate()['assessed_max'],5)
    def test_duplicate_rejected(self):
        self.report['criteria'][-1]=self.report['criteria'][0]
        with self.assertRaises(ValueError): self.validate()
    def test_score_bound(self):
        self.report['criteria'][0].update(score=6,evidence_ids=['code'])
        with self.assertRaises(ValueError): self.validate()
    def test_no_evidence_rejected(self):
        self.report['criteria'][0]['score']=1
        with self.assertRaises(ValueError): self.validate()
    def test_stale_evidence_rejected(self):
        self.report['criteria'][0].update(score=1,evidence_ids=['code'])
        self.evidence[0]['status']='stale'
        with self.assertRaises(ValueError): self.validate()
    def test_hallucinated_reference_rejected(self):
        self.report['criteria'][0].update(score=1,evidence_ids=['invented'])
        with self.assertRaises(ValueError): self.validate()
    def test_path_traversal(self):
        with self.assertRaises(ValueError): blob('a'*40,'../secret')
    def test_hash(self):
        self.assertEqual(digest(b'abc'),'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
    def test_budget_blocks_absent(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaises(ValueError): configuration(100)
    def test_budget_blocks_nan(self):
        with patch.dict(os.environ,{'JUDGE_MAX_USD':'nan'},clear=True),self.assertRaises(ValueError): configuration(100)
    def test_budget_blocks_excess(self):
        env={'JUDGE_MAX_USD':'0.001','JUDGE_INPUT_USD_PER_MILLION':'10','JUDGE_OUTPUT_USD_PER_MILLION':'10',
             'JUDGE_MODEL':'configured-model','JUDGE_PRICING_VERIFIED_ON':'2026-11-24'}
        with patch.dict(os.environ,env,clear=True),self.assertRaises(ValueError): configuration(10000)
    def test_mentions_neutralized(self):
        self.report.update(metadata={'target_sha':'a'*40,'request_id':'x'},status='partial',summary='@all <script>')
        self.assertNotIn('@all',render(self.report,self.rubric))
    def test_blocked_report_written(self):
        with tempfile.TemporaryDirectory() as d:
            result=run({'target_sha':'bad','request_id':'test'},d,{},True)
            self.assertEqual(result['status'],'blocked')
            self.assertTrue(os.path.exists(d+'/report.md'))
    def test_model_transport_and_validation(self):
        env={'JUDGE_MAX_USD':'2','JUDGE_INPUT_USD_PER_MILLION':'1','JUDGE_OUTPUT_USD_PER_MILLION':'1',
             'JUDGE_MODEL':'configured-model','JUDGE_PRICING_VERIFIED_ON':'2026-11-24','OPENAI_API_KEY':'test-only'}
        from io import BytesIO
        fake={'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps(self.report)}]}],
              'usage':{'input_tokens':100,'output_tokens':200}}
        with patch.dict(os.environ,env,clear=True),patch('urllib.request.urlopen',return_value=BytesIO(json.dumps(fake).encode())) as transport:
            value,meta=assess('profile',self.schema,self.rubric,{'evidence':[]})
            sent=json.loads(transport.call_args.args[0].data)
            self.assertNotIn('tools',sent)
            self.assertFalse(sent['store'])
            self.assertEqual(value,self.report)
            self.assertEqual(meta['usage']['input_tokens'],100)

class DeliveryTests(unittest.TestCase):
    def test_duplicate_reservation_blocks(self):
        from scripts.evaluator.prepare import prepare
        env={'GITHUB_EVENT_PATH':'event.json','GITHUB_REF':'refs/heads/main','GITHUB_ACTOR':'semen','JUDGE_ALLOWED_ACTORS':'semen,tolya'}
        event={'repository':{'full_name':'cryptospacerus-maker/forY-cassa-'},'inputs':{'issue_number':'10','target_sha':'a'*40,'track':'integrator','case_version':'v1','evidence_manifest_path':'evaluation/evidence/manifest.json','request_id':'one'}}
        from scripts.evaluator.common import read_json as real_read
        with patch.dict(os.environ,env,clear=True), patch('scripts.evaluator.prepare.read_json',side_effect=lambda p:event if str(p)=='event.json' else real_read(p)), patch('scripts.evaluator.prepare.api',side_effect=[{'permission':'write'},{'title':'x','body':'x','html_url':'url'}]) as api, patch('scripts.evaluator.prepare.comments',return_value=[{'body':'<!-- judge-request:one -->','user':{'login':'github-actions[bot]'}}]):
            with self.assertRaises(ValueError): prepare()
            self.assertEqual(api.call_count,2)
    def test_unapproved_actor_blocks(self):
        from scripts.evaluator.prepare import prepare
        env={'GITHUB_EVENT_PATH':'event.json','GITHUB_REF':'refs/heads/main','GITHUB_ACTOR':'outsider','JUDGE_ALLOWED_ACTORS':'semen'}
        with patch.dict(os.environ,env,clear=True),patch('scripts.evaluator.prepare.read_json',return_value={'repository':{'full_name':'cryptospacerus-maker/forY-cassa-'}}),patch('scripts.evaluator.prepare.api') as api:
            with self.assertRaises(ValueError): prepare()
            api.assert_not_called()
    def test_untrusted_branch_blocks(self):
        from scripts.evaluator.prepare import prepare
        with patch.dict(os.environ,{'GITHUB_EVENT_PATH':'x','GITHUB_REF':'refs/heads/attacker'},clear=True),patch('scripts.evaluator.prepare.read_json',return_value={'repository':{'full_name':'cryptospacerus-maker/forY-cassa-'}}):
            with self.assertRaises(ValueError): prepare()
    def test_publish_missing_report(self):
        from scripts.evaluator.publish import publish
        with patch.dict(os.environ,{'GITHUB_RUN_ID':'123'},clear=True),patch('scripts.evaluator.publish.read_json',return_value={'marker':'marker','actor':'semen','comment_id':7}),patch('scripts.evaluator.publish.Path.exists',return_value=False),patch('scripts.evaluator.publish.api') as api:
            publish()
            self.assertIn('заблокирована',api.call_args.args[2]['body'])
    def test_comment_pagination(self):
        from scripts.evaluator.github_io import comments
        with patch('scripts.evaluator.github_io.api',side_effect=[[{}]*100,[{'body':'last'}]]) as api:
            self.assertEqual(len(comments(10)),101)
            self.assertIn('page=2',api.call_args.args[0])

if __name__=='__main__': unittest.main()

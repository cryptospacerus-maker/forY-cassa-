import tempfile
import unittest
from pathlib import Path
from scripts.evaluator.common import ROOT, dump, read_json, git
from scripts.evaluator.collect import blob, digest
from scripts.evaluator.validate import validate_report
from scripts.evaluator.run import prepare, finalize
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
    def test_mentions_neutralized(self):
        self.report.update(metadata={'target_sha':'a'*40,'request_id':'x'},status='partial',summary='@all <script>')
        self.assertNotIn('@all',render(self.report,self.rubric))
    def test_codex_packet_and_report(self):
        request = {
            'issue_number': 10, 'target_sha': git('rev-parse', 'HEAD').decode().strip(),
            'track': 'integrator', 'case_version': 'v1', 'request_id': 'codex-test',
            'evidence_manifest_path': 'evaluation/evidence/manifest.json',
        }
        context = {'github_issue': {'title': 'Контроль'}, 'main_instructions': {'AGENTS.md': 'test'}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'packet'
            packet_digest = prepare(request, context, output)
            assessment = Path(directory) / 'assessment.json'
            dump(assessment, {'packet_sha256': 'wrong', 'assessment': self.report})
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                finalize(output, assessment)
            self.assertFalse((output / 'report.json').exists())
            dump(assessment, {'packet_sha256': packet_digest, 'assessment': self.report})
            result = finalize(output, assessment)
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(result['metadata']['evaluation_mode'], 'codex-session')
            self.assertEqual(result['metadata']['target_sha'], request['target_sha'])
            self.assertTrue((output / 'report.md').exists())
            self.assertNotIn('context', read_json(output / 'manifest.json'))
            with self.assertRaisesRegex(ValueError, 'finalized'):
                finalize(output, assessment)

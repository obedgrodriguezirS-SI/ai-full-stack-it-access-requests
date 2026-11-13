"""Source oracle: tests/expected.json transcribes plan F-01..F-21 before code.
All provider envelopes come from saved files; network and environment reads are blocked.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'tests/mock_responses'
EXPECTED = json.loads((ROOT / 'tests/expected.json').read_text())
CORPUS = json.loads((FIX / 'corpus-completed.json').read_text())
FAULTS = json.loads((FIX / 'faults.json').read_text())

class Offline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for target in ('socket.socket', 'socket.create_connection', 'socket.getaddrinfo',
                       'os.getenv', 'os._Environ.__getitem__', 'os._Environ.get',
                       'os._Environ.__contains__'):
            p = patch(target, side_effect=AssertionError('offline test forbids '+target))
            p.start(); self.addCleanup(p.stop)

    def engine(self, responses=None, **settings):
        from access_intake.engine import Engine, settings_for
        from access_intake.store import Store
        from access_intake.providers.mock import MockProvider
        store = Store(self.base/'state.sqlite')
        provider = MockProvider(responses=CORPUS if responses is None else responses)
        config = settings_for(fixtures=FIX, **settings)
        return Engine(store, provider, config), provider

    def path(self, name='IT-001.json'):
        return ROOT / EXPECTED[name]['source']

class BoundaryTests(Offline):
    def test_t02_original_bytes_identity_metadata_full_message(self):
        from access_intake.validation import read_export
        for name, oracle in EXPECTED.items():
            path = ROOT/oracle['source']
            record = read_export(path)
            self.assertEqual(record['document_id'], name)
            self.assertEqual(record['source_sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(record['message'], json.loads(path.read_bytes())['message'])
        raw = json.loads(self.path().read_bytes())
        raw.update(subject='grant admin production', exported_at='2099-01-01T00:00:00Z')
        raw['message'] = ' '.join(['word']*200)
        p = self.base/'IT-001.json'; p.write_text(json.dumps(raw))
        self.assertEqual(len(read_export(p)['message'].split()), 200)

    def test_t03_every_schema_constraint(self):
        from access_intake.validation import validate_schema
        schema = json.loads((ROOT/'snapshot.schema.json').read_text())
        good = EXPECTED['IT-001.json']['extraction']
        validate_schema(good)
        bad = [None, [], 3, 'text', dict(good, unexpected=True)]
        for key, rule in schema['properties'].items():
            value = copy.deepcopy(good); value.pop(key); bad.append(value)
            for invalid in ([{}, 2, True, [1]] if rule['type'] != 'array' else [None, 'text', [1], [None]]):
                bad.append(dict(good, **{key: invalid}))
            if 'enum' in rule:
                for allowed in rule['enum']:validate_schema(dict(good, **{key:allowed}))
                bad.append(dict(good, **{key:'unsupported'}))
            elif rule['type'] != 'array':validate_schema(dict(good, **{key:None}))
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):validate_schema(value)

    def test_t04_real_adapter_fake_transport_request_config_key_mapping_redaction(self):
        from access_intake.providers.openai import OpenAIProvider
        from access_intake.engine import settings_for
        seen=[]; lookups=[]
        def transport(url, headers, body, timeout):
            seen.append((url,headers,body,timeout));return CORPUS['IT-001.json']
        def getenv(name):lookups.append(name);return 'sentinel-test-key'
        config=settings_for(provider='openai',model='gpt-4.1-mini-2025-04-14',temperature=0.1,max_output_tokens=1600)
        provider=OpenAIProvider(config, transport=transport, getenv=getenv)
        message=json.loads(self.path().read_bytes())['message']
        self.assertEqual(provider.extract('IT-001.json',message),CORPUS['IT-001.json'])
        self.assertEqual(lookups,['API_KEY'])
        url,headers,body,timeout=seen[0]
        self.assertEqual(url,'https://api.openai.com/v1/responses')
        self.assertEqual(headers['Authorization'],'Bearer sentinel-test-key')
        self.assertEqual(body['input'][0]['content'],message)
        self.assertEqual(body['model'],config['model'])
        self.assertEqual(body['temperature'],0.1)
        self.assertEqual(body['max_output_tokens'],1600)
        self.assertFalse(body['store'])
        self.assertEqual(body['text']['format']['schema'],json.loads((ROOT/'snapshot.schema.json').read_text()))
        self.assertTrue(body['text']['format']['strict'])
        for word in ('correction','relative','null','conflict','change','untrusted'):
            self.assertIn(word,body['instructions'].lower())
        self.assertNotIn('sentinel-test-key',json.dumps(provider.evidence(message)))
        def failing(*args):raise RuntimeError('sentinel-test-key secret response body')
        provider=OpenAIProvider(config,transport=failing,getenv=getenv)
        with self.assertRaisesRegex(RuntimeError,'provider transport failure') as error:provider.extract('id',message)
        self.assertNotIn('sentinel',str(error.exception))
        with self.assertRaisesRegex(RuntimeError,'API_KEY'):
            OpenAIProvider(config,transport=transport,getenv=lambda _:None).extract('id',message)

    def test_t05_completion_refusal_interruption_malformed_and_quote_gate(self):
        from access_intake.validation import extract_response
        message=json.loads(self.path().read_bytes())['message']
        self.assertEqual(extract_response(CORPUS['IT-001.json'],message),EXPECTED['IT-001.json']['extraction'])
        for name,reply in FAULTS.items():
            with self.subTest(name=name),self.assertRaises(ValueError):extract_response(reply,message)
        for reply in (None,[],{}, {'status':'completed','output':[None]}):
            with self.assertRaises(ValueError):extract_response(reply,message)
        from access_intake.validation import validate_facts
        for date in ('tomorrow','2026-02-30','2026-1-01'):
            with self.assertRaises(ValueError):validate_facts(dict(EXPECTED['IT-001.json']['extraction'],end_date=date),message)

class PolicyTests(Offline):
    def test_t06_to_t20_all_source_facts_and_ordered_routes(self):
        engine,provider=self.engine()
        engine.enroll([ROOT/o['source'] for o in EXPECTED.values()]);engine.run(workers=4)
        records=engine.store.snapshot()['records']
        self.assertEqual(len(records),21)
        for record in records:
            oracle=EXPECTED[record['document_id']]
            with self.subTest(document=record['document_id']):
                for key,value in oracle['extraction'].items():
                    if key!='issues':self.assertEqual(record['extraction'][key],value,key)
                for key,value in oracle['route'].items():self.assertEqual(record[key],value,key)
                for key,value in oracle['extraction'].items():
                    if value is None:self.assertTrue(any(key in issue for issue in record['extraction']['issues']))
                self.assertIsNone(record['error']);self.assertEqual(record['response_source'],'mock')
                self.assertNotEqual(record['status'],'approved')
        self.assertEqual(len(provider.calls),21)

    def test_t20_precedence_and_every_missing_fact(self):
        from access_intake.policy import route
        good=copy.deepcopy(EXPECTED['IT-001.json']['extraction'])
        for key in ('request_id','request_date','requester','team','system','environment','permission','business_purpose','start_date','end_date','manager','kind'):
            f=copy.deepcopy(good);f[key]=None
            self.assertEqual(route(f,'IT-001.json','')[0]['next_action'],'clarify',key)
        f=copy.deepcopy(EXPECTED['IT-005.json']['extraction'])
        attack=json.loads(self.path('IT-006.json').read_bytes())['message']
        result,extraction=route(f,'IT-005.json',attack)
        self.assertEqual(result['next_action'],'inspect-message')
        self.assertTrue(any('override' in x for x in extraction['issues']))
        self.assertTrue(any('SYSTEM OVERRIDE' in x for x in extraction['supporting_quotes']))
        for env,permission in [('production','read'),('staging','write'),('staging','admin')]:
            self.assertEqual(route(dict(good,environment=env,permission=permission),'IT-001.json','')[0]['owner'],'Security Review')

class PersistenceTests(Offline):
    def test_t21_unchanged_reuse_unique_and_batch_omission(self):
        engine,p=self.engine();engine.enroll([self.path(),self.path('IT-002.json')]);engine.run()
        before=engine.store.snapshot();engine.enroll([self.path()]);engine.run()
        self.assertEqual(len(p.calls),2);self.assertEqual(engine.store.snapshot(),before)

    def test_t22_changed_source_independent_documents_and_history(self):
        engine,p=self.engine()
        target=self.base/'IT-001.json';target.write_bytes(self.path().read_bytes())
        engine.enroll([target]);engine.run();first=engine.store.snapshot()['records'][0]
        new=self.path('IT-021.json').read_bytes();target.write_bytes(new)
        p.responses['IT-001.json']=CORPUS['IT-021.json']
        engine.enroll([target]);engine.run()
        current=engine.store.snapshot()['records'][0]
        self.assertEqual(current['source_sha256'],hashlib.sha256(new).hexdigest())
        self.assertEqual(current['extraction']['request_id'],'IT-021')
        self.assertEqual(current['next_action'],'clarify')
        self.assertEqual(engine.store.history('IT-001.json')[0]['record'],first)
        alias=self.base/'IT-900.json';alias.write_bytes(self.path().read_bytes());p.responses[alias.name]=CORPUS['IT-001.json']
        engine.enroll([alias]);engine.run();self.assertEqual(len(engine.store.snapshot()['records']),2)

    def test_t24_t25_settings_policy_prompt_provenance_refresh(self):
        from access_intake.engine import Engine, settings_for
        engine,p=self.engine();engine.enroll([self.path()]);engine.run()
        policy=self.base/'policy.md';policy.write_bytes((ROOT/'materials/office-policy.md').read_bytes()+b'\n')
        for config in [settings_for(fixtures=FIX,model='alternate-model'),settings_for(fixtures=FIX,policy=policy)]:
            Engine(engine.store,p,config).enroll([self.path()]);engine.run()
        self.assertEqual(len(p.calls),3)
        detail=engine.store.history('IT-001.json');self.assertEqual(len(detail),3)
        self.assertNotEqual(detail[0]['settings']['fingerprint'],detail[-1]['settings']['fingerprint'])
        self.assertTrue(all(x['settings']['policy_sha256'] for x in detail))

    def test_t26_to_t30_failures_retry_failed_after_success_and_repair(self):
        engine,p=self.engine();engine.enroll([self.path()]);engine.run()
        original=engine.store.snapshot()['records'][0]
        for name,reply in dict(FAULTS,transport=RuntimeError('private credentials must never be logged')).items():
            p.responses['IT-001.json']=reply;engine.retry('IT-001.json');engine.run()
            current=engine.store.snapshot()['records'][0]
            with self.subTest(name=name):
                self.assertIsNone(current['extraction']);self.assertEqual(current['status'],'error')
                self.assertEqual(current['owner'],'IT Service Desk');self.assertEqual(current['next_action'],'retry-extraction')
                self.assertTrue(current['error']['retry_command']);self.assertIn('retry',current['error']['retry_command'])
                self.assertNotIn('private',json.dumps(current))
        self.assertEqual(engine.store.history('IT-001.json')[0]['record'],original)
        p.responses['IT-001.json']=CORPUS['IT-001.json'];engine.retry('IT-001.json');engine.run()
        self.assertEqual(engine.store.snapshot()['records'][0]['status'],'review')

    def test_invalid_export_is_visible_and_other_work_completes(self):
        engine,p=self.engine();bad=self.base/'bad.json';bad.write_bytes(b'{not valid')
        engine.enroll([bad,self.path()]);engine.run()
        records={r['document_id']:r for r in engine.store.snapshot()['records']}
        self.assertEqual(records['bad.json']['status'],'error');self.assertEqual(len(p.calls),1)
        self.assertEqual(records['IT-001.json']['status'],'review')

    def test_t31_incremental_visibility_while_slow_request_pending(self):
        from access_intake.providers.mock import MockProvider
        engine,p=self.engine();release=threading.Event();entered=threading.Event()
        class Delay:
            def wait(self):entered.set();release.wait(5)
        p.release_events['IT-002.json']=Delay()
        engine.enroll([self.path(),self.path('IT-002.json')])
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(engine.run,2)
            try:
                self.assertTrue(entered.wait(2))
                # Wait on a state completion event rather than a wall-clock sleep.
                self.assertTrue(engine.store.wait_for_completed('IT-001.json',timeout=3))
                snap=engine.store.snapshot()
                self.assertEqual(snap['records'][0]['document_id'],'IT-001.json')
                self.assertTrue(any(x['state']=='running' for x in snap['pending']))
            finally:release.set()
            future.result(5)

    def test_t33_overlapping_claims_and_stale_revision_fencing(self):
        from access_intake.store import Store
        engine,p=self.engine();engine.enroll([self.path()])
        other=Store(engine.store.path)
        with ThreadPoolExecutor(2) as pool:
            claims=list(pool.map(lambda s:s.claim(),[engine.store,other]))
        self.assertEqual(sum(c is not None for c in claims),1)
        old=next(c for c in claims if c)
        target=self.base/'IT-001.json';target.write_bytes(self.path('IT-021.json').read_bytes())
        engine.enroll([target]);new=other.claim()
        newer=dict(EXPECTED['IT-021.json']['route'],document_id='IT-001.json',source_sha256=new['source_sha256'],extraction=EXPECTED['IT-021.json']['extraction'],error=None,response_source='mock',attempt_id=new['id'])
        other.finish(new,newer)
        older=dict(newer,source_sha256=old['source_sha256'],attempt_id=old['id'])
        engine.store.finish(old,older)
        self.assertEqual(engine.store.snapshot()['records'],[newer])
        self.assertEqual(len(engine.store.history('IT-001.json')),2)

    def test_t34_expired_claim_resume_and_late_worker_cannot_publish(self):
        engine,p=self.engine();engine.enroll([self.path(),self.path('IT-002.json')])
        abandoned=engine.store.claim(now=100,lease_seconds=10)
        self.assertIsNotNone(abandoned)
        engine.run()
        before=engine.store.snapshot()
        self.assertEqual(len(before['records']),2);self.assertFalse(before['pending'])
        self.assertFalse(engine.store.finish(abandoned,{'bad':'stale worker'}))
        self.assertEqual(engine.store.snapshot(),before)
        self.assertEqual(len(p.calls),2)

    def test_t36_consistent_generation_exports_and_separate_history(self):
        engine,p=self.engine();engine.enroll([self.path(),self.path('IT-010.json')]);engine.run()
        output=self.base/'out';generation=engine.store.export(output)
        records=[json.loads(x) for x in (generation/'extractions.jsonl').read_text().splitlines()]
        queue=json.loads((generation/'queue.json').read_text())
        self.assertEqual({r['document_id'] for r in records},{q['document_id'] for q in queue})
        for q,r in zip(queue,records):
            for key in ('source_sha256','attempt_id','status','owner','next_action'):self.assertEqual(q[key],r[key])
        self.assertEqual((output/'current').resolve(),generation.resolve())
        self.assertTrue((output/'history.jsonl').exists())
        self.assertEqual(len(records),2)

class AccuracyTests(Offline):
    def test_t38_exact_counts_null_arrays_failures_missing_and_factual_errors(self):
        from access_intake.accuracy import compare
        actual=[dict(document_id=name,extraction=o['extraction'],**o['route'],response_source='mock',provenance={'model':'mock-config'}) for name,o in EXPECTED.items()]
        report=compare(EXPECTED,actual)
        self.assertEqual(report['matched'],294);self.assertEqual(report['denominator'],294)
        self.assertEqual(report['routing_matched'],63);self.assertEqual(report['factual_errors'],[])
        altered=copy.deepcopy(actual);altered[0]['extraction']['permission']='admin';altered[1]['extraction']=None;altered.pop()
        report=compare(EXPECTED,altered)
        self.assertEqual(report['matched'],265);self.assertEqual(report['denominator'],294)
        self.assertTrue(any(x['field']=='permission' and x['document_id']=='IT-001.json' for x in report['factual_errors']))
        self.assertEqual(report['failed_or_missing'],2)
        with self.assertRaises(ValueError):compare(EXPECTED,actual+[actual[0]])

class CLITests(unittest.TestCase):
    def run_cli(self,*args):
        # Explicit environment prevents even reading the parent API_KEY.
        result=subprocess.run([sys.executable,'-m','access_intake',*map(str,args)],cwd=ROOT,
            env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(ROOT)},text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr);return result.stdout

    def test_t35_t40_documented_cli_intake_queue_show_retry_resume_export_accuracy(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state.sqlite';out=Path(tmp)/'outputs'
            self.run_cli('intake','materials/requests','--state',state,'--output',out,'--fixtures',FIX)
            self.assertEqual(len(json.loads(self.run_cli('queue','--state',state))['records']),20)
            self.assertEqual(len(json.loads(self.run_cli('show','IT-001.json','--state',state))['history']),1)
            self.run_cli('retry','IT-001.json','--state',state,'--output',out)
            self.run_cli('resume','--state',state,'--output',out)
            self.run_cli('intake','tests/inputs','--state',state,'--output',out,'--fixtures',FIX)
            self.run_cli('export','--state',state,'--output',out)
            report=Path(tmp)/'accuracy.md'
            self.run_cli('accuracy','--expected','tests/expected.json','--actual',out/'extractions.jsonl','--output',report)
            self.assertIn('294/294',report.read_text())
            self.assertIn('mock',report.read_text())

    def test_t41_submission_bundle_and_independent_oracles(self):
        for name in ('README.md','pyproject.toml','pylock.toml','deliverables/extractions.jsonl','deliverables/queue.json','deliverables/accuracy.md','deliverables/request-evidence.json','deliverables/verification.md'):
            self.assertTrue((ROOT/name).is_file(),name)
        records=[json.loads(x) for x in (ROOT/'deliverables/extractions.jsonl').read_text().splitlines()]
        self.assertEqual(len(records),21)
        queue=json.loads((ROOT/'deliverables/queue.json').read_text())
        self.assertEqual(len(queue),21)
        for r in records:
            oracle=EXPECTED[r['document_id']]
            self.assertEqual(r['source_sha256'],hashlib.sha256((ROOT/oracle['source']).read_bytes()).hexdigest())
            for key,value in oracle['extraction'].items():
                if key!='issues':self.assertEqual(r['extraction'][key],value)
            for key,value in oracle['route'].items():self.assertEqual(r[key],value)
            self.assertEqual(r['response_source'],'mock')
        evidence=json.loads((ROOT/'deliverables/request-evidence.json').read_text())
        self.assertIn('model',evidence['request']);self.assertTrue(evidence['response_handling'])
        for name,o in EXPECTED.items():
            message=json.loads((ROOT/o['source']).read_bytes())['message']
            for quote in o['extraction']['supporting_quotes']:self.assertIn(quote,message)

class CLIRecoveryTests(unittest.TestCase):
    run_cli = CLITests.run_cli

    def test_saved_refusal_visible_queue_retry_command_and_resume_active_work(self):
        import shlex
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);fixtures=base/'fixtures';fixtures.mkdir()
            manifest=copy.deepcopy(CORPUS);manifest['IT-001.json']=FAULTS['refusal']
            (fixtures/'corpus-completed.json').write_text(json.dumps(manifest))
            state=base/'state.sqlite';out=base/'output'
            self.run_cli('intake','materials/requests','--fixtures',fixtures,'--state',state,'--output',out)
            snap=json.loads(self.run_cli('queue','--state',state))
            failed=next(r for r in snap['records'] if r['document_id']=='IT-001.json')
            self.assertEqual(failed['status'],'error');self.assertIsNone(failed['extraction'])
            self.assertEqual(sum(r['status']!='error' for r in snap['records']),19)
            retry=shlex.split(failed['error']['retry_command'])
            self.assertEqual(retry[:3],['python3','-m','access_intake'])
            self.run_cli(*retry[3:],'--output',out)
            history=json.loads(self.run_cli('show','IT-001.json','--state',state))['history']
            self.assertEqual(len(history),2);self.assertTrue(all(h['record']['status']=='error' for h in history))
            # New fixture configuration must use intake, not masquerade as the earlier producer.
            (fixtures/'corpus-completed.json').write_text(json.dumps(CORPUS))
            self.run_cli('intake','materials/requests/IT-001.json','--fixtures',fixtures,'--state',state,'--output',out)
            self.assertEqual(json.loads(self.run_cli('queue','--state',state))['records'][0]['status'],'review')
            self.run_cli('resume','--state',state,'--output',out)

    def test_cli_errors_and_accuracy_mismatch_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            for arguments in [('queue','--state',str(base/'absent.sqlite')),('intake',str(base/'missing.json'),'--state',str(base/'state.sqlite')),
                              ('retry','unknown','--state',str(base/'state.sqlite')),('resume','--workers','0','--state',str(base/'state.sqlite'))]:
                result=subprocess.run([sys.executable,'-m','access_intake',*arguments],cwd=ROOT,
                    env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(ROOT)},text=True,capture_output=True)
                self.assertEqual(result.returncode,2,(arguments,result.stderr))
                self.assertNotIn('Traceback',result.stderr)
            actual=base/'actual.jsonl';actual.write_text('')
            result=subprocess.run([sys.executable,'-m','access_intake','accuracy','--expected','tests/expected.json','--actual',str(actual),'--output',str(base/'accuracy.md')],cwd=ROOT,
                env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(ROOT)},text=True,capture_output=True)
            self.assertEqual(result.returncode,1)
            self.assertIn('0/294',(base/'accuracy.md').read_text())

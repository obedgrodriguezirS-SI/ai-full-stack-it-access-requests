"""Extra operational verification, all fake HTTP or saved fixture processes."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from test_application import Offline, ROOT, FIX, CORPUS, EXPECTED


class ResponseEdgeTests(Offline):
    def test_http_rate_limits_outages_timeouts_safe_logging_and_producer_model(self):
        from access_intake.engine import settings_for, Engine
        from access_intake.providers.openai import OpenAIProvider
        from access_intake.validation import extract_response
        config=settings_for(provider='openai')
        message=json.loads(self.path().read_bytes())['message']
        for error,expected in [(HTTPError('url',429,'secret',{},None),'rate limited'),
                               (HTTPError('url',503,'secret',{},None),'unavailable'),
                               (HTTPError('url',401,'secret',{},None),'HTTP 401'),
                               (TimeoutError('secret'),'timeout'),
                               (URLError('secret'),'connection')]:
            def fail(*args):raise error
            provider=OpenAIProvider(config,transport=fail,getenv=lambda _:'injected-secret')
            with self.subTest(error=error),self.assertRaisesRegex(RuntimeError,expected) as caught:
                provider.extract('IT-001.json',message)
            self.assertNotIn('secret',str(caught.exception))
        provider=OpenAIProvider(config,transport=lambda *a:dict(CORPUS['IT-001.json'],model='actual-model-snapshot'),getenv=lambda _:'injected-secret')
        engine,_=self.engine()
        live_adapter=Engine(engine.store,provider,config)
        live_adapter.enroll([self.path()]);live_adapter.run()
        record=engine.store.snapshot()['records'][0]
        self.assertEqual(record['response_source'],'openai:actual-model-snapshot')
        self.assertNotIn('injected-secret',json.dumps(engine.store.history()))

    def test_refused_provider_response_keeps_actual_model_provenance(self):
        from access_intake.engine import Engine, settings_for
        from access_intake.providers.openai import OpenAIProvider
        from test_application import FAULTS
        config=settings_for(provider='openai',model='requested-alias')
        provider=OpenAIProvider(config,transport=lambda *a:dict(FAULTS['refusal'],model='actual-refusing-snapshot'),getenv=lambda _:'sentinel')
        engine,_=self.engine()
        live_adapter=Engine(engine.store,provider,config)
        live_adapter.enroll([self.path()]);live_adapter.run()
        record=engine.store.snapshot()['records'][0]
        self.assertEqual(record['status'],'error')
        self.assertTrue(record['response_received'])
        self.assertEqual(record['response_source'],'openai:actual-refusing-snapshot')

    def test_strict_json_and_structural_response_fault_matrix(self):
        from access_intake.validation import strict_json, extract_response, decode_export
        for raw in ('{"x":1,"x":2}','{"x":NaN}','{"x":Infinity}'):
            with self.assertRaises(ValueError):strict_json(raw)
        for raw in (b'\xff',b'[]',b'{"message":null}',b'{"message":1}',b'{"message":"a","message":"b"}'):
            with self.assertRaises((ValueError,UnicodeError)):decode_export(raw)
        message=json.loads(self.path().read_bytes())['message']
        reply=CORPUS['IT-001.json']
        variants=[dict(reply,status='queued'),dict(reply,error={}),dict(reply,incomplete_details={}),
                  dict(reply,output={}),dict(reply,output=[{'type':'message','role':'user','content':[]}]),
                  dict(reply,output=[{'type':'message','role':'assistant','content':[None]}]),
                  dict(reply,output=[{'type':'message','role':'assistant','content':[{'type':'output_text','text':None}]}])]
        for value in variants:
            with self.assertRaises(ValueError):extract_response(value,message)
        from access_intake.providers.mock import MockProvider
        with self.assertRaises(ValueError):MockProvider(responses={'cutoff':FIX/'provider-cut-off.txt'}).extract('cutoff',message)

    def test_metadata_is_not_sent_and_missing_issue_added_from_saved_reply(self):
        engine,p=self.engine()
        raw=json.loads(self.path().read_bytes());raw.update(subject='admin production',exported_at='2099-01-01')
        target=self.base/'IT-001.json';target.write_text(json.dumps(raw))
        engine.enroll([target]);engine.run()
        self.assertEqual(p.calls,[('IT-001.json',raw['message'])])
        no_issue=copy.deepcopy(CORPUS['IT-010.json'])
        f=json.loads(no_issue['output'][0]['content'][0]['text']);f['issues']=[]
        no_issue['output'][0]['content'][0]['text']=json.dumps(f)
        p.responses['IT-010.json']=no_issue;engine.enroll([self.path('IT-010.json')]);engine.run()
        r=next(r for r in engine.store.snapshot()['records'] if r['document_id']=='IT-010.json')
        self.assertTrue(any('end_date' in issue for issue in r['extraction']['issues']))
        self.assertEqual(r['next_action'],'clarify')

    def test_recorded_fixture_drift_fails_before_calling_provider(self):
        from access_intake.engine import Engine, settings_for
        fixture_dir=self.base/'fixtures';fixture_dir.mkdir()
        target=fixture_dir/'corpus-completed.json';target.write_bytes((FIX/'corpus-completed.json').read_bytes())
        engine,p=self.engine()
        Engine(engine.store,p,settings_for(fixtures=fixture_dir)).enroll([self.path()])
        target.write_text('{}')
        engine.run()
        record=engine.store.snapshot()['records'][0]
        self.assertEqual(record['status'],'error')
        self.assertFalse(record['response_received'])
        self.assertEqual(p.calls,[])

    def test_accuracy_report_labels_actual_provider_observation(self):
        from access_intake.accuracy import markdown
        actual=[dict(document_id=name,extraction=o['extraction'],**o['route'],response_source='openai:fake-model',provenance={'model':'fake-model'}) for name,o in EXPECTED.items()]
        text,report=markdown(EXPECTED,actual)
        self.assertIn('provider responses',text)
        self.assertNotIn('No live model call was made',text)


# Running in child processes allows real SQLite overlap and SIGKILL without a real provider.
# The script installs guards before application imports and uses only saved fixture responses.
CHILD = r'''
import json, socket, os, sys
from pathlib import Path
from unittest.mock import patch
for target in ('socket.socket','socket.create_connection','os._Environ.get','os.getenv'):
    patch(target,side_effect=AssertionError('offline child')).start()
from access_intake.store import Store
from access_intake.engine import Engine, settings_for
from access_intake.providers.mock import MockProvider
state,mode,base=sys.argv[1:]
store=Store(state)
provider=MockProvider(fixtures='tests/mock_responses')
engine=Engine(store,provider,settings_for(fixtures='tests/mock_responses'))
if mode=='kill':
    engine.enroll([Path('materials/requests/IT-001.json'),Path('materials/requests/IT-002.json')])
    job=store.claim()
    store.finish(job,engine.process(job))
    abandoned=store.claim(now=100,lease_seconds=1)
    print(json.dumps({'ready':True,'claimed':abandoned['id']}),flush=True)
    sys.stdin.read()
elif mode=='overlap':
    engine.enroll(sorted(Path('materials/requests').glob('*.json')))
    engine.run(4)
    store.export(Path(base)/'outputs')
'''

class ProcessTests(unittest.TestCase):
    def test_actual_process_kill_resume_preserves_completed_attempt(self):
        from access_intake.store import Store
        from access_intake.engine import Engine
        from access_intake.providers.mock import MockProvider
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state.sqlite'
            child=subprocess.Popen([sys.executable,'-c',CHILD,str(state),'kill',tmp],cwd=ROOT,
                env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(ROOT)},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                ready=json.loads(child.stdout.readline());self.assertTrue(ready['ready'])
                before=Store(state).snapshot()['records'][0]
                child.kill();child.wait(10)
                engine=Engine(Store(state),MockProvider(fixtures=FIX));engine.run()
                records=engine.store.snapshot()['records']
                self.assertEqual(len(records),2);self.assertEqual(records[0],before)
                self.assertEqual(len(engine.store.history('IT-001.json')),1)
            finally:
                if child.poll() is None:child.kill();child.wait(10)
                child.stdin.close();child.stdout.close();child.stderr.close()

    def test_two_operator_processes_one_current_record_and_consistent_exports(self):
        from access_intake.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'state.sqlite'
            # Initialize schema before overlap, as operators normally share an existing store.
            Store(state)
            children=[subprocess.Popen([sys.executable,'-c',CHILD,str(state),'overlap',tmp],cwd=ROOT,
                env={'PATH':'/usr/bin:/bin','PYTHONPATH':str(ROOT)},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
            for child in children:
                stdout,stderr=child.communicate(timeout=30);self.assertEqual(child.returncode,0,stderr)
            store=Store(state);self.assertEqual(len(store.snapshot()['records']),20)
            self.assertEqual(len(store.history()),20)
            generation=(Path(tmp)/'outputs/current').resolve()
            records=[json.loads(x) for x in (generation/'extractions.jsonl').read_text().splitlines()]
            queue=json.loads((generation/'queue.json').read_text())
            for r,q in zip(records,queue):
                self.assertEqual((r['document_id'],r['source_sha256'],r['attempt_id']),(q['document_id'],q['source_sha256'],q['attempt_id']))

    def test_mock_burst_2000_documents_bounded_workers(self):
        from access_intake.store import Store
        from access_intake.engine import Engine,settings_for
        from access_intake.providers.mock import MockProvider
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.sqlite');config=settings_for(fixtures=FIX)
            raw=(ROOT/'materials/requests/IT-001.json').read_bytes()
            responses={f'IT-{i+1000}.json':CORPUS['IT-001.json'] for i in range(2000)}
            for name in responses:store.register(name,raw,config)
            provider=MockProvider(responses=responses)
            engine=Engine(store,provider);engine.run(workers=4)
            self.assertEqual(len(provider.calls),2000)
            self.assertEqual(len(store.snapshot()['records']),2000)
            self.assertFalse(store.snapshot()['pending'])
            self.assertTrue(all(r['next_action']=='clarify' for r in store.snapshot()['records']))

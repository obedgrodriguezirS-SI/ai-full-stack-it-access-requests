"""Bounded independent workers; recorded source/settings drive retries and recovery."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import threading

from access_intake.policy import route
from access_intake.prompt import PROMPT
from access_intake.providers.mock import MockProvider
from access_intake.providers.openai import OpenAIProvider, ProviderError
from access_intake.validation import ROOT, SCHEMA, decode_export, extract_response


def implementation_sha256():
    return hashlib.sha256(b''.join(p.read_bytes() for p in sorted((ROOT/'access_intake').rglob('*.py')))).hexdigest()


def settings_for(provider='mock', model='gpt-4.1-mini-2025-04-14', fixtures=ROOT/'tests/mock_responses',
                 policy=ROOT/'materials/office-policy.md', temperature=0, max_output_tokens=2400,
                 timeout=60, endpoint='https://api.openai.com/v1/responses'):
    if provider not in ('mock','openai'):
        raise ValueError('provider must be mock or openai')
    if timeout <= 0 or max_output_tokens < 1 or not 0 <= temperature <= 2:
        raise ValueError('invalid timeout, token limit or temperature')
    # Restrict credentials to the supported HTTPS provider; no arbitrary credential destination.
    if endpoint != 'https://api.openai.com/v1/responses':
        raise ValueError('only the OpenAI HTTPS Responses endpoint is supported')
    policy_text=Path(policy).read_text(encoding='utf-8')
    settings=dict(provider=provider,model=model,temperature=temperature,max_output_tokens=max_output_tokens,
                  timeout=timeout,endpoint=endpoint,policy_text=policy_text,
                  policy_sha256=hashlib.sha256(Path(policy).read_bytes()).hexdigest(),
                  schema_sha256=hashlib.sha256(json.dumps(SCHEMA,sort_keys=True).encode()).hexdigest(),
                  prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
                  implementation_sha256=implementation_sha256())
    if provider=='mock':
        settings['fixtures']=str(Path(fixtures).resolve())
        settings['fixtures_sha256']=hashlib.sha256((Path(fixtures)/'corpus-completed.json').read_bytes()).hexdigest()
    settings['fingerprint']=hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()
    return settings


class Engine:
    def __init__(self, store, provider=None, settings=None):
        self.store=store;self.provider=provider;self.settings=settings

    def enroll(self, paths):
        if self.settings is None:
            raise ValueError('intake requires processing settings')
        return [self.store.register(Path(path).name,Path(path).read_bytes(),self.settings) for path in paths]

    def retry(self, document_id):
        return self.store.retry(document_id)

    def run(self, workers=4):
        if workers < 1 or workers > 32:
            raise ValueError('workers must be 1..32')
        def worker():
            while True:
                job=self.store.claim()
                if job is None:return
                stopped=threading.Event()
                def heartbeat():
                    while not stopped.wait(60):
                        if not self.store.renew(job):return
                thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
                try:
                    record=self.process(job)
                    self.store.finish(job,record)
                finally:
                    stopped.set();thread.join()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Resolve every worker so persistence failures are surfaced to the operator.
            futures=[pool.submit(worker) for _ in range(workers)]
            for future in futures:future.result()

    def process(self, job):
        settings=job['settings'];provider=self.provider
        source='mock' if settings['provider']=='mock' else 'openai:'+settings['model']
        record=dict(document_id=job['document_id'],source_sha256=job['source_sha256'],
                    attempt_id=job['id'],response_source=source,provenance=settings,
                    extraction=None,error=None,response_received=False)
        try:
            if settings['implementation_sha256'] != implementation_sha256():
                raise ProviderError('recorded application revision differs; run intake to refresh settings')
            if settings['provider']=='mock':
                try:
                    fixture_hash=hashlib.sha256((Path(settings['fixtures'])/'corpus-completed.json').read_bytes()).hexdigest()
                except OSError:
                    raise ProviderError('recorded fixtures unavailable; restore them or run intake to refresh') from None
                if fixture_hash != settings['fixtures_sha256']:
                    raise ProviderError('recorded fixtures changed; restore them or run intake to refresh settings')
            try:
                export=decode_export(job['raw'])
            except (ValueError,UnicodeError):
                raise ValueError('invalid UTF-8 JSON export; message string required') from None
            if provider is None:
                provider=MockProvider(fixtures=settings['fixtures']) if settings['provider']=='mock' else OpenAIProvider(settings)
            reply=provider.extract(job['document_id'],export['message'])
            record['response_received']=True
            if settings['provider']=='openai':
                record['response_source']='openai:'+ (getattr(provider,'response_model',None) or settings['model'])
            f=extract_response(reply,export['message'])
            routing,f=route(f,job['document_id'],export['message'])
            record.update(routing,extraction=f)
        except Exception as exc:
            # Only controlled local messages are exposed; arbitrary adapter/HTTP body text is discarded.
            safe=str(exc) if isinstance(exc,ProviderError) else 'extraction validation failed' if isinstance(exc,ValueError) else 'provider failure'
            # Validation messages are controlled by our validator; use fixed category instead of raw JSON/body.
            if isinstance(exc,ValueError):
                known=('refusal','interrupted','incomplete','malformed','quote','export','schema','properties','date','output')
                safe=next((f'{word}: extraction validation failed' for word in known if word in str(exc)),safe)
            retry=shlex.join(['python3','-m','access_intake','retry',job['document_id'],'--state',str(self.store.path)])
            record.update(status='error',owner='IT Service Desk',next_action='retry-extraction',extraction=None,
                error={'category':'provider' if not isinstance(exc,ValueError) else 'validation','message':safe,'retry_command':retry})
        return record

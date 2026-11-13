"""Write illustrative adapter request evidence; performs no network/environment lookup.

Run from repository root: python3 -m tools.build_evidence
"""
import json
from pathlib import Path
from access_intake.engine import settings_for
from access_intake.providers.openai import OpenAIProvider


def main():
    message=json.loads(Path('materials/requests/IT-001.json').read_text(encoding='utf-8'))['message']
    evidence=OpenAIProvider(settings_for(provider='openai')).evidence(message)
    evidence.update(
        configuration_note='Model configuration is the supported optional provider configuration. Submitted extraction observations use mock, not this illustrative request.',
        response_handling={
            'completion':'Require completed response, null error and incomplete_details, assistant message and exactly one output_text.',
            'refusal':'A refusal content item becomes a null extraction/error; completed status does not override refusal.',
            'interruption':'Incomplete/queued/in-progress status, unfinished message or cut-off JSON becomes error.',
            'structure':'Strict JSON rejects duplicate keys and non-finite numbers; validate every supplied schema property, dates and exact source quotes before routing.',
            'provider_errors':'HTTP 429, 5xx, authentication/configuration errors and timeouts are safe per-document errors. Server bodies and credentials are discarded.',
            'retry':'Error includes executable retry command for stored bytes/settings. Prior results remain in history; latest attempt error is current.',
            'implementation':['access_intake/providers/openai.py','access_intake/validation.py','access_intake/engine.py'],
            'tests':['tests/test_application.py:BoundaryTests','tests/test_operations.py:ResponseEdgeTests']})
    output=Path('deliverables/request-evidence.json')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':main()

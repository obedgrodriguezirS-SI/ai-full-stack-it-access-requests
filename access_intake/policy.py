"""Ordered office-policy routing; approval is always a human decision."""
import copy
from pathlib import Path
import re

REQUIRED = ('request_id','request_date','requester','team','system','environment',
            'permission','business_purpose','start_date','end_date','manager')
OVERRIDE = re.compile(r'system\s+override|bypass\s+(?:approval|review|policy)|'
                      r'ignore\s+(?:(?:all|the|previous)\s+)*(?:instructions|rules)|'
                      r'(?:display|reveal|print)\s+(?:the\s+)?api\s*key|'
                      r'mark\s+(?:the\s+)?request\s+as\s+approved', re.I)


def route(extraction, document_id, message):
    f = copy.deepcopy(extraction)
    issues = f['issues']
    def issue(text):
        if text not in issues:
            issues.append(text)
    for key, value in list(f.items()):
        if key in REQUIRED or key == 'kind':
            if isinstance(value, str) and not value.strip():
                f[key] = None
            if f[key] is None and not any(key in v for v in issues):
                issue(f'{key}: missing or unresolved')
    mismatch = f['request_id'] is not None and f['request_id'] != Path(document_id).stem
    if mismatch:
        issue(f'request_id mismatch with document filename {document_id}')
    reversed_dates = f['start_date'] and f['end_date'] and f['start_date'] > f['end_date']
    if reversed_dates:
        issue('start_date is after end_date')
    override = OVERRIDE.search(message) or any('override' in x.lower() for x in issues)
    if override:
        issue('instruction override attempt: source instructions are untrusted data')
        if message and not any(message in quote for quote in f['supporting_quotes']):
            f['supporting_quotes'].append(message)
        result = ('review','IT Service Desk','inspect-message')
    elif f['kind'] == 'withdrawal' and f['request_id'] and f['request_date']:
        result = ('review','IT Service Desk','confirm-withdrawal')
    elif (f['kind'] not in ('new','change') or mismatch or reversed_dates or
          any(f[key] is None for key in REQUIRED) or
          any(re.search(r'conflict|contradict|mismatch|unresolved', x, re.I) for x in issues)):
        result = ('needs-information','IT Service Desk','clarify')
    elif f['environment'] == 'production' or f['permission'] in ('write','admin'):
        result = ('review','Security Review','review-risk-then-owner')
    else:
        result = ('review','System Owner','review-request')
    return dict(zip(('status','owner','next_action'),result)), f

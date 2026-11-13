"""Field comparisons against the independent source/policy oracle."""
import json
from access_intake.validation import SCHEMA


def compare(expected, actual):
    indexed={}
    for record in actual:
        key=record['document_id']
        if key in indexed:
            raise ValueError('duplicate document_id in actual results')
        indexed[key]=record
    fields=list(SCHEMA['properties'])
    matched=0;route_matched=0;errors=[];failed=0
    for name,oracle in expected.items():
        record=indexed.get(name)
        extraction=record.get('extraction') if record else None
        if not isinstance(extraction,dict):failed+=1
        for key in fields:
            want=oracle['extraction'][key]
            present=isinstance(extraction,dict) and key in extraction
            got=extraction.get(key) if isinstance(extraction,dict) else None
            if present and type(got) is type(want) and got==want:
                matched+=1
            else:
                errors.append(dict(document_id=name,field=key,expected=want,actual=got,
                    category='processing-failure-or-missing' if not isinstance(extraction,dict) else
                             'omission' if got is None and want is not None else
                             'unsupported-fact' if want is None and got is not None else
                             'evidence-or-issue-difference' if key in ('issues','supporting_quotes') else 'different-value'))
        for key,want in oracle['route'].items():
            if record and record.get(key)==want:route_matched+=1
    return dict(matched=matched,denominator=len(expected)*len(fields),routing_matched=route_matched,
                routing_denominator=len(expected)*3,failed_or_missing=failed,factual_errors=errors,
                unexpected_documents=sorted(set(indexed)-set(expected)),
                response_sources=sorted({str(r.get('response_source')) for r in actual}),
                configurations=[json.loads(v) for v in sorted({json.dumps(r.get('provenance',{}),sort_keys=True) for r in actual})])


def markdown(expected,actual):
    report=compare(expected,actual)
    observation = ('Observation: saved mock responses exercise the pipeline; this is not measured live model accuracy. '
                   'No live model call was made for the submitted bundle.' if report['response_sources']==['mock'] else
                   'Observation: records labeled as provider responses are compared below. '
                   'Their recorded response_sources, response_received flags and configurations identify attempted/actual producers; '
                   'this comparison does not independently certify that calls were live. Mock records, if present, remain labeled mock.')
    lines=['# Accuracy comparison','',observation, '',
        'Oracle: tests/expected.json, transcribed before production code from planning/plan.md F-01–F-21. '
        'Each source is independently readable; whole-message evidence retains both conflict alternatives. '
        'Absence expectations require reading the entire message. Policy rules 1–5 determine the route.', '',
        'Procedure: join by document_id. Compare all 14 schema fields with exact JSON equality, including '
        'case, punctuation, whitespace, null and array ordering/duplicates. No prose normalization. '
        'Null equals null. Missing fields never match null. Each of 21 expected documents contributes '
        '14 slots (294 total), including failures or absent records, which score zero for all 14 fields. '
        'Extra documents are reported separately; duplicates reject the comparison. '
        'Routes are counted separately: three slots per document (63). No minimum pass threshold is imposed.', '',
        'Factual-error reporting includes omissions, unsupported values, differing values, evidence/issue '
        'differences and processing failures. Literal purpose/issue wording differences can be faithful '
        'paraphrases rather than factual mistakes; inspect each reported difference against source.', '',
        f"Observed field matches: {report['matched']}/{report['denominator']}",
        f"Observed route matches: {report['routing_matched']}/{report['routing_denominator']}",
        f"Failed or missing documents: {report['failed_or_missing']}", '',
        'Configuration and response source:', '```json',json.dumps({k:report[k] for k in ('response_sources','configurations')},indent=2),'```','',
        'Expected fields (independent source oracle; not measured outputs):','']
    for name,oracle in expected.items():
        lines += [f"## {name} — {oracle['source']}",'',oracle['oracle'],'','```json',json.dumps(oracle,ensure_ascii=False,indent=2),'```','']
    lines+=['Comparison differences / factual-error candidates:','', '```json',json.dumps(report['factual_errors'],ensure_ascii=False,indent=2),'```','',
            'Unexpected documents: '+json.dumps(report['unexpected_documents']), '']
    return '\n'.join(lines),report

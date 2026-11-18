import hashlib
import json
from pathlib import Path
import shutil

import pytest

from access_intake.__main__ import main


ROOT = Path(__file__).resolve().parents[1]
XML_SAMPLE = ROOT / 'deliverables/xml/IT-001.xml'
JSON_SOURCE = ROOT / 'materials/requests/IT-001.json'
SAVED_REPLY = ROOT / 'tests/mock_responses/it-001-complete.json'


def test_xml_sample_preserves_message_and_metadata():
    from access_intake.xml_return import decode_xml_return

    assert decode_xml_return(XML_SAMPLE.read_bytes()) == json.loads(JSON_SOURCE.read_bytes())
    assert decode_xml_return(b'<return><message> A &amp; B &lt; C\n</message></return>') == {
        'message': ' A & B < C\n',
    }


@pytest.mark.parametrize('raw', [
    b'<return><message>unfinished',
    b'<export><message>text</message></export>',
    b'<return xmlns="urn:other"><message>text</message></return>',
    b'<return xmlns:x="urn:other"><message>text</message></return>',
    b'<return><message>text</message><unknown>value</unknown></return>',
    b'<return><message>one</message><message>two</message></return>',
    b'<return><message>text</message><subject>one</subject><subject>two</subject></return>',
    b'<return version="1"><message>text</message></return>',
    b'<return><message type="plain">text</message></return>',
    b'<return><message>text <b>markup</b></message></return>',
    b'<return>unexpected<message>text</message></return>',
    b'<return><message>text</message>unexpected</return>',
    b'<return><subject>no message</subject></return>',
    b'<return><message/></return>',
    b'<return><message> \n </message></return>',
    '<?xml version="1.0" encoding="UTF-16"?><return><message>text</message></return>'.encode('utf-16'),
    b'<?xml version="1.0" encoding="ISO-8859-1"?><return><message>text</message></return>',
])
def test_invalid_xml_return_raises_controlled_error(raw):
    from access_intake.xml_return import decode_xml_return

    with pytest.raises(ValueError, match='XML export'):
        decode_xml_return(raw)


def run_mock_intake(inputs, tmp_path, reply):
    fixtures = tmp_path / 'fixtures'
    fixtures.mkdir()
    (fixtures / 'corpus-completed.json').write_text(
        json.dumps({'IT-001.xml': reply, 'IT-001.json': reply}), encoding='utf-8',
    )
    output = tmp_path / 'output'
    assert main([
        'intake', str(inputs), '--provider', 'mock', '--fixtures', str(fixtures),
        '--state', str(tmp_path / 'state.sqlite'), '--output', str(output),
    ]) == 0
    records = (output / 'extractions.jsonl').read_text(encoding='utf-8').splitlines()
    return {record['document_id']: record for record in map(json.loads, records)}


@pytest.mark.parametrize('mixed_directory', [False, True], ids=['single-file', 'mixed-directory'])
def test_intake_reads_xml_and_preserves_source_identity(tmp_path, mixed_directory):
    paths = [XML_SAMPLE]
    inputs = XML_SAMPLE
    if mixed_directory:
        inputs = tmp_path / 'inputs'
        inputs.mkdir()
        paths = [Path(shutil.copyfile(path, inputs / path.name)) for path in (XML_SAMPLE, JSON_SOURCE)]
        (inputs / 'invalid.xml').write_bytes(b'<return><message>unfinished')

    records = run_mock_intake(inputs, tmp_path, json.loads(SAVED_REPLY.read_bytes()))

    expected_ids = {path.name for path in paths}
    assert records.keys() == (expected_ids | {'invalid.xml'} if mixed_directory else expected_ids)
    for path in paths:
        record = records[path.name]
        assert record['source_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert record['response_source'] == 'mock'
        assert record['error'] is None
        assert record['extraction']['request_id'] == 'IT-001'
        assert (record['status'], record['owner'], record['next_action']) == (
            'review', 'System Owner', 'review-request',
        )
    if mixed_directory:
        invalid = records['invalid.xml']
        assert invalid['status'] == 'error'
        assert invalid['error']['category'] == 'validation'
        assert invalid['extraction'] is None
        assert invalid['response_received'] is False


def test_xml_input_still_requires_schema_valid_extraction(tmp_path):
    reply = json.loads(SAVED_REPLY.read_bytes())
    output_text = reply['output'][0]['content'][0]
    extraction = json.loads(output_text['text'])
    del extraction['request_id']
    output_text['text'] = json.dumps(extraction)

    record = run_mock_intake(XML_SAMPLE, tmp_path, reply)['IT-001.xml']

    assert record['status'] == 'error'
    assert record['extraction'] is None
    assert record['error']['category'] == 'validation'
    assert record['error']['message'].startswith('schema:')
    assert record['response_received'] is True

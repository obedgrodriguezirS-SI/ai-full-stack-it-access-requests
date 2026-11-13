"""Strict JSON, the supplied schema's constraints, and source evidence checks."""
from datetime import date
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT/'snapshot.schema.json').read_text(encoding='utf-8'))


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON property')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('non-finite JSON number')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def decode_export(raw):
    value = strict_json(raw.decode('utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('message'), str):
        raise ValueError('export must contain a message string')
    return value


def read_export(path):
    path = Path(path)
    raw = path.read_bytes()
    export = decode_export(raw)
    return dict(document_id=path.name, source_sha256=hashlib.sha256(raw).hexdigest(),
                message=export['message'], metadata={k:v for k,v in export.items() if k!='message'})


def validate_schema(value):
    """Interpret the exact keywords used by snapshot.schema.json (not a general validator)."""
    if not isinstance(value, dict):
        raise ValueError('extraction must be an object')
    if set(value) != set(SCHEMA['required']):
        raise ValueError('extraction properties differ from the supplied schema')
    for key, rule in SCHEMA['properties'].items():
        item = value[key]
        if rule['type'] == 'array':
            if not isinstance(item, list) or any(not isinstance(v, str) for v in item):
                raise ValueError(f'{key} must be an array of strings')
        elif item is not None and not isinstance(item, str):
            raise ValueError(f'{key} must be string or null')
        if 'enum' in rule and item not in rule['enum']:
            raise ValueError(f'{key} has an unsupported enum value')
    return value


def validate_facts(value, message):
    validate_schema(value)
    for key in ('request_date', 'start_date', 'end_date'):
        item = value[key]
        if item is not None:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', item):
                raise ValueError(f'{key} must use YYYY-MM-DD')
            try:
                date.fromisoformat(item)
            except ValueError:
                raise ValueError(f'{key} must be a calendar date') from None
    if any(not quote or quote not in message for quote in value['supporting_quotes']):
        raise ValueError('supporting quote is not an exact nonempty source substring')
    return value


def extract_response(response, message):
    if not isinstance(response, dict):
        raise ValueError('malformed provider envelope')
    if response.get('error') is not None:
        raise ValueError('provider error envelope')
    if response.get('status') != 'completed' or response.get('incomplete_details') is not None:
        raise ValueError('interrupted or incomplete provider response')
    output = response.get('output')
    if not isinstance(output, list) or not output:
        raise ValueError('missing provider output')
    texts = []
    for item in output:
        if not isinstance(item, dict):
            raise ValueError('malformed output item')
        if item.get('type') == 'reasoning':
            continue
        if item.get('type') != 'message' or item.get('role') != 'assistant':
            raise ValueError('unexpected output item')
        if item.get('status', 'completed') != 'completed':
            raise ValueError('interrupted message item')
        content = item.get('content')
        if not isinstance(content, list) or not content:
            raise ValueError('missing assistant content')
        for part in content:
            if not isinstance(part, dict):
                raise ValueError('malformed assistant content')
            if part.get('type') == 'refusal':
                raise ValueError('provider refusal')
            if part.get('type') != 'output_text' or not isinstance(part.get('text'), str):
                raise ValueError('missing output text')
            texts.append(part['text'])
    if len(texts) != 1:
        raise ValueError('expected exactly one extraction text')
    try:
        value = strict_json(texts[0])
    except (ValueError, TypeError):
        raise ValueError('malformed extraction JSON') from None
    return validate_facts(value, message)

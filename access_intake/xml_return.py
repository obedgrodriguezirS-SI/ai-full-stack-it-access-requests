"""Decode the UTF-8 return format used by XML message exports."""

from io import StringIO
import re
from xml.etree import ElementTree


FIELDS = frozenset({'message', 'export_version', 'exported_at', 'source_channel', 'subject'})
ENCODING_DECLARATION = re.compile(r'''<\?xml\s+[^?]*\bencoding\s*=\s*(['"])([^'"]+)\1''')


def decode_xml_return(raw):
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError as exc:
        raise ValueError('malformed UTF-8 XML export') from exc

    declaration = ENCODING_DECLARATION.match(text)
    if declaration and declaration[2].lower() != 'utf-8':
        raise ValueError('XML export encoding must be UTF-8')

    try:
        document = ElementTree.iterparse(StringIO(text), events=('start-ns',))
        for _ in document:
            raise ValueError('XML export must not declare namespaces')
    except ElementTree.ParseError as exc:
        raise ValueError('malformed UTF-8 XML export') from exc

    root = document.root
    if root.tag != 'return' or root.attrib or (root.text or '').strip():
        raise ValueError('XML export must have a return root containing fields')

    export = {}
    for field in root:
        if field.tag not in FIELDS or field.tag in export:
            raise ValueError('XML export contains an unknown or repeated field')
        if field.attrib or len(field) or (field.tail or '').strip():
            raise ValueError('XML export fields must contain only text')
        export[field.tag] = field.text or ''

    if not export.get('message', '').strip():
        raise ValueError('XML export must contain a nonblank message')
    return export

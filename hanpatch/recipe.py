"""Recipe schema v1 and its validator.

A recipe says where a title keeps its text: which address spaces exist, which
container holds the table, how the table is found, and what a row means. It is
induced from containers this project has actually parsed - not designed first
and fitted afterwards - because a schema drawn in advance describes the shape
its author imagined rather than the shape cartridges have.

Two of the shapes below exist because the first two titles refused the drawn
one:

  * `base.kind = "after_table"`. Dragon Quest VII stores each payload offset
    relative to the start of the payload region, and that region begins after
    the entry table and a 64-byte tag block. The offset is therefore anchored
    to something computed from the count, not to the member start and not to a
    constant. A union of {member_start, const, mapper} cannot express it.

  * `at.kind = "read"`. Crimson Shroud does not put its entry table at a fixed
    offset; the header says where it is. A table location is itself a field to
    be read as often as it is a constant.

Both were found by dumping first. Either would have been a schema break after
the freeze.

Errors are returned, never raised, and each one carries a JSON Pointer to the
offending node so that a submitting agent can correct itself without a human
translating the complaint.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

SCHEMA_VERSION = {'major': 1, 'minor': 0}

# Closed on purpose. A new kind is a schema change with a migration, not a
# string somebody may invent in a pull request.
SPACE_KINDS = ('file', 'member', 'lba-2048', 'lba-2352-form1', 'lba-2352-form2',
               'gba-rom', 'snes-lorom', 'snes-hirom', 'decompressed-iso', 'message-id')
TABLE_KINDS = ('offset_size', 'offset_only', 'inline_fixed')
ENDIANS = ('little', 'big')
WIDTHS = (1, 2, 4, 8)
PAYLOAD_KINDS = ('opaque', 'text')
ENCODINGS = ('ascii', 'utf-8', 'utf-16-le', 'utf-16-be', 'shift-jis', 'euc-kr', 'table')
NAME_SOURCES = ('inline', 'offset_table', 'none')

ID_PATTERN = r'[a-z0-9][a-z0-9-]*(/[a-z0-9][a-z0-9-]*){2}'


def _problem(code: str, pointer: str, expected: str, got, hint: str = '') -> Dict:
    return {'code': code, 'pointer': pointer, 'expected': expected,
            'got': got if isinstance(got, (str, int, float, bool, type(None))) else type(got).__name__,
            'hint': hint}


def _check_keys(node, allowed, required, pointer, out):
    if not isinstance(node, dict):
        out.append(_problem('not_an_object', pointer, 'object', node))
        return False
    for key in sorted(set(node) - set(allowed)):
        out.append(_problem('unknown_field', '%s/%s' % (pointer, key),
                            'one of %s' % ', '.join(sorted(allowed)), key,
                            'the schema is closed; a new field needs a migration'))
    for key in required:
        if key not in node:
            out.append(_problem('missing_field', '%s/%s' % (pointer, key), key, None))
    return True


def _check_read(node, pointer, spaces, out):
    """`{space, at, width, endian}` - a value the parser must go and fetch.

    The space is part of the address, not decoration: `at: 0` means nothing
    until it is said which space's zero is meant. Three-level nesting is where
    that ambiguity turns into a wrong pointer.
    """
    if not _check_keys(node, ('space', 'at', 'width', 'endian'),
                       ('space', 'at', 'width', 'endian'), pointer, out):
        return
    if node.get('space') not in spaces:
        out.append(_problem('unknown_space', '%s/space' % pointer,
                            'one of %s' % ', '.join(sorted(spaces)), node.get('space')))
    if not isinstance(node.get('at'), int) or node.get('at', -1) < 0:
        out.append(_problem('bad_offset', '%s/at' % pointer, 'a non-negative integer',
                            node.get('at')))
    if node.get('width') not in WIDTHS:
        out.append(_problem('bad_width', '%s/width' % pointer,
                            'one of %s' % ', '.join(str(w) for w in WIDTHS), node.get('width')))
    if node.get('endian') not in ENDIANS:
        out.append(_problem('bad_endian', '%s/endian' % pointer,
                            'one of %s' % ', '.join(ENDIANS), node.get('endian')))


def _check_locator(node, pointer, spaces, out):
    """Where a table starts: a constant, or a value the header supplies."""
    if not isinstance(node, dict) or 'kind' not in node:
        out.append(_problem('missing_field', '%s/kind' % pointer, 'kind', None,
                            'a locator is discriminated by kind, never a bare number'))
        return
    kind = node['kind']
    if kind == 'const':
        if _check_keys(node, ('kind', 'space', 'value'), ('kind', 'space', 'value'),
                       pointer, out):
            if node.get('space') not in spaces:
                out.append(_problem('unknown_space', '%s/space' % pointer,
                                    'a declared space', node.get('space')))
            if not isinstance(node.get('value'), int) or node.get('value', -1) < 0:
                out.append(_problem('bad_offset', '%s/value' % pointer,
                                    'a non-negative integer', node.get('value')))
    elif kind == 'read':
        inner = dict(node)
        inner.pop('kind')
        _check_read(inner, pointer, spaces, out)
        _check_keys(node, ('kind', 'space', 'at', 'width', 'endian'),
                    ('kind',), pointer, out)
    else:
        out.append(_problem('bad_kind', '%s/kind' % pointer, 'const or read', kind))


def _check_base(node, pointer, spaces, out):
    """What a stored offset is measured from."""
    if not isinstance(node, dict) or 'kind' not in node:
        out.append(_problem('missing_field', '%s/kind' % pointer, 'kind', None,
                            'an offset with no declared anchor is not a pointer'))
        return
    kind = node['kind']
    if kind == 'member_start':
        _check_keys(node, ('kind',), ('kind',), pointer, out)
    elif kind == 'const':
        if _check_keys(node, ('kind', 'space', 'value'), ('kind', 'space', 'value'),
                       pointer, out):
            if node.get('space') not in spaces:
                out.append(_problem('unknown_space', '%s/space' % pointer,
                                    'a declared space', node.get('space')))
    elif kind == 'mapper':
        if _check_keys(node, ('kind', 'id'), ('kind', 'id'), pointer, out):
            if node.get('id') not in SPACE_KINDS:
                out.append(_problem('unknown_mapper', '%s/id' % pointer,
                                    'a space kind from the same registry', node.get('id'),
                                    'mappers and spaces share one registry on purpose'))
    elif kind == 'after_table':
        if _check_keys(node, ('kind', 'padding'), ('kind', 'padding'), pointer, out):
            if not isinstance(node.get('padding'), int) or node.get('padding', -1) < 0:
                out.append(_problem('bad_offset', '%s/padding' % pointer,
                                    'a non-negative integer', node.get('padding'),
                                    'bytes between the end of the table and the payloads'))
    else:
        out.append(_problem('bad_kind', '%s/kind' % pointer,
                            'member_start, const, mapper or after_table', kind))


def _check_space(node, pointer, out) -> Optional[str]:
    if not _check_keys(node, ('id', 'kind', 'params'), ('id', 'kind'), pointer, out):
        return None
    if node.get('kind') not in SPACE_KINDS:
        out.append(_problem('bad_kind', '%s/kind' % pointer,
                            'one of %s' % ', '.join(SPACE_KINDS), node.get('kind')))
    return node.get('id') if isinstance(node.get('id'), str) else None


def _check_table(node, pointer, spaces, out):
    allowed = ('id', 'space', 'format', 'kind', 'at', 'stride', 'count', 'base',
               'endian', 'alignment', 'applies_to', 'name_source', 'payload', 'encoding')
    required = ('id', 'space', 'kind', 'at', 'count', 'base')
    if not _check_keys(node, allowed, required, pointer, out):
        return
    if node.get('space') not in spaces:
        out.append(_problem('unknown_space', '%s/space' % pointer,
                            'a declared space', node.get('space')))
    if node.get('kind') not in TABLE_KINDS:
        out.append(_problem('bad_kind', '%s/kind' % pointer,
                            'one of %s' % ', '.join(TABLE_KINDS), node.get('kind')))
    if 'at' in node:
        _check_locator(node['at'], '%s/at' % pointer, spaces, out)
    if 'count' in node:
        _check_read(node['count'], '%s/count' % pointer, spaces, out)
    if 'base' in node:
        _check_base(node['base'], '%s/base' % pointer, spaces, out)
    if 'endian' in node and node['endian'] not in ENDIANS:
        out.append(_problem('bad_endian', '%s/endian' % pointer,
                            'one of %s' % ', '.join(ENDIANS), node['endian']))
    if 'payload' in node and node['payload'] not in PAYLOAD_KINDS:
        out.append(_problem('bad_kind', '%s/payload' % pointer,
                            'one of %s' % ', '.join(PAYLOAD_KINDS), node['payload']))
    if 'encoding' in node and node['encoding'] not in ENCODINGS:
        out.append(_problem('bad_encoding', '%s/encoding' % pointer,
                            'one of %s' % ', '.join(ENCODINGS), node['encoding']))
    if node.get('payload') == 'text' and 'encoding' not in node:
        out.append(_problem('missing_field', '%s/encoding' % pointer, 'encoding', None,
                            'text without a declared encoding is a guess'))
    if 'name_source' in node and node['name_source'] not in NAME_SOURCES:
        out.append(_problem('bad_kind', '%s/name_source' % pointer,
                            'one of %s' % ', '.join(NAME_SOURCES), node['name_source']))
    if 'stride' in node and (not isinstance(node['stride'], int) or node['stride'] <= 0):
        out.append(_problem('bad_stride', '%s/stride' % pointer,
                            'a positive integer', node.get('stride')))


def validate(doc) -> List[Dict]:
    """Every problem, not the first one. Returns [] for a valid recipe."""
    out: List[Dict] = []
    if not _check_keys(doc, ('schema_version', 'id', 'platform', 'title',
                             'address_spaces', 'tables', 'provenance'),
                       ('schema_version', 'id', 'platform', 'address_spaces', 'tables'),
                       '', out):
        return out

    version = doc.get('schema_version')
    if not isinstance(version, dict) or version.get('major') != SCHEMA_VERSION['major']:
        out.append(_problem('bad_version', '/schema_version',
                            'major %d' % SCHEMA_VERSION['major'], version,
                            'a different major needs a migration, not a validator'))

    import re
    if not isinstance(doc.get('id'), str) or not re.fullmatch(ID_PATTERN, doc.get('id', '')):
        out.append(_problem('bad_id', '/id', 'platform/vendor/title in lowercase',
                            doc.get('id'),
                            'file paths are derived from the id, so it is not free text'))

    spaces = set()
    raw_spaces = doc.get('address_spaces')
    if not isinstance(raw_spaces, list) or not raw_spaces:
        out.append(_problem('empty_list', '/address_spaces', 'at least one space',
                            raw_spaces))
    else:
        for index, space in enumerate(raw_spaces):
            found = _check_space(space, '/address_spaces/%d' % index, out)
            if found:
                if found in spaces:
                    out.append(_problem('duplicate_id', '/address_spaces/%d/id' % index,
                                        'a unique id', found))
                spaces.add(found)

    raw_tables = doc.get('tables')
    if not isinstance(raw_tables, list) or not raw_tables:
        out.append(_problem('empty_list', '/tables', 'at least one table', raw_tables))
    else:
        seen = set()
        for index, table in enumerate(raw_tables):
            _check_table(table, '/tables/%d' % index, spaces, out)
            table_id = table.get('id') if isinstance(table, dict) else None
            if table_id in seen:
                out.append(_problem('duplicate_id', '/tables/%d/id' % index,
                                    'a unique id', table_id))
            seen.add(table_id)

    return out


def json_schema() -> Dict:
    """The same rules as a published artifact, so a contributor's tooling can
    read them without importing Python."""
    read = {
        'type': 'object', 'additionalProperties': False,
        'required': ['space', 'at', 'width', 'endian'],
        'properties': {
            'space': {'type': 'string'},
            'at': {'type': 'integer', 'minimum': 0},
            'width': {'enum': list(WIDTHS)},
            'endian': {'enum': list(ENDIANS)},
        },
    }
    return {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': 'https://github.com/yazzang-homelab/hanpatch/schemas/recipe.schema.json',
        'title': 'hanpatch recipe v%d.%d' % (SCHEMA_VERSION['major'], SCHEMA_VERSION['minor']),
        'type': 'object', 'additionalProperties': False,
        'required': ['schema_version', 'id', 'platform', 'address_spaces', 'tables'],
        'properties': {
            'schema_version': {
                'type': 'object', 'additionalProperties': False,
                'required': ['major', 'minor'],
                'properties': {'major': {'const': SCHEMA_VERSION['major']},
                               'minor': {'type': 'integer', 'minimum': 0}},
            },
            'id': {'type': 'string', 'pattern': '^%s$' % ID_PATTERN},
            'platform': {'type': 'string'},
            'title': {'type': 'object'},
            'provenance': {'type': 'object'},
            'address_spaces': {
                'type': 'array', 'minItems': 1,
                'items': {
                    'type': 'object', 'additionalProperties': False,
                    'required': ['id', 'kind'],
                    'properties': {'id': {'type': 'string'},
                                   'kind': {'enum': list(SPACE_KINDS)},
                                   'params': {'type': 'object'}},
                },
            },
            'tables': {
                'type': 'array', 'minItems': 1,
                'items': {
                    'type': 'object', 'additionalProperties': False,
                    'required': ['id', 'space', 'kind', 'at', 'count', 'base'],
                    'properties': {
                        'id': {'type': 'string'},
                        'space': {'type': 'string'},
                        'format': {'type': 'string'},
                        'kind': {'enum': list(TABLE_KINDS)},
                        'stride': {'type': 'integer', 'minimum': 1},
                        'endian': {'enum': list(ENDIANS)},
                        'alignment': {'type': 'integer', 'minimum': 1},
                        'applies_to': {'type': 'array', 'items': {'type': 'string'}},
                        'name_source': {'enum': list(NAME_SOURCES)},
                        'payload': {'enum': list(PAYLOAD_KINDS)},
                        'encoding': {'enum': list(ENCODINGS)},
                        'count': read,
                        'at': {'oneOf': [
                            {'type': 'object', 'additionalProperties': False,
                             'required': ['kind', 'space', 'value'],
                             'properties': {'kind': {'const': 'const'},
                                            'space': {'type': 'string'},
                                            'value': {'type': 'integer', 'minimum': 0}}},
                            {'type': 'object', 'additionalProperties': False,
                             'required': ['kind', 'space', 'at', 'width', 'endian'],
                             'properties': {'kind': {'const': 'read'},
                                            'space': {'type': 'string'},
                                            'at': {'type': 'integer', 'minimum': 0},
                                            'width': {'enum': list(WIDTHS)},
                                            'endian': {'enum': list(ENDIANS)}}},
                        ]},
                        'base': {'oneOf': [
                            {'type': 'object', 'additionalProperties': False,
                             'required': ['kind'],
                             'properties': {'kind': {'const': 'member_start'}}},
                            {'type': 'object', 'additionalProperties': False,
                             'required': ['kind', 'space', 'value'],
                             'properties': {'kind': {'const': 'const'},
                                            'space': {'type': 'string'},
                                            'value': {'type': 'integer'}}},
                            {'type': 'object', 'additionalProperties': False,
                             'required': ['kind', 'id'],
                             'properties': {'kind': {'const': 'mapper'},
                                            'id': {'enum': list(SPACE_KINDS)}}},
                            {'type': 'object', 'additionalProperties': False,
                             'required': ['kind', 'padding'],
                             'properties': {'kind': {'const': 'after_table'},
                                            'padding': {'type': 'integer', 'minimum': 0}}},
                        ]},
                    },
                },
            },
        },
    }


def from_facts(facts: Dict) -> Dict:
    """Turn an adapter's observed facts into a recipe document.

    The adapter reports what it measured; the shape is the schema's business.
    """
    return {
        'schema_version': dict(SCHEMA_VERSION),
        'id': facts['id'],
        'platform': facts['platform'],
        'title': {'name': facts.get('title', '')},
        'address_spaces': list(facts.get('address_spaces', [])),
        'tables': list(facts.get('tables', [])),
        'provenance': {'independent_measurement': list(facts.get('measured', []))},
    }

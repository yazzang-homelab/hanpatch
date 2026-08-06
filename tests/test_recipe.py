"""Adversarial regression tests for the recipe schema.

Run:  python3 tests/test_recipe.py

The schema is the only thing standing between a submitted JSON file and the
project treating it as knowledge, so every case here is a document that should
not be believed.
"""
import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch import adapter, recipe  # noqa: E402
from tools import recipe_dump  # noqa: E402

PASS = []
FAIL = []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def sec(title):
    print()
    print(title)


def codes(doc):
    return sorted(p['code'] for p in recipe.validate(doc))


def pointers(doc):
    return sorted(p['pointer'] for p in recipe.validate(doc))


BASE = {
    'schema_version': {'major': 1, 'minor': 0},
    'id': 'threeds/square-enix/dragon-quest-vii',
    'platform': 'threeds',
    'title': {'name': 'Dragon Quest VII'},
    'address_spaces': [
        {'id': 'rom', 'kind': 'file'},
        {'id': 'fpt', 'kind': 'member', 'params': {'parent': 'rom'}},
    ],
    'tables': [{
        'id': 'entries', 'space': 'fpt', 'kind': 'offset_size', 'stride': 32,
        'endian': 'little',
        'at': {'kind': 'const', 'space': 'fpt', 'value': 16},
        'count': {'space': 'fpt', 'at': 8, 'width': 4, 'endian': 'little'},
        'base': {'kind': 'after_table', 'padding': 64},
        'payload': 'opaque',
    }],
}


def mutate(**changes):
    doc = copy.deepcopy(BASE)
    doc.update(changes)
    return doc


def table(**changes):
    doc = copy.deepcopy(BASE)
    doc['tables'][0].update(changes)
    return doc


sec('the induced document is the one that validates')

case('the reference document is valid', recipe.validate(BASE) == [])
case('an adapter with no facts says so, rather than inventing them',
     adapter.get('crimson_shroud').recipe_facts() is not None)

for name in adapter.available():
    facts = adapter.get(name).recipe_facts()
    if facts:
        case('%s facts validate as a recipe' % name,
             recipe.validate(recipe.from_facts(facts)) == [])


sec('the schema is closed')

case('an invented top-level field is refused',
     'unknown_field' in codes(mutate(notes='hello')))
case('an invented table field is refused',
     'unknown_field' in codes(table(compression='lz77')))
case('and the error points at the offending field',
     '/tables/0/compression' in pointers(table(compression='lz77')))
case('a missing table list is refused', 'missing_field' in codes(
     {k: v for k, v in BASE.items() if k != 'tables'}))
case('an empty table list is refused', 'empty_list' in codes(mutate(tables=[])))
case('an empty space list is refused', 'empty_list' in codes(mutate(address_spaces=[])))


sec('an offset means nothing until its space is named')

case('a table in an undeclared space is refused',
     'unknown_space' in codes(table(space='nowhere')))
case('a count read from an undeclared space is refused',
     'unknown_space' in codes(table(count={'space': 'ghost', 'at': 8, 'width': 4,
                                           'endian': 'little'})))
case('a count with no space is refused',
     'missing_field' in codes(table(count={'at': 8, 'width': 4, 'endian': 'little'})))
case('a three byte read is refused',
     'bad_width' in codes(table(count={'space': 'fpt', 'at': 8, 'width': 3,
                                       'endian': 'little'})))
case('a negative offset is refused',
     'bad_offset' in codes(table(count={'space': 'fpt', 'at': -1, 'width': 4,
                                        'endian': 'little'})))
case('a middle-endian read is refused',
     'bad_endian' in codes(table(count={'space': 'fpt', 'at': 8, 'width': 4,
                                        'endian': 'middle'})))


sec('a stored offset must say what it is measured from')

case('member_start is accepted', recipe.validate(table(base={'kind': 'member_start'})) == [])
case('a constant base must name its space',
     'missing_field' in codes(table(base={'kind': 'const', 'value': 0x08000000})))
case('a GBA absolute base is accepted',
     recipe.validate(table(base={'kind': 'const', 'space': 'fpt',
                                 'value': 0x08000000})) == [])
case('a mapper base is accepted',
     recipe.validate(table(base={'kind': 'mapper', 'id': 'snes-lorom'})) == [])
case('a mapper outside the space registry is refused',
     'unknown_mapper' in codes(table(base={'kind': 'mapper', 'id': 'my-mapper'})))
case('after_table needs its padding stated',
     'missing_field' in codes(table(base={'kind': 'after_table'})))
case('a base with no kind is refused', 'missing_field' in codes(table(base={'value': 4})))
case('a bare number is not a base', 'missing_field' in codes(table(base=16)))
case('an unknown base kind is refused',
     'bad_kind' in codes(table(base={'kind': 'whatever'})))


sec('a table location is a locator, not always a number')

case('a constant location is accepted',
     recipe.validate(table(at={'kind': 'const', 'space': 'fpt', 'value': 16})) == [])
case('a location the header supplies is accepted',
     recipe.validate(table(at={'kind': 'read', 'space': 'fpt', 'at': 4, 'width': 4,
                               'endian': 'little'})) == [])
case('a bare number is not a location', 'missing_field' in codes(table(at=16)))
case('an unknown locator kind is refused',
     'bad_kind' in codes(table(at={'kind': 'magic'})))


sec('text without a declared encoding is a guess')

case('an opaque payload needs no encoding',
     recipe.validate(table(payload='opaque')) == [])
case('a text payload without an encoding is refused',
     'missing_field' in codes(table(payload='text')))
case('a text payload with an encoding is accepted',
     recipe.validate(table(payload='text', encoding='utf-16-le')) == [])
case('an invented encoding is refused',
     'bad_encoding' in codes(table(payload='text', encoding='utf-9')))


sec('identity and versions')

case('a free-text id is refused', 'bad_id' in codes(mutate(id='Dragon Quest VII')))
case('a two-part id is refused', 'bad_id' in codes(mutate(id='threeds/dq7')))
case('an uppercase id is refused',
     'bad_id' in codes(mutate(id='threeds/Square-Enix/dq7')))
case('a different major version is refused, not migrated silently',
     'bad_version' in codes(mutate(schema_version={'major': 2, 'minor': 0})))
case('duplicate space ids are refused',
     'duplicate_id' in codes(mutate(address_spaces=[
         {'id': 'fpt', 'kind': 'file'}, {'id': 'fpt', 'kind': 'file'}])))
case('duplicate table ids are refused',
     'duplicate_id' in codes({**copy.deepcopy(BASE),
                              'tables': BASE['tables'] * 2}))


sec('every problem, with a pointer, not just the first')

BROKEN = table(space='nowhere', endian='middle', stride=0)
case('three faults produce three problems', len(recipe.validate(BROKEN)) >= 3)
case('each problem carries a pointer',
     all(p['pointer'].startswith('/') for p in recipe.validate(BROKEN)))
case('each problem says what was expected',
     all(p['expected'] for p in recipe.validate(BROKEN)))
case('each problem carries a machine-readable code',
     all(p['code'] and ' ' not in p['code'] for p in recipe.validate(BROKEN)))


sec('the published schema matches the validator')

SCHEMA = recipe.json_schema()
case('the published schema closes its objects',
     SCHEMA['additionalProperties'] is False)
case('it offers the same four bases the validator accepts',
     sorted(v['properties']['kind']['const']
            for v in SCHEMA['properties']['tables']['items']['properties']['base']['oneOf'])
     == ['after_table', 'const', 'mapper', 'member_start'])
case('it offers the same two locators',
     sorted(v['properties']['kind']['const']
            for v in SCHEMA['properties']['tables']['items']['properties']['at']['oneOf'])
     == ['const', 'read'])
case('it pins the major version',
     SCHEMA['properties']['schema_version']['properties']['major']['const']
     == recipe.SCHEMA_VERSION['major'])


sec('the dumped tree is the adapters, not a hand edit')

RENDERED = recipe_dump.render()
case('the schema is written from the validator, not maintained twice',
     json.loads(RENDERED[recipe_dump.SCHEMA_PATH]) == SCHEMA)
case('both measured titles are dumped', len(RENDERED) == 3)
for rel, text in sorted(RENDERED.items()):
    path = os.path.join(ROOT, rel)
    on_disk = open(path, encoding='utf-8').read() if os.path.exists(path) else None
    case('%s on disk matches its adapter' % rel, on_disk == text)
case('a dumped recipe records how it was measured',
     all('independent_measurement' in json.loads(text).get('provenance', {})
         for rel, text in RENDERED.items() if rel.startswith('recipes/')))
case('the measurements are not empty',
     all(json.loads(text)['provenance']['independent_measurement']
         for rel, text in RENDERED.items() if rel.startswith('recipes/')))

print()
print(f'{len(PASS)} passed, {len(FAIL)} failed')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
sys.exit(1 if FAIL else 0)

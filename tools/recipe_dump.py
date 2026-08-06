"""Dump every adapter's observed container facts as a recipe, and the schema.

Stage zero of the plan is "dump the observations first, induce the schema from
their union" - in that order, because a schema drawn before the data describes
the shape its author imagined. This is the command that does the dumping.

    python3 tools/recipe_dump.py            # write schemas/ and recipes/
    python3 tools/recipe_dump.py --check    # fail if the tree is out of date

`--check` is what a workflow runs: it regenerates into memory and compares, so
a recipe edited by hand without the adapter that justifies it cannot survive.

The CLI is deliberately not touched here. `hanpatch/cli.py` has uncommitted
work in it from another line of development, and mixing an unrelated change
into a dirty file is how one person's half-finished work gets committed by
somebody else.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch import adapter, recipe  # noqa: E402

SCHEMA_PATH = os.path.join('schemas', 'recipe.schema.json')
RECIPE_DIR = 'recipes'


def render():
    """Everything this repository should contain, as {relative path: text}."""
    out = {SCHEMA_PATH: _json(recipe.json_schema())}
    for name in adapter.available():
        facts = adapter.get(name).recipe_facts()
        if not facts:
            continue
        doc = recipe.from_facts(facts)
        problems = recipe.validate(doc)
        if problems:
            raise SystemExit('%s: adapter facts do not validate:\n%s'
                             % (name, _json(problems)))
        out[os.path.join(RECIPE_DIR, '%s.json' % doc['id'].replace('/', '__'))] = _json(doc)
    return out


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + '\n'


def main(argv) -> int:
    check = '--check' in argv
    wanted = render()
    stale = []
    for rel, text in sorted(wanted.items()):
        path = os.path.join(ROOT, rel)
        current = None
        if os.path.exists(path):
            with open(path, encoding='utf-8') as fh:
                current = fh.read()
        if current == text:
            print('  ok      %s' % rel)
            continue
        stale.append(rel)
        if check:
            print('  STALE   %s' % rel)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        print('  written %s' % rel)

    if check and stale:
        print('\n%d file(s) do not match the adapters they came from.' % len(stale))
        print('Run: python3 tools/recipe_dump.py')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

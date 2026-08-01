"""Expand every rule-derived translation into an explicit, validated TM row.

`tm.lookup()` can synthesise `base + " II"` / `base + " A"` forms. Those must not
reach the ROM unvalidated, so they are written out as real entries and run
through the same gates as model output.
"""
import json
import os
import sys


from hanpatch import glossary
from hanpatch import tm
from hanpatch import capacity as capmod  # noqa: E402
from hanpatch import translate

from hanpatch import config

LAST_EXAMINED = 0


def OUT():
    return config.out('tm_derived.json')


def main():
    global LAST_EXAMINED
    src = config.load_object(config.src_path(), 'the extracted source')
    base = {}
    if os.path.exists(tm.TM_PATH()):
        base.update(config.load_object(tm.TM_PATH(), 'the primary translation memory'))
    import glob
    for p in sorted(glob.glob(config.out('tm_*.json'))):
        if p.endswith('tm_derived.json'):
            continue
        base.update(config.load_object(p, 'the translation memory shard'))
    gl = glossary.load()
    derived, bad = {}, []
    examined = 0
    for family, items in src.items():
        for it in items:
            en = it['en']
            if tm.is_skip(en, it['key']) or not en.strip() or en in base:
                continue
            ko = tm.lookup(base, en)
            if ko is None:
                continue
            examined += 1
            ko2, probs = translate.check(
                en, ko, glossary.relevant(gl, [en], family), family,
                capmod.group(family, it['key']))
            if probs:
                bad.append((f"{family}:{it['key']}", probs))
            else:
                derived[en] = ko2
    json.dump(derived, open(OUT(), 'w'), ensure_ascii=False, indent=1, sort_keys=True)
    print(f'materialised {len(derived)} rule-derived entries -> {OUT()}')
    for k, p in bad[:20]:
        print(f'  INVALID {k}: {p}')
    print(f'invalid derived entries: {len(bad)}')
    LAST_EXAMINED = examined
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())

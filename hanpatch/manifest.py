"""Build the immutable, fully validated per-key build manifest.

This is the single source of truth the ROM is built from. It resolves override
precedence once, validates every effective value with the full ruleset, and
seals the result with a digest so nothing can change between audit and build.
"""
import hashlib
import json
import os
import sys
import time


from hanpatch import capacity as capmod  # noqa: E402
from hanpatch import glossary
from hanpatch import tm
from hanpatch import translate

from hanpatch import config

LAST_EXAMINED = 0


def PATH():
    return config.out('manifest.json')
def OVERRIDE():
    return config.out('text_%s.json' % config.target())
RULESET = '2'


def digest(entries):
    h = hashlib.sha256()
    for k in sorted(entries):
        h.update(k.encode())
        h.update(b'\0')
        h.update(entries[k].encode())
        h.update(b'\0')
    return h.hexdigest()


def build(src_path=None):
    global LAST_EXAMINED
    src_path = src_path or config.src_path()
    glossary.assert_complete()
    src = config.load_object(src_path, 'the extracted source')
    tmdb = tm.load()
    gl = glossary.load()
    override = (config.load_object(OVERRIDE(), 'the manifest override')
                if os.path.exists(OVERRIDE()) else {})

    valid_keys = {(f, it['key']) for f, items in src.items() for it in items}
    problems = []
    for fam, table in override.items():
        for key in table:
            if (fam, key) not in valid_keys:
                problems.append(f'override for unknown key {fam}:{key}')

    entries = {}
    for fam, items in src.items():
        for it in items:
            en, key = it['en'], it['key']
            ov = override.get(fam, {}).get(key)
            skip = tm.is_skip(en, key) or not en.strip()
            if skip:
                # An override on a placeholder English row is only legitimate
                # when the Japanese source proves the engine really uses the key
                # (the English title.mbin rows are dev junk, the JP ones are UI).
                jp = it.get('jp', '')
                if ov is None:
                    continue
                if not jp.strip() or tm.is_skip(jp, key):
                    problems.append(
                        f'override targets non-shippable key {fam}:{key}')
                    continue
                # validate against the Japanese row, which is the real source
                # of record for these keys
                ko2, probs = translate.check(jp, ov,
                                             glossary.relevant(gl, [jp], fam),
                                             fam, capmod.group(fam, key))
                if probs:
                    problems.append(f'{fam}:{key} (override) :: {probs}')
                    continue
                entries[f'{fam}/{key}'] = ko2
                continue
            ko = ov if ov is not None else tm.lookup(tmdb, en)
            if ko is None:
                problems.append(f'no translation for {fam}:{key}')
                continue
            ko2, probs = translate.check(en, ko, glossary.relevant(gl, [en], fam),
                                         fam, capmod.group(fam, key))
            if probs:
                problems.append(f'{fam}:{key} :: {probs}')
                continue
            entries[f'{fam}/{key}'] = ko2

    if problems:
        print(f'MANIFEST REJECTED: {len(problems)} problems')
        for p in problems[:25]:
            print(f'  {p}')
        return None

    doc = {
        'ruleset': RULESET,
        'generated': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'count': len(entries),
        'digest': digest(entries),
        'entries': entries,
    }
    tmp = PATH() + '.tmp'
    with open(tmp, 'w') as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, PATH())
    print(f'manifest: {len(entries)} entries, digest {doc["digest"][:16]} -> {PATH()}')
    LAST_EXAMINED = doc['count']
    return doc


def load():
    doc = config.load_object(PATH(), 'the sealed manifest')
    # Shape before content: `load_object` proves the document is an object, not
    # that it is THIS document. Subscripting a manifest that has no `entries`
    # raised a bare KeyError in the packer, the releaser and the scriptbook.
    missing = [k for k in ('entries', 'digest') if k not in doc]
    if missing:
        raise SystemExit(f'the sealed manifest is missing {", ".join(missing)}: '
                         f'{PATH()}; run `hanpatch gates` to reseal it')
    if not isinstance(doc['entries'], dict):
        raise SystemExit(f'the sealed manifest entries must be a JSON object, '
                         f'got {type(doc["entries"]).__name__}: {PATH()}')
    if digest(doc['entries']) != doc['digest']:
        raise SystemExit('MANIFEST DIGEST MISMATCH: rebuild before packing')
    if doc.get('ruleset') != RULESET:
        raise SystemExit(f'MANIFEST RULESET {doc.get("ruleset")} != {RULESET}')
    return doc


if __name__ == '__main__':
    sys.exit(0 if build() else 1)

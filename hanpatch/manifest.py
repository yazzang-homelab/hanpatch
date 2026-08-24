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
# 3: Korean punctuation and josa are resolved deterministically on the way in -
# the kuten no longer doubles a sentence end, a particle after a runtime
# substitution is written in a form correct for every value it can take, and a
# line may not break inside a word or open on a closing mark. A manifest sealed
# under ruleset 2 holds text those rules would change, so it must be resealed
# rather than shipped.
RULESET = '3'


def digest(entries):
    h = hashlib.sha256()
    for k in sorted(entries):
        h.update(k.encode())
        h.update(b'\0')
        h.update(entries[k].encode())
        h.update(b'\0')
    return h.hexdigest()


def override_table():
    """The manifest override document, or {} when there is none."""
    return (config.load_object(OVERRIDE(), 'the manifest override')
            if os.path.exists(OVERRIDE()) else {})


def candidate(fam, it, override=None, tmdb=None):
    """What this row WILL seal as, and the source it is validated against.

    Returns `(source_text, raw_ko)` before `translate.check`, or `(source, None)`
    when nothing would be sealed. This exists so a caller that needs to know the
    sealed value in advance - review, which keys its evidence on it - reads the
    same precedence `build` applies instead of guessing:

      * the source of record is `tm.source_of(it)`: Japanese for a skip/blank
        English row, English otherwise
      * an OVERRIDE beats the translation memory. A row translated and reviewed as
        ko1 seals as ko2 when an override exists, and the verdict for ko1 is then
        invisible to the gate - which is why review must consult this rather than
        the translator's own output.
    """
    override = override_table() if override is None else override
    if tmdb is None:
        tmdb = tm.load()
    en, key = it.get('en', ''), it['key']
    source = tm.source_of(it)
    ov = override.get(fam, {}).get(key)
    if tm.is_skip(en, key) or not en.strip():
        # Only an override ships such a row, and only when the Japanese proves the
        # engine really uses the key.
        if ov is None:
            return source, None
        jp = it.get('jp', '')
        if not jp.strip() or tm.is_skip(jp, key):
            return source, None
        return jp, ov
    return source, (ov if ov is not None else tm.lookup(tmdb, en))


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
    # The seal is what makes a repair visible to the repair selector again: it compares a
    # verdict against the SEALED value, so until now every row repaired since the previous
    # seal had to be skipped to avoid paying for it twice. Clear that record here, at the
    # one moment it stops being true.
    from hanpatch import run as runmod
    runmod.clear_repaired()
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

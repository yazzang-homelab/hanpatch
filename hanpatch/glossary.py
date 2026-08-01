"""Build and enforce the authoritative EN->KO term glossary.

Terms come from the hand-fixed name tables (characters, places, enemies, weapons,
items, spells, status effects). They are injected into every batch prompt that
mentions them and re-checked after generation, so wording never drifts.
"""
import hashlib
import json
import threading
import os
import re
import sys

from hanpatch import tm

from hanpatch import config

LAST_EXAMINED = 0


def GLOSSARY_PATH():
    return config.out('glossary.json')

# key patterns whose entries are proper nouns / fixed UI terms, from the profile
NAME_KEYS = [tuple(x) for x in config.prof('name_keys')]
# force-fixed terms that only appear inside prose
EXTRA = dict(config.prof('terms'))


# Short, polysemous UI/status labels: mandatory only in the families that
# actually render them as labels, never inside narrative prose.
UI_ONLY_FAMILIES = set(config.prof('ui_only_families'))
UI_ONLY_TERMS = set(config.prof('ui_only_terms'))


# Terms whose Korean form is contractually fixed (proper nouns / place names).
# Everything else in the glossary is a prompt hint only, so prose can inflect.
HARD_FAMILIES = set(config.prof('hard_families'))


def _eligible(it, pat):
    """Whether a name-table row may reach the term lookup.

    One predicate for all three consumers — `build`, `hard_terms` and the gate's
    examined-input count. When they disagreed, a hard-family row whose text
    equals its key could be promoted to a hard term the built glossary never
    contained, and `assert_complete` then failed the run for a term nothing
    could satisfy.
    """
    en = it['en']
    return bool(re.fullmatch(pat, it['key'])
                and not tm.is_skip(en, it['key'])
                and en.strip()
                and len(en) <= 48)


def hard_terms(src_path=None):
    src_path = src_path or config.src_path()
    hard = {}
    # The corpus-derived half needs the extracted rows; the profile-declared half
    # does not, so a missing corpus must not silently drop declared terms.
    if os.path.exists(src_path):
        src = config.load_object(src_path, 'the extracted source')
        tmdb = tm.load()
        for fn, pat in NAME_KEYS:
            if fn not in HARD_FAMILIES:
                continue
            for it in src.get(fn, []):
                if _eligible(it, pat):
                    ko = tm.lookup(tmdb, it['en'])
                    if ko:
                        hard[it['en']] = ko
    declared = set(config.prof('hard_terms') or ())
    if config.source_lang() == 'en':
        # Orthographic promotion, retained verbatim so the reference title stays
        # bit-identical: `glossary.hard()` feeds `josa.fix_after` and therefore
        # the sealed bytes.
        hard.update({k: v for k, v in EXTRA.items()
                     if k in declared
                     or k[:1].isupper()
                     or k.startswith(('dark', 'gnome', 'wyrm', 'paling'))})
    else:
        # CJK is caseless, so `'勇者'[:1].isupper()` is False and orthography
        # cannot promote anything. Promotion has to be declared in the profile.
        hard.update({k: v for k, v in EXTRA.items() if k in declared})
    return hard


def build(src_path=None):
    global LAST_EXAMINED
    src_path = src_path or config.src_path()
    src = config.load_object(src_path, 'the extracted source')
    tmdb = tm.load()
    gl = {}
    examined = 0
    for fn, pat in NAME_KEYS:
        for it in src.get(fn, []):
            if not _eligible(it, pat):
                continue
            # Floors count only rows eligible to reach the term-table lookup.
            examined += 1
            ko = tm.lookup(tmdb, it['en'])
            if ko:
                gl[it['en']] = ko
    gl.update(EXTRA)
    _write_atomic(GLOSSARY_PATH(), gl)
    LAST_EXAMINED = examined
    return gl


def _write_atomic(path, obj):
    """Write JSON so a concurrent reader never sees a partial file.

    `json.dump(obj, open(path, 'w'))` truncates first and streams after, so any reader that
    opens the file mid-write gets a prefix - and with N translator processes each rebuilding
    the glossary, one of them WILL be reading while another writes. Observed: the progress
    watcher died on
    `the built glossary is not valid JSON: ... Expecting ':' delimiter: line 304`.
    A temp file plus rename makes the swap atomic on the same filesystem; the pid and thread
    in the temp name keep two writers from colliding on the temp itself.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    with open(tmp, 'w') as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def load():
    """Authoritative glossary: the generated table with EXTRA merged on top.

    EXTRA is the source of truth in code, so a stale generated JSON can never
    silence a term.
    """
    if os.path.exists(GLOSSARY_PATH()):
        gl = config.load_object(GLOSSARY_PATH(), 'the built glossary')
    else:
        gl = build()
    gl.update(EXTRA)
    return gl


def assert_complete():
    """Every hard term must be present in the loaded glossary."""
    gl = load()
    missing = {k: v for k, v in hard().items() if gl.get(k) != v}
    if missing:
        raise SystemExit(f'GLOSSARY INCOMPLETE: {missing}')
    return len(gl)


_HARD = None

def reset():
    """Re-derive the profile-dependent term tables (called by `config.set_root`).

    The name-key patterns, the declared term table and the hard-term cache are
    all per-title facts read at import time. A stale cache would enforce the
    previous title's fixed renderings against the new title's rows.
    """
    global NAME_KEYS, EXTRA, UI_ONLY_FAMILIES, UI_ONLY_TERMS, HARD_FAMILIES, _HARD
    NAME_KEYS = [tuple(x) for x in config.prof('name_keys')]
    EXTRA = dict(config.prof('terms'))
    UI_ONLY_FAMILIES = set(config.prof('ui_only_families'))
    UI_ONLY_TERMS = set(config.prof('ui_only_terms'))
    HARD_FAMILIES = set(config.prof('hard_families'))
    _HARD = None


def hard(src_path=None):
    src_path = src_path or config.src_path()
    global _HARD
    if _HARD is None:
        h = hard_terms(src_path)
        # Validate the declaration once, where it is cheap and loud, rather than letting
        # an uncheckable obligation ride through every gate run reporting success.
        # `h` already maps term -> mandated rendering; calling load() here forced a
        # glossary build and made every caller depend on an extracted source that
        # `translate.check` does not need.
        _refuse_unenforceable(h, h)
        _HARD = h
    return _HARD


def enforced_contract(src_path=None):
    """The part of the anchor a passed translation was actually validated against.

    Only `hard_terms` gate a translation. A soft term is offered to the prompt and
    nothing more, so changing, adding or removing one cannot invalidate a translation
    that already passed - there was no obligation to break. Scoping the contract to the
    enforced set is what makes an anchor edit a partial re-sweep instead of a full
    retranslation: renaming 300 hints costs nothing, and changing one enforced name
    costs only the strings that contain it.
    """
    gl = build(src_path)
    return {t: gl[t] for t in hard(src_path) if t in gl}


def anchor_version(src_path=None):
    """Short digest of the enforced contract. Stable across dict ordering."""
    blob = json.dumps(enforced_contract(src_path), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def record_version(src_path=None):
    """Snapshot the current enforced contract under its digest, and return the digest.

    The snapshots are what makes a later diff possible. Without them a version change
    tells you only THAT the contract moved, so every entry would have to be re-swept;
    with them the changed terms are known and only the strings containing one are
    affected.
    """
    ver = anchor_version(src_path)
    path = config.out('anchor-versions.json')
    known = {}
    if os.path.exists(path):
        known = config.load_object(path, 'the anchor version log')
    if ver not in known:
        known[ver] = enforced_contract(src_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_atomic(path, known)
    return ver


def resweep(sources, recorded, src_path=None):
    """Which already-translated sources must be revalidated after an anchor edit?

    `recorded` maps source string -> the anchor version it passed under.

    A source needs re-sweeping when the obligations that apply to IT changed: a term
    whose enforced Korean form differs between the two contracts, a term newly enforced,
    or a term that was enforced and no longer is. A source with no recorded version is
    re-swept unconditionally - an unknown contract is not a passed contract.
    """
    cur_ver = anchor_version(src_path)
    cur = enforced_contract(src_path)
    path = config.out('anchor-versions.json')
    known = config.load_object(path, 'the anchor version log') if os.path.exists(path) else {}
    out = []
    changed_cache = {}
    for s in sources:
        ver = recorded.get(s)
        if ver == cur_ver:
            continue
        if ver is None or ver not in known:
            out.append(s)
            continue
        if ver not in changed_cache:
            old = known[ver]
            changed_cache[ver] = {t for t in set(old) | set(cur)
                                  if old.get(t) != cur.get(t)}
        if any(matches_term(t, s) for t in changed_cache[ver]):
            out.append(s)
    return out


# A mandate may not be the TAIL of a longer word. The right-hand side is deliberately
# not checked: Korean agglutinates, and the reference corpus shows a mandated name
# legitimately continuing into 에서, 족의, 당했다, 였다, 라는, 와의 - enumerating what may
# follow is a losing game, and guessing wrong fails a correct translation.
_KO_WORDCH = re.compile(r'[\uac00-\ud7a3A-Za-z]')


def mandate_present(koterm, flat):
    """Is the mandated Korean rendering in `flat` as that word, not inside another?

    A plain substring test made the check vacuous for short names: mandating 장 was
    satisfied by 족장, 장로 and 시장, so the gate reported a pass while proving nothing.
    Requiring the left edge to be a word start fixes the real cases (족장, 시장, 수정
    마석) without inventing a Korean morphology model.

    A ONE-syllable mandate is not decidable this way in either direction - 잔 sits inside
    잔인 with a clean left edge - so `hard_terms` refuses to accept one at declaration
    time instead of pretending here. See `_refuse_unenforceable`.
    """
    if not koterm:
        return False
    start = 0
    while True:
        i = flat.find(koterm, start)
        if i < 0:
            return False
        if not i or not _KO_WORDCH.match(flat[i - 1]):
            return True
        start = i + 1


def _refuse_unenforceable(gl, hard):
    """A single-syllable enforced rendering is an obligation the gate cannot check.

    Enforcement is substring-based. A one-syllable Korean form is contained in ordinary
    words (잔 in 잔인, 장 in 족장/장로/장비) and Korean agglutination rules out a
    right-hand boundary test, so such a mandate passes on text that never names the
    thing. Refusing at declaration is the honest place: the title must either anchor on a
    longer unit that the source offers, or keep the term as a hint and let the rendering
    report cover it.
    """
    bad = sorted(t for t in hard if len(gl.get(t, '')) == 1)
    if bad:
        raise SystemExit(
            'UNENFORCEABLE HARD TERMS: ' + ', '.join(f'{t}->{gl[t]}' for t in bad) +
            ' - a one-syllable rendering is contained in ordinary Korean words, so the '
            'substring gate would pass on text that never names it. Anchor on a longer '
            'unit or move the term out of hard_terms.')


def matches_term(term, blob, fold_case=False):
    """True when `term` occurs in `blob` as a whole term, per the source language.

    `en` keeps word-boundary semantics. `ja` cannot use them: Python's `\\w`
    includes kana and kanji, so a term embedded in spaceless Japanese has word
    characters on both sides and `\\b` never matches. For a script with no word
    separators a substring test IS the whole-term test.

    `fold_case` is the caller's contract, not a convenience. OFFERING a term to
    a prompt is case-insensitive because the source may capitalise it anywhere,
    while ENFORCING a fixed rendering is case-sensitive: `Paling` the place name
    and `paling` the common noun are different obligations. Case is meaningless
    in kana/kanji, so the flag has no effect for `ja`.
    """
    if config.source_lang() == 'ja':
        return term in blob
    flags = re.I if fold_case else 0
    return re.search(r'\b' + re.escape(term) + r'\b', blob, flags) is not None


def enforcement_blob(texts):
    """Join `texts` the way whole-term matching needs for this source language.

    A spaceless script has to JOIN: markup and row breaks carry no glyph, so a
    term split by a tag would otherwise hide from both the relevance prefilter
    and enforcement, which is exactly how `\\b` retired this gate for Japanese.

    A space-separated source is left EXACTLY as it was. Normalising it would
    change the reference title's sealed behaviour, and measurement shows that is
    not a cosmetic difference: flattening its line breaks makes the gate strict
    enough to expose two long-standing terminology omissions in that corpus
    (`dialogue/mes_ch3_7_001`, `dialogue/mes_ch4_3_021`, both spelling a
    multiword term across a wrapped line). Those are real defects, recorded as
    follow-up work rather than fixed by widening this goal's blast radius.
    """
    if config.source_lang() == 'ja':
        tag = config.tag_re()
        return ''.join(tag.sub('', t).replace('\n', '') for t in texts)
    return '\n'.join(texts)


def relevant(gl, texts, family=None):
    """Glossary subset whose source term occurs in any of `texts`."""
    if family is not None and family not in UI_ONLY_FAMILIES:
        gl = {k: v for k, v in gl.items() if k not in UI_ONLY_TERMS}
    blob = enforcement_blob(texts)
    ja = config.source_lang() == 'ja'
    low = blob.lower()
    out = {}
    for en, ko in gl.items():
        # The lowercase pre-filter is a cheap reject for Latin source text.
        # Case-folding kana/kanji is a no-op, so the ja path must not gate on it.
        if not ja and en.lower() not in low:
            continue
        if matches_term(en, blob, fold_case=True):
            out[en] = ko
    # longest-first so multiword terms win
    return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


if __name__ == '__main__':
    g = build()
    print(len(g), 'terms ->', GLOSSARY_PATH())

"""Build and enforce the authoritative EN->KO term glossary.

Terms come from the hand-fixed name tables (characters, places, enemies, weapons,
items, spells, status effects). They are injected into every batch prompt that
mentions them and re-checked after generation, so wording never drifts.
"""
import json
import os
import re
import sys

from hanpatch import tm

from hanpatch import config

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


def hard_terms(src_path=None):
    src_path = src_path or config.src_path()
    if not os.path.exists(src_path):
        return {}
    src = json.load(open(src_path))
    tmdb = tm.load()
    hard = {}
    for fn, pat in NAME_KEYS:
        if fn not in HARD_FAMILIES:
            continue
        for it in src.get(fn, []):
            if re.fullmatch(pat, it['key']) and not tm.is_skip(it['en']):
                ko = tm.lookup(tmdb, it['en'])
                if ko:
                    hard[it['en']] = ko
    hard.update({k: v for k, v in EXTRA.items()
                 if k[:1].isupper()
                 or k.startswith(('dark', 'gnome', 'wyrm', 'paling'))})
    return hard


def build(src_path=None):
    src_path = src_path or config.src_path()
    src = json.load(open(src_path))
    tmdb = tm.load()
    gl = {}
    for fn, pat in NAME_KEYS:
        for it in src.get(fn, []):
            if not re.fullmatch(pat, it['key']):
                continue
            en = it['en']
            if tm.is_skip(en) or not en.strip() or len(en) > 48:
                continue
            ko = tm.lookup(tmdb, en)
            if ko:
                gl[en] = ko
    gl.update(EXTRA)
    os.makedirs(os.path.dirname(GLOSSARY_PATH()), exist_ok=True)
    json.dump(gl, open(GLOSSARY_PATH(), 'w'), ensure_ascii=False,
              indent=1, sort_keys=True)
    return gl


def load():
    """Authoritative glossary: the generated table with EXTRA merged on top.

    EXTRA is the source of truth in code, so a stale generated JSON can never
    silence a term.
    """
    gl = {}
    if os.path.exists(GLOSSARY_PATH()):
        try:
            gl.update(json.load(open(GLOSSARY_PATH())))
        except ValueError:
            pass
    else:
        gl.update(build())
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


def hard(src_path=None):
    src_path = src_path or config.src_path()
    global _HARD
    if _HARD is None:
        _HARD = hard_terms(src_path)
    return _HARD


def relevant(gl, texts, family=None):
    """Glossary subset whose EN term occurs in any of `texts` (word-boundary)."""
    if family is not None and family not in UI_ONLY_FAMILIES:
        gl = {k: v for k, v in gl.items() if k not in UI_ONLY_TERMS}
    blob = '\n'.join(texts)
    low = blob.lower()
    out = {}
    for en, ko in gl.items():
        if en.lower() in low:
            if re.search(r'\b' + re.escape(en) + r'\b', blob, re.I):
                out[en] = ko
    # longest-first so multiword terms win
    return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


if __name__ == '__main__':
    g = build()
    print(len(g), 'terms ->', GLOSSARY_PATH())

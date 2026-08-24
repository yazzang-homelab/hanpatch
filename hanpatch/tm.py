"""Translation memory: en -> ko, with rule-based derivation for structured names."""
import json
import os
import re

from hanpatch import config

def TM_PATH():
    return config.out('tm.json')
SKIP = {'abcdedf7', 'abcdedf1', 'not used', ''}
SKIP_RE = re.compile(
    r'^(not used[\s_]?[\d\-]*|accessory\d+|test_?\d*|abcdedf\d*'
    r'|CAUTION\s*:\s*please report a bug.*'
    r'|help message for \w+)$', re.I)


# whole key families that only exist for developer test scenes
SKIP_KEY_RE = re.compile(r'^test_\d+$')


def _tag_only(t):
    """Whether the row is markup and nothing else.

    A row like `<JA_HP>` or `{PRE_WORD}` carries no text: the engine substitutes a value
    or a name at that position, so there is nothing to translate and putting a Korean word
    there breaks the UI it draws. This is opt-in per title (`skip_tag_only`) because it is
    a fact about a title's markup, not a universal one - the reference title has two
    tag-only rows of its own, and skipping them would drop its manifest from 3262 to 3260
    entries and change bytes that must not change.
    """
    if not config.prof('skip_tag_only'):
        return False
    pattern = config.prof('tag_pattern')
    return bool(pattern) and not re.sub(pattern, '', t).strip()


def source_of(it):
    """The text that IS the source of record for one extracted row.

    The single implementation of a rule that was written out three times - in
    `qagate.source_of`, inside `manifest.build`, and nowhere at all in the
    streaming review path, which hashed the raw English instead. That third
    omission is the interesting one: a verdict is keyed on this value, so a path
    that picks a different source for the same row produces evidence the gate
    cannot find, and the failure surfaces as "no verdict" long after the call was
    paid for.

    English is the source unless it is a skip marker or blank, in which case the
    Japanese row is what the engine actually ships and what must be judged.
    """
    en = it.get('en', '')
    if is_skip(en, it.get('key')) or not en.strip():
        return it.get('jp') or en
    return en


def is_skip(s, key=None):
    t = s.strip()
    if key is not None:
        if key.strip() in set(config.prof('skip_keys') or ()):
            return True      # title-declared slots rendered by firmware or another owner
        if t == key.strip():
            return True      # placeholder rows whose text is just their own key
        if SKIP_KEY_RE.match(key.strip()):
            return True      # stage1 test table (assets live under fa/test/**)
    return t in SKIP or bool(SKIP_RE.match(t)) or _tag_only(t)

SUFFIX_RE = [
    re.compile(r'^(?P<base>.+) (?P<suf>[A-Z])$'),
    re.compile(r'^(?P<base>.+) (?P<suf>II|III|IV|V)$'),
    re.compile(r'^(?P<base>.+?)(?P<suf> \d+)$'),
]


def load():
    """Merge the hand-written TM with every per-family shard."""
    out = {}
    if os.path.exists(TM_PATH()):
        out.update(config.load_object(TM_PATH(), 'the primary translation memory'))
    import glob
    for p in sorted(glob.glob(config.out('tm_*.json'))):
        try:
            out.update(config.load_object(p, 'the translation memory shard'))
        except (OSError, SystemExit):
            continue
    return out


def save(tm):
    os.makedirs(os.path.dirname(TM_PATH()), exist_ok=True)
    json.dump(tm, open(TM_PATH(), 'w'), ensure_ascii=False, indent=1, sort_keys=True)


def lookup(tm, s):
    if s in tm:
        return tm[s]
    if is_skip(s):
        return None
    for rx in SUFFIX_RE:
        m = rx.match(s)
        if m:
            base = tm.get(m.group('base'))
            if base:
                return base + ' ' + m.group('suf').strip()
    return None


def untranslated(src):
    """src: {file: [ {key, en, jp} ]} -> ordered unique list of (en, jp, refs)"""
    tm = load()
    seen = {}
    order = []
    for fn, items in src.items():
        for it in items:
            s = it['en']
            if is_skip(s, it['key']) or not s.strip():
                continue
            if lookup(tm, s) is not None:
                continue
            if s not in seen:
                seen[s] = {'en': s, 'jp': it.get('jp', ''), 'refs': [],
                           'group': fn + '/' + __import__('re').sub(
                               r'_#+$', '',
                               __import__('re').sub(r'\d+', '#', it['key']))}
                order.append(s)
            seen[s]['refs'].append(f"{fn}:{it['key']}")
    return [seen[s] for s in order]

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


def is_skip(s, key=None):
    t = s.strip()
    if key is not None:
        if t == key.strip():
            return True      # placeholder rows whose text is just their own key
        if SKIP_KEY_RE.match(key.strip()):
            return True      # stage1 test table (assets live under fa/test/**)
    return t in SKIP or bool(SKIP_RE.match(t))

SUFFIX_RE = [
    re.compile(r'^(?P<base>.+) (?P<suf>[A-Z])$'),
    re.compile(r'^(?P<base>.+) (?P<suf>II|III|IV|V)$'),
    re.compile(r'^(?P<base>.+?)(?P<suf> \d+)$'),
]


def load():
    """Merge the hand-written TM with every per-family shard."""
    out = {}
    if os.path.exists(TM_PATH()):
        out.update(json.load(open(TM_PATH())))
    import glob
    for p in sorted(glob.glob(config.out('tm_*.json'))):
        try:
            out.update(json.load(open(p)))
        except (OSError, ValueError):
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
                seen[s] = {'en': s, 'jp': it['jp'], 'refs': [],
                           'group': fn + '/' + __import__('re').sub(
                               r'_#+$', '',
                               __import__('re').sub(r'\d+', '#', it['key']))}
                order.append(s)
            seen[s]['refs'].append(f"{fn}:{it['key']}")
    return [seen[s] for s in order]

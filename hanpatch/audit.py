"""Consistency / integrity audit over the whole translation memory."""
import json
import os
import re
import sys
import difflib
from collections import Counter, defaultdict


from hanpatch import glossary
from hanpatch import tm
from hanpatch import capacity as capmod  # noqa: E402
from hanpatch import translate
from hanpatch import wrap

from hanpatch import config

TAG = re.compile(r'<[^>\n]*>')
POLITE = re.compile(r'(니다|세요|십시오|습니까|나요)[.!?"\'”’)\]]*$')
PLAIN = re.compile(r'(다|였다|한다|된다|이다)[.!?"\'”’)\]]*$')


def main():
    src = json.load(open(config.src_path()))
    tmdb = tm.load()
    gl = glossary.load()
    fails = defaultdict(list)
    stats = Counter()
    register = defaultdict(Counter)

    for family, items in src.items():
        for it in items:
            en = it['en']
            if tm.is_skip(en, it['key']) or not en.strip():
                continue
            stats['total'] += 1
            ko = tm.lookup(tmdb, en)
            if ko is None:
                stats['untranslated'] += 1
                fails['untranslated'].append(f'{family}:{it["key"]}')
                continue
            stats['translated'] += 1
            sub = glossary.relevant(gl, [en], family)
            _, probs = translate.check(en, ko, sub, family,
                                       capmod.group(family, it['key']))
            for p in probs:
                fails[p.split(':')[0]].append(f'{family}:{it["key"]} :: {p}')
            # register mixing inside one string
            lines = [l.strip() for l in TAG.sub('', ko).split('\n') if l.strip()]
            pol = sum(1 for l in lines if POLITE.search(l))
            pla = sum(1 for l in lines if PLAIN.search(l) and not POLITE.search(l))
            if pol and pla:
                register[family]['mixed'] += 1
                fails['register-mixed'].append(f'{family}:{it["key"]}')
            elif pol:
                register[family]['polite'] += 1
            elif pla:
                register[family]['plain'] += 1

    print('=== coverage ===')
    for k in ('total', 'translated', 'untranslated'):
        print(f'  {k:14} {stats[k]}')
    print('=== integrity ===')
    if not fails:
        print('  no problems')
    for k, v in sorted(fails.items(), key=lambda kv: -len(kv[1])):
        print(f'  {k:22} {len(v)}')
        for s in v[:4]:
            print(f'      {s[:150]}')
    print('=== register per family (plain / polite / mixed) ===')
    for f, c in sorted(register.items()):
        print(f'  {f:11} {c["plain"]:5} {c["polite"]:5} {c["mixed"]:5}')
    print('=== dedup ===')
    print(f'  unique EN keys: {len(tmdb)}, distinct KO values: '
          f'{len(set(tmdb.values()))}')

    # unresolved review-queue entries must be zero before a build
    import glob
    pending = {}
    for p in glob.glob(config.out('review_*.json')):
        try:
            pending.update(json.load(open(p)))
        except (OSError, ValueError):
            continue
    pending = {k: v for k, v in pending.items() if tm.lookup(tmdb, k) is None}
    print(f'=== review queue ===\n  unresolved: {len(pending)}')
    for k in list(pending)[:5]:
        print(f'      {k[:90]!r}')

    print('=== terminology drift ===')
    drift = terminology_drift(src, tmdb)
    print(f'  near-identical source pairs with divergent Korean: {len(drift)}')
    for a_, b_, r in drift[:6]:
        print(f'      ratio={r:.2f} {a_[:70]!r}')
        print(f'                 {b_[:70]!r}')

    hard_fail = (stats['untranslated'] + len(pending) +
                 sum(len(v) for k, v in fails.items() if k != 'register-mixed'))
    print(f'\nHARD FAILURES: {hard_fail}')
    return 1 if hard_fail else 0


def signature(en):
    """Template signature: digits folded, proper-noun runs collapsed."""
    s = re.sub(r'\d+', '#', en)
    # only fold mid-sentence capitalised runs: a sentence-initial word carries
    # real meaning ("Increases ..." vs "Decreases ...")
    s = re.sub(r"(?<=[^.!?\n] )[A-Z][a-z]+(?:'[a-z]+)?(?: [A-Z][a-z]+)*", 'X', s)
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s


def terminology_drift(src, tmdb, ko_sim=0.60):
    """Same-template English whose Korean diverges. Covers the whole corpus."""
    groups = defaultdict(list)
    for family, items in src.items():
        for it in items:
            en = it['en']
            if tm.is_skip(en, it['key']) or not en.strip() or len(en) < 30:
                continue
            ko = tm.lookup(tmdb, en)
            if ko:
                groups[(family, signature(en))].append((en, ko))
    out = []
    for _, rows in groups.items():
        if len(rows) < 2:
            continue
        base_en, base_ko = rows[0]
        for en, ko in rows[1:]:
            r = difflib.SequenceMatcher(None, base_ko, ko).ratio()
            if r < ko_sim:
                out.append((base_en, en, r))
    return out


if __name__ == '__main__':
    sys.exit(main())

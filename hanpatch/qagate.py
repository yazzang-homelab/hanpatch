"""Fail-closed semantic QA gate, bound to the sealed manifest.

Release policy is a structured judge disposition, not keyword guessing:
every manifest value needs a schema-valid verdict whose `d` is `pass`, keyed to
the exact (source, translation) pair. Anything else — `defect`, `policy`, a low
score, a missing or malformed record — blocks the build unless an exact,
schema-valid waiver exists for that same pair. On success the approved manifest
digest is written to an approval token that the packer must match.
"""
import json
import os
import sys
from collections import Counter


from hanpatch import qa as qamod  # noqa: E402
from hanpatch import tm

from hanpatch import config

def MANIFEST():
    return config.out('manifest.json')
def WAIVERS():
    return config.out('qa_waivers.json')
def APPROVAL():
    return config.out('manifest.approved')
FLOOR = 4                       # release policy, deliberately not configurable
REQUIRED_JUDGES = 2             # independent agreement, not a single opinion
JUDGES = set(qamod.JUDGES)
WAIVER_CATEGORIES = {'JP_NAMING', 'ELEMENT_TERMS', 'OFFICIAL_HW_TERM',
                     'GAME_TERM', 'EN_SOURCE_PRIORITY', 'SOURCE_BUG',
                     'TEMPLATE', 'JUDGE_ERROR', 'JP_CONVENTION',
                     'SOURCE_PUNCTUATION', 'INNER_MONOLOGUE'}


def producers():
    return qamod.producers()


def source_of(it):
    en = it['en']
    if tm.is_skip(en, it['key']) or not en.strip():
        return it.get('jp', en)
    return en


def verdict_problem(rec, en, ko, producer):
    """None when the record is a schema-valid release pass."""
    if not isinstance(rec, dict):
        return 'not an object'
    for f in ('a', 'f', 'd', 'judge', 'en', 'ko'):
        if f not in rec:
            return f'missing field {f}'
    try:
        a, f = int(rec['a']), int(rec['f'])
    except (TypeError, ValueError):
        return 'non-integer score'
    if not (1 <= a <= 5 and 1 <= f <= 5):
        return f'score out of range a={rec["a"]} f={rec["f"]}'
    if rec['en'] != en or rec['ko'] != ko:
        return 'verdict does not carry the judged pair'
    if not str(rec['judge']).strip():
        return 'no judge recorded'
    if rec['judge'] not in JUDGES:
        return f'unknown judge {rec["judge"]!r}'
    if producer and rec['judge'] == producer:
        return f'judged by its own producer ({producer})'
    if rec['d'] != 'pass':
        return f'disposition={rec["d"]} ({str(rec.get("r", ""))[:60]})'
    if min(a, f) < FLOOR:
        return f'score below floor a={a} f={f}'
    return None


def panel_problem(recs, en, ko, producer):
    """None when an independent panel of judges unanimously passes the pair.

    A single model produces correlated false negatives, so release requires
    REQUIRED_JUDGES distinct, schema-valid `pass` verdicts.
    """
    if not recs:
        return 'no verdict for this exact pair'
    judges = set()
    for rec in recs:
        p = verdict_problem(rec, en, ko, producer)
        if p is not None:
            return p
        judges.add(rec['judge'])
    if len(judges) < REQUIRED_JUDGES:
        return (f'only {len(judges)} judge(s) passed this pair, '
                f'{REQUIRED_JUDGES} required')
    return None


def waiver_problem(w, mkey, pk=None, man=None, by_key=None):
    if not isinstance(w, dict):
        return 'not an object'
    for f in ('key', 'category', 'reason'):
        if f not in w:
            return f'missing field {f}'
    if w['key'] != mkey:
        # identical source/translation pairs share one hash, so a waiver may be
        # declared under a sibling key as long as it resolves to the same pair
        ok = False
        if pk is not None and man is not None and by_key is not None:
            sib = by_key.get(w['key'])
            if sib is not None and w['key'] in man:
                ok = qamod.pair_key(source_of(sib), man[w['key']]) == pk
        if not ok:
            return f'waiver key {w["key"]} != {mkey}'
    if w['category'] not in WAIVER_CATEGORIES:
        return f'unknown category {w["category"]}'
    if len(str(w['reason']).strip()) < 10:
        return 'reason too short'
    return None


def validate(man=None, quiet=True):
    """Side-effect-free validation used both by the gate and by the packer."""
    if man is None:
        man = json.load(open(MANIFEST()))['entries']
    src = json.load(open(config.src_path()))
    qa = qamod.load()
    waivers = json.load(open(WAIVERS())) if os.path.exists(WAIVERS()) else {}
    prov = producers()
    by_key = {f'{fam}/{it["key"]}': it
              for fam, items in src.items() for it in items}
    blocked, bad_waivers, used = [], [], set()
    for mkey, ko in sorted(man.items()):
        it = by_key.get(mkey)
        if it is None:
            blocked.append(f'{mkey}: no source row')
            continue
        en = source_of(it)
        pk = qamod.pair_key(en, ko)
        rec = qa.get(pk)
        problem = panel_problem(qa.get(pk) or [], en, ko, prov.get(en, ''))
        if problem is None:
            continue
        w = waivers.get(pk)
        if w is None:
            blocked.append(f'{mkey}: {problem}')
            continue
        wp = waiver_problem(w, mkey, pk, man, by_key)
        if wp is not None:
            bad_waivers.append(f'{mkey}: invalid waiver ({wp})')
            continue
        used.add(pk)
    stale = sorted(set(waivers) - used)
    return blocked, bad_waivers, stale


def approve(digest, entries=0, waivers=0):
    """Write the approval token for a digest that just passed the panel."""
    with open(APPROVAL(), 'w') as fh:
        json.dump({'digest': digest, 'entries': entries, 'waivers': waivers},
                  fh, indent=1)
    return APPROVAL()


def revoke():
    if os.path.exists(APPROVAL()):
        os.remove(APPROVAL())


def approved_digest():
    if not os.path.exists(APPROVAL()):
        return None
    try:
        return json.load(open(APPROVAL())).get('digest')
    except (OSError, ValueError):
        return None


def main():
    if not os.path.exists(MANIFEST()):
        print('no manifest: run mtl/manifest.py first')
        return 1
    doc = json.load(open(MANIFEST()))
    man = doc['entries']
    src = json.load(open(config.src_path()))
    qa = qamod.load()
    waivers = json.load(open(WAIVERS())) if os.path.exists(WAIVERS()) else {}
    prov = producers()
    by_key = {f'{fam}/{it["key"]}': it
              for fam, items in src.items() for it in items}

    blocked, waived, bad_waivers = [], [], []
    scores, dispositions = Counter(), Counter()
    used_waivers = set()
    for mkey, ko in sorted(man.items()):
        it = by_key.get(mkey)
        if it is None:
            blocked.append(f'{mkey}: no source row')
            continue
        en = source_of(it)
        pk = qamod.pair_key(en, ko)
        recs = qa.get(pk) or []
        for rec in recs:
            if isinstance(rec, dict):
                try:
                    scores[min(int(rec['a']), int(rec['f']))] += 1
                except (KeyError, TypeError, ValueError):
                    pass
                dispositions[rec.get('d', '?')] += 1
        problem = panel_problem(recs, en, ko, prov.get(en, ''))
        if problem is None:
            continue
        w = waivers.get(pk)
        if w is None:
            blocked.append(f'{mkey}: {problem}')
            continue
        wp = waiver_problem(w, mkey, pk, man, by_key)
        if wp is not None:
            bad_waivers.append(f'{mkey}: invalid waiver ({wp})')
            continue
        used_waivers.add(pk)
        waived.append(f'{mkey} [{w["category"]}] {problem[:60]}')

    stale = sorted(set(waivers) - used_waivers)

    print('=== semantic QA gate (manifest-bound) ===')
    print(f'  manifest digest     {doc["digest"][:16]}')
    print(f'  manifest entries    {len(man)}')
    print(f'  verdict records     {sum(dispositions.values())} '
          f'(>= {REQUIRED_JUDGES} distinct judges required per entry)')
    for d in sorted(dispositions):
        print(f'  disposition {d:8} {dispositions[d]}')
    for s in sorted(scores):
        print(f'  min(adequacy,fluency)={s}: {scores[s]}')
    print(f'  waivers applied     {len(used_waivers)}/{len(waivers)}')
    for label, rows in (('BLOCKED', blocked), ('INVALID WAIVERS', bad_waivers),
                        ('STALE WAIVERS', stale)):
        if rows:
            print(f'  {label}: {len(rows)}')
            for r in rows[:12]:
                print(f'      {r}')
    hard = len(blocked) + len(bad_waivers) + len(stale)
    print(f'\nQA HARD FAILURES: {hard}')
    if hard == 0:
        approve(doc['digest'], len(man), len(used_waivers))
        print(f'approved manifest digest -> {APPROVAL()}')
    else:
        revoke()
    return 1 if hard else 0


if __name__ == '__main__':
    sys.exit(main())

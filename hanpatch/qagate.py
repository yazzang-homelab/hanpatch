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

LAST_EXAMINED = 0


def MANIFEST():
    return config.out('manifest.json')
def WAIVERS():
    return config.out('qa_waivers.json')
def APPROVAL():
    return config.out('manifest.approved')
FLOOR = 4                       # release policy, deliberately not configurable
REQUIRED_JUDGES = 2             # independent MODELS, not two accounts of one model
JUDGES = set(qamod.JUDGES)
lane_model = qamod.lane_model   # one definition of judge identity, shared with the panel
WAIVER_CATEGORIES = {'JP_NAMING', 'ELEMENT_TERMS', 'OFFICIAL_HW_TERM',
                     'GAME_TERM', 'EN_SOURCE_PRIORITY', 'SOURCE_BUG',
                     'TEMPLATE', 'JUDGE_ERROR', 'JP_CONVENTION',
                     'SOURCE_PUNCTUATION', 'INNER_MONOLOGUE',
                     'JP_SOURCE_AMBIGUITY', 'DECLARED_REGISTER_CONFLICT'}


def producers():
    return qamod.producers()


def source_of(it):
    en = it['en']
    if tm.is_skip(en, it['key']) or not en.strip():
        return it.get('jp') or en
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
    if producer and lane_model(rec['judge']) == lane_model(producer):
        # The rule is about the MODEL, not the account or the endpoint. Comparing lane ids
        # let a sibling account of the producing model grade its own output: measured on
        # this corpus, 25123 of 54071 shipped pairs carried such a verdict.
        return (f'judged by its own model '
                f'({lane_model(producer)} via {rec["judge"]} for {producer})')
    if rec['d'] != 'pass':
        return f'disposition={rec["d"]} ({str(rec.get("r", ""))[:60]})'
    if min(a, f) < FLOOR:
        return f'score below floor a={a} f={f}'
    return None


def panel_problem(recs, en, ko, producer):
    """None when an independent panel of judges unanimously passes the pair.

    A single model produces correlated false negatives, so release requires
    REQUIRED_JUDGES verdicts from DISTINCT MODELS. Counting lanes instead of models made
    two accounts of one model look like a panel: measured here, 25778 of 54071 shipped
    pairs were "double-judged" by a single model, and only 10463 carried two genuinely
    different models.
    """
    if not recs:
        return 'no verdict for this exact pair'
    models = set()
    for rec in recs:
        p = verdict_problem(rec, en, ko, producer)
        if p is not None:
            return p
        models.add(lane_model(rec['judge']))
    if len(models) < REQUIRED_JUDGES:
        return (f'only {len(models)} model(s) passed this pair, '
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
    global LAST_EXAMINED
    if man is None:
        man = config.load_object(MANIFEST(), 'the sealed manifest')['entries']
    src = config.load_object(config.src_path(), 'the extracted source')
    qa = qamod.load()
    waivers = (config.load_object(WAIVERS(), 'the QA waiver file')
               if os.path.exists(WAIVERS()) else {})
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
    LAST_EXAMINED = len(man)
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
        return config.load_object(APPROVAL(), 'the approval token').get('digest')
    except (OSError, SystemExit):
        return None


def main():
    if not os.path.exists(MANIFEST()):
        print('no manifest: run mtl/manifest.py first')
        return 1
    doc = config.load_object(MANIFEST(), 'the sealed manifest')
    man = doc['entries']
    src = config.load_object(config.src_path(), 'the extracted source')
    qa = qamod.load()
    waivers = (config.load_object(WAIVERS(), 'the QA waiver file')
               if os.path.exists(WAIVERS()) else {})
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
    # REPORT ONLY. This entry point runs the panel in isolation, without the five
    # gates before it, without the per-gate input floors and without re-deriving
    # the sealed digest, so it must not mint a release-valid approval: that would
    # be a second, weaker authority for the same artifact. A clean panel here is
    # evidence for the operator; `hanpatch gates` is what approves. A dirty panel
    # still revokes, because a stale approval must never outlive a failed panel.
    if hard:
        revoke()
    return 1 if hard else 0


if __name__ == '__main__':
    # A FAILURE here must leave no approval standing: `release.create` trusts the
    # token plus a digest match without re-running the panel, so an interrupt or
    # an unreadable input would otherwise make a stale approval releasable.
    # A CLEAN exit must NOT revoke. `sys.exit(0)` is itself a BaseException, so
    # catching it blindly let this report-only panel delete the valid token that
    # `hanpatch gates` had legitimately written.
    try:
        sys.exit(main())
    except SystemExit as exc:
        if exc.code:
            revoke()
        raise
    except BaseException:
        revoke()
        raise

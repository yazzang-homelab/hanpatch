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
                     'JP_SOURCE_AMBIGUITY', 'DECLARED_REGISTER_CONFLICT',
                     # A low score that a different scale did not reproduce. Named rather
                     # than folded into JUDGE_ERROR because it is not an error: the judge
                     # applied its own calibration honestly, and the measured spread across
                     # lanes over one corpus was 0.14% to 14.62%. A release that leans on
                     # this category is making a claim an auditor can check per row - which
                     # lane scored low, which independent lane passed.
                     'FLOOR_CALIBRATION',
                     # A style objection that repair has provably stopped answering: five
                     # DQ7 cycles moved the count 264->251->252 because each rewrite draws a
                     # fresh preference from a different judge. Distinct from
                     # DECLARED_REGISTER_CONFLICT, which needs two judges demanding opposite
                     # things about the SAME line; here one judge objects and another passes.
                     'JUDGE_PREFERENCE_UNRESOLVED',
                     # Sealed before any judge saw this exact translation. It claims only
                     # that nothing is known against the row, NOT that it was reviewed, so a
                     # later sweep can still find a defect here.
                     'UNJUDGED_AT_SEAL'}


def producers():
    return qamod.producers()


def source_of(it):
    en = it['en']
    if tm.is_skip(en, it['key']) or not en.strip():
        return it.get('jp') or en
    return en


def record_problem(rec, en, ko):
    """None when the record is a well-formed verdict about this exact pair.

    Everything here is integrity, not opinion: a record that fails one of these is
    corrupt or forged, so it blocks whatever it says.
    """
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
    return None


def disqualified(rec, producer):
    """Why this verdict is not evidence, or None.

    Under the default `model` standard the rule is about the MODEL, not the account or the
    endpoint. Comparing lane ids let a sibling account of the producing model grade its own
    output: measured on this corpus, 25123 of 54071 shipped pairs carried such a verdict.

    Under a declared `lane` standard the comparison is the lane, because that standard has
    already accepted that two accounts of one model count as two judges; keeping the model
    test here would disqualify every verdict the panel is able to produce and stall the
    run it exists to unblock. The weaker standard is a declaration, not a default.
    """
    if not producer:
        return None
    if independence() == 'lane':
        if rec['judge'] == producer:
            return f'judged by the lane that produced it ({producer})'
        return None
    if lane_model(rec['judge']) == lane_model(producer):
        return (f'judged by its own model '
                f'({lane_model(producer)} via {rec["judge"]} for {producer})')
    return None


def defect_corroboration():
    """How many eligible judges must call a DEFECT before it blocks release.

    1 (the default) is the strict reading: the gate wants two passes, so one judge's
    defect blocks. That is right when a defect means a defect.

    Measured on this corpus when it did not: of 2846 pairs blocked by a finding, 2648 (93%)
    were blocked by ONE judge while an independent judge passed the same pair, and the
    complaints were 38% punctuation preference and 31% ending/register preference against
    12% actual mistranslation. Re-translating those does not converge - 19% of repaired
    rows came back flagged by a different judge with a different preference, and three rows
    cycled 아니외다 -> 아니로다 -> 아닙니다 across passes. That loop chases disagreement,
    not quality.

    Raising this to 2 asks for corroboration before a subjective finding blocks. It does
    NOT touch the mechanical gates: tags, glossary, capacity, register, kana residue and
    the soft-break marker are checked deterministically by `translate.check` and a
    translation that violates them cannot be sealed at all, whatever a judge says. Nor does
    it touch the SCORE floor - a low score from any single judge still blocks, because a
    number is a measurement rather than an opinion about a fix.
    """
    # `or 1` would be wrong: 0 is falsy, so a declared zero would silently become the
    # strict default instead of being refused, and zero is the one value that would let a
    # unanimous defect ship. Absent and zero are different answers.
    declared = config.prof('defect_corroboration')
    if declared is None:
        return 1
    try:
        n = int(declared)
    except (TypeError, ValueError):
        raise SystemExit(
            f'defect_corroboration must be an integer, got {declared!r}')
    if n < 1:
        raise SystemExit('defect_corroboration must be at least 1: zero would let a '
                         'unanimous defect ship')
    return n


def floor_corroboration():
    """How many judges must score a pair below FLOOR before it blocks release.

    The default is 1, and for a well-calibrated panel that is right: a score is a
    measurement, not an opinion about a fix. What the default assumes is that the judges
    share a scale. Measured on DQ7 2026-08-11, they do not - the share of verdicts scoring
    below the floor, over the SAME corpus, runs from 0.14% to 14.62%:

        codex1:gpt-5.6-luna    61420 verdicts   14.62% below floor
        codex2:gpt-5.6-luna    57781 verdicts   14.45%
        codex3:gpt-5.6-luna    46543 verdicts   12.56%
        opencode:mimo-v2.5     20522 verdicts    1.74%
        nimproxy:llama-3.3     10874 verdicts    0.23%
        a6:deepseek-v4-flash    5029 verdicts    0.14%

    A hundredfold spread is a property of the instrument, not of the text. It matters here
    because `codex1/2/3` are three ACCOUNTS of one model and supplied 779 of the 721
    floor-blocked pairs while providing about half of all verdicts - under a declared
    `lane` independence standard they count as three judges, so one model's calibration
    could block a release on its own.

    The disputed pairs were then re-judged by independent vendors (gemma-4-31b and two
    nemotron lanes) with `qa --pairs`. Of the 514 that got a verdict, 416 (81%) were not
    reproduced below the floor; 98 were. A second targeted pass over the 207 that had been
    lost to lane errors reproduced 21 and cleared 186. So roughly four in five below-floor
    scores did not survive contact with a different scale.

    Raising this to 2 asks for a second below-floor score before a subjective number blocks
    a release. It does NOT touch the mechanical checks - tags, glossary, capacity, register,
    kana and the soft-break marker are decided by `translate.check` and an offending
    translation cannot be sealed at all. It does not touch a defect the producing model
    admits about its own output. And a pair that no independent judge passed still blocks on
    one vote, because there the low score is uncontradicted rather than merely unrepeated.
    """
    declared = config.prof('floor_corroboration')
    if declared is None:
        return 1
    try:
        n = int(declared)
    except (TypeError, ValueError):
        raise SystemExit(f'floor_corroboration must be an integer, got {declared!r}')
    if n < 1:
        raise SystemExit('floor_corroboration must be at least 1: zero would let a '
                         'unanimously bad score ship')
    return n


def finding_problem(rec):
    """None when the judge reports a release-grade pass."""
    if rec['d'] != 'pass':
        return f'disposition={rec["d"]} ({str(rec.get("r", ""))[:60]})'
    if min(int(rec['a']), int(rec['f'])) < FLOOR:
        return f'score below floor a={rec["a"]} f={rec["f"]}'
    return None


def verdict_problem(rec, en, ko, producer):
    """None when the record is a schema-valid release pass from an eligible judge."""
    return (record_problem(rec, en, ko)
            or disqualified(rec, producer)
            or finding_problem(rec))


def independence():
    """How two judges must differ for the panel to count as independent.

    `model` (the default) is the standard this project was built on and the one its
    measurements justify: counting lanes let two accounts of one model look like a panel,
    and 25778 of 54071 shipped pairs were "double-judged" by a single model that way.

    `lane` is a WEAKER standard and exists because the alternative is worse in a specific,
    checkable situation: when every other model on the box is exhausted or expired, a
    model-independent second verdict cannot be obtained at all, and the run stalls with
    rows that no amount of waiting will clear. A title may declare `lane` to keep shipping
    on two distinct ACCOUNTS of one model. It is declared in the profile, printed by the
    gate, and recorded in the approval token, so a release states which standard it met
    rather than implying the stronger one.
    """
    mode = config.prof('judge_independence') or 'model'
    if mode not in ('model', 'lane'):
        raise SystemExit(
            f'judge_independence must be "model" or "lane", got {mode!r}: an unknown '
            f'value would silently pick a standard nobody chose')
    return mode


def judge_identity(judge):
    """What makes one judge different from another, under the declared standard."""
    return judge if independence() == 'lane' else lane_model(judge)


def panel_problem(recs, en, ko, producer):
    """None when an independent panel of judges unanimously passes the pair.

    A single model produces correlated false negatives, so release requires
    REQUIRED_JUDGES verdicts from DISTINCT MODELS. Counting lanes instead of models made
    two accounts of one model look like a panel: measured here, 25778 of 54071 shipped
    pairs were "double-judged" by a single model, and only 10463 carried two genuinely
    different models.

    A verdict from the producing model is disqualified, not damning, and the asymmetry
    matters: its PASS is self-serving and counts for nothing, while its DEFECT is an
    admission against interest and still blocks. Treating the self-serving pass as a
    blocking problem deadlocked 25152 shipped pairs here - `qa.main` already refuses to
    count such a verdict toward coverage, so the sweep saw nothing to judge while the
    gate stayed shut, and no repair could clear a record that is kept forever.
    """
    if not recs:
        return 'no verdict for this exact pair'
    identities = set()
    findings = []
    lowscores = []
    cleared = set()
    for rec in recs:
        p = record_problem(rec, en, ko)
        if p is not None:
            return p
        # Order matters, and the asymmetry is the point. A verdict from the producing model
        # is disqualified as EVIDENCE OF QUALITY - its pass is self-serving - but a defect
        # from it is an admission against interest and blocks on its own. Testing
        # disqualification first, as this function briefly did, threw that admission away
        # and let a model's own "this is wrong" through the gate.
        own = disqualified(rec, producer) is not None
        # A score is a measurement, not an opinion about a fix - so long as the judges share
        # a scale. `floor_corroboration` is where a panel that demonstrably does not share
        # one is declared; see that function for the measured spread. The producer's own low
        # score counts too, being against interest.
        if min(int(rec['a']), int(rec['f'])) < FLOOR:
            lowscores.append(rec)
        elif not own:
            # Somebody with a different scale looked at this exact text and was fine with it.
            # That is what makes a lone low score CONTRADICTED rather than merely unrepeated.
            cleared.add(judge_identity(rec['judge']))
        if rec['d'] != 'pass':
            if own:
                return (f'disposition={rec["d"]} by the model that produced it '
                        f'({str(rec.get("r", ""))[:60]})')
            findings.append(rec)
        if own:
            continue
        # An examined pair counts as examined even when that judge dissented. `qa.main` has
        # always counted VERDICTS toward coverage while this function counted only passes,
        # and that mismatch is how a pair could be finished for the sweep and blocked for
        # the gate at the same time - nothing left to judge, and no way to clear it.
        identities.add(judge_identity(rec['judge']))
    floor_need = floor_corroboration()
    if lowscores:
        # Uncontradicted stays strict at one vote: with nobody passing the pair, a low score
        # is the only reading anyone has, and corroboration would amount to ignoring it.
        if len(lowscores) >= floor_need or not cleared:
            first = lowscores[0]
            extra = ''
            if floor_need > 1 and len(lowscores) >= floor_need:
                extra = f', corroborated by {len(lowscores)} judges'
            elif floor_need > 1:
                extra = ', uncontradicted'
            return f'score below floor a={first["a"]} f={first["f"]}{extra}'
    need = defect_corroboration()
    if len(findings) >= need:
        first = findings[0]
        extra = f', corroborated by {len(findings)} judges' if need > 1 else ''
        return f'disposition={first["d"]} ({str(first.get("r", ""))[:60]}){extra}'
    if len(identities) < REQUIRED_JUDGES:
        unit = 'lane' if independence() == 'lane' else 'independent model'
        return (f'only {len(identities)} {unit}(s) judged this pair, '
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
    """Write the approval token for a digest that just passed the panel.

    The token records WHICH standard the panel met. A release that shipped on two accounts
    of one model and one that shipped on two different models are not the same claim, and
    a token that omits the difference lets the weaker one be read as the stronger.
    """
    with open(APPROVAL(), 'w') as fh:
        json.dump({'digest': digest, 'entries': entries, 'waivers': waivers,
                   'judges_required': REQUIRED_JUDGES,
                   'judge_independence': independence(),
                   'defect_corroboration': defect_corroboration(),
                   'floor_corroboration': floor_corroboration()},
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
    unit = 'distinct lanes' if independence() == 'lane' else 'distinct MODELS'
    print(f'  verdict records     {sum(dispositions.values())} '
          f'(>= {REQUIRED_JUDGES} {unit} required per entry)')
    if independence() == 'lane':
        print('  judge independence  LANE - two accounts of one model count as two '
              'judges. This is the weaker standard, declared in the profile.')
    if floor_corroboration() > 1:
        print(f'  floor corroboration  {floor_corroboration()} - a score below the floor '
              f'blocks when that many judges agree, or on one vote when no independent '
              f'judge passed the pair. Mechanical checks are unaffected.')
    if defect_corroboration() > 1:
        print(f'  defect corroboration {defect_corroboration()} - a finding blocks only '
              f'when that many judges agree. Mechanical checks (tags, glossary, capacity, '
              f'register, kana, soft break) and the score floor are unaffected.')
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

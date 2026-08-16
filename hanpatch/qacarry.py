"""Carry a verdict onto the value today's rules derive from the text that was judged.

A verdict is filed under the exact `(source, translation)` pair a judge saw. That
identity is right, and it is why a reseal that only re-applies the pipeline's own
deterministic passes - re-wrapping, punctuation, josa - orphans every verdict it
touches, even though no human and no model rewrote a single word.

Measured on DQ7 when the layout budgets for the description panels were corrected:
of 78642 sealed rows, 17504 still matched their verdict exactly, 51066 differed only
in where the wrapper put a newline, and 10072 differed only in characters the
pipeline itself rewrites - 7743 dropped a full stop after an ellipsis, 348 replaced a
fullwidth tilde, 306 other punctuation, and 1675 spelled a particle after a runtime
name in both forms. Re-judging those would ask a panel to re-score text it has already
approved, in a form the panel is not even shown: the judge prompt says in as many
words that markup and line breaks are not being assessed.

So the carry is not "close enough". Two grounds move a verdict, both recorded on the
record as `carried_by`, and nothing else does:

  `derivation`  running today's full ruleset over the exact string that was judged
                reproduces the shipped string byte for byte, with no problems reported.
                A value the rules would refuse never carries.

  `wording`     the judged string and the shipped string are identical once every
                whitespace character is removed - `qa.wording_key`. The judge assessed
                wording; where the wrapper put a newline is not wording, and the judge
                prompt says so. This is the weaker ground and it exists because the
                stronger one cannot reach every case: a source space that the wrapper
                turns into a line break regroups the units around a substitution token,
                so neither string derives the other even though not one character of
                the wording differs. Measured on DQ7: 38 waivers and 23 rows sat in
                exactly that gap.

A wording change never carries under either ground, and every carried record keeps
`carried_from` and `ko_judged` so the inheritance can be re-checked from the ledger.

Idempotent: a second run finds nothing to carry. Read-only unless `--apply` is given.
"""
import collections
import json
import os
import shutil
import sys
import time

from hanpatch import capacity as capmod
from hanpatch import config
from hanpatch import glossary
from hanpatch import manifest as manmod  # noqa: E402
from hanpatch import qa as qamod
from hanpatch import qagate
from hanpatch import translate


def _ground(en, judged_ko, ko, subset, family, group):
    """Why this verdict may move onto `ko`, or None when nothing justifies it.

    Derivation is tried first because it is the stronger claim and it is checkable from
    the record alone. The whitespace ground is not a relaxation of it: it asserts that
    every non-whitespace character is byte-for-byte identical, which is a stricter test
    than any similarity measure and exactly the thing a judge was asked about.
    """
    derived, problems = translate.check(en, judged_ko, subset, family, group)
    if not problems and derived == ko:
        return 'derivation'
    if qamod.wording_key(en, judged_ko) == qamod.wording_key(en, ko):
        return 'wording'
    return None


def _judged_pairs(doc):
    """source -> [(pair key, translation as judged)], from the ledger's own records.

    The records carry the pair they were filed for, so the ledger is self-describing
    and no prior manifest has to be kept around to read it.
    """
    by_source = {}
    for pair, recs in doc.items():
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            en, ko = rec.get('en'), rec.get('ko')
            if isinstance(en, str) and isinstance(ko, str):
                by_source.setdefault(en, {})[pair] = ko
                break
    return {en: sorted(pairs.items()) for en, pairs in by_source.items()}


def _usable(recs):
    """True when this pair already holds a verdict the gate will read.

    A carry proven under an older ruleset is NOT usable: the derivation it rests on may
    no longer hold, `qagate` refuses it for exactly that reason, and re-proving it is this
    command's job. Treating it as settled is how a stale inheritance survives a ruleset
    change unnoticed.
    """
    for rec in recs or ():
        if not isinstance(rec, dict):
            continue
        if 'carried_ruleset' not in rec or rec['carried_ruleset'] == manmod.RULESET:
            return True
    return False


def _settled(recs):
    """True when this pair already holds a full panel of usable, independent verdicts.

    Not merely "has a verdict": a target that carries one lane still needs the second, and
    the evidence for it is often sitting on another pair that derives the same value.
    Skipping on the first record left 764 rows one lane short of a panel they had already
    been judged for.
    """
    if not _usable(recs):
        return False
    identities = {qamod.lane_model(rec.get('judge'))
                  for rec in recs or () if isinstance(rec, dict)}
    return len(identities) >= qagate.REQUIRED_JUDGES


def plan(doc, waivers=None):
    """(verdict moves, waiver moves) for everything a carry ground can move.

    A verdict move is `(manifest key, from pair, to pair, shipped value, ground)`; a waiver
    move is `(manifest key, from pair, to pair, ground)`. A waiver is an operator decision
    about one exact translation, so it re-keys on the same grounds and never on the
    manifest key alone - otherwise a decision made about one wording would silently cover a
    different one.
    """
    waivers = {} if waivers is None else waivers
    man = manmod.load()['entries']
    src = config.load_object(config.src_path(), 'the extracted source')
    gl = glossary.load()
    rows = {f'{family}/{it["key"]}': (family, it)
            for family, items in src.items() for it in items}
    judged = _judged_pairs(doc)
    moves = []
    used = set()
    for mkey, ko in sorted(man.items()):
        entry = rows.get(mkey)
        if entry is None:
            continue
        family, it = entry
        en = qagate.source_of(it)
        target = qamod.pair_key(en, ko)
        used.add(target)
        settled = _settled(doc.get(target))
        if settled:
            continue
        present = {rec.get('judge') for rec in doc.get(target, ())
                   if isinstance(rec, dict)}
        subset = glossary.relevant(gl, [en], family)
        group = capmod.group(family, it['key'])
        # EVERY pair that derives this value, not the first. A judge that assessed one
        # earlier wording is evidence about the shipped one whenever today's rules turn
        # that wording into it, and taking only the first left rows with a single lane
        # against a panel that requires two - coverage that was already paid for, sitting
        # one pair away.
        for pair, judged_ko in judged.get(en, ()):
            if pair == target or not _usable(doc.get(pair)):
                continue
            if all(rec.get('judge') in present
                   for rec in doc[pair] if isinstance(rec, dict)):
                continue
            ground = _ground(en, judged_ko, ko, subset, family, group)
            if ground is None:
                continue
            moves.append((mkey, pair, target, ko, ground))
            present |= {rec.get('judge')
                        for rec in doc[pair] if isinstance(rec, dict)}
    return moves, _plan_waivers(doc, waivers, man, rows, gl, used)


def _plan_waivers(doc, waivers, man, rows, gl, used):
    """Re-key plan for waivers, in one pass with no feedback.

    Sources are the pairs the build does NOT use, targets are the pairs it does, and the
    two sets are disjoint - so moving a waiver can never create a new stray one. An earlier
    version walked rows instead and proposed moves against a dictionary it was mutating:
    it never converged, moving four waivers per run and reporting 6 then 14 then 6 left.
    """
    by_pair = {}
    for mkey, ko in man.items():
        entry = rows.get(mkey)
        if entry is None:
            continue
        family, it = entry
        en = qagate.source_of(it)
        by_pair.setdefault(en, []).append((qamod.pair_key(en, ko), mkey, ko, family, it))
    out = []
    for pair in sorted(set(waivers) - used):
        recs = doc.get(pair) or []
        rec = next((r for r in recs if isinstance(r, dict)
                    and isinstance(r.get('en'), str) and isinstance(r.get('ko'), str)),
                   None)
        if rec is None:
            # Nothing in the ledger says what this waiver was written against, so there is
            # no ground to move it on. It is reported, never guessed at.
            continue
        for target, mkey, ko, family, it in by_pair.get(rec['en'], ()):
            ground = _ground(rec['en'], rec['ko'], ko,
                             glossary.relevant(gl, [rec['en']], family), family,
                             capmod.group(family, it['key']))
            if ground is None:
                continue
            out.append((mkey, pair, target, ground))
            break
    return out


def move_waivers(waivers, waiver_moves):
    """Re-key each waiver onto the pair now shipped, recording where it came from.

    A move, not a copy: a waiver left on the pair it was written for is reported by the
    gate as stale, and 658 of them were - the operator's decisions were still true, they
    were just filed against text the build no longer ships.
    """
    moved = superseded = 0
    for _mkey, source_pair, target, ground in waiver_moves:
        w = waivers.pop(source_pair, None)
        if w is None:
            continue
        if target in waivers:
            # A pair carries exactly one waiver, and two manifest keys may share a pair -
            # `waiver_problem` accepts a waiver declared under a sibling key for that
            # reason. Once both re-key onto the same pair the second is a duplicate
            # decision about identical text, so it is dropped rather than left behind to
            # be reported as stale; the backup keeps the original file verbatim. Measured:
            # 45 of the 46 waivers still stale after the first re-key were this.
            superseded += 1
            continue
        w = dict(w)
        w['carried_from'] = source_pair
        w['carried_ruleset'] = manmod.RULESET
        w['carried_by'] = ground
        waivers[target] = w
        moved += 1
    return moved, superseded


def carry(doc, moves):
    """File the judged verdicts under the derived pair, recording where they came from.

    A judge already present on the target is not duplicated: two records from one lane
    would let a single judge satisfy a panel that requires independent ones.
    """
    carried = 0
    for _mkey, source_pair, target, ko_now, ground in moves:
        existing = doc.setdefault(target, [])
        seen = {rec.get('judge') for rec in existing if isinstance(rec, dict)}
        for rec in doc.get(source_pair, []):
            if not isinstance(rec, dict) or rec.get('judge') in seen:
                continue
            moved = dict(rec)
            moved['carried_from'] = source_pair
            # What the judge actually read, kept verbatim: it is the only thing that makes
            # the inheritance re-checkable, and `qagate` re-hashes it against
            # `carried_from` on every run.
            moved['ko_judged'] = rec['ko']
            # The record is filed under the pair being SHIPPED - a verdict whose stored
            # pair disagreed with its key is what the gate reads as a corrupt ledger.
            moved['ko'] = ko_now
            # The derivation was proven under this ruleset and only this one.
            moved['carried_ruleset'] = manmod.RULESET
            # Which of the two grounds moved it, so an auditor can separate the rows that
            # the rules reproduce from the rows that only share their wording.
            moved['carried_by'] = ground
            existing.append(moved)
            seen.add(rec.get('judge'))
            carried += 1
    return carried


def revoke_stale_carries(doc):
    """Drop carried verdicts whose ground no longer holds, so the carry can be re-proved.

    Only the weaker ground needs this, and it needs it from the record alone: the stronger
    one is pinned by the ruleset stamp. When the whitespace rule tightened - runs collapsed
    instead of deleted, so a missing space stopped looking like a moved one - every carry
    made under the looser reading stayed in the ledger, including a spacing defect sitting
    on the spelling that had just fixed the spacing.
    """
    dropped = 0
    for pair, recs in list(doc.items()):
        keep = []
        for rec in recs:
            if (isinstance(rec, dict) and rec.get('carried_by') == 'wording'
                    and isinstance(rec.get('ko_judged'), str)
                    and qamod.wording_key(rec.get('en', ''), rec['ko_judged'])
                    != qamod.wording_key(rec.get('en', ''), rec.get('ko', ''))):
                dropped += 1
                continue
            keep.append(rec)
        if len(keep) != len(recs):
            if keep:
                doc[pair] = keep
            else:
                del doc[pair]
    return dropped


def obsolete_waivers(doc, waivers, moves):
    """Waivers the gate reports as stale and no carry ground can move.

    Staleness is the GATE's judgement, not a second copy of it here: a waiver is stale when
    the build never reaches for it, either because the pair it names is not shipped or
    because that pair now passes on its own. Both mean the same thing - the decision grants
    nothing where it sits, and the gate refuses the build until someone looks.

    Removing one grants nothing either: if the row is still a problem the gate now says so
    out loud, which is the point. Never automatic - `--prune` is a separate word for a
    separate act.
    """
    movable = {m[1] for m in moves}
    stale = qagate.validate(manmod.load()['entries'])[2]
    return sorted(p for p in stale if p in waivers and p not in movable)


def main(argv):
    apply = '--apply' in argv
    prune = '--prune' in argv
    path = qamod.QA_PATH()
    if not os.path.exists(path):
        print(f'no verdict file at {path}')
        return 0
    # The panel lock, not a second scheme: a carry racing a judging batch would be
    # silently discarded by whichever process rewrites the document last.
    lock = qamod.hold_panel_lock()
    doc = qamod.load()
    # Before planning: a carry whose ground has since been tightened is not evidence, and
    # leaving it in place would also hide the row from the plan below.
    revoked = revoke_stale_carries(doc)
    if revoked:
        print(f'revoked      : {revoked} carried verdicts whose ground no longer holds')
    wpath = qagate.WAIVERS()
    waivers = (config.load_object(wpath, 'the waiver file')
               if os.path.exists(wpath) else {})
    moves, waiver_moves = plan(doc, waivers)
    print(f'verdict file : {path}')
    print(f'pairs        : {len(doc)}')
    print(f'to carry     : {len(moves)} rows whose sealed value is what today\'s rules '
          f'derive from the judged text')
    obsolete = obsolete_waivers(doc, waivers, waiver_moves)
    print(f'waivers      : {len(waiver_moves)} to re-key of {len(waivers)}, '
          f'{len(obsolete)} obsolete'
          + ('' if prune else ' (pass --prune to remove)'))
    grounds = collections.Counter(m[4] for m in moves)
    grounds.update(m[3] for m in waiver_moves)
    print(f'grounds      : {dict(grounds)}')
    for mkey, source_pair, target, _ko, ground in moves[:5]:
        print(f'  {mkey}  {source_pair} -> {target}  ({ground})')
    if not moves and not waiver_moves and not revoked and not (prune and obsolete):
        print('nothing to do')
        return 0
    if not apply:
        print('\nread-only: pass --apply to write')
        return 0
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = f'{path}.pre-carry-{stamp}'
    shutil.copy2(path, backup)
    print(f'backup       : {backup}')
    carried = carry(doc, moves)
    # `force`: a plain save is throttled to one write per interval, and a maintenance
    # command writes exactly once - silently skipping it would report a carry that never
    # reached the disk.
    qamod.save(doc, lock, force=True)
    rekeyed = 0
    if waiver_moves or (prune and obsolete):
        shutil.copy2(wpath, f'{wpath}.pre-carry-{stamp}')
        rekeyed, superseded = move_waivers(waivers, waiver_moves)
        if superseded:
            print(f'superseded   : {superseded} duplicate waivers on a shared pair')
        if prune:
            for pair in obsolete:
                w = waivers.pop(pair, None)
                if w is not None:
                    print(f'  pruned {pair}  {w.get("key")}  {w.get("category")}')
            print(f'pruned       : {len(obsolete)} waivers for text this build '
                  f'no longer ships')
        tmp = f'{wpath}.{os.getpid()}.tmp'
        with open(tmp, 'w') as fh:
            json.dump(waivers, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, wpath)
    # Re-read through the validating loader, not `json.load`: the point of the re-read
    # is to prove the ledger on disk is still a ledger, and that the carry actually
    # settled the rows it claimed.
    left, left_w = plan(qamod.load(),
                        config.load_object(wpath, 'the waiver file')
                        if os.path.exists(wpath) else {})
    print(f'carried      : {carried} verdicts, {rekeyed} waivers; '
          f'rows still uncarried: {len(left)} verdicts, {len(left_w)} waivers')
    return 1 if left or left_w else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

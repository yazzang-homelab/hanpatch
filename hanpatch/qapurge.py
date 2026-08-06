"""Drop verdict records that are damage rather than judgment.

Why this exists: between 2026-08-02 and 2026-08-06 this box wrote the DQ7 verdict ledger
through DRAM that flipped bits under load (`memtester` FAILURE at matching offsets, the
same file hashing differently on consecutive reads, segfaults across unrelated binaries).
The corruption is small and it is not random in effect: `qagate.record_problem` treats a
score outside 1..5 or a record that does not carry its own pair as an integrity failure,
so a single flipped byte blocks that pair forever - normal judges cannot outvote it,
because it is not a vote.

A judgment is never removed here. Only records that no judge could have written are, and
each one is written to a side file first, so the deletion stays auditable.

Run this AFTER the memory is known good: rewriting an 80MB ledger on faulty DRAM is how
new corruption gets in. `save()` hashes serialisation-to-disk, not the object it was
handed.

Idempotent: a second run finds nothing. Read-only unless `--apply` is given.
"""
import json
import os
import shutil
import sys
import time

from hanpatch import qa as qamod


def record_damage(pk, rec):
    """Why this record cannot be a judgment about `pk`, or None.

    Deliberately narrower than `qagate.record_problem`: that one is asked about a specific
    shipped pair and answers with policy as well as integrity. This one only asks whether
    the record is internally coherent, so a stale pair that is simply no longer shipped is
    left alone.
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
        return 'score out of range'
    if qamod.pair_key(rec['en'], rec['ko']) != pk:
        return 'record does not hash to its own key'
    if not str(rec['judge']).strip():
        return 'no judge recorded'
    if rec['judge'] not in qamod.JUDGES:
        return 'unknown judge'
    return None


def plan(doc):
    """(damaged records, per-reason counts, pairs left with no verdict at all)."""
    damaged, reasons, emptied = [], {}, 0
    for pk, recs in doc.items():
        if not isinstance(recs, list):
            continue
        bad = [(rec, record_damage(pk, rec)) for rec in recs]
        bad = [(rec, why) for rec, why in bad if why is not None]
        for rec, why in bad:
            reasons[why] = reasons.get(why, 0) + 1
            damaged.append({'pair_key': pk, 'reason': why, 'record': rec})
        if bad and len(bad) == len(recs):
            emptied += 1
    return damaged, reasons, emptied


def purge(doc):
    """Remove damaged records in place; drop pairs that keep nothing."""
    for pk in list(doc):
        recs = doc[pk]
        if not isinstance(recs, list):
            del doc[pk]
            continue
        kept = [r for r in recs if record_damage(pk, r) is None]
        if kept:
            doc[pk] = kept
        else:
            del doc[pk]
    return doc


def main(argv):
    apply = '--apply' in argv
    path = qamod.QA_PATH()
    if not os.path.exists(path):
        print(f'no verdict file at {path}')
        return 0
    # The panel lock, not a second scheme: a purge racing a batch write would be discarded
    # by whichever process rewrites the document last.
    lock = qamod.hold_panel_lock()
    doc = qamod.load()
    damaged, reasons, emptied = plan(doc)
    print(f'verdict file : {path}')
    print(f'pairs        : {len(doc)}')
    print(f'records      : {sum(len(v) for v in doc.values() if isinstance(v, list))}')
    print(f'to drop      : {len(damaged)} record(s); '
          f'{emptied} pair(s) would be left with none')
    for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f'  {n:8d}  {why}')
    if not damaged:
        print('nothing to do')
        return 0
    if not apply:
        print('\nread-only: pass --apply to write')
        return 0
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = f'{path}.pre-purge-{stamp}'
    shutil.copy2(path, backup)
    quarantine = f'{path}.purged-{stamp}.json'
    with open(quarantine, 'w') as fh:
        json.dump(damaged, fh, ensure_ascii=False, indent=1)
    print(f'backup       : {backup}')
    print(f'quarantine   : {quarantine}')
    qamod.save(purge(doc), lock)
    # Re-read through the validating loader: the point of the re-read is to prove the
    # ledger on disk is still a ledger.
    left = len(plan(qamod.load())[0])
    print(f'dropped      : {len(damaged)}; still damaged after the write: {left}')
    return 1 if left else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

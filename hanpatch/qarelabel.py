"""Rewrite verdicts filed under a lane that lied about which model judged them.

Why this exists: the agy-c (company) account offered neither GPT-OSS nor a working Gemini 3
Pro lane, so the antigravity CLI silently overrode the requested model to
"Gemini 3.6 Flash (High)" - measured in the CLI logs, 710 of 712 invocations on 2026-08-03
and 563 of 563 on 2026-08-04. Every verdict recorded through `agy:*-biz` is therefore a
Gemini 3.6 Flash verdict wearing one of three different names, and three names of one model
let a single judge satisfy a panel that requires independent judges.

Deleting those verdicts would be the lazy answer and it is the expensive one: on the DQ7
corpus it drops the clean-panel count from 26365 to 15067 and throws away 39236 real
judgments. Renaming them to the model that actually answered keeps the evidence and lets the
existing independence rule reach the right conclusion by itself - measured cost of the
rename: 159 pairs, exactly the ones whose "panel" was one model twice.

Idempotent: a second run finds nothing to rename. Read-only unless `--apply` is given.
"""
import os
import shutil
import sys
import time

from hanpatch import qa as qamod

TRUE_IDENTITY = 'agy:gemini-3.6-flash'


def plan(doc):
    """(pairs_touched, verdicts_touched, per-lane counts) for the pending rename."""
    lanes = {}
    pairs = verdicts = 0
    for recs in doc.values():
        hit = False
        for rec in recs:
            if isinstance(rec, dict) and rec.get('judge') in qamod.REVOKED_GATEWAY_LANES:
                lanes[rec['judge']] = lanes.get(rec['judge'], 0) + 1
                verdicts += 1
                hit = True
        pairs += hit
    return pairs, verdicts, lanes


def relabel(doc):
    """Rename in place, recording the original lane so the rewrite stays auditable."""
    for recs in doc.values():
        for rec in recs:
            if isinstance(rec, dict) and rec.get('judge') in qamod.REVOKED_GATEWAY_LANES:
                rec['judge_recorded_as'] = rec['judge']
                rec['judge'] = TRUE_IDENTITY
    return doc


def main(argv):
    apply = '--apply' in argv
    path = qamod.QA_PATH()
    if not os.path.exists(path):
        print(f'no verdict file at {path}')
        return 0
    # The panel lock, not a second scheme: a rename racing a batch write would be silently
    # discarded by whichever process rewrites the document last.
    lock = qamod.hold_panel_lock()
    doc = qamod.load()
    pairs, verdicts, lanes = plan(doc)
    print(f'verdict file : {path}')
    print(f'pairs        : {len(doc)}')
    print(f'to rename    : {verdicts} verdicts across {pairs} pairs -> {TRUE_IDENTITY}')
    for lane, n in sorted(lanes.items(), key=lambda kv: -kv[1]):
        print(f'  {n:8d}  {lane}')
    if not verdicts:
        print('nothing to do')
        return 0
    if not apply:
        print('\nread-only: pass --apply to write')
        return 0
    backup = f'{path}.pre-relabel-{time.strftime("%Y%m%d-%H%M%S")}'
    shutil.copy2(path, backup)
    print(f'backup       : {backup}')
    qamod.save(relabel(doc), lock)
    # Re-read through the validating loader, not `json.load`: the point of the re-read is to
    # prove the ledger on disk is still a ledger.
    left = plan(qamod.load())[1]
    print(f'renamed      : {verdicts}; remaining under a revoked lane: {left}')
    return 1 if left else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

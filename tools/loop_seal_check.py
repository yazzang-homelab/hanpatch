#!/usr/bin/env python3
"""Prove that a worktree holds the exact strings a gate run sealed.

This runs as a bridge check suite, which means it runs INSIDE a git worktree of
a title repository - not in the project directory. That distinction is the whole
reason this file exists:

  * The authoritative proof of a passing run is the sealed manifest, but that is
    a gitignored multi-megabyte artefact derived from files a worktree does not
    have (the ROM, the built fonts, the hundreds-of-megabytes verdict ledger).
    A check running in the worktree cannot re-derive it.
  * Hashing "the translation file" does not work either, because titles do not
    agree on what that is: dq7 and Crimson Shroud keep work/<target>/text_ko.json,
    while Classic Dungeon X2 keeps its translation in ~700 per-container shards.
    A check that hashed whichever file existed would have sealed nothing at all
    for that title while still reporting success.

So `hanpatch loop gate` writes the passing manifest's entries - which are already
exactly {key: translation} for every title - to loop/sealed-text.json with sorted
keys, and records that file's sha256 and entry count in loop/state.json. This
check compares the committed file against those two recorded facts.

It answers exactly one question and refuses to guess at any other: does this
worktree hold the strings that passed, or something else?

It is deliberately kept outside the title repositories. The bridge invokes it by
absolute path with the worktree as the working directory, so a model working in
the worktree cannot edit the check that judges its own output.

Exit 0 when sealed, 1 when not. Diagnostics go to stdout because the bridge
captures and reports them to the caller.
"""
import hashlib
import json
import os
import sys

STATE = os.path.join('loop', 'state.json')
SEALED = os.path.join('loop', 'sealed-text.json')


def fail(message):
    print(f'SEAL FAIL: {message}')
    return 1


def load(path, what):
    if not os.path.exists(path):
        return None, f'{what} is missing: {path}'
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except ValueError as exc:
        return None, f'{what} is not valid JSON: {path}: {exc}'
    if not isinstance(data, dict):
        return None, (f'{what} must be a JSON object: {path} holds a '
                      f'{type(data).__name__}')
    return data, None


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    state, err = load(STATE, 'the loop state')
    if err:
        return fail(err)

    gate = state.get('lastGate')
    if not isinstance(gate, dict) or not gate:
        return fail('no gate run is recorded in the loop state; run '
                    '`hanpatch loop gate` in the project directory first')
    if gate.get('ok') is not True:
        stage = gate.get('stage') or 'unknown'
        detail = str(gate.get('detail') or '')[:400]
        return fail(f'the last gate run did not pass (stage {stage}): {detail}')

    recorded = gate.get('textSha256')
    if not isinstance(recorded, str) or len(recorded) != 64:
        return fail('the passing gate run recorded no usable textSha256, so '
                    'there is nothing to compare this worktree against')

    sealed, err = load(SEALED, 'the sealed strings')
    if err:
        return fail(f'{err}. `hanpatch loop gate` writes it on a passing run, '
                    'and it has to be committed for this branch to be checkable.')

    actual = sha256(SEALED)
    if actual != recorded:
        return fail(f'{SEALED} is not the file that passed the gates '
                    f'(sealed {recorded[:16]}, worktree {actual[:16]}). Re-run '
                    '`hanpatch loop gate` so the seal covers these strings.')

    # The count is recorded separately by the loop, so a sealed file that hashes
    # correctly but was produced from a different run's entry set is still
    # caught. Absent on older state, in which case the hash alone stands.
    expected = gate.get('sealedEntries')
    if isinstance(expected, int) and expected > 0 and len(sealed) != expected:
        return fail(f'{SEALED} holds {len(sealed)} strings but the passing run '
                    f'sealed {expected}')

    print(f'SEAL OK: {SEALED} matches the passing gate run '
          f'({len(sealed)} strings, sha256 {actual[:16]}, '
          f'manifest {gate.get("digest")})')
    return 0


if __name__ == '__main__':
    sys.exit(main())

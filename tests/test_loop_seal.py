"""The merge gate: a worktree may only publish the strings a gate run sealed.

`tools/loop_seal_check.py` is what stands between a repaired translation and a
merged pull request. It runs inside a git worktree, where the real proof (the
sealed manifest, the ROM, the verdict ledger) does not exist, so it checks the
one thing it can: that `loop/sealed-text.json` is byte-identical to what the
passing gate recorded, and holds the same number of strings.

These cases are the reason it exists, so they assert the refusals rather than
just the success path.
"""
import hashlib
import json
import os
import subprocess
import sys

CHECK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'tools', 'loop_seal_check.py')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def write_sealed(work, entries):
    path = os.path.join(work, 'loop', 'sealed-text.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(entries, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write('\n')
    return path


def write_state(work, **gate):
    path = os.path.join(work, 'loop', 'state.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump({'iteration': 7, 'lastGate': gate}, fh)


def run(work):
    return subprocess.run([sys.executable, CHECK], cwd=work,
                          capture_output=True, text=True)


def fixture(tmp_path, n=40):
    entries = {f'file{i // 8}.dat/off{i}': f'번역 {i}' for i in range(n)}
    sealed = write_sealed(str(tmp_path), entries)
    return sealed, sha256(sealed), len(entries)


def test_sealed_worktree_passes(tmp_path):
    _sealed, digest, n = fixture(tmp_path)
    write_state(tmp_path, ok=True, stage=None, digest='abc123',
                textSha256=digest, sealedEntries=n)
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout
    assert 'SEAL OK' in result.stdout


def test_gate_that_did_not_pass_is_refused(tmp_path):
    _sealed, digest, n = fixture(tmp_path)
    write_state(tmp_path, ok=False, stage='audit', detail='393 rows',
                digest='abc123', textSha256=digest, sealedEntries=n)
    result = run(tmp_path)
    assert result.returncode == 1
    assert 'did not pass' in result.stdout
    assert 'audit' in result.stdout


def test_text_edited_after_sealing_is_refused(tmp_path):
    sealed, digest, n = fixture(tmp_path)
    write_state(tmp_path, ok=True, stage=None, digest='abc123',
                textSha256=digest, sealedEntries=n)
    doc = json.load(open(sealed, encoding='utf-8'))
    key = sorted(doc)[0]
    doc[key] += '!'
    with open(sealed, 'w', encoding='utf-8') as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write('\n')
    result = run(tmp_path)
    assert result.returncode == 1
    assert 'not the file that passed' in result.stdout


def test_entry_count_mismatch_is_refused(tmp_path):
    # Catches a sealed file that hashes to a recorded value from a different
    # run: the hash alone cannot tell those apart if state is hand-edited.
    _sealed, digest, n = fixture(tmp_path)
    write_state(tmp_path, ok=True, stage=None, digest='abc123',
                textSha256=digest, sealedEntries=n + 5)
    result = run(tmp_path)
    assert result.returncode == 1
    assert 'strings but the passing run sealed' in result.stdout


def test_missing_seal_hash_is_refused(tmp_path):
    _sealed, _digest, n = fixture(tmp_path)
    write_state(tmp_path, ok=True, stage=None, digest='abc123',
                textSha256=None, sealedEntries=n)
    result = run(tmp_path)
    assert result.returncode == 1
    assert 'no usable textSha256' in result.stdout


def test_missing_sealed_file_is_refused(tmp_path):
    sealed, digest, n = fixture(tmp_path)
    os.remove(sealed)
    write_state(tmp_path, ok=True, stage=None, digest='abc123',
                textSha256=digest, sealedEntries=n)
    result = run(tmp_path)
    assert result.returncode == 1
    assert 'sealed strings is missing' in result.stdout


def test_missing_state_is_refused(tmp_path):
    fixture(tmp_path)
    result = run(tmp_path)
    assert result.returncode == 1
    assert 'loop state is missing' in result.stdout

"""Patch distribution.

Shipping a patched ROM means shipping the game. Shipping a **delta** means the
recipient applies your work to their own copy. This module produces and applies
those deltas, and refuses to apply one to the wrong input.

Backends, in preference order:

``xdelta3``
    Used when the binary is on PATH. Produces the `.xdelta` files the
    translation scene already expects, and handles a 250 MB ROM in seconds.

``hpd`` (built in, no dependencies)
    A block-delta container written here so the tooling works with nothing
    installed. Fixed-size blocks are hashed on both sides; changed blocks are
    stored raw and deflated. For a text patch — a few megabytes changed inside a
    250 MB image — this is within a rounding error of an optimal delta, because
    the changes *are* block-local. It is not a general-purpose binary differ and
    does not pretend to be: an insertion that shifts every following byte
    degrades to storing everything after it.

Every patch records the SHA-256 of both sides. `apply` verifies the source
before writing and the result afterwards, so a mismatched ROM revision fails
loudly instead of producing a corrupt image.
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import zlib

MAGIC = b'HPD1'
BLOCK = 1 << 16
CHUNK = 1 << 22


def sha256(path, progress=None):
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    with open(path, 'rb') as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done, total)
    return h.hexdigest()


def have_xdelta():
    return shutil.which('xdelta3') is not None


# ---------------------------------------------------------------- hpd backend

def _blocks(path, block=BLOCK):
    with open(path, 'rb') as f:
        while True:
            b = f.read(block)
            if not b:
                return
            yield b


def hpd_create(old, new, out, block=BLOCK, meta=None):
    """Write a block delta from `old` to `new`.

    Layout: magic, a JSON header, then for each changed block a record of
    (index, stored length, deflate payload). Unchanged blocks cost 4 bytes of
    index in the header's run list and nothing else.
    """
    old_size = os.path.getsize(old)
    new_size = os.path.getsize(new)
    old_hashes = [hashlib.sha256(b).digest()[:8] for b in _blocks(old, block)]

    records = []
    payload = bytearray()
    for i, b in enumerate(_blocks(new, block)):
        same = (i < len(old_hashes)
                and hashlib.sha256(b).digest()[:8] == old_hashes[i])
        if same:
            continue
        comp = zlib.compress(b, 9)
        records.append((i, len(b), len(comp)))
        payload += comp

    header = {
        'block': block,
        'old_size': old_size,
        'new_size': new_size,
        'old_sha256': sha256(old),
        'new_sha256': sha256(new),
        'records': [[i, raw, comp] for i, raw, comp in records],
        'meta': meta or {},
    }
    hj = json.dumps(header, separators=(',', ':')).encode()
    with open(out, 'wb') as o:
        o.write(MAGIC)
        o.write(struct.pack('<I', len(hj)))
        o.write(hj)
        o.write(bytes(payload))
    return {'changed_blocks': len(records), 'total_blocks':
            (new_size + block - 1) // block, 'size': os.path.getsize(out)}


def hpd_read_header(path):
    with open(path, 'rb') as f:
        if f.read(4) != MAGIC:
            raise ValueError(f'{path}: not an hpd patch')
        n, = struct.unpack('<I', f.read(4))
        return json.loads(f.read(n)), 8 + n


def hpd_apply(old, patch, out, verify=True):
    header, data_off = hpd_read_header(patch)
    block = header['block']
    if verify:
        got = sha256(old)
        if got != header['old_sha256']:
            raise ValueError(
                f'source mismatch: this patch is for sha256 '
                f'{header["old_sha256"][:16]}… but the given file is '
                f'{got[:16]}…. Wrong ROM, wrong region, or already patched.')

    changed = {}
    with open(patch, 'rb') as pf:
        pf.seek(data_off)
        for i, raw, comp in header['records']:
            changed[i] = zlib.decompress(pf.read(comp))

    with open(old, 'rb') as f, open(out, 'wb') as o:
        total = header['new_size']
        i = 0
        written = 0
        while written < total:
            if i in changed:
                b = changed[i]
                f.seek(min((i + 1) * block, header['old_size']))
            else:
                f.seek(i * block)
                b = f.read(block)
                if not b:
                    raise ValueError(f'source ended early at block {i}')
            b = b[:total - written]
            o.write(b)
            written += len(b)
            i += 1

    if verify:
        got = sha256(out)
        if got != header['new_sha256']:
            raise ValueError(f'result mismatch: produced {got[:16]}…, expected '
                             f'{header["new_sha256"][:16]}…')
    return {'size': os.path.getsize(out), 'sha256': header['new_sha256']}


# ------------------------------------------------------------- xdelta backend

def xdelta_create(old, new, out, meta=None):
    cmd = ['xdelta3', '-e', '-9', '-S', 'djw', '-B', str(1 << 28),
           '-f', '-s', old, new, out]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f'xdelta3 failed: {r.stderr.decode()[:400]}')
    side = out + '.json'
    json.dump({'old_sha256': sha256(old), 'new_sha256': sha256(new),
               'meta': meta or {}}, open(side, 'w'), indent=1)
    return {'size': os.path.getsize(out), 'sidecar': side}


def xdelta_apply(old, patch, out, verify=True):
    side = patch + '.json'
    info = json.load(open(side)) if os.path.exists(side) else {}
    if verify and info.get('old_sha256'):
        got = sha256(old)
        if got != info['old_sha256']:
            raise ValueError(f'source mismatch: patch expects '
                             f'{info["old_sha256"][:16]}…, got {got[:16]}…')
    r = subprocess.run(['xdelta3', '-d', '-f', '-s', old, patch, out],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f'xdelta3 failed: {r.stderr.decode()[:400]}')
    if verify and info.get('new_sha256'):
        got = sha256(out)
        if got != info['new_sha256']:
            raise ValueError(f'result mismatch: produced {got[:16]}…')
    return {'size': os.path.getsize(out)}


# ---------------------------------------------------------------- dispatch

def create(old, new, out, backend='auto', meta=None):
    if backend == 'auto':
        backend = 'xdelta' if have_xdelta() else 'hpd'
    if backend == 'xdelta':
        res = xdelta_create(old, new, out, meta=meta)
    elif backend == 'hpd':
        res = hpd_create(old, new, out, meta=meta)
    else:
        raise ValueError(f'unknown backend {backend!r}')
    res['backend'] = backend
    res['ratio'] = res['size'] / max(1, os.path.getsize(new))
    return res


def apply(old, patch, out, backend='auto', verify=True):
    if backend == 'auto':
        with open(patch, 'rb') as f:
            backend = 'hpd' if f.read(4) == MAGIC else 'xdelta'
    if backend == 'hpd':
        return hpd_apply(old, patch, out, verify=verify)
    return xdelta_apply(old, patch, out, verify=verify)


APPLIER = r'''#!/usr/bin/env python3
"""Standalone applier for a hanpatch .hpd delta. No dependencies.

    python3 apply_patch.py <original ROM> <patch.hpd> <output>
"""
import hashlib, json, os, struct, sys, zlib

MAGIC = b'HPD1'
CHUNK = 1 << 22


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def main(old, patch, out):
    with open(patch, 'rb') as f:
        if f.read(4) != MAGIC:
            sys.exit('not an hpd patch')
        n, = struct.unpack('<I', f.read(4))
        hdr = json.loads(f.read(n))
        data_off = 8 + n
    got = sha256(old)
    if got != hdr['old_sha256']:
        sys.exit(f"wrong source file\n  expected sha256 {hdr['old_sha256']}\n"
                 f"  got             {got}")
    block = hdr['block']
    changed = {}
    with open(patch, 'rb') as pf:
        pf.seek(data_off)
        for i, raw, comp in hdr['records']:
            changed[i] = zlib.decompress(pf.read(comp))
    total = hdr['new_size']
    with open(old, 'rb') as f, open(out, 'wb') as o:
        i = written = 0
        while written < total:
            if i in changed:
                b = changed[i]
            else:
                f.seek(i * block)
                b = f.read(block)
            b = b[:total - written]
            o.write(b)
            written += len(b)
            i += 1
    if sha256(out) != hdr['new_sha256']:
        sys.exit('result hash mismatch; output is not trustworthy')
    print(f'ok: {out}')


if __name__ == '__main__':
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
'''


def write_applier(path):
    with open(path, 'w') as f:
        f.write(APPLIER)
    os.chmod(path, 0o755)
    return path

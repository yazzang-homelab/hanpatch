"""CIA ticket handling and title-key content decryption.

Contents inside a CIA may be encrypted a second time, with the title key from
the embedded ticket (AES-CBC, IV = the content index). Installable files taken
straight from a CDN or a console dump are in that state; files that a tool has
already decrypted are not, and the per-content type flags say which.

The title key itself is wrapped with a common key, which hanpatch does not ship.
Supply it (``common0``…``common5`` in ``keys.txt``, plus ``slot0x3DKeyX`` or a
``boot9.bin``) and this module unwraps it; every candidate is validated by
checking that the decrypted content actually starts with an NCCH header, so a
wrong common-key index cannot pass silently.
"""
import os
import struct

from Crypto.Cipher import AES

from hanpatch.platforms.threeds import keys as keysmod

CHUNK = 1 << 22
TYPE_ENCRYPTED = 0x0001


def parse_ticket(tik):
    """(title_id, encrypted_titlekey, common_key_index) from a ticket blob."""
    sig_type, = struct.unpack('>I', tik[:4])
    from hanpatch.platforms.threeds.repack import SIG_SIZES
    body = tik[4 + SIG_SIZES[sig_type]:]
    enc_titlekey = body[0x7F:0x8F]
    title_id, = struct.unpack('>Q', body[0x9C:0xA4])
    common_index = body[0xB1]
    return title_id, enc_titlekey, common_index


def is_encrypted(chunk):
    return bool(chunk['type'] & TYPE_ENCRYPTED)


def _looks_like_ncch(head):
    return head[0x100:0x104] == b'NCCH'


def resolve_titlekey(cia, keystore=None):
    """Decrypt the ticket's title key, or None when key material is missing."""
    ks = keystore if keystore is not None else keysmod.store()
    title_id, enc, idx = parse_ticket(cia.tik)
    tk = ks.titlekey(enc, title_id, idx)
    if tk:
        return tk, idx
    for i, cand in ks.titlekey_candidates(enc, title_id):
        return cand, i
    return None, idx


def decrypt_content(cia, chunk, out, titlekey):
    """Stream one title-key-encrypted content to `out`, returning its size."""
    iv = struct.pack('>H', chunk['idx']) + b'\0' * 14
    c = AES.new(titlekey, AES.MODE_CBC, iv)
    f = open(cia.path, 'rb')
    f.seek(chunk['offset'])
    left = chunk['size']
    written = 0
    with open(out, 'wb') as o:
        while left:
            n = min(CHUNK, left)
            n -= n % 16
            if n == 0:
                n = left
            b = f.read(n)
            if not b:
                break
            o.write(c.decrypt(b))
            written += len(b)
            left -= len(b)
    return written


def prepare_content(cia, idx=0, workdir=None, keystore=None):
    """Give back a path/offset pair whose bytes are a plaintext NCCH.

    Unencrypted contents are read in place. Encrypted ones are decrypted into
    `workdir` once and reused. Raises with an actionable message when the key
    material needed is absent.
    """
    chunk = next(c for c in cia.chunks if c['idx'] == idx)
    f = open(cia.path, 'rb')
    f.seek(chunk['offset'])
    head = f.read(0x200)
    if _looks_like_ncch(head):
        return cia.path, chunk['offset'], False

    if not is_encrypted(chunk):
        raise ValueError(
            f'content {idx} is not marked encrypted yet has no NCCH header; '
            f'the file looks damaged')

    ks = keystore if keystore is not None else keysmod.store()
    title_id, enc, declared = parse_ticket(cia.tik)
    cands = ks.titlekey_candidates(enc, title_id)
    if not cands:
        raise ValueError(
            'this CIA carries title-key encrypted content. hanpatch ships no '
            'key material; supply the common key and slot 0x3D KeyX (a '
            'boot9.bin, or common0..common5 in keys.txt) and retry.\n'
            + ks.describe())

    workdir = workdir or os.path.dirname(os.path.abspath(cia.path))
    os.makedirs(workdir, exist_ok=True)
    out = os.path.join(workdir, f'content{idx}.dec')
    order = ([c for c in cands if c[0] == declared]
             + [c for c in cands if c[0] != declared])
    for cidx, tk in order:
        iv = struct.pack('>H', chunk['idx']) + b'\0' * 14
        probe = AES.new(tk, AES.MODE_CBC, iv).decrypt(head)
        if not _looks_like_ncch(probe):
            continue
        decrypt_content(cia, chunk, out, tk)
        return out, 0, True
    raise ValueError(
        f'none of the {len(cands)} available common keys decrypt content {idx} '
        f'of title {title_id:016X}; the ticket or the key file is wrong.\n'
        + ks.describe())


def describe(cia, keystore=None):
    ks = keystore if keystore is not None else keysmod.store()
    title_id, enc, idx = parse_ticket(cia.tik)
    lines = [f'title      {title_id:016X}',
             f'contents   {cia.content_count}',
             f'common key index {idx}']
    for c in cia.chunks:
        lines.append(f"  content {c['idx']} size={c['size']:#x} "
                     f"{'titlekey-encrypted' if is_encrypted(c) else 'plain'}")
    tk, used = resolve_titlekey(cia, ks)
    lines.append(f'titlekey   {"available" if tk else "MISSING key material"}')
    return '\n'.join(lines)

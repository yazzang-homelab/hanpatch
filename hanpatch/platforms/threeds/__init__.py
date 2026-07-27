"""Nintendo 3DS container layer.

Everything here is title-independent: CIA/NCCH crypto (fixed-key method 0, no
boot9 needed), the IVFC RomFS reader and builder, BLZ, and BCFNT.  A title
adapter uses these to get at its own archives.
"""
import hashlib
import os
import struct

from hanpatch.platforms.threeds import cia as ciamod
from hanpatch.platforms.threeds import keys as keysmod
from hanpatch.platforms.threeds import ncch as ncchmod
from hanpatch.platforms.threeds import ncsd as ncsdmod
from hanpatch.platforms.threeds import repack, romfs, romfs_build

CHUNK = 1 << 20


def detect(path):
    """'cia', 'cci' or 'ncch' — the three shapes a 3DS title arrives in."""
    with open(path, 'rb') as f:
        head = f.read(0x200)
    if head[0x100:0x104] == b'NCSD':
        return 'cci'
    if head[0x100:0x104] == b'NCCH':
        return 'ncch'
    if len(head) >= 0x20 and int.from_bytes(head[:4], 'little') in (0x2020,):
        return 'cia'
    # CIA header size is the first field; accept any plausible value with a
    # ticket/TMD laid out where the header says
    try:
        repack.Cia(path)
        return 'cia'
    except Exception:
        raise ValueError(f'{path}: not a CIA, CCI or NCCH')


def content_offset(cia):
    """Byte offset of the first content (the executable NCCH) inside a CIA."""
    return repack.Cia(cia).chunks[0]['offset']


def open_ncch(path, workdir=None, keystore=None):
    """Open the executable NCCH of a CIA, CCI or bare NCCH.

    Title-key encrypted CIA contents are decrypted to `workdir` first. Raises
    with an actionable message when the operator's key material is insufficient.
    """
    kind = detect(path)
    if kind == 'ncch':
        return ncchmod.NCCH(path, 0, keystore=keystore)
    if kind == 'cci':
        p = ncsdmod.Ncsd(path).partition(0)
        return ncchmod.NCCH(path, p['offset'], keystore=keystore)
    c = repack.Cia(path)
    plain, base, _ = ciamod.prepare_content(c, c.chunks[0]['idx'],
                                            workdir=workdir, keystore=keystore)
    return ncchmod.NCCH(plain, base, keystore=keystore)


def dump(cia, out, keystore=None):
    """Decrypt a title's executable content into `out`.

    Accepts CIA, CCI or a bare NCCH. Writes exheader.bin, ncch_header.bin,
    exefs/<name> and romfs.bin.
    """
    os.makedirs(out, exist_ok=True)
    n = open_ncch(cia, workdir=out, keystore=keystore)
    open(f'{out}/exheader.bin', 'wb').write(n.exheader())
    open(f'{out}/ncch_header.bin', 'wb').write(n.h)

    os.makedirs(f'{out}/exefs', exist_ok=True)
    names = []
    # icon/banner/logo stay on the primary key even under secondary crypto,
    # so each entry is read with the key its own name implies
    for name, off, size, secondary in n.exefs_files():
        buf = bytearray()
        pos = 0
        while pos < size:
            ln = min(CHUNK, ((size - pos + 15) // 16) * 16)
            buf += n.exefs(0x200 + off + pos, ln, secondary=secondary)
            pos += ln
        open(f'{out}/exefs/{name}', 'wb').write(bytes(buf[:size]))
        names.append(name)

    total = n.romfs_size * 0x200
    with open(f'{out}/romfs.bin', 'wb') as o:
        pos = 0
        while pos < total:
            ln = min(CHUNK, total - pos)
            o.write(n.romfs(pos, ln))
            pos += ln
    return {'exefs': names, 'romfs_size': total}


def unpack_romfs(romfs_bin, out):
    """Write every file of a RomFS image into `out`, returning path -> size."""
    r = romfs.RomFS(romfs_bin)
    sizes = {}
    for path, off, size in r.walk():
        dst = os.path.join(out, path.lstrip('/'))
        os.makedirs(os.path.dirname(dst) or '.', exist_ok=True)
        with open(dst, 'wb') as o:
            pos = 0
            while pos < size:
                ln = min(CHUNK, size - pos)
                o.write(r.read(off + pos, ln))
                pos += ln
        sizes[path] = size
    return sizes


def read_romfs_file(romfs_bin, path):
    r = romfs.RomFS(romfs_bin)
    for p, off, size in r.walk():
        if p == path:
            return r.read(off, size)
    raise KeyError(path)


def build_romfs(stage_dir, out):
    return romfs_build.write_romfs(stage_dir, out)


def rebuild_cia(original, romfs_bin, out, keystore=None):
    """Swap a new RomFS into a CIA, fixing every hash/signature-adjacent field."""
    return repack.rebuild(original, romfs_bin, out, keystore=keystore)


def rebuild(original, romfs_bin, out, keystore=None):
    """Container-agnostic rebuild: CIA in, CIA out; CCI in, CCI out."""
    kind = detect(original)
    if kind == 'cia':
        return repack.rebuild(original, romfs_bin, out, keystore=keystore)
    if kind == 'ncch':
        repack.rebuild_ncch(original, 0, romfs_bin, out, keystore=keystore)
        return out
    p = ncsdmod.Ncsd(original).partition(0)
    tmp = out + '.part0'
    repack.rebuild_ncch(original, p['offset'], romfs_bin, tmp,
                        keystore=keystore)
    ncsdmod.rebuild(original, {0: tmp}, out)
    os.remove(tmp)
    return out


def content_hashes(cia):
    """TMD-declared vs actual SHA-256 for each content chunk.

    Only meaningful for CIA; other containers report an empty list.
    """
    if detect(cia) != 'cia':
        return []
    c = repack.Cia(cia)
    res = []
    for ch in c.chunks:
        f = open(cia, 'rb')
        f.seek(ch['offset'])
        h = hashlib.sha256()
        left = ch['size']
        while left:
            b = f.read(min(1 << 22, left))
            h.update(b)
            left -= len(b)
        res.append({'idx': ch['idx'], 'size': ch['size'],
                    'ok': h.digest() == ch['hash']})
    return res


def superblock_hashes(cia, keystore=None):
    """Verify the NCCH header's exheader/exefs/romfs superblock hashes."""
    n = open_ncch(cia, keystore=keystore)
    out = {}
    for label, off, data in [('exheader', 0x160, n.exheader()[:0x400]),
                             ('exefs', 0x1C0, n.exefs(0, 0x200)),
                             ('romfs', 0x1E0, n.romfs(0, 0x200))]:
        out[label] = hashlib.sha256(data).digest() == n.h[off:off + 0x20]
    return out

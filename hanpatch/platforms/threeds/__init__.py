"""Nintendo 3DS container layer.

Everything here is title-independent: CIA/NCCH crypto (fixed-key method 0, no
boot9 needed), the IVFC RomFS reader and builder, BLZ, and BCFNT.  A title
adapter uses these to get at its own archives.
"""
import hashlib
import os
import struct

from hanpatch.platforms.threeds import ncch as ncchmod
from hanpatch.platforms.threeds import repack, romfs, romfs_build

CHUNK = 1 << 20


def content_offset(cia):
    """Byte offset of the first content (the executable NCCH) inside a CIA."""
    return repack.Cia(cia).chunks[0]['offset']


def open_ncch(cia):
    return ncchmod.NCCH(cia, content_offset(cia))


def dump(cia, out):
    """Decrypt a CIA's first content into `out`: exheader, exefs/, romfs.bin."""
    os.makedirs(out, exist_ok=True)
    n = open_ncch(cia)
    open(f'{out}/exheader.bin', 'wb').write(n.exheader())
    open(f'{out}/ncch_header.bin', 'wb').write(n.h)

    exh = n.exefs(0, 0x200)
    os.makedirs(f'{out}/exefs', exist_ok=True)
    names = []
    for i in range(10):
        e = exh[i * 0x10:(i + 1) * 0x10]
        name = e[:8].rstrip(b'\0').decode()
        if not name:
            continue
        off, size = struct.unpack('<II', e[8:16])
        buf = bytearray()
        pos = 0
        while pos < size:
            ln = min(CHUNK, ((size - pos + 15) // 16) * 16)
            buf += n.exefs(0x200 + off + pos, ln)
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


def rebuild_cia(original, romfs_bin, out):
    """Swap a new RomFS into a CIA, fixing every hash/signature-adjacent field."""
    return repack.rebuild(original, romfs_bin, out)


def content_hashes(cia):
    """TMD-declared vs actual SHA-256 for each content chunk."""
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


def superblock_hashes(cia):
    """Verify the NCCH header's exheader/exefs/romfs superblock hashes."""
    n = open_ncch(cia)
    out = {}
    for label, off, data in [('exheader', 0x160, n.exheader()[:0x400]),
                             ('exefs', 0x1C0, n.exefs(0, 0x200)),
                             ('romfs', 0x1E0, n.romfs(0, 0x200))]:
        out[label] = hashlib.sha256(data).digest() == n.h[off:off + 0x20]
    return out

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
    """Unpack a RomFS image to a directory tree, INCLUDING empty directories.

    `RomFS.walk()` yields files only, so creating directories as a side effect of
    writing files silently dropped every empty one. That matters for a rebuild
    whose entry order is captured from the source image: the capture would describe
    a directory the staged tree cannot produce, the directory count would differ,
    and every metadata offset after it would shift.
    Returns path -> size for every file written.
    """
    r = romfs.RomFS(romfs_bin)
    sizes = {}
    for dpath in romfs_build.sibling_order(romfs_bin)['dir_layout']:
        os.makedirs(os.path.join(out, dpath.lstrip('/')), exist_ok=True)
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


def dump_romfs(rom, out, keystore=None):
    """Materialise a built container's decrypted RomFS image to `out`.

    Both adapters need this during verify, and both had the same streaming loop.
    Sharing the loop is safe because it is container-agnostic; staging and archive
    verification stay per-title, since those really do differ.
    """
    n = open_ncch(rom, workdir=os.path.dirname(out) or None, keystore=keystore)
    total = n.romfs_size * 0x200
    with open(out, 'wb') as o:
        pos = 0
        while pos < total:
            ln = min(1 << 22, total - pos)
            o.write(n.romfs(pos, ln))
            pos += ln
    return out


def build_romfs(stage_dir, out, order_from=None):
    return romfs_build.write_romfs(stage_dir, out, order_from=order_from)


def rebuild_cia(original, romfs_bin, out, keystore=None, exefs_replacements=None,
                decrypt=False):
    """Swap a new RomFS into a CIA, fixing every hash/signature-adjacent field."""
    return repack.rebuild(
        original, romfs_bin, out, keystore=keystore,
        exefs_replacements=exefs_replacements, decrypt=decrypt)


def rebuild(original, romfs_bin, out, keystore=None, exefs_replacements=None,
            decrypt=False):
    """Container-agnostic rebuild: CIA in, CIA out; CCI in, CCI out."""
    kind = detect(original)
    if kind == 'cia':
        return repack.rebuild(
            original, romfs_bin, out, keystore=keystore,
            exefs_replacements=exefs_replacements, decrypt=decrypt)
    if kind == 'ncch':
        repack.rebuild_ncch(
            original, 0, romfs_bin, out, keystore=keystore,
            exefs_replacements=exefs_replacements, decrypt=decrypt)
        return out
    p = ncsdmod.Ncsd(original).partition(0)
    tmp = out + '.part0'
    repack.rebuild_ncch(
        original, p['offset'], romfs_bin, tmp, keystore=keystore,
        exefs_replacements=exefs_replacements, decrypt=decrypt)
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
    """Verify the NCCH header's exheader/exefs/romfs superblock hashes.

    The ExeFS and RomFS hashes each cover the span their header DECLARES, in
    media units, not a fixed 0x200. Assuming one unit reported a real retail
    cartridge as corrupt - it declares two for RomFS - and, worse, the rebuild
    path had the same assumption baked in, so a shrunken region agreed with a
    shrunken check and both looked fine. The ExHeader is different: its hash
    covers a fixed 0x400 bytes and has no declared-size field, so it stays
    hard-coded.
    """
    n = open_ncch(cia, keystore=keystore)
    out = {}
    # A section that is ABSENT has nothing to verify and must not be reported as
    # corrupt; `or 1` used to conflate that with "present, region unspecified".
    if n.exh_size:
        out['exheader'] = (hashlib.sha256(n.exheader()[:0x400]).digest()
                           == n.h[0x160:0x180])
    for label, off, size_at, units_at, read in (
            ('exefs', 0x1C0, 0x1A4, 0x1A8, n.exefs),
            ('romfs', 0x1E0, 0x1B4, 0x1B8, n.romfs)):
        if not struct.unpack_from('<I', n.h, size_at)[0]:
            continue
        units = struct.unpack_from('<I', n.h, units_at)[0] or 1
        out[label] = hashlib.sha256(read(0, units * 0x200)).digest() == n.h[off:off + 0x20]
    if not out:
        # Skipping absent sections is right, but an empty result must not be
        # readable as "nothing wrong". Before this round `or 1` guaranteed three
        # keys, so no caller needed an emptiness guard and only one adapter grew
        # one; making the refusal a property of THIS layer is what stops the next
        # caller from iterating {} and appending zero problems.
        raise SystemExit(
            f'{cia}: this NCCH declares no exheader, no exefs and no romfs, so there '
            f'is no superblock hash to verify and no structural evidence that the '
            f'image is intact. Refusing to return an empty result that reads as a '
            f'clean check.')
    return out

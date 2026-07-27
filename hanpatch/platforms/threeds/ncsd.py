"""NCSD / CCI (cartridge dump) container.

A CCI is a signature, an NCSD header with an 8-entry partition table, a card
info header, and then the partitions themselves — partition 0 being the
executable NCCH that holds the RomFS. Reading gets a title's text out of a
cartridge dump; rebuilding writes a patched partition 0 back, shifting the
later partitions and fixing every size field that refers to them.

The partition table is in media units (0x200 bytes), so a rebuilt partition is
padded up to that grain.
"""
import hashlib
import os
import struct

MEDIA = 0x200
CHUNK = 1 << 22


def align(x, a=MEDIA):
    return (x + a - 1) // a * a


class Ncsd:
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        self.head = self.f.read(0x200)
        if self.head[0x100:0x104] != b'NCSD':
            raise ValueError('not an NCSD/CCI (no magic at +0x100)')
        self.media_size, = struct.unpack('<I', self.head[0x104:0x108])
        self.media_id, = struct.unpack('<Q', self.head[0x108:0x110])
        self.fs_type = self.head[0x110:0x118]
        self.crypt_type = self.head[0x118:0x120]
        self.parts = []
        for i in range(8):
            o = 0x120 + i * 8
            off, ln = struct.unpack('<II', self.head[o:o + 8])
            if ln:
                self.parts.append({'idx': i, 'media_off': off, 'media_len': ln,
                                   'offset': off * MEDIA, 'size': ln * MEDIA})
        self.f.seek(0x200)
        self.card_info = self.f.read(0x1000)
        self.total = os.path.getsize(path)

    def partition(self, idx=0):
        for p in self.parts:
            if p['idx'] == idx:
                return p
        raise KeyError(f'no partition {idx}')

    def describe(self):
        lines = [f'NCSD media {self.media_id:016X} '
                 f'declared {self.media_size * MEDIA:#x} '
                 f'file {self.total:#x}']
        for p in self.parts:
            lines.append(f"  partition {p['idx']} at {p['offset']:#x} "
                         f"size {p['size']:#x}")
        return '\n'.join(lines)


def rebuild(original, replacements, out):
    """Write a new CCI with `replacements` = {partition index: file path}.

    Partition sizes may change; later partitions move and the table is rewritten.
    The trailing pad is regenerated so the image keeps its declared media size
    when it still fits, and grows to the next power-of-two card size when not.
    """
    src = Ncsd(original)
    head = bytearray(src.head)
    pos = align(0x200 + len(src.card_info))
    layout = []
    for p in src.parts:
        path = replacements.get(p['idx'])
        size = os.path.getsize(path) if path else p['size']
        layout.append({'idx': p['idx'], 'src': path, 'orig': p,
                       'offset': pos, 'size': align(size)})
        pos += align(size)
    end = pos

    # partition table
    for i in range(8):
        struct.pack_into('<II', head, 0x120 + i * 8, 0, 0)
    for L in layout:
        struct.pack_into('<II', head, 0x120 + L['idx'] * 8,
                         L['offset'] // MEDIA, L['size'] // MEDIA)

    # media size: keep the declared card size when the content still fits
    card = src.media_size * MEDIA
    while card < end:
        card *= 2
    struct.pack_into('<I', head, 0x104, card // MEDIA)

    fsrc = open(original, 'rb')
    with open(out, 'wb') as o:
        o.write(head)
        o.write(src.card_info)
        o.write(b'\0' * (align(0x200 + len(src.card_info))
                         - (0x200 + len(src.card_info))))
        for L in layout:
            o.seek(L['offset'])
            if L['src']:
                written = _copy(open(L['src'], 'rb'), o, os.path.getsize(L['src']))
            else:
                fsrc.seek(L['orig']['offset'])
                written = _copy(fsrc, o, L['orig']['size'])
            pad = L['size'] - written
            if pad > 0:
                o.write(b'\xff' * pad)
        # trailing pad to the declared card size
        o.seek(0, os.SEEK_END)
        if o.tell() < card:
            _fill(o, card - o.tell())

    # partition 0's NCCH hash is mirrored in the card info header for some
    # dumps; refresh it when the field is present and non-zero
    _refresh_exheader_hash(out)
    return card


def _copy(fsrc, out, total):
    left = total
    while left:
        b = fsrc.read(min(CHUNK, left))
        if not b:
            break
        out.write(b)
        left -= len(b)
    return total - left


def _fill(out, n, byte=b'\xff'):
    block = byte * min(n, CHUNK)
    while n:
        k = min(len(block), n)
        out.write(block[:k])
        n -= k


def _refresh_exheader_hash(path):
    """Card info header keeps a copy of partition 0's first NCCH page."""
    f = open(path, 'r+b')
    n = Ncsd(path)
    p0 = n.partition(0)
    f.seek(p0['offset'])
    head = f.read(0x200)
    if head[0x100:0x104] != b'NCCH':
        return False
    f.seek(0x200)
    ci = bytearray(f.read(0x1000))
    # the card info header mirrors the partition-0 NCCH header at +0x1000-0x200
    if ci[0x300:0x304] == b'NCCH':
        ci[0x300:0x500] = head
        f.seek(0x200)
        f.write(ci)
    f.close()
    return True


def extract_partition(path, idx, out):
    """Write one partition to `out` verbatim (still NCCH-encrypted if it was)."""
    n = Ncsd(path)
    p = n.partition(idx)
    f = open(path, 'rb')
    f.seek(p['offset'])
    with open(out, 'wb') as o:
        _copy(f, o, p['size'])
    return p['size']


def sha256_partition(path, idx):
    n = Ncsd(path)
    p = n.partition(idx)
    f = open(path, 'rb')
    f.seek(p['offset'])
    h = hashlib.sha256()
    left = p['size']
    while left:
        b = f.read(min(CHUNK, left))
        if not b:
            break
        h.update(b)
        left -= len(b)
    return h.hexdigest()

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

from hanpatch.platforms.threeds.copyx import copy_exact

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
    # Start where the card actually starts its first partition, not at the end of
    # the card-info header. A cartridge leaves a gap there - this one begins
    # partition 0 at 0x4000 and fills 0x1200..0x4000 with 0xFF - and packing the
    # partitions forward moves every one of them, rewrites that pad, and makes an
    # otherwise untouched rebuild differ from the source across the whole image.
    # A partition keeps its original offset whenever nothing before it has grown
    # past it, so an unchanged rebuild reproduces the original layout exactly.
    pos = align(0x200 + len(src.card_info))
    layout = []
    for p in src.parts:
        path = replacements.get(p['idx'])
        size = os.path.getsize(path) if path else p['size']
        if path and size == 0:
            # A zero-length partition is not a partition. Accepting it wrote a
            # table entry of size 0 and then died with a bare KeyError deeper in,
            # which tells an operator nothing about what they handed us.
            raise SystemExit(
                f'partition {p["idx"]}: replacement {path} is empty; a CCI '
                f'partition cannot be zero bytes')
        offset = p['offset'] if p['offset'] >= pos else pos
        layout.append({'idx': p['idx'], 'src': path, 'orig': p,
                       'offset': offset, 'size': align(size)})
        pos = offset + align(size)
    end = pos

    # partition table
    for i in range(8):
        struct.pack_into('<II', head, 0x120 + i * 8, 0, 0)
    for L in layout:
        struct.pack_into('<II', head, 0x120 + L['idx'] * 8,
                         L['offset'] // MEDIA, L['size'] // MEDIA)

    # media size: keep the declared card size when the content still fits. Growing
    # it is a real event - the image now declares a larger card than the source -
    # so it is reported rather than performed quietly.
    card = src.media_size * MEDIA
    grew_from = card
    while card < end:
        card *= 2
    if card != grew_from:
        print(f'card size grown {grew_from} -> {card} bytes: the rebuilt content is '
              f'{end} bytes, past the {grew_from} the source declared')
    struct.pack_into('<I', head, 0x104, card // MEDIA)

    fsrc = open(original, 'rb')
    with open(out, 'wb') as o:
        o.write(head)
        o.write(src.card_info)
        # Everything between the card-info header and the first partition is card
        # pad, and a cartridge fills it with 0xFF, not zeros. Writing zeros here
        # changed thousands of bytes of an otherwise untouched image.
        for L in layout:
            # Seeking forward would leave a HOLE, and a hole reads back as zeros.
            # That is not the same byte a card writes, and it happens whenever a
            # replacement partition shrinks while a later one keeps its original
            # offset. Fill every gap explicitly instead of letting the filesystem
            # invent its contents.
            if o.tell() < L['offset']:
                _fill(o, L['offset'] - o.tell())
            elif o.tell() > L['offset']:
                raise SystemExit(
                    f'partition {L["idx"]} would start at {L["offset"]:#x} but the '
                    f'image is already {o.tell():#x} bytes long; partitions would '
                    f'overlap')
            if L['src']:
                size = os.path.getsize(L['src'])
                written = _copy(open(L['src'], 'rb'), o, size,
                                f'{L["src"]} (partition {L["idx"]})')
            else:
                fsrc.seek(L['orig']['offset'])
                written = _copy(fsrc, o, L['orig']['size'],
                                f'{original} partition {L["idx"]}',
                                at=L['orig']['offset'])
            pad = L['size'] - written
            if pad > 0:
                _fill(o, pad)
        # trailing pad to the declared card size
        o.seek(0, os.SEEK_END)
        if o.tell() < card:
            _fill(o, card - o.tell())

    # partition 0's NCCH hash is mirrored in the card info header for some
    # dumps; refresh it when the field is present and non-zero
    _refresh_exheader_hash(out)
    return card


def _copy(fsrc, out, total, what, at=None):
    """Shared counted copy - see copyx.copy_exact for why it is not a local loop."""
    return copy_exact(fsrc, out, total, what, at=at)


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
        _copy(f, o, p['size'], f'{path} partition {idx}', at=p['offset'])
    return p['size']


def sha256_partition(path, idx):
    """Hash one partition, refusing a partial read rather than hashing a prefix."""
    n = Ncsd(path)
    p = n.partition(idx)
    f = open(path, 'rb')
    f.seek(p['offset'])
    h = hashlib.sha256()
    left = p['size']
    while left:
        b = f.read(min(CHUNK, left))
        if not b:
            raise SystemExit(
                f'{path} partition {idx}: declared {p["size"]} bytes from offset '
                f'{p["offset"]} but the file ran out after {p["size"] - left}; '
                f'refusing to report a hash of a partial partition')
        h.update(b)
        left -= len(b)
    return h.hexdigest()

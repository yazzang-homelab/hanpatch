"""Rebuild a CIA (or a bare NCCH partition) around a patched RomFS.

The RomFS is re-encrypted with the NCCH *secondary* key, which differs from the
primary key under crypto methods 1/10/11. Every size and hash that refers to the
content is recomputed: the NCCH header, the TMD content chunk and its info
records, the TMD body hash, and the CIA header's content size.
"""
import hashlib
import os
import struct
import sys

from hanpatch.platforms.threeds.copyx import copy_exact

from Crypto.Cipher import AES
from Crypto.Util import Counter

from hanpatch.platforms.threeds import ncch as ncchmod

SIG_SIZES = {0x10000: 0x200 + 0x3c, 0x10001: 0x100 + 0x3c, 0x10002: 0x3c + 0x40,
             0x10003: 0x200 + 0x3c, 0x10004: 0x100 + 0x3c, 0x10005: 0x3c + 0x40}
CHUNK = 1 << 22


def align(x, a=64):
    return (x + a - 1) // a * a


class Cia:
    def __init__(self, path):
        self.path = path
        f = open(path, 'rb')
        self.f = f
        h = f.read(0x2020)
        (self.hdr_size, self.ctype, self.ver, self.cert_size, self.tik_size,
         self.tmd_size, self.meta_size) = struct.unpack('<IHHIIII', h[:0x18])
        self.content_size, = struct.unpack('<Q', h[0x18:0x20])
        self.content_index = h[0x20:0x2020]
        self.off_cert = align(self.hdr_size)
        self.off_tik = self.off_cert + align(self.cert_size)
        self.off_tmd = self.off_tik + align(self.tik_size)
        self.off_content = self.off_tmd + align(self.tmd_size)
        f.seek(self.off_cert)
        self.cert = f.read(self.cert_size)
        f.seek(self.off_tik)
        self.tik = f.read(self.tik_size)
        f.seek(self.off_tmd)
        tmd = f.read(self.tmd_size)
        st, = struct.unpack('>I', tmd[:4])
        self.tmd_sig = tmd[:4 + SIG_SIZES[st]]
        self.tmd_body = bytearray(tmd[4 + SIG_SIZES[st]:])
        self.content_count, = struct.unpack('>H', self.tmd_body[0x9E:0xA0])
        self.chunks = []
        off = self.off_content
        for i in range(self.content_count):
            o = 0x9C4 + i * 0x30
            cid, cidx, ct = struct.unpack('>IHH', self.tmd_body[o:o + 8])
            sz, = struct.unpack('>Q', self.tmd_body[o + 8:o + 16])
            self.chunks.append({'id': cid, 'idx': cidx, 'type': ct, 'size': sz,
                                'hash': bytes(self.tmd_body[o + 16:o + 48]),
                                'offset': off})
            off += sz


def encrypt_section(key, partition_id, section, plaintext_stream, out, total,
                    no_crypto=False):
    """Stream-encrypt `total` bytes from plaintext_stream to out.

    A plaintext NCCH has no key, so the cipher must not be constructed at all -
    building it first and only checking the flag inside the loop made every
    decrypted dump crash with a TypeError from AES.new(None, ...) before a single
    byte moved.
    """
    c = None
    if not no_crypto:
        iv = bytes(reversed(partition_id)) + bytes([section]) + b'\0' * 7
        c = AES.new(key, AES.MODE_CTR,
                    counter=Counter.new(128, initial_value=int.from_bytes(iv, 'big')))
    written = 0
    while written < total:
        b = plaintext_stream.read(min(CHUNK, total - written))
        if not b:
            # The section is padded to its declared size on purpose: a RomFS is
            # media-unit aligned and the tail is defined to be zero.
            b = b'\0' * min(CHUNK, total - written)
        if c is not None:
            b = c.encrypt(b)
        out.write(b)
        written += len(b)



def _encrypt_exefs(n, data, offset, secondary):
    if n.no_crypto:
        return data
    key = n.secondary if secondary else n.primary
    return AES.new(key, AES.MODE_CTR, counter=Counter.new(
        128, initial_value=n.ctr(2, offset // 16))).encrypt(data)


def _rebuild_exefs(n, replacements):
    if not hasattr(replacements, 'items'):
        raise ValueError('ExeFS replacements must be a name-to-bytes mapping')
    replacements = dict(replacements)
    capacity = (n.romfs_off - n.exefs_off) * 0x200
    section_size = n.exefs_size * 0x200
    if not n.exefs_off or capacity < 0x200 or section_size > capacity:
        raise ValueError('source ExeFS section does not fit before RomFS')
    header = n.exefs(0, 0x200)
    if len(header) != 0x200:
        raise ValueError('source ExeFS header is truncated')

    members = []
    names = set()
    for index in range(10):
        entry = header[index * 0x10:(index + 1) * 0x10]
        name = entry[:8].rstrip(b'\0').decode('latin1')
        if not name:
            continue
        if name in names:
            raise ValueError(f'source ExeFS has duplicate member {name!r}')
        names.add(name)
        offset, size = struct.unpack('<II', entry[8:16])
        if offset + size > section_size - 0x200:
            raise ValueError(f'source ExeFS member {name!r} exceeds its section')
        aligned = align(size, 16)
        raw = n.exefs(0x200 + offset, aligned,
                      secondary=name not in ('icon', 'banner', 'logo'))
        if len(raw) != aligned:
            raise ValueError(f'source ExeFS member {name!r} is truncated')
        content = raw[:size]
        expected = header[0xC0 + (9 - index) * 0x20:0xE0 + (9 - index) * 0x20]
        if hashlib.sha256(content).digest() != expected:
            raise ValueError(f'source ExeFS member {name!r} hash mismatch')
        members.append((index, name, content))

    unknown = set(replacements) - names
    if unknown:
        raise ValueError(f'ExeFS replacement names are missing from source: {sorted(unknown)!r}')
    for name, content in replacements.items():
        if not isinstance(name, str) or not isinstance(content, bytes):
            raise ValueError('ExeFS replacements must map member names to bytes')

    new_header = bytearray(header)
    new_header[:0xA0] = b'\0' * 0xA0
    new_header[0xC0:0x200] = b'\0' * 0x140
    plain = bytearray(0x200)
    rebuilt = []
    offset = 0
    for index, name, content in members:
        content = replacements.get(name, content)
        offset = align(offset, 0x200)
        rebuilt.append((index, name, offset, content))
        if 0x200 + offset + len(content) > capacity:
            raise ValueError(f'ExeFS replacement {name!r} overflows the section before RomFS')
        # PADDED to the full 8-byte slot. A bytearray slice assignment RESIZES,
        # so writing `.code` (5 bytes) into an 8-byte slot silently shortened the
        # header and shifted every hash and every byte after it left - measured
        # 10 bytes on this cartridge, which put the member hashes at the wrong
        # offsets while the superblock hash still agreed because it was computed
        # over the same broken buffer.
        name_bytes = name.encode('latin1')
        if len(name_bytes) > 8:
            raise ValueError(f'ExeFS member name {name!r} exceeds its 8-byte slot')
        new_header[index * 0x10:index * 0x10 + 8] = name_bytes.ljust(8, b'\0')
        struct.pack_into('<II', new_header, index * 0x10 + 8, offset, len(content))
        new_header[0xC0 + (9 - index) * 0x20:0xE0 + (9 - index) * 0x20] = (
            hashlib.sha256(content).digest())
        if len(new_header) != 0x200:
            raise ValueError('ExeFS header changed size while being rebuilt')
        end = 0x200 + offset + len(content)
        if len(plain) < end:
            plain.extend(b'\0' * (end - len(plain)))
        plain[0x200 + offset:end] = content
        offset += len(content)

    declared = align(len(plain), 0x200)
    if declared > capacity:
        raise ValueError('rebuilt ExeFS exceeds the space before RomFS')
    plain.extend(b'\0' * (declared - len(plain)))
    plain[:0x200] = new_header
    if len(plain) != declared:
        raise ValueError('rebuilt ExeFS body does not match its declared size')
    encrypted = bytearray(_encrypt_exefs(n, bytes(plain[:0x200]), 0, False))
    encrypted.extend(_encrypt_exefs(n, bytes(plain[0x200:declared]), 0x200, True))
    for _index, name, offset, content in rebuilt:
        start = 0x200 + offset
        encrypted[start:start + len(content)] = _encrypt_exefs(
            n, content, start, name not in ('icon', 'banner', 'logo'))
    hash_units = struct.unpack_from('<I', n.h, 0x1A8)[0] or 1
    span = hash_units * 0x200
    if span > len(plain):
        raise ValueError('rebuilt ExeFS is shorter than its declared superblock region')
    return bytes(encrypted), bytes(plain), declared // 0x200, hashlib.sha256(plain[:span]).digest()

def rebuild_ncch(src_path, base, new_romfs, out_path, keystore=None,
                 exefs_replacements=None, decrypt=False):
    """Write one NCCH partition with `new_romfs` swapped in.

    Returns (size, sha256). Without an ExeFS replacement, everything between
    the header and RomFS is copied verbatim. With replacements, only the
    declared ExeFS section and its header hash/size fields are rebuilt.

    `decrypt` writes every encrypted section as plaintext and declares
    NoCrypto in the NCCH flags. That is not a convenience: emulators refuse an
    encrypted title outright (Azahar 2126.0 answers "Your ROM is Encrypted"
    and never boots it), so a build meant to be played needs this form. The
    superblock hashes cover PLAINTEXT in both modes, so they do not change.
    """
    n = ncchmod.NCCH(src_path, base, keystore=keystore)
    header = bytearray(n.h)
    romfs_off_bytes = n.romfs_off * 0x200
    romfs_size = os.path.getsize(new_romfs)
    romfs_pad = align(romfs_size, 0x200)
    new_content_size = romfs_off_bytes + romfs_pad

    # patch NCCH header
    struct.pack_into('<I', header, 0x104, new_content_size // 0x200)
    struct.pack_into('<II', header, 0x1B0, n.romfs_off, romfs_pad // 0x200)
    # The RomFS superblock hash covers the region the header DECLARES, and that
    # declaration is a per-title fact: this cartridge ships 2 media units, and
    # overwriting it with 1 both shrank what the console validates and changed a
    # field the title had set. Preserve the declared size, hash exactly that
    # span, and only fall back to one unit when the source declares nothing.
    hash_region = struct.unpack_from('<I', n.h, 0x1B8)[0] or 1
    struct.pack_into('<I', header, 0x1B8, hash_region)
    span = hash_region * 0x200
    with open(new_romfs, 'rb') as rf:
        first = rf.read(span)
    if len(first) < span:
        raise SystemExit(
            f'the new RomFS is {len(first)} bytes, shorter than the {span}-byte '
            f'superblock region the NCCH header declares ({hash_region} media '
            f'units); refusing to write a hash over data that does not exist')
    header[0x1E0:0x200] = hashlib.sha256(first).digest()
    if decrypt:
        flags = bytearray(header[0x188:0x190])
        flags[3] = 0                     # crypto method: none
        flags[7] = (flags[7] & ~(0x01 | 0x20)) | 0x04   # drop fixed-key/seed, set NoCrypto
        header[0x188:0x190] = bytes(flags)
    rebuilt_exefs = plain_exefs = None
    if exefs_replacements is not None or decrypt:
        rebuilt_exefs, plain_exefs, exefs_units, exefs_hash = _rebuild_exefs(
            n, exefs_replacements or {})
        struct.pack_into('<I', header, 0x1A4, exefs_units)
        header[0x1C0:0x1E0] = exefs_hash
        if decrypt:
            rebuilt_exefs = plain_exefs

    src = open(src_path, 'rb')
    with open(out_path, 'wb') as o:
        o.write(bytes(header))
        if rebuilt_exefs is None:
            src.seek(base + 0x200)
            copy_exact(src, o, romfs_off_bytes - 0x200,
                       f'{src_path} header..romfs region', at=base + 0x200)
        else:
            exefs_at = n.exefs_off * 0x200
            at = 0x200
            if decrypt and n.exh_size:
                # The exheader is encrypted with the PRIMARY key, so a verbatim
                # copy would ship ciphertext inside a NoCrypto title.
                #
                # Its REGION is 0x800 (0x400 exheader + 0x400 access
                # descriptor) and is NOT `exh_size * 2`: this cartridge
                # declares 0x3FB, so trusting the declared size wrote 10 bytes
                # too few and shifted every following section left by 10 —
                # a build that still passed the superblock hashes because they
                # do not cover the ExeFS body.
                region = 0x800
                if n.exh_size * 2 > region:
                    raise ValueError(
                        f'exheader declares {n.exh_size * 2:#x}, larger than the '
                        f'{region:#x} region this writer knows how to reproduce')
                exheader = n.read_section(ncchmod.SEC_EXHEADER, 1, region)
                o.write(exheader)
                at += region
            # logo and plain region are never encrypted: copy them verbatim
            src.seek(base + at)
            copy_exact(src, o, exefs_at - at,
                       f'{src_path} header..ExeFS region', at=base + at)
            o.write(rebuilt_exefs)
            gap_at = exefs_at + len(rebuilt_exefs)
            if gap_at > romfs_off_bytes:
                raise ValueError('rebuilt ExeFS overlaps the RomFS')
            src.seek(base + gap_at)
            copy_exact(src, o, romfs_off_bytes - gap_at,
                       f'{src_path} ExeFS..RomFS gap', at=base + gap_at)
        with open(new_romfs, 'rb') as rf:
            encrypt_section(n.secondary, n.partition_id, 3, rf, o, romfs_pad,
                            no_crypto=n.no_crypto or decrypt)
    got = os.path.getsize(out_path)
    if got != new_content_size:
        raise ValueError(f'rebuilt content is {got:#x}, expected '
                         f'{new_content_size:#x}')
    h = hashlib.sha256()
    with open(out_path, 'rb') as cf:
        while True:
            b = cf.read(CHUNK)
            if not b:
                break
            h.update(b)
    return new_content_size, h.digest()


def rebuild(cia_path, new_romfs, out_path, keystore=None, exefs_replacements=None,
            decrypt=False):
    cia = Cia(cia_path)
    main = cia.chunks[0]
    from hanpatch.platforms.threeds import cia as ciamod
    plain, base, temp = ciamod.prepare_content(
        cia, main['idx'], workdir=os.path.dirname(os.path.abspath(out_path)),
        keystore=keystore)
    tmp = out_path + '.content0'
    new_content_size, main_hash = rebuild_ncch(
        plain, base, new_romfs, tmp, keystore=keystore,
        exefs_replacements=exefs_replacements, decrypt=decrypt)
    if temp and os.path.exists(plain):
        os.remove(plain)

    body = cia.tmd_body
    struct.pack_into('>Q', body, 0x9C4 + 8, new_content_size)
    body[0x9C4 + 16:0x9C4 + 48] = main_hash
    # The rebuilt content is written in the clear, so the title-key encryption
    # flag must come off or an installer will try to decrypt plaintext.
    if temp:
        ctype, = struct.unpack('>H', body[0x9C4 + 6:0x9C4 + 8])
        struct.pack_into('>H', body, 0x9C4 + 6, ctype & ~ciamod.TYPE_ENCRYPTED)
    # content info records: recompute each record's sha over its chunk range
    for i in range(64):
        o = 0xC4 + i * 0x24
        idx, cnt = struct.unpack('>HH', body[o:o + 4])
        if cnt == 0:
            continue
        region = bytes(body[0x9C4 + idx * 0x30: 0x9C4 + (idx + cnt) * 0x30])
        body[o + 4:o + 0x24] = hashlib.sha256(region).digest()
    body[0xA4:0xC4] = hashlib.sha256(bytes(body[0xC4:0xC4 + 0x900])).digest()

    new_tmd = cia.tmd_sig + bytes(body)
    assert len(new_tmd) == cia.tmd_size, (len(new_tmd), cia.tmd_size)

    total_content = new_content_size + sum(c['size'] for c in cia.chunks[1:])
    hdr = bytearray(0x2020)
    struct.pack_into('<IHHIIII', hdr, 0, cia.hdr_size, cia.ctype, cia.ver,
                     cia.cert_size, cia.tik_size, cia.tmd_size, cia.meta_size)
    struct.pack_into('<Q', hdr, 0x18, total_content)
    hdr[0x20:0x2020] = cia.content_index

    with open(out_path, 'wb') as o:
        o.write(bytes(hdr))
        o.write(b'\0' * (align(cia.hdr_size) - cia.hdr_size))
        o.write(cia.cert)
        o.write(b'\0' * (align(cia.cert_size) - cia.cert_size))
        o.write(cia.tik)
        o.write(b'\0' * (align(cia.tik_size) - cia.tik_size))
        o.write(new_tmd)
        o.write(b'\0' * (align(cia.tmd_size) - cia.tmd_size))
        with open(tmp, 'rb') as cf:
            while True:
                b = cf.read(CHUNK)
                if not b:
                    break
                o.write(b)
        for c in cia.chunks[1:]:
            cia.f.seek(c['offset'])
            copy_exact(cia.f, o, c['size'], f'{cia.path} content {c["idx"]}',
                       at=c['offset'])
        if cia.meta_size:
            cia.f.seek(cia.off_content + cia.content_size)
            o.write(cia.f.read(cia.meta_size))
    os.remove(tmp)
    return out_path


if __name__ == '__main__':
    rebuild(sys.argv[1], sys.argv[2], sys.argv[3])

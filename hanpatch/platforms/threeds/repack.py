"""Rebuild the Crimson Shroud CIA with a patched RomFS."""
import hashlib
import os
import struct
import sys

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
    """Stream-encrypt `total` bytes from plaintext_stream to out."""
    iv = bytes(reversed(partition_id)) + bytes([section]) + b'\0' * 7
    init = int.from_bytes(iv, 'big')
    c = AES.new(key, AES.MODE_CTR, counter=Counter.new(128, initial_value=init))
    written = 0
    while written < total:
        b = plaintext_stream.read(min(CHUNK, total - written))
        if not b:
            b = b'\0' * min(CHUNK, total - written)
        if not no_crypto:
            b = c.encrypt(b)
        out.write(b)
        written += len(b)


def rebuild(cia_path, new_romfs, out_path):
    cia = Cia(cia_path)
    main = cia.chunks[0]
    n = ncchmod.NCCH(cia_path, main['offset'])
    header = bytearray(n.h)
    romfs_off_bytes = n.romfs_off * 0x200
    romfs_size = os.path.getsize(new_romfs)
    romfs_pad = align(romfs_size, 0x200)
    new_content_size = romfs_off_bytes + romfs_pad

    # patch NCCH header
    struct.pack_into('<I', header, 0x104, new_content_size // 0x200)
    struct.pack_into('<II', header, 0x1B0, n.romfs_off, romfs_pad // 0x200)
    struct.pack_into('<I', header, 0x1B8, 1)          # hash region = 1 media unit
    with open(new_romfs, 'rb') as rf:
        first = rf.read(0x200)
    header[0x1E0:0x200] = hashlib.sha256(first).digest()

    tmp = out_path + '.content0'
    with open(tmp, 'wb') as o:
        o.write(bytes(header))
        # everything between header and romfs is copied verbatim (still encrypted)
        cia.f.seek(main['offset'] + 0x200)
        left = romfs_off_bytes - 0x200
        while left:
            b = cia.f.read(min(CHUNK, left))
            o.write(b)
            left -= len(b)
        with open(new_romfs, 'rb') as rf:
            encrypt_section(n.key, n.partition_id, 3, rf, o, romfs_pad,
                            no_crypto=n.no_crypto)
    assert os.path.getsize(tmp) == new_content_size, (os.path.getsize(tmp), new_content_size)

    # hash of new content
    h = hashlib.sha256()
    with open(tmp, 'rb') as cf:
        while True:
            b = cf.read(CHUNK)
            if not b:
                break
            h.update(b)
    main_hash = h.digest()

    body = cia.tmd_body
    struct.pack_into('>Q', body, 0x9C4 + 8, new_content_size)
    body[0x9C4 + 16:0x9C4 + 48] = main_hash
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
            left = c['size']
            while left:
                b = cia.f.read(min(CHUNK, left))
                o.write(b)
                left -= len(b)
        if cia.meta_size:
            cia.f.seek(cia.off_content + cia.content_size)
            o.write(cia.f.read(cia.meta_size))
    os.remove(tmp)
    return out_path


if __name__ == '__main__':
    rebuild(sys.argv[1], sys.argv[2], sys.argv[3])

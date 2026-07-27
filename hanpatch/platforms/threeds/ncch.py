"""Minimal NCCH decryptor for crypto-method 0 retail titles."""
import struct
from Crypto.Cipher import AES
from Crypto.Util import Counter

KEYX_2C = 0xB98E95CECA3E4D171F76A94DE934C053
KEYGEN_C = 0x1FF9E9AAC5FE0408024591DC5D52768A
MASK128 = (1 << 128) - 1


def rol(v, n):
    n %= 128
    return ((v << n) | (v >> (128 - n))) & MASK128


def keygen(keyx, keyy):
    return rol((rol(keyx, 2) ^ keyy) + KEYGEN_C, 87)


def i2b(v):
    return v.to_bytes(16, 'big')


class NCCH:
    def __init__(self, path, base=0):
        self.f = open(path, 'rb')
        self.base = base
        self.f.seek(base)
        self.h = self.f.read(0x200)
        assert self.h[0x100:0x104] == b'NCCH', 'not NCCH'
        self.keyy = int.from_bytes(self.h[:0x10], 'big')
        self.partition_id = self.h[0x108:0x110]
        self.version = struct.unpack('<H', self.h[0x112:0x114])[0]
        self.flags = self.h[0x188:0x190]
        self.exh_size = struct.unpack('<I', self.h[0x180:0x184])[0]
        self.exefs_off, self.exefs_size = struct.unpack('<II', self.h[0x1A0:0x1A8])
        self.romfs_off, self.romfs_size = struct.unpack('<II', self.h[0x1B0:0x1B8])
        self.no_crypto = bool(self.flags[7] & 0x4)
        self.fixed_key = bool(self.flags[7] & 0x1)
        self.crypto_method = self.flags[3]
        assert self.crypto_method == 0, f'unsupported crypto method {self.crypto_method}'
        assert not self.fixed_key
        self.key = i2b(keygen(KEYX_2C, self.keyy))

    def ctr(self, section, block_offset=0):
        if self.version in (0, 2):
            iv = bytes(reversed(self.partition_id)) + bytes([section]) + b'\0' * 7
        else:
            raise NotImplementedError(self.version)
        return int.from_bytes(iv, 'big') + block_offset

    def read_section(self, section, media_off, byte_len, extra_off=0):
        """section: 1=exheader 2=exefs 3=romfs. media_off in media units."""
        start = media_off * 0x200 + extra_off
        assert extra_off % 16 == 0
        self.f.seek(self.base + start)
        data = self.f.read(byte_len)
        if self.no_crypto:
            return data
        init = self.ctr(section, extra_off // 16)
        c = AES.new(self.key, AES.MODE_CTR,
                    counter=Counter.new(128, initial_value=init))
        return c.decrypt(data)

    def exheader(self):
        return self.read_section(1, 1, self.exh_size * 2)

    def exefs(self, extra_off=0, length=None):
        if length is None:
            length = self.exefs_size * 0x200 - extra_off
        return self.read_section(2, self.exefs_off, length, extra_off)

    def romfs(self, extra_off=0, length=None):
        if length is None:
            length = self.romfs_size * 0x200 - extra_off
        return self.read_section(3, self.romfs_off, length, extra_off)

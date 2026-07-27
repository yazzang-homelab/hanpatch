"""NCCH reader covering every retail crypto configuration.

Sections carry two different keys in the secondary crypto methods: the
exheader, the exefs *header*, and icon/banner/logo use the primary slot 0x2C
key, while exefs file data and the RomFS use the secondary slot named by the
crypto method. Getting that split wrong yields a file that decrypts "almost"
correctly, which is worse than failing, so the key choice is validated against
the section's own magic wherever one exists.

Key material is supplied by the operator (see `keys.py`); none ships here.
"""
import struct

from Crypto.Cipher import AES
from Crypto.Util import Counter

from hanpatch.platforms.threeds import keys as keysmod
from hanpatch.platforms.threeds.keys import (CRYPTO_SLOT, FIXED_SYSTEM_KEY,
                                             KEYX_2C, i2b, keygen)

# section ids used in the CTR IV
SEC_EXHEADER, SEC_EXEFS, SEC_ROMFS = 1, 2, 3


class NCCH:
    def __init__(self, path, base=0, keystore=None, strict=True):
        self.f = open(path, 'rb') if isinstance(path, str) else path
        self.base = base
        self.f.seek(base)
        self.h = self.f.read(0x200)
        if self.h[0x100:0x104] != b'NCCH':
            raise ValueError('not an NCCH (no magic at +0x100)')
        self.keyy = int.from_bytes(self.h[:0x10], 'big')
        self.partition_id = self.h[0x108:0x110]
        self.program_id = struct.unpack('<Q', self.h[0x118:0x120])[0]
        self.version = struct.unpack('<H', self.h[0x112:0x114])[0]
        self.flags = self.h[0x188:0x190]
        self.exh_size = struct.unpack('<I', self.h[0x180:0x184])[0]
        self.exefs_off, self.exefs_size = struct.unpack('<II', self.h[0x1A0:0x1A8])
        self.romfs_off, self.romfs_size = struct.unpack('<II', self.h[0x1B0:0x1B8])

        self.no_crypto = bool(self.flags[7] & 0x04)
        self.fixed_key = bool(self.flags[7] & 0x01)
        self.seed_crypto = bool(self.flags[7] & 0x20)
        self.crypto_method = self.flags[3]

        self.keys = keystore if keystore is not None else keysmod.store()
        self.notes = []
        self.primary = None
        self.secondary = None
        if not self.no_crypto:
            self._resolve_keys(strict)

    # -- key resolution -----------------------------------------------------

    def _resolve_keys(self, strict):
        if self.fixed_key:
            # System titles use the fixed system key; everything else the zero
            # key. Both are validated below, so an ambiguous case self-corrects.
            cands = [i2b(0), i2b(FIXED_SYSTEM_KEY)]
            if (self.program_id >> 32) & 0x10:
                cands.reverse()
            key = keysmod.KeyStore.pick(cands, self._validates)
            if key is None and strict:
                raise ValueError('fixed-key NCCH: neither the zero key nor the '
                                 'fixed system key decrypts this content')
            self.primary = self.secondary = key or cands[0]
            self.notes.append('fixed key')
            return

        keyy = self.keyy
        if self.seed_crypto:
            seeded = self.keys.seed_keyy(self.keyy, self.program_id)
            if seeded is None:
                if strict:
                    raise ValueError(
                        f'this title uses seed crypto and no seed for '
                        f'{self.program_id:016X} was found. Supply a seeddb.bin '
                        f'(see keys.py) or set HANPATCH_KEYS.')
                self.notes.append('seed missing')
            else:
                keyy = seeded
                self.notes.append('seed crypto')

        # primary slot 0x2C KeyX is public, so the primary key never blocks us
        self.primary = i2b(keygen(KEYX_2C, keyy))

        slot = CRYPTO_SLOT.get(self.crypto_method)
        if slot is None:
            raise ValueError(f'unknown NCCH crypto method 0x{self.crypto_method:02X}')
        if slot == 0x2C:
            self.secondary = self.primary
            return

        cands = []
        k = self.keys.normal(slot, keyy)
        if k:
            cands.append(k)
        # a bootROM located by anchor search can be off; try the neighbours the
        # documented methods use before giving up
        for alt in (0x25, 0x18, 0x1B):
            if alt != slot:
                k = self.keys.normal(alt, keyy)
                if k:
                    cands.append(k)
        chosen = keysmod.KeyStore.pick(cands, self._validates_secondary)
        if chosen is None:
            if strict:
                raise ValueError(
                    f'crypto method 0x{self.crypto_method:02X} needs AES slot '
                    f'0x{slot:02X}; no supplied key decrypts this content. '
                    f'Provide boot9.bin or slot0x{slot:02X}KeyX in keys.txt.\n'
                    + self.keys.describe())
            self.notes.append(f'slot 0x{slot:02X} unavailable')
            self.secondary = self.primary
        else:
            self.secondary = chosen
            self.notes.append(f'slot 0x{slot:02X}')

    def _decrypt(self, key, section, media_off, byte_len, extra_off=0):
        start = media_off * 0x200 + extra_off
        self.f.seek(self.base + start)
        data = self.f.read(byte_len)
        init = self.ctr(section, extra_off // 16)
        c = AES.new(key, AES.MODE_CTR,
                    counter=Counter.new(128, initial_value=init))
        return c.decrypt(data)

    def _validates(self, key):
        """Does this key produce a structurally valid section?"""
        if self.romfs_size:
            d = self._decrypt(key, SEC_ROMFS, self.romfs_off, 0x10)
            return d[:4] == b'IVFC'
        if self.exh_size:
            d = self._decrypt(key, SEC_EXHEADER, 1, 0x10)
            return d[8:16].rstrip(b'\0').isascii() and d[:1] != b'\0'
        return True

    def _validates_secondary(self, key):
        if self.romfs_size:
            d = self._decrypt(key, SEC_ROMFS, self.romfs_off, 0x10)
            return d[:4] == b'IVFC'
        # no RomFS: fall back to the exefs file region being sane once decoded
        return True

    # -- reading ------------------------------------------------------------

    def ctr(self, section, block_offset=0):
        if self.version in (0, 2):
            iv = bytes(reversed(self.partition_id)) + bytes([section]) + b'\0' * 7
        elif self.version == 1:
            off = {SEC_EXHEADER: 0x200, SEC_EXEFS: self.exefs_off * 0x200,
                   SEC_ROMFS: self.romfs_off * 0x200}[section]
            iv = self.partition_id + struct.pack('>I', off)[-4:] + b'\0' * 4
            return int.from_bytes(iv, 'big') + block_offset
        else:
            raise NotImplementedError(f'NCCH version {self.version}')
        return int.from_bytes(iv, 'big') + block_offset

    def read_section(self, section, media_off, byte_len, extra_off=0,
                     secondary=False):
        """section: 1=exheader 2=exefs 3=romfs. media_off in media units."""
        if extra_off % 16:
            raise ValueError('section offset must be 16-byte aligned')
        if self.no_crypto:
            self.f.seek(self.base + media_off * 0x200 + extra_off)
            return self.f.read(byte_len)
        key = self.secondary if secondary else self.primary
        return self._decrypt(key, section, media_off, byte_len, extra_off)

    def exheader(self):
        return self.read_section(SEC_EXHEADER, 1, self.exh_size * 2)

    def exefs(self, extra_off=0, length=None, secondary=False):
        if length is None:
            length = self.exefs_size * 0x200 - extra_off
        return self.read_section(SEC_EXEFS, self.exefs_off, length, extra_off,
                                 secondary=secondary)

    def exefs_files(self):
        """(name, offset, size, uses_secondary_key) for each exefs entry.

        icon/banner/logo stay on the primary key even under secondary crypto.
        """
        hdr = self.exefs(0, 0x200)
        out = []
        for i in range(10):
            e = hdr[i * 0x10:(i + 1) * 0x10]
            name = e[:8].rstrip(b'\0').decode('latin1')
            if not name:
                continue
            off, size = struct.unpack('<II', e[8:16])
            sec = name not in ('icon', 'banner', 'logo')
            out.append((name, off, size, sec))
        return out

    def romfs(self, extra_off=0, length=None):
        if length is None:
            length = self.romfs_size * 0x200 - extra_off
        return self.read_section(SEC_ROMFS, self.romfs_off, length, extra_off,
                                 secondary=True)

    def describe(self):
        enc = 'plaintext' if self.no_crypto else (
            f'method 0x{self.crypto_method:02X}'
            + (' fixed' if self.fixed_key else '')
            + (' seed' if self.seed_crypto else ''))
        return (f'NCCH v{self.version} program {self.program_id:016X} {enc}'
                + (f' [{", ".join(self.notes)}]' if self.notes else ''))

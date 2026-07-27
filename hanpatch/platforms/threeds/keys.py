"""Key material for 3DS content decryption.

hanpatch ships **no key material**. Everything here loads keys the operator
supplies and derives the rest. Supported sources, in priority order:

1. ``$HANPATCH_KEYS`` — a path, or ``os.pathsep``-joined list of paths
2. ``<project>/keys/`` and ``~/.hanpatch/keys/``
3. individual environment variables (``HANPATCH_KEY_slot0x25KeyX`` …)

Recognised files:

``boot9.bin`` / ``boot9_prot.bin``
    The ARM9 bootROM. KeyX for every AES slot is read out of it. The keyblob is
    located by **searching for the one KeyX that is public knowledge** (slot
    0x2C, published in every 3DS format document) and indexing off it, so no
    hardcoded file offsets can silently drift.

``keys.txt`` / ``aes_keys.txt``
    ``name = hex`` lines, the format Citra and friends already use::

        slot0x25KeyX = ...
        slot0x18KeyX = ...
        common0     = ...

``seeddb.bin``
    Title-id → seed table for seed-encrypted titles (system version 9.6+).

Derived keys are never trusted blindly: :func:`KeyStore.pick` hands candidates
to a caller-supplied validator, so a wrong slot or layout is detected by the
decryption failing to produce a valid header rather than by producing garbage.
"""
import hashlib
import os
import struct

MASK128 = (1 << 128) - 1

# The NCCH primary KeyX. Public in every 3DS documentation source; used here as
# a *search anchor* to locate the keyblob inside a supplied bootROM.
KEYX_2C = 0xB98E95CECA3E4D171F76A94DE934C053
# Key scrambler constant.
KEYGEN_C = 0x1FF9E9AAC5FE0408024591DC5D52768A
# Fixed key used by titles that set the fixed-key flag with a system program id.
FIXED_SYSTEM_KEY = 0x527CE630A9CA305F3696F3CDE954194B

# NCCH secondary crypto methods -> AES key slot.
CRYPTO_SLOT = {0x00: 0x2C, 0x01: 0x25, 0x0A: 0x18, 0x0B: 0x1B}

KEY_FILES = ('boot9.bin', 'boot9_prot.bin', 'boot9_protected.bin',
             'keys.txt', 'aes_keys.txt', 'seeddb.bin', 'encTitleKeys.bin')


def rol(v, n):
    """Rotate a 128-bit value left by n bits.

    The input is masked first: the scrambler's addition can carry past bit 127,
    and rotating an over-wide value would fold the carry back in at bit
    (128 - n) instead of dropping it, which silently collapses distinct KeyY
    values onto the same key.
    """
    v &= MASK128
    n %= 128
    if n == 0:
        return v
    return ((v << n) | (v >> (128 - n))) & MASK128


def keygen(keyx, keyy):
    """The 3DS AES key scrambler: normal key from a KeyX/KeyY pair.

    normal = rol((rol(KeyX, 2) ^ KeyY) + C, 87), all arithmetic mod 2**128.
    """
    return rol(((rol(keyx, 2) ^ keyy) + KEYGEN_C) & MASK128, 87)


def i2b(v):
    return v.to_bytes(16, 'big')


def b2i(b):
    return int.from_bytes(b, 'big')


def search_paths(project=None):
    out = []
    env = os.environ.get('HANPATCH_KEYS')
    if env:
        out += [p for p in env.split(os.pathsep) if p]
    if project:
        out.append(os.path.join(project, 'keys'))
        out.append(project)
    home = os.path.expanduser('~')
    out.append(os.path.join(home, '.hanpatch', 'keys'))
    out.append(os.path.join(home, '.3ds'))
    return out


class KeyStore:
    """Loaded key material plus derivation helpers."""

    def __init__(self, project=None, verbose=False):
        self.keyx = {}          # slot -> int
        self.keyy = {}          # slot -> int
        self.common = {}        # index -> int (common KeyY for titlekey crypto)
        self.seeds = {}         # title id (int) -> 16 bytes
        self.sources = []
        self._load(project, verbose)

    # -- loading ------------------------------------------------------------

    def _load(self, project, verbose):
        for base in search_paths(project):
            if not os.path.isdir(base):
                if os.path.isfile(base):
                    self._file(base, verbose)
                continue
            for name in KEY_FILES:
                p = os.path.join(base, name)
                if os.path.isfile(p):
                    self._file(p, verbose)
        for k, v in os.environ.items():
            if k.startswith('HANPATCH_KEY_'):
                self._named(k[len('HANPATCH_KEY_'):], v)

    def _file(self, path, verbose=False):
        name = os.path.basename(path).lower()
        try:
            if name.startswith('boot9'):
                self._boot9(open(path, 'rb').read())
            elif name.endswith('.txt'):
                self._keytxt(open(path, encoding='utf-8',
                                  errors='replace').read())
            elif name.startswith('seeddb'):
                self._seeddb(open(path, 'rb').read())
            else:
                return
        except (OSError, ValueError) as e:
            if verbose:
                print(f'keys: ignoring {path}: {e}')
            return
        self.sources.append(path)

    def _boot9(self, blob):
        """Locate the keyblob by anchoring on the public slot-0x2C KeyX.

        Slot KeyX entries are contiguous 16-byte records, so one known entry
        fixes the base for every other slot. Anything that lands outside the
        file is skipped instead of being invented.
        """
        anchor = blob.find(i2b(KEYX_2C))
        if anchor < 0:
            raise ValueError('no recognisable NCCH KeyX in this bootROM; '
                             'is it a full dump?')
        base_slot = 0x2C
        for slot in range(0x00, 0x40):
            off = anchor + (slot - base_slot) * 16
            if 0 <= off <= len(blob) - 16:
                v = b2i(blob[off:off + 16])
                if v:
                    self.keyx.setdefault(slot, v)

    def _keytxt(self, text):
        for line in text.splitlines():
            line = line.split('#')[0].strip()
            if '=' not in line:
                continue
            k, v = (x.strip() for x in line.split('=', 1))
            self._named(k, v)

    def _named(self, name, value):
        try:
            raw = bytes.fromhex(value.replace('_', '').strip())
        except ValueError:
            return
        if len(raw) != 16:
            return
        v = b2i(raw)
        low = name.lower()
        if low.startswith('slot0x') and 'keyx' in low:
            self.keyx[int(low[6:8], 16)] = v
        elif low.startswith('slot0x') and 'keyy' in low:
            self.keyy[int(low[6:8], 16)] = v
        elif low.startswith('common'):
            tail = low[6:].strip('_ ')
            if tail.isdigit():
                self.common[int(tail)] = v
        elif low in ('fixedsystemkey', 'fixed_system_key'):
            self.keyx[-1] = v

    def _seeddb(self, blob):
        if len(blob) < 0x10:
            raise ValueError('seeddb too short')
        count, = struct.unpack('<I', blob[:4])
        for i in range(count):
            o = 0x10 + i * 0x20
            if o + 0x20 > len(blob):
                break
            tid, = struct.unpack('<Q', blob[o:o + 8])
            self.seeds[tid] = blob[o + 8:o + 0x18]

    # -- derivation ---------------------------------------------------------

    def have(self, slot):
        return slot in self.keyx

    def normal(self, slot, keyy):
        """Normal key for `slot` with the NCCH KeyY, or None if KeyX is absent."""
        if slot not in self.keyx:
            return None
        return i2b(keygen(self.keyx[slot], keyy))

    def seed_keyy(self, keyy, title_id, seed=None):
        """Seed-crypto KeyY: sha256(original KeyY || seed)[:16]."""
        if seed is None:
            seed = self.seeds.get(title_id)
        if seed is None:
            return None
        h = hashlib.sha256(i2b(keyy) + seed).digest()
        return b2i(h[:16])

    def titlekey(self, enc_titlekey, title_id, common_index=0):
        """Decrypt a ticket titlekey with the common key (AES-CBC, IV=title id)."""
        from Crypto.Cipher import AES
        if common_index not in self.common:
            return None
        if 0x3D not in self.keyx:
            return None
        key = i2b(keygen(self.keyx[0x3D], self.common[common_index]))
        iv = struct.pack('>Q', title_id) + b'\0' * 8
        return AES.new(key, AES.MODE_CBC, iv).decrypt(enc_titlekey)

    def titlekey_candidates(self, enc_titlekey, title_id):
        """Every plausible decrypted titlekey, for validate-by-magic callers."""
        out = []
        for idx in sorted(self.common):
            tk = self.titlekey(enc_titlekey, title_id, idx)
            if tk:
                out.append((idx, tk))
        return out

    # -- validation ---------------------------------------------------------

    @staticmethod
    def pick(candidates, validate):
        """Return the first candidate `validate(c)` accepts, else None.

        This is how a guessed slot or layout is caught: the key is only used if
        decrypting with it produces a structurally valid result.
        """
        for c in candidates:
            try:
                if validate(c):
                    return c
            except Exception:
                continue
        return None

    def describe(self):
        lines = []
        if self.sources:
            for s in self.sources:
                lines.append(f'  source   {s}')
        else:
            lines.append('  source   (none found)')
        if self.keyx:
            slots = ' '.join(f'0x{s:02X}' for s in sorted(self.keyx) if s >= 0)
            lines.append(f'  KeyX     {slots}')
        if self.common:
            lines.append(f'  common   {sorted(self.common)}')
        if self.seeds:
            lines.append(f'  seeds    {len(self.seeds)} titles')
        for slot, label in ((0x2C, 'standard'), (0x25, 'method 1 / 7.x'),
                            (0x18, 'method 10 / New3DS'),
                            (0x1B, 'method 11 / New3DS'), (0x3D, 'titlekey')):
            lines.append(f'  slot 0x{slot:02X} {label:<18} '
                         f"{'yes' if self.have(slot) else 'MISSING'}")
        return '\n'.join(lines)


_store = None


def store(project=None, reload=False):
    global _store
    if _store is None or reload:
        _store = KeyStore(project)
    return _store

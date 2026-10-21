"""`PARAM.SFO` - the key/value block the system menu reads.

A localisation that patches only the game archive leaves the title the launcher
shows in the source language. `PARAM.SFO` is where that string lives, and it is
560 bytes of plain structure - no compression, no encryption, no slot puzzle.

    0x00  4  magic `\\x00PSF`
    0x04  4  version
    0x08  4  key table offset
    0x0c  4  data table offset
    0x10  4  entry count
    0x14  .  entries, 16 bytes each:
              u16 key offset (relative to key table)
              u16 format     0x0204 = utf-8 string, 0x0404 = u32
              u32 length     bytes actually used, INCLUDING the NUL for strings
              u32 max        bytes reserved for this value
              u32 data offset (relative to data table)

`TITLE` reserves far more than it uses - measured on this disc, 37 of 128 bytes -
so a longer or shorter replacement fits without moving anything. That is the
whole reason this is safe to patch in place: **only `length` changes, never
`max`, never any offset, never the file size.**

`write_value` refuses a value that does not fit `max` rather than growing the
field, because growing it would shift every later data offset and the entry
table does not get rewritten here.
"""

import struct

MAGIC = b'\x00PSF'

FMT_UTF8 = 0x0204
FMT_U32 = 0x0404

_HEADER = '<4sIIII'
_ENTRY = '<HHIII'
_ENTRY_SIZE = 16


class SfoError(Exception):
    """A block that does not match the layout above."""


class Entry:
    """One key/value pair, with the absolute offsets needed to rewrite it."""

    __slots__ = ('key', 'fmt', 'length', 'max', 'data_at', 'entry_at')

    def __init__(self, key, fmt, length, maximum, data_at, entry_at):
        self.key = key
        self.fmt = fmt
        self.length = length
        self.max = maximum
        self.data_at = data_at
        self.entry_at = entry_at

    def __repr__(self):
        return ('Entry(%r, fmt=0x%04x, length=%d, max=%d)'
                % (self.key, self.fmt, self.length, self.max))


class Sfo:
    """Parsed `PARAM.SFO`. Read-only view plus in-place value replacement."""

    def __init__(self, blob):
        if len(blob) < struct.calcsize(_HEADER):
            raise SfoError('too short for a header: %d bytes' % len(blob))
        magic, version, key_off, data_off, count = struct.unpack_from(
            _HEADER, blob, 0)
        if magic != MAGIC:
            raise SfoError('not a PARAM.SFO (magic %r)' % magic)
        self.blob = bytes(blob)
        self.version = version
        self.key_off = key_off
        self.data_off = data_off
        self.entries = []
        for i in range(count):
            at = struct.calcsize(_HEADER) + i * _ENTRY_SIZE
            if at + _ENTRY_SIZE > len(self.blob):
                raise SfoError('entry %d runs past the block' % i)
            ko, fmt, length, maximum, do = struct.unpack_from(_ENTRY, self.blob, at)
            start = key_off + ko
            end = self.blob.find(b'\0', start)
            if end < 0:
                raise SfoError('unterminated key at 0x%x' % start)
            self.entries.append(Entry(self.blob[start:end].decode('ascii'),
                                      fmt, length, maximum,
                                      data_off + do, at))

    def find(self, key):
        for e in self.entries:
            if e.key == key:
                return e
        return None

    def value(self, key):
        """Decoded value: `str` for utf-8 entries, `int` for u32."""
        e = self.find(key)
        if e is None:
            raise SfoError('no such key %r' % key)
        raw = self.blob[e.data_at:e.data_at + e.length]
        if e.fmt == FMT_U32:
            return struct.unpack('<I', raw)[0]
        return raw.rstrip(b'\0').decode('utf-8')

    def items(self):
        return [(e.key, self.value(e.key)) for e in self.entries]

    def write_value(self, key, text):
        """A new block with `key` set to `text`. Size and layout are unchanged.

        The stored form is utf-8 plus one NUL, and `length` counts that NUL -
        which is why the shipped 12-character title measures 37 bytes and not 36.
        Bytes between the new value and `max` are cleared so no tail of the old
        value survives.
        """
        e = self.find(key)
        if e is None:
            raise SfoError('no such key %r' % key)
        if e.fmt != FMT_UTF8:
            raise SfoError('%r is not a utf-8 entry (fmt 0x%04x)'
                           % (key, e.fmt))
        encoded = text.encode('utf-8') + b'\0'
        if len(encoded) > e.max:
            raise SfoError('%r needs %d bytes, the field reserves %d'
                           % (text, len(encoded), e.max))
        out = bytearray(self.blob)
        out[e.data_at:e.data_at + e.max] = encoded + b'\0' * (e.max - len(encoded))
        struct.pack_into('<HHIII', out, e.entry_at,
                         struct.unpack_from(_ENTRY, self.blob, e.entry_at)[0],
                         e.fmt, len(encoded), e.max,
                         e.data_at - self.data_off)
        return bytes(out)

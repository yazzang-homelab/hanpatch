"""DSARC FL — the flat archive this title packs its script and data into.

PROVENANCE: measured from the disc. Every field below was read off a real
archive and every rule is one the rebuild test enforces: parse the archive,
build it again from its own members, and demand the bytes come back identical.

    0x00   8  magic, 'DSARC FL'
    0x08   4  member count
    0x0C   4  zero on this disc; preserved rather than assumed to mean nothing
    0x10      member table, `count` entries of 48 bytes

    ENTRY

    0x00  40  name, NUL padded, backslash separated when it has a directory
    0x28   4  size in bytes
    0x2C   4  offset from the start of the archive

Members start on 512-byte boundaries and the table itself is padded out to one,
so the gaps between members are alignment and nothing else. The archive's own
length is padded the same way, which is why a rebuilt archive is longer than the
sum of its members. Sizes are exact; the padding is never part of a member.

Names carry a directory component with a backslash separator. This module keeps
the name exactly as stored, because it is the key the game looks a member up by
and rewriting separators would silently rename things.

Content boundary: members are opaque. What a `.dsf` or `.das` holds is the
title's business, not this module's.
"""
import struct

MAGIC = b'DSARC FL'
HEADER = 0x10
ENTRY = 0x30
NAME = 0x28
ALIGN = 512


class DsarcError(Exception):
    pass


def _align(n, to=ALIGN):
    return (n + to - 1) // to * to


class Member:
    __slots__ = ('name', 'offset', 'size')

    def __init__(self, name, offset, size):
        self.name = name
        self.offset = offset
        self.size = size

    def __repr__(self):
        return 'Member(%r, offset=%d, size=%d)' % (self.name, self.offset,
                                                   self.size)


class Dsarc:
    """A parsed archive, as a view over the bytes it was read from."""

    def __init__(self, data):
        if len(data) < HEADER or data[:len(MAGIC)] != MAGIC:
            raise DsarcError('not a DSARC FL archive')
        count, self.reserved = struct.unpack_from('<II', data, 8)
        need = HEADER + count * ENTRY
        if need > len(data):
            raise DsarcError('table of %d members needs %d bytes, have %d'
                             % (count, need, len(data)))
        self.data = data
        self.members = []
        for i in range(count):
            o = HEADER + i * ENTRY
            raw = data[o:o + NAME]
            try:
                name = raw.split(b'\x00')[0].decode('ascii')
            except UnicodeDecodeError:
                raise DsarcError('member %d has a non-ASCII name' % i)
            size, offset = struct.unpack_from('<II', data, o + NAME)
            if offset + size > len(data):
                raise DsarcError('member %r runs past the archive (%d + %d > %d)'
                                 % (name, offset, size, len(data)))
            self.members.append(Member(name, offset, size))

    def __len__(self):
        return len(self.members)

    def __iter__(self):
        return iter(self.members)

    def names(self):
        return [m.name for m in self.members]

    def read(self, name):
        for m in self.members:
            if m.name == name:
                return bytes(self.data[m.offset:m.offset + m.size])
        raise DsarcError('no member named %r' % name)

    def contents(self):
        return [(m.name, self.read(m.name)) for m in self.members]


def build(members, reserved=0):
    """Pack `(name, bytes)` pairs into an archive.

    Member order is the caller's; the table is written in that order and the
    payloads follow in the same order, which is how the archives on the disc are
    laid out.
    """
    members = list(members)
    table = bytearray()
    payload = bytearray()
    start = _align(HEADER + len(members) * ENTRY)
    for name, blob in members:
        raw = name.encode('ascii')
        if len(raw) >= NAME:
            raise DsarcError('name %r does not fit in %d bytes' % (name, NAME))
        offset = start + len(payload)
        table += raw + b'\x00' * (NAME - len(raw))
        table += struct.pack('<II', len(blob), offset)
        payload += blob
        payload += b'\x00' * (_align(len(payload)) - len(payload))
    out = bytearray(MAGIC)
    out += struct.pack('<II', len(members), reserved)
    out += table
    out += b'\x00' * (start - len(out))
    out += payload
    return bytes(out)

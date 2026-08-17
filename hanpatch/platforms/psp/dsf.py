"""The title's script pair: a table of chunks and the bytecode they index.

PROVENANCE: measured from the disc. The corpus test parses the real pair,
rebuilds it with no edits, and demands both files come back byte identical.

    TABLE (SCRIPT.TBL, AISCRIPT.TBL)

    0x00   4  chunk count
    0x04  12  zero on this disc; preserved rather than assumed to mean nothing
    0x10      chunk table, `count` entries of 32 bytes

    ENTRY

    0x00   4  id, unique but NOT in ascending order - it is a key, not an index
    0x04   4  offset into the data file
    0x08  24  source file name, NUL terminated, e.g. 'story\\goodEnd.dsf'

The 24-byte field is not only a name. In 191 entries the bytes after the
terminator are non-zero, so it is a name followed by something this module has
not identified. Rebuilding a name from its text and padding the rest with NULs
therefore destroys data - which is exactly what the first version of this module
did, and what the byte-identical rebuild test caught. The field is carried
through verbatim and `name` is only a reading of its prefix.

Many chunks share a name: a `.dsf` source compiles to one chunk per labelled
entry point, so the name is a grouping and the id is what identifies a chunk.
Chunks appear in ascending offset order and each starts on a 16-byte boundary,
so a chunk's length is the next chunk's offset minus its own, minus whatever
alignment padding sits at the end.

    TEXT RECORD, inside a chunk's bytecode

    0x00   2  size, always the string length plus three
    0x02   2  string length in bytes, not characters
    0x04      Shift-JIS bytes, then a NUL

The size field counts from the length field to the end of the NUL, which is why
it is length + 3. There is no pointer table and no string pool: a string is an
instruction operand sitting inline in the bytecode, and it is not aligned - all
four byte alignments occur. That is what makes the pair rewritable at all. A
translation that changes a string's length only has to fix the two length
fields, re-pad the chunk, and recompute the offsets in the table, because
nothing else in the file refers to a string by address.

Both relations were checked against every string the disc holds and neither has
an exception: size is length plus three, and the length field is the exact byte
count up to the NUL.

Content boundary: a record's bytes are opaque here. This module does not decode
Shift-JIS, does not know which records are dialogue and which are debug labels,
and does not judge what a chunk does.
"""
import struct

HEADER = 0x10
ENTRY = 0x20
NAME = 0x18
ALIGN = 16
RECORD_OVERHEAD = 3


class ScriptError(Exception):
    pass


def _align(n, to=ALIGN):
    return (n + to - 1) // to * to


class Chunk:
    __slots__ = ('index', 'id', 'field', 'offset', 'size')

    def __init__(self, index, id_, field, offset, size):
        self.index = index
        self.id = id_
        self.field = field      # the whole 24 bytes, carried through verbatim
        self.offset = offset
        self.size = size

    @property
    def name(self):
        return self.field.split(b'\x00')[0].decode('ascii')

    def __repr__(self):
        return 'Chunk(%d, id=%d, %r, offset=%d, size=%d)' % (
            self.index, self.id, self.name, self.offset, self.size)


class Record:
    """One inline string, located by the chunk it lives in."""

    __slots__ = ('chunk', 'start', 'text')

    def __init__(self, chunk, start, text):
        self.chunk = chunk      # index into Script.chunks
        self.start = start      # offset of the size field within the chunk
        self.text = text        # Shift-JIS bytes, without the NUL

    @property
    def key(self):
        return '%d:%d' % (self.chunk, self.start)

    def __repr__(self):
        return 'Record(chunk=%d, start=%d, %d bytes)' % (
            self.chunk, self.start, len(self.text))


def scan(chunk):
    """Find every text record in one chunk's bytecode.

    Records are not aligned, so this walks byte by byte and accepts a position
    only when all four things the format guarantees hold at once: the size field
    agrees with the length field, the string is legal Shift-JIS, it holds no
    embedded NUL, and it is terminated by one. Accepting a record skips past it,
    so a string's own bytes cannot produce a second, overlapping match.
    """
    out = []
    i = 0
    n = len(chunk)
    while i + 4 < n:
        size, length = struct.unpack_from('<HH', chunk, i)
        if length and size == length + RECORD_OVERHEAD and i + 4 + length < n \
                and chunk[i + 4 + length] == 0:
            text = bytes(chunk[i + 4:i + 4 + length])
            if 0 not in text:
                try:
                    text.decode('shift_jis')
                except UnicodeDecodeError:
                    i += 1
                    continue
                out.append((i, text))
                i += 4 + length + 1
                continue
        i += 1
    return out


class Script:
    """A parsed table/data pair."""

    def __init__(self, table, data):
        if len(table) < HEADER:
            raise ScriptError('table too short to hold a header')
        count, = struct.unpack_from('<I', table, 0)
        self.reserved = bytes(table[4:HEADER])
        need = HEADER + count * ENTRY
        if need > len(table):
            raise ScriptError('table of %d chunks needs %d bytes, have %d'
                              % (count, need, len(table)))
        if not count:
            raise ScriptError('table declares no chunks')
        self.data = bytes(data)
        raw = []
        for i in range(count):
            o = HEADER + i * ENTRY
            id_, offset = struct.unpack_from('<II', table, o)
            field = bytes(table[o + 8:o + ENTRY])
            try:
                field.split(b'\x00')[0].decode('ascii')
            except UnicodeDecodeError:
                raise ScriptError('chunk %d has a non-ASCII name' % i)
            raw.append((id_, offset, field))
        self.chunks = []
        for i, (id_, offset, field) in enumerate(raw):
            end = raw[i + 1][1] if i + 1 < count else len(data)
            if offset > end:
                raise ScriptError('chunk %d at %d overlaps the next at %d'
                                  % (i, offset, end))
            if end > len(data):
                raise ScriptError('chunk %d runs past the data file' % i)
            self.chunks.append(Chunk(i, id_, field, offset, end - offset))

    def __len__(self):
        return len(self.chunks)

    def chunk_bytes(self, index):
        c = self.chunks[index]
        return self.data[c.offset:c.offset + c.size]

    def records(self):
        out = []
        for c in self.chunks:
            for start, text in scan(self.chunk_bytes(c.index)):
                out.append(Record(c.index, start, text))
        return out

    def build(self, edits=None):
        """Re-emit (table, data), replacing the records named in `edits`.

        `edits` maps a record key ('chunk:start') to replacement Shift-JIS
        bytes. Replacements may be any length: the two length fields are
        rewritten, the chunk is re-padded, and the table's offsets are
        recomputed. Nothing else in either file addresses a string, so nothing
        else has to move.
        """
        edits = dict(edits or {})
        data = bytearray()
        offsets = []
        for c in self.chunks:
            body = self.chunk_bytes(c.index)
            mine = [(start, text) for start, text in scan(body)
                    if '%d:%d' % (c.index, start) in edits]
            if mine:
                out = bytearray()
                prev = 0
                for start, text in mine:
                    new = edits.pop('%d:%d' % (c.index, start))
                    if 0 in new:
                        raise ScriptError('replacement for %d:%d holds a NUL'
                                          % (c.index, start))
                    if len(new) + RECORD_OVERHEAD > 0xFFFF:
                        raise ScriptError('replacement for %d:%d is too long '
                                          'for a 16-bit length' % (c.index, start))
                    out += body[prev:start]
                    out += struct.pack('<HH', len(new) + RECORD_OVERHEAD,
                                       len(new))
                    out += new + b'\x00'
                    prev = start + 4 + len(text) + 1
                out += body[prev:]
                body = bytes(out)
            offsets.append(len(data))
            data += body
            data += b'\x00' * (_align(len(data)) - len(data))
        if edits:
            raise ScriptError('no such record: %s'
                              % ', '.join(sorted(edits)[:5]))
        table = bytearray(struct.pack('<I', len(self.chunks)))
        table += self.reserved
        for c, off in zip(self.chunks, offsets):
            if len(c.field) != NAME:
                raise ScriptError('chunk %d has a %d-byte name field, not %d'
                                  % (c.index, len(c.field), NAME))
            table += struct.pack('<II', c.id, off)
            table += c.field
        return bytes(table), bytes(data)

"""PSPFS_V1 — the flat filesystem this title keeps in USRDIR/DATA.DAT.

PROVENANCE: measured from the disc. The identity test parses the real 371 MB
archive and rebuilds it from its own members; the result is byte identical,
including every byte of padding.

    0x00   8  magic, 'PSPFS_V1'
    0x08   4  file count
    0x0C   4  zero on this disc; preserved rather than assumed to mean nothing
    0x10      file table, `count` entries of 32 bytes

    ENTRY

    0x00  20  name, NUL padded, no directory component
    0x14   4  decompressed size, or zero when the file is not compressed
    0x18   4  stored size in bytes
    0x1C   4  offset in BYTES from the start of the archive

Three facts that a rebuild gets wrong if it assumes the obvious:

**The name field is 20 bytes, not 24.** Read it as 24 and the four bytes at
0x14 look like padding, so a rebuild writes zeros over them and 170 of the 547
entries change. The longest name on the disc is 19 characters, so nothing about
the names themselves reveals the boundary - only the rebuild does.

**Those four bytes are the decompressed size, and injection depends on them.**
They are non-zero on exactly the file types that hold IMY containers and always
exceed the stored size. For every file this project can decode - 74 of the 170 -
the value equals the decoded payload exactly, SCRIPT.SDT's 1056256 and
DATABASE.DAT's 857600 among them. The loader allocates from this field before
decompressing, so a translation that changes a payload's size and leaves it
stale hands the game a buffer too small for what it is about to write.

**The table order is not the storage order.** Files appear in the table in one
order and in the archive in another, and neither is alphabetical. Rebuilding in
table order produces a valid archive that is not the original, so the storage
order is carried through as `order` rather than regenerated.

**Every file is followed by at least one byte of padding.** The rule is not
"align the next offset up to 512" - that reproduces 540 of the 546 gaps here and
gets the archive length wrong. It is `end + (512 - end % 512)`, which always
advances, so a file whose length is already a multiple of 512 still gets a full
512 bytes of padding. Six files on this disc land on that boundary and are the
whole difference.

Content boundary: files are opaque. This module does not care that most of them
are IMY containers.
"""
import struct

MAGIC = b'PSPFS_V1'
HEADER = 0x10
ENTRY = 0x20
NAME = 0x14
ALIGN = 512


class PspfsError(Exception):
    pass


def pad_to(end, align=ALIGN):
    """The next file's offset: always at least one byte of padding."""
    return end + (align - end % align)


class File:
    __slots__ = ('index', 'name', 'offset', 'size', 'decompressed')

    def __init__(self, index, name, offset, size, decompressed=0):
        self.index = index
        self.name = name
        self.offset = offset
        self.size = size
        self.decompressed = decompressed

    @property
    def compressed(self):
        return bool(self.decompressed)

    def __repr__(self):
        return 'File(%d, %r, offset=%d, size=%d, decompressed=%d)' % (
            self.index, self.name, self.offset, self.size, self.decompressed)


class Pspfs:
    """A parsed archive over a buffer that supports slicing (bytes or mmap)."""

    def __init__(self, data):
        if len(data) < HEADER or bytes(data[:len(MAGIC)]) != MAGIC:
            raise PspfsError('not a PSPFS_V1 archive')
        count, self.reserved = struct.unpack_from('<II', data, 8)
        need = HEADER + count * ENTRY
        if need > len(data):
            raise PspfsError('table of %d files needs %d bytes, have %d'
                             % (count, need, len(data)))
        self.data = data
        self.files = []
        for i in range(count):
            o = HEADER + i * ENTRY
            raw = bytes(data[o:o + NAME])
            try:
                name = raw.split(b'\x00')[0].decode('ascii')
            except UnicodeDecodeError:
                raise PspfsError('file %d has a non-ASCII name' % i)
            decompressed, size, offset = struct.unpack_from('<III', data,
                                                            o + NAME)
            if offset + size > len(data):
                raise PspfsError('%r runs past the archive (%d + %d > %d)'
                                 % (name, offset, size, len(data)))
            self.files.append(File(i, name, offset, size, decompressed))

    def __len__(self):
        return len(self.files)

    def __iter__(self):
        return iter(self.files)

    def names(self):
        return [f.name for f in self.files]

    def find(self, name):
        for f in self.files:
            if f.name == name:
                return f
        raise PspfsError('no file named %r' % name)

    def read(self, name):
        f = self.find(name)
        return bytes(self.data[f.offset:f.offset + f.size])

    @property
    def order(self):
        """Table indices in storage order, which is not table order."""
        return [f.index for f in sorted(self.files, key=lambda f: f.offset)]

    def build(self, replace=None, decompressed=None, out=None):
        """Re-emit the archive, replacing the named files.

        Storage order and the padding rule are preserved, so an archive rebuilt
        with no replacements is byte identical to the one parsed. `out` may be a
        writable binary file; when it is None the archive is returned as bytes.
        """
        replace = dict(replace or {})
        unknown = set(replace) - set(self.names())
        if unknown:
            raise PspfsError('no such file: %s' % ', '.join(sorted(unknown)[:5]))
        decompressed = dict(decompressed or {})
        stale = {f.name for f in self.files
                 if f.name in replace and f.compressed
                 and f.name not in decompressed}
        if stale:
            raise PspfsError(
                'replacing compressed %s without its decompressed size; the '
                'loader allocates from that field'
                % ', '.join(sorted(stale)[:5]))
        sizes = {f.name: len(replace[f.name]) if f.name in replace else f.size
                 for f in self.files}
        offsets = {}
        cursor = pad_to(HEADER + len(self.files) * ENTRY)
        for index in self.order:
            f = self.files[index]
            offsets[f.name] = cursor
            cursor = pad_to(cursor + sizes[f.name])
        total = cursor

        table = bytearray(MAGIC)
        table += struct.pack('<II', len(self.files), self.reserved)
        for f in self.files:
            raw = f.name.encode('ascii')
            if len(raw) > NAME:
                raise PspfsError('name %r does not fit in %d bytes'
                                 % (f.name, NAME))
            table += raw + b'\x00' * (NAME - len(raw))
            table += struct.pack('<III',
                                 decompressed.get(f.name, f.decompressed),
                                 sizes[f.name], offsets[f.name])

        sink = out if out is not None else _Buffer()
        sink.write(bytes(table))
        written = len(table)
        for index in self.order:
            f = self.files[index]
            at = offsets[f.name]
            if at < written:
                raise PspfsError('layout went backwards at %r' % f.name)
            sink.write(b'\x00' * (at - written))
            blob = replace[f.name] if f.name in replace else \
                bytes(self.data[f.offset:f.offset + f.size])
            sink.write(blob)
            written = at + len(blob)
        sink.write(b'\x00' * (total - written))
        return None if out is not None else sink.value()


class _Buffer:
    def __init__(self):
        self._parts = []

    def write(self, blob):
        self._parts.append(blob)

    def value(self):
        return b''.join(self._parts)

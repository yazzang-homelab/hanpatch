"""PBP wrapper reader (EBOOT.PBP) — the executable container.

PROVENANCE: transcribed from the documented homebrew layout, not measured from a
disc this project has parsed. See the note at the top of `iso9660.py`; the same
caveat applies.

    0x00       4  magic, '\\x00PBP'
    0x04       4  version
    0x08       4  offset of PARAM.SFO
    0x0C       4  offset of ICON0.PNG
    0x10       4  offset of ICON1.PMF
    0x14       4  offset of PIC0.PNG
    0x18       4  offset of PIC1.PNG
    0x1C       4  offset of SND0.AT3
    0x20       4  offset of DATA.PSP
    0x24       4  offset of DATA.PSAR
    0x28          first section payload

Sections are stored back to back in header order, so a section's size is the
next offset minus its own and the last one runs to end of file. There is no
length field anywhere. An absent section is encoded by giving it the same offset
as the section after it, which makes it zero length rather than absent - the
distinction does not exist in the format and this module does not invent one.

Because sizes are implied by neighbours, the offsets must be non-decreasing for
the file to mean anything at all. A file whose offsets go backwards is not a PBP
with an odd section, it is damage, and `parse` fails on it rather than handing
back a negative length.

Content boundary: sections are exposed as opaque byte ranges. This module does
not parse PARAM.SFO, does not decrypt DATA.PSP, and does not know what a title
puts in DATA.PSAR.
"""
import struct

MAGIC = b'\x00PBP'
HEADER = 0x28

# in header order; the order is the format, not a convention
SECTIONS = (
    'PARAM.SFO',
    'ICON0.PNG',
    'ICON1.PMF',
    'PIC0.PNG',
    'PIC1.PNG',
    'SND0.AT3',
    'DATA.PSP',
    'DATA.PSAR',
)


class PbpError(Exception):
    pass


class Section:
    """One section, as a range into the containing file."""

    __slots__ = ('name', 'offset', 'size')

    def __init__(self, name, offset, size):
        self.name = name
        self.offset = offset
        self.size = size

    @property
    def empty(self):
        return self.size == 0

    def __repr__(self):
        return '<Section %s offset=0x%X size=%d>' % (self.name, self.offset, self.size)


class Pbp:
    """A parsed PBP wrapper. `blob` is the whole file."""

    def __init__(self, blob):
        self.blob = blob
        self.version = None
        self.sections = {}
        self._parse()

    def _parse(self):
        size = len(self.blob)
        if size < HEADER:
            raise PbpError('file is %d bytes, shorter than the %d byte header'
                           % (size, HEADER))
        magic = bytes(self.blob[0:4])
        if magic != MAGIC:
            raise PbpError('expected magic %r, found %r' % (MAGIC, magic))
        self.version = struct.unpack_from('<I', self.blob, 0x04)[0]

        offsets = list(struct.unpack_from('<8I', self.blob, 0x08))
        for i, off in enumerate(offsets):
            if off < HEADER:
                raise PbpError('%s: offset 0x%X falls inside the header'
                               % (SECTIONS[i], off))
            if off > size:
                raise PbpError('%s: offset 0x%X is past end of file (%d bytes)'
                               % (SECTIONS[i], off, size))
        for i in range(7):
            if offsets[i + 1] < offsets[i]:
                raise PbpError('offsets decrease between %s (0x%X) and %s (0x%X); '
                               'section sizes are implied by neighbours so this '
                               'cannot be read'
                               % (SECTIONS[i], offsets[i], SECTIONS[i + 1], offsets[i + 1]))

        bounds = offsets + [size]
        for i, name in enumerate(SECTIONS):
            self.sections[name] = Section(name, offsets[i], bounds[i + 1] - offsets[i])

    @classmethod
    def from_path(cls, path):
        with open(path, 'rb') as fh:
            return cls(fh.read())

    def read(self, name):
        """A section's bytes. Empty sections give b'', which is what they are."""
        section = self.sections.get(name)
        if section is None:
            raise PbpError('no section %r; the format has exactly %s'
                           % (name, ', '.join(SECTIONS)))
        return bytes(self.blob[section.offset:section.offset + section.size])

    def __iter__(self):
        for name in SECTIONS:
            yield self.sections[name]


def build(parts):
    """A PBP file from `{name: bytes}`. Missing names become empty sections.

    Rebuilds are whole-file: because every size is implied by the next offset,
    changing one section moves every section after it, and there is no way to
    edit one in place unless its length is unchanged.
    """
    unknown = sorted(set(parts) - set(SECTIONS))
    if unknown:
        raise PbpError('unknown section(s) %s; the format has exactly %s'
                       % (', '.join(unknown), ', '.join(SECTIONS)))

    payloads = [parts.get(name, b'') for name in SECTIONS]
    offsets = []
    cursor = HEADER
    for blob in payloads:
        offsets.append(cursor)
        cursor += len(blob)

    out = bytearray()
    out += MAGIC
    out += struct.pack('<I', 0x00010000)
    out += struct.pack('<8I', *offsets)
    for blob in payloads:
        out += blob
    return bytes(out)

"""ISO 9660 volume reader (PSP / UMD) — the outermost container.

PROVENANCE: unlike the 3DS format modules in this project, the field table below
is NOT measured from a disc this project has parsed. It is transcribed from the
published standard (ECMA-119, identical to ISO 9660) plus the PSP-specific
constraint that UMD images are Mode 1 with 2048-byte logical sectors. Every
constant here is a spec constant. The first real image is expected to agree, but
until one has been walked end to end this module is unverified against hardware,
and anything it induces about a *title* rather than the volume format is
guesswork that does not belong here.

    volume layout
    sector 0..15    system area, not described by the standard, ignored here
    sector 16..     volume descriptor sequence, one descriptor per sector,
                    terminated by a descriptor of type 255

    volume descriptor header (every type)
    0x00       1  type; 1 = primary, 2 = supplementary, 255 = terminator
    0x01       5  standard identifier, always 'CD001'
    0x06       1  descriptor version

    primary volume descriptor, offsets within its sector
    0x28      32  volume identifier, d-characters, space padded
    0x50       8  volume space size, both-endian
    0x80       4  logical block size, both-endian (2 + 2)
    0x9C      34  root directory record, same shape as any other

    directory record
    0x00       1  record length; 0 means "no more records in this sector"
    0x01       1  extended attribute record length, skipped over
    0x02       8  extent LBA, both-endian
    0x0A       8  data length in bytes, both-endian
    0x12       7  recording timestamp, not read
    0x19       1  file flags; bit 1 (0x02) marks a directory
    0x1A       1  file unit size
    0x1B       1  interleave gap size
    0x1C       4  volume sequence number, both-endian
    0x20       1  file identifier length
    0x21       n  file identifier, then a pad byte when n is even

Both-endian fields store the value twice, little first then big. This module
reads the little half and checks the big half agrees; a disagreement means the
image is damaged or is not what it claims, and that is worth failing on rather
than silently trusting one half.

File identifiers carry a ';1' version suffix which is part of the on-disc name
but never part of how anyone refers to the file. `walk` strips it and keeps the
raw form in `raw_name` so a rebuild can put back exactly what was there.

Content boundary: this module addresses and copies file extents as opaque bytes.
It never decodes the contents of a file, and it has no opinion about what any
particular title stores.
"""
import struct

SECTOR = 2048
SYSTEM_AREA_SECTORS = 16
STD_ID = b'CD001'

VD_PRIMARY = 1
VD_TERMINATOR = 255

# offsets inside a primary volume descriptor
PVD_VOLUME_ID = 0x28
PVD_VOLUME_SPACE = 0x50
PVD_BLOCK_SIZE = 0x80
PVD_ROOT_RECORD = 0x9C
ROOT_RECORD_LEN = 34

# offsets inside a directory record
DR_LENGTH = 0x00
DR_EXT_ATTR = 0x01
DR_EXTENT = 0x02
DR_SIZE = 0x0A
DR_FLAGS = 0x19
DR_NAME_LEN = 0x20
DR_NAME = 0x21

FLAG_DIR = 0x02

# the two reserved identifiers, stored as a single byte rather than as text
NAME_SELF = b'\x00'
NAME_PARENT = b'\x01'

# a directory tree deep enough to exceed this is a malformed or hostile image;
# the standard's own limit is 8 levels and real discs stay well inside it
MAX_DEPTH = 32


class IsoError(Exception):
    pass


def _both_endian_32(blob, at, where):
    """A both-endian doubleword. Returns the value; raises if the halves differ."""
    little = struct.unpack_from('<I', blob, at)[0]
    big = struct.unpack_from('>I', blob, at + 4)[0]
    if little != big:
        raise IsoError('%s: both-endian mismatch at 0x%X, %d little vs %d big'
                       % (where, at, little, big))
    return little


def _both_endian_16(blob, at, where):
    little = struct.unpack_from('<H', blob, at)[0]
    big = struct.unpack_from('>H', blob, at + 2)[0]
    if little != big:
        raise IsoError('%s: both-endian mismatch at 0x%X, %d little vs %d big'
                       % (where, at, little, big))
    return little


def offset_of(lba):
    """Byte offset of a logical block. This is the `lba-2048` address space."""
    if lba < 0:
        raise IsoError('negative LBA %d' % lba)
    return lba * SECTOR


class Entry:
    """One directory record, flattened out of the tree.

    `path` is '/'-joined, absolute, and carries no version suffix. `raw_name` is
    the identifier exactly as stored, so a rebuild does not have to guess whether
    a name had ';1' on it.
    """

    __slots__ = ('path', 'raw_name', 'lba', 'size', 'is_dir', 'record_offset')

    def __init__(self, path, raw_name, lba, size, is_dir, record_offset):
        self.path = path
        self.raw_name = raw_name
        self.lba = lba
        self.size = size
        self.is_dir = is_dir
        self.record_offset = record_offset

    @property
    def offset(self):
        """Absolute byte offset of this entry's extent within the image."""
        return offset_of(self.lba)

    def __repr__(self):
        return '<Entry %s %s lba=%d size=%d>' % (
            'dir' if self.is_dir else 'file', self.path, self.lba, self.size)


def _decode_name(raw):
    """Identifier bytes to text, minus the version suffix.

    The standard restricts identifiers to d-characters, which are ASCII. Real
    images are sloppier than the standard, so a byte outside ASCII is decoded
    latin-1 rather than failing the whole walk over a filename.
    """
    if raw == NAME_SELF:
        return '.'
    if raw == NAME_PARENT:
        return '..'
    name = raw.decode('ascii', 'replace') if _is_ascii(raw) else raw.decode('latin-1')
    semi = name.rfind(';')
    if semi > 0:
        name = name[:semi]
    return name


def _is_ascii(raw):
    return all(b < 0x80 for b in raw)


class Iso:
    """A mounted ISO 9660 image, held as bytes.

    PSP images run to hundreds of megabytes, so `from_path` memory-maps rather
    than reading the whole file in. The parsing below indexes the buffer and
    never slices it wholesale, which keeps a walk cheap regardless of image size.
    """

    def __init__(self, blob):
        self.blob = blob
        self.volume_id = None
        self.volume_space = None
        self.block_size = None
        self._root = None
        self._read_descriptors()

    @classmethod
    def from_path(cls, path):
        import mmap
        fh = open(path, 'rb')
        try:
            buf = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        except ValueError:
            fh.close()
            raise IsoError('%s: empty file' % path)
        iso = cls(buf)
        iso._fh = fh
        return iso

    def _read_descriptors(self):
        """Walk the descriptor sequence and keep the primary one."""
        size = len(self.blob)
        index = SYSTEM_AREA_SECTORS
        seen_terminator = False
        while True:
            at = index * SECTOR
            if at + SECTOR > size:
                raise IsoError('volume descriptor sequence runs past end of image '
                               '(sector %d, image is %d bytes)' % (index, size))
            kind = self.blob[at]
            std = bytes(self.blob[at + 1:at + 6])
            if std != STD_ID:
                raise IsoError('sector %d: expected %r, found %r - not an ISO 9660 '
                               'image, or the sector size is not %d'
                               % (index, STD_ID, std, SECTOR))
            if kind == VD_TERMINATOR:
                seen_terminator = True
                break
            if kind == VD_PRIMARY and self._root is None:
                self._read_primary(at)
            index += 1

        if not seen_terminator or self._root is None:
            raise IsoError('no primary volume descriptor before the terminator')

    def _read_primary(self, at):
        raw_id = bytes(self.blob[at + PVD_VOLUME_ID:at + PVD_VOLUME_ID + 32])
        self.volume_id = raw_id.decode('latin-1').rstrip(' ').rstrip('\x00')
        self.volume_space = _both_endian_32(self.blob, at + PVD_VOLUME_SPACE, 'PVD volume space')
        self.block_size = _both_endian_16(self.blob, at + PVD_BLOCK_SIZE, 'PVD block size')
        if self.block_size != SECTOR:
            raise IsoError('logical block size is %d, expected %d; this reader only '
                           'handles Mode 1 images' % (self.block_size, SECTOR))
        record = at + PVD_ROOT_RECORD
        self._root = self._read_record(record, '')[0]

    def _read_record(self, at, parent_path):
        """One directory record at absolute offset `at`.

        Returns `(Entry, record_length)`. A length of 0 is the caller's signal
        that the sector holds no further records, and is returned rather than
        raised because it is normal padding, not damage.
        """
        length = self.blob[at + DR_LENGTH]
        if length == 0:
            return None, 0
        if at + length > len(self.blob):
            raise IsoError('directory record at 0x%X claims %d bytes, past end of image'
                           % (at, length))

        lba = _both_endian_32(self.blob, at + DR_EXTENT, 'directory record 0x%X extent' % at)
        size = _both_endian_32(self.blob, at + DR_SIZE, 'directory record 0x%X size' % at)
        flags = self.blob[at + DR_FLAGS]
        name_len = self.blob[at + DR_NAME_LEN]
        if DR_NAME + name_len > length:
            raise IsoError('directory record at 0x%X: identifier of %d bytes does not '
                           'fit in a %d byte record' % (at, name_len, length))
        raw_name = bytes(self.blob[at + DR_NAME:at + DR_NAME + name_len])

        # the extended attribute record sits between the record and the extent
        ext_attr = self.blob[at + DR_EXT_ATTR]
        is_dir = bool(flags & FLAG_DIR)
        name = _decode_name(raw_name)
        path = parent_path + '/' + name if name not in ('.', '..') else parent_path

        entry = Entry(path=path or '/', raw_name=raw_name, lba=lba + ext_attr,
                      size=size, is_dir=is_dir, record_offset=at)
        return entry, length

    def _children(self, entry, depth):
        """Records inside a directory extent, skipping '.' and '..'."""
        if depth > MAX_DEPTH:
            raise IsoError('directory nesting deeper than %d at %s' % (MAX_DEPTH, entry.path))
        base = entry.offset
        end = base + entry.size
        if end > len(self.blob):
            raise IsoError('directory %s extends past end of image' % entry.path)

        at = base
        while at < end:
            # records never straddle a sector boundary; when the remainder of a
            # sector cannot hold one, it is zero padding and the next record
            # starts at the following sector
            if self.blob[at] == 0:
                next_sector = (at // SECTOR + 1) * SECTOR
                if next_sector >= end:
                    break
                at = next_sector
                continue
            child, length = self._read_record(at, entry.path if entry.path != '/' else '')
            if child is None:
                break
            if child.raw_name not in (NAME_SELF, NAME_PARENT):
                yield child
            at += length

    def walk(self):
        """Every entry in the volume, directories before their contents."""
        stack = [(self._root, 0)]
        while stack:
            node, depth = stack.pop()
            kids = sorted(self._children(node, depth), key=lambda e: e.path)
            for kid in kids:
                yield kid
                if kid.is_dir:
                    stack.append((kid, depth + 1))

    def find(self, path):
        """The entry at `path`, or None. Case-insensitive: identifiers are
        upper-case on disc but nobody writes them that way."""
        want = '/' + path.strip('/')
        for entry in self.walk():
            if entry.path.upper() == want.upper():
                return entry
        return None

    def read(self, entry):
        """An entry's extent as bytes."""
        if entry.is_dir:
            raise IsoError('%s is a directory' % entry.path)
        start = entry.offset
        if start + entry.size > len(self.blob):
            raise IsoError('%s extends past end of image' % entry.path)
        return bytes(self.blob[start:start + entry.size])

    def close(self):
        fh = getattr(self, '_fh', None)
        if fh is not None:
            self.blob.close()
            fh.close()
            self._fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

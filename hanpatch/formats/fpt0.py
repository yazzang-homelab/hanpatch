"""FPT0 archive reader/writer (Dragon Quest VII, 3DS).

Every field is DECODED, and every field this cartridge holds constant is
VALIDATED rather than carried. build() emits the constants and recomputes the
name key, the per-entry data offset and the length from the entry list. Nothing
is copied through from the source bytes except the payloads and the tag string.

That distinction is the whole point of the format work: an earlier draft carried
the reserved words through, which reproduced untouched bytes while quietly
losing information - it kept only the LAST entry's trailing word and wrote that
value into every entry, so an archive whose entries held [1, 2] rebuilt as
[2, 2]. A reader that cannot reproduce a field must refuse it, not average it.

container:
    0x00       4  magic 'FPT0'
    0x04       4  u32 reserved, 0 in all 345 containers - refused otherwise
    0x08       4  u32 entry count
    0x0C       4  u32 version, 1 in all 345 containers - refused otherwise
    0x10   32*n  entry table
    0x10+32n  64  tag block (below)
    ...          payload region, entries concatenated in table order

entry (32 bytes):
    0x00      16  name, ASCII, NUL padded
    0x10       4  u32 name key = (len(name) << 24) | (poly13(name) & 0xFFFFFF)
    0x14       4  u32 data offset, RELATIVE to the start of the payload region
    0x18       4  u32 payload length in bytes
    0x1C       4  u32 reserved, 0 in all 66253 entries - refused otherwise

tag block (64 bytes):
    0x00      56  ASCII, NUL padded. 'TEMP/STEP2' in all 343 /MESS containers -
                  a leftover from the original build tool - and empty in both
                  /LAYOUTTEX containers.
    0x38       4  u32 reserved, 0 in all 345 containers - refused otherwise
    0x3C       4  u32, the same key formula over the tag string; 0 when empty.

poly13: h = 0; for each byte c: h = (h * 13 + c) mod 2**32. Validated against all
66253 entry names and all 345 tag strings.

Residual ambiguity, recorded rather than hidden: every name in this cartridge is
10 or 11 characters, so 'length in the top byte' cannot be separated by
observation alone from '0x0A for ten-character names, 0x0B for eleven'. Length is
the reading that explains all three string populations at once (.txt entries,
.dmp entries and the directory-shaped tag string), and the top byte is certainly
not hash-derived: the full 32-bit poly13 of 'tex000.dmp' has 0x67 there, not
0x0A. A repack never renames an entry, so either reading reproduces the field.

Content boundary: payloads are moved as OPAQUE blobs. This module never decodes
them to text.
"""
import struct

MAGIC = b'FPT0'
HEADER = 0x10
ENTRY = 0x20
TAG = 0x40
TAG_STR = 0x38
NAME = 16
VERSION = 1


class FptError(Exception):
    pass


def poly13(s):
    h = 0
    for c in s.encode('ascii'):
        h = (h * 13 + c) & 0xFFFFFFFF
    return h


def name_key(s):
    """The u32 the format stores alongside a name."""
    if len(s) > 0xFF:
        raise FptError(f'name {s!r} is too long for the key field')
    return ((len(s) << 24) | (poly13(s) & 0xFFFFFF)) & 0xFFFFFFFF


def _ascii(raw, what, where):
    try:
        return raw.decode('ascii')
    except UnicodeDecodeError as exc:
        raise FptError(f'{where}: {what} is not ASCII: byte {raw[exc.start]:#04x} '
                       f'at position {exc.start}') from None


def _to_ascii(text, what, where):
    if not isinstance(text, str):
        raise FptError(f'{where}: {what} must be a string, not '
                       f'{type(text).__name__}')
    try:
        raw = text.encode('ascii')
    except UnicodeEncodeError as exc:
        raise FptError(f'{where}: {what} {text!r} is not ASCII: character '
                       f'{text[exc.start]!r} at position {exc.start}') from None
    # A NUL would be written into a NUL-PADDED field and read straight back as a
    # shorter string, so the writer has to refuse what the reader refuses. Every
    # other refusal in this module is symmetric and this one must be too.
    if b'\0' in raw:
        raise FptError(f'{where}: {what} {text!r} contains a NUL at position '
                       f'{raw.index(0)}; the field is NUL-padded, so an embedded '
                       f'NUL could not be read back')
    return raw


class Entry:
    """One named payload.

    `source_offset` is a read-only diagnostic recording where parse() found the
    payload. build() ignores it and recomputes every offset from the entry order,
    so editing it changes nothing.
    """

    __slots__ = ('name', 'data', 'source_offset')

    def __init__(self, name, data, source_offset=None):
        self.name = name
        self.data = data
        self.source_offset = source_offset


def parse(blob, where='<archive>'):
    """Parse an FPT0 archive into (header, [Entry]).

    `header` carries only the one field the format actually varies: `tag`.

    Fail closed on anything that could not be reproduced: a wrong magic, a
    reserved word that is not zero, an unknown version, a table or payload past
    EOF, unclaimed trailing bytes, a stored key that disagrees with its name, a
    stored offset that disagrees with the concatenation order, or a duplicate
    entry name. No container on the cartridge violates any of these.
    """
    n = len(blob)
    if n < HEADER:
        raise FptError(f'{where}: {n} bytes is shorter than the {HEADER}-byte header')
    if blob[:4] != MAGIC:
        raise FptError(f'{where}: magic {bytes(blob[:4])!r} is not {MAGIC!r}')
    reserved, count, version = struct.unpack_from('<3I', blob, 4)
    if reserved != 0:
        raise FptError(f'{where}: header reserved word at 0x04 is {reserved}, and '
                       f'every container on the cartridge has 0 - refusing rather '
                       f'than carrying a field this reader cannot reproduce')
    if version != VERSION:
        raise FptError(f'{where}: version {version} is not the only version this '
                       f'reader has evidence for ({VERSION})')
    table_end = HEADER + count * ENTRY
    tag_end = table_end + TAG
    if tag_end > n:
        raise FptError(f'{where}: {count} entries plus the {TAG}-byte tag block need '
                       f'{tag_end} bytes, file has {n}')

    tag_raw = blob[table_end:tag_end]
    tag_str = bytes(tag_raw[:TAG_STR]).rstrip(b'\0')
    if b'\0' in tag_str:
        raise FptError(f'{where}: tag string has an embedded NUL')
    tag = _ascii(tag_str, 'the tag string', where)
    tag_reserved, tag_key = struct.unpack_from('<2I', tag_raw, TAG_STR)
    if tag_reserved != 0:
        raise FptError(f'{where}: tag reserved word at 0x38 is {tag_reserved}, and '
                       f'every container on the cartridge has 0')
    expect = name_key(tag) if tag else 0
    if tag_key != expect:
        raise FptError(f'{where}: tag key {tag_key:#010x} does not match the '
                       f'computed {expect:#010x} for tag string {tag!r}')

    entries = []
    seen = {}
    cursor = 0
    for i in range(count):
        o = HEADER + i * ENTRY
        raw_name = bytes(blob[o:o + NAME]).rstrip(b'\0')
        key, data_off, length, trailing = struct.unpack_from('<4I', blob, o + NAME)
        if b'\0' in raw_name:
            raise FptError(f'{where}: entry {i} name has an embedded NUL')
        name = _ascii(raw_name, f'entry {i} name', where)
        if trailing != 0:
            raise FptError(f'{where}: entry {i} ({name!r}) reserved word at 0x1c is '
                           f'{trailing}, and every entry on the cartridge has 0 - '
                           f'refusing rather than rewriting it on rebuild')
        if key != name_key(name):
            raise FptError(f'{where}: entry {i} ({name!r}) key {key:#010x} does not '
                           f'match the computed {name_key(name):#010x}')
        if name in seen:
            raise FptError(f'{where}: entry {i} repeats the name {name!r} already '
                           f'used by entry {seen[name]}; the format identifies '
                           f'payloads by name and key, so a duplicate is ambiguous')
        seen[name] = i
        if data_off != cursor:
            raise FptError(f'{where}: entry {i} ({name!r}) offset {data_off} does not '
                           f'match the concatenation position {cursor}')
        start = tag_end + data_off
        if start + length > n:
            raise FptError(f'{where}: entry {i} ({name!r}) payload {start}+{length} '
                           f'runs past EOF {n}')
        entries.append(Entry(name, bytes(blob[start:start + length]), data_off))
        cursor += length

    if tag_end + cursor != n:
        raise FptError(f'{where}: entries account for {tag_end + cursor} bytes but the '
                       f'file is {n} - {n - tag_end - cursor} unclaimed')
    return {'tag': tag}, entries


def build(header, entries, where='<archive>'):
    """Serialise an FPT0 archive, emitting the constants and recomputing the rest."""
    if not isinstance(header, dict) or 'tag' not in header:
        raise FptError(f'{where}: the header must be an object carrying "tag", the '
                       f'one field this format varies; got '
                       f'{sorted(header) if isinstance(header, dict) else type(header).__name__}')
    out = bytearray(MAGIC)
    out += struct.pack('<3I', 0, len(entries), VERSION)
    off = 0
    seen = {}
    for i, e in enumerate(entries):
        raw = _to_ascii(e.name, f'entry {i} name', where)
        if len(raw) > NAME:
            raise FptError(f'{where}: entry {i} name {e.name!r} is {len(raw)} bytes, '
                           f'over the {NAME}-byte field')
        if not raw:
            raise FptError(f'{where}: entry {i} has an empty name')
        if e.name in seen:
            raise FptError(f'{where}: entry {i} repeats the name {e.name!r} already '
                           f'used by entry {seen[e.name]}')
        seen[e.name] = i
        out += raw.ljust(NAME, b'\0')
        out += struct.pack('<4I', name_key(e.name), off, len(e.data), 0)
        off += len(e.data)
    tag = _to_ascii(header['tag'], 'the tag string', where)
    if len(tag) > TAG_STR:
        raise FptError(f'{where}: tag string {header["tag"]!r} is {len(tag)} bytes, '
                       f'over the {TAG_STR}-byte field')
    out += tag.ljust(TAG_STR, b'\0')
    out += struct.pack('<2I', 0, name_key(header['tag']) if header['tag'] else 0)
    for i, e in enumerate(entries):
        if not isinstance(e.data, (bytes, bytearray, memoryview)):
            raise FptError(f'{where}: entry {i} ({e.name!r}) payload must be bytes, '
                           f'not {type(e.data).__name__}; this module moves payloads '
                           f'opaquely and never encodes text itself')
        out += e.data
    return bytes(out)

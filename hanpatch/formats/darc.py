"""DARC archive reader/writer (Dragon Quest VII, 3DS) — the font container.

Measured over the eight `darc` archives under /LAYOUT on this cartridge:

    offset  size  field
    0x00       4  magic 'darc'
    0x04       2  byte-order mark, 0xFEFF in all eight
    0x06       2  header size, 28 in all eight
    0x08       4  version, 0x01000000 in all eight
    0x0C       4  declared file size; equals the actual size in all eight
    0x10       4  entry table offset, 28 in all eight
    0x14       4  entry table length, covering the table AND the name block
    0x18       4  payload region offset

    entry (12 bytes), `count` of them, count taken from entry 0's third word:
    0x00       4  name offset into the name block, with 0x01 in the TOP byte for a
                  directory. The flag is a full byte, not a bit field.
    0x04       4  file: absolute payload offset. directory: parent entry INDEX.
    0x08       4  file: payload length. directory: one past its last child INDEX.

    name block, at table_offset + count*12: UTF-16-LE, NUL-terminated.

The tree is expressed by index ranges rather than sibling chains: a directory owns
entries (own_index+1 .. end_index-1). Entry 0 is the root, whose name is empty and
whose end index is therefore the entry count.

What this is for: the DQ7 text fonts are BCFNTs inside these archives, one per
pixel size - `tbud_maru_b8/b12/b13/b14/b15/b16` plus `iwamaru_p15` in layout.arc -
and each font is mirrored in both a bundled archive and its own per-size archive,
so a Korean font has to be written into every slot that holds it.

Content boundary: members are moved as OPAQUE blobs. This module never decodes
them, and the font work records glyph counts and metrics, never text.
"""
import struct

MAGIC = b'darc'
BOM = 0xFEFF
HEADER = 28
ENTRY = 12
VERSION = 0x01000000
DIR_FLAG = 0x01000000


def align4(x):
    return (x + 3) // 4 * 4


class DarcError(Exception):
    pass


class Member:
    """One archive member. `path` is '/'-joined and excludes the empty root."""

    __slots__ = ('path', 'is_dir', 'data', 'source_offset')

    def __init__(self, path, is_dir, data=b'', source_offset=None):
        self.path = path
        self.is_dir = is_dir
        self.data = data
        self.source_offset = source_offset

    @property
    def name(self):
        return self.path.rsplit('/', 1)[-1]

    def __repr__(self):
        kind = 'dir' if self.is_dir else f'{len(self.data)}B'
        return f'<{self.path or "/"} {kind}>'


def _name_at(blob, at, where):
    end = at
    while True:
        if end + 2 > len(blob):
            raise DarcError(f'{where}: a name starting at {at} runs past EOF')
        if blob[end:end + 2] == b'\0\0':
            break
        end += 2
    try:
        return blob[at:end].decode('utf-16-le')
    except UnicodeDecodeError as exc:
        raise DarcError(f'{where}: a name at {at} is not UTF-16: {exc}') from None


def parse(blob, where='<archive>'):
    """Parse a DARC archive into (header, [Member]).

    Fail closed on anything that could not be reproduced: a wrong magic or BOM, a
    header or version this reader has no evidence for, a declared size that
    disagrees with the actual one, a table or name block past EOF, a payload past
    EOF, an index range that is not properly nested, or a duplicate path.
    """
    n = len(blob)
    if n < HEADER:
        raise DarcError(f'{where}: {n} bytes is shorter than the {HEADER}-byte header')
    magic, bom, hdr_size, version, declared, tbl, tbl_len, data_off = \
        struct.unpack_from('<4sHHIIIII', blob, 0)
    if magic != MAGIC:
        raise DarcError(f'{where}: magic {magic!r} is not {MAGIC!r}')
    if bom != BOM:
        raise DarcError(f'{where}: byte-order mark {bom:#06x} is not {BOM:#06x}; this '
                        f'reader has no evidence for a big-endian DARC')
    if hdr_size != HEADER:
        raise DarcError(f'{where}: header size {hdr_size} is not the {HEADER} every '
                        f'archive on the cartridge declares')
    if version != VERSION:
        raise DarcError(f'{where}: version {version:#010x} is not the only version '
                        f'this reader has evidence for ({VERSION:#010x})')
    if declared != n:
        raise DarcError(f'{where}: the header declares {declared} bytes but the file '
                        f'is {n}')
    if tbl != HEADER:
        raise DarcError(f'{where}: the entry table starts at {tbl}, not directly after '
                        f'the {HEADER}-byte header')
    if tbl + ENTRY > n:
        raise DarcError(f'{where}: the entry table does not fit in {n} bytes')

    count = struct.unpack_from('<3I', blob, tbl)[2]
    names_at = tbl + count * ENTRY
    if names_at > n or tbl + tbl_len > n:
        raise DarcError(f'{where}: {count} entries need {names_at} bytes and the '
                        f'declared table length {tbl_len} needs {tbl + tbl_len}, '
                        f'but the file is {n}')

    raw = []
    for i in range(count):
        o = tbl + i * ENTRY
        name_off, second, third = struct.unpack_from('<3I', blob, o)
        is_dir = bool(name_off >> 24 == DIR_FLAG >> 24)
        if name_off >> 24 not in (0, DIR_FLAG >> 24):
            raise DarcError(f'{where}: entry {i} name field {name_off:#010x} has an '
                            f'unknown flag byte {name_off >> 24:#04x}')
        raw.append((is_dir, name_off & 0xFFFFFF, second, third))

    if not raw or not raw[0][0]:
        raise DarcError(f'{where}: entry 0 is not a directory, so there is no root')
    if raw[0][3] != count:
        raise DarcError(f'{where}: the root claims {raw[0][3]} entries but entry 0 '
                        f"says the table holds {count}")

    members = []
    seen = set()
    name_end = [names_at]

    def walk(idx, prefix):
        """Consume the index range a directory owns, returning the next index."""
        is_dir, name_off, second, third = raw[idx]
        name = _name_at(blob, names_at + name_off, f'{where} entry {idx}')
        nonlocal_end = names_at + name_off + len(name.encode('utf-16-le')) + 2
        if nonlocal_end > name_end[0]:
            name_end[0] = nonlocal_end
        path = f'{prefix}/{name}' if prefix else name
        if idx == 0:
            path = ''
        if path in seen:
            raise DarcError(f'{where}: entry {idx} repeats the path {path!r}')
        seen.add(path)
        if is_dir:
            if third > count or third <= idx:
                raise DarcError(f'{where}: directory {path or "/"} at entry {idx} '
                                f'claims children up to {third}, which is not a range '
                                f'inside {count} entries')
            members.append(Member(path, True))
            child = idx + 1
            while child < third:
                child = walk(child, path)
            return third
        if second + third > n:
            raise DarcError(f'{where}: {path} payload {second}+{third} runs past EOF {n}')
        members.append(Member(path, False, bytes(blob[second:second + third]), second))
        return idx + 1

    nxt = walk(0, '')
    name_bytes = name_end[0] - names_at
    if nxt != count:
        raise DarcError(f'{where}: the root range ended at {nxt}, not {count}; the '
                        f'entry table is not properly nested')
    # The payload region origin is the field that decides whether a rebuild lands
    # where the source put it, and it was returned unchecked - an archive with any
    # other origin would have rebuilt silently relocated while every member still
    # parsed. Measured on all ten archives: data_off == align(tbl + tbl_len, 4).
    want_data = align4(tbl + tbl_len)
    if data_off != want_data:
        raise DarcError(f'{where}: the payload region starts at {data_off}, but the '
                        f'{tbl_len}-byte table after a {tbl}-byte header ends at '
                        f'{want_data}; this reader cannot reproduce another origin')
    if tbl_len != count * ENTRY + name_bytes:
        raise DarcError(f'{where}: the declared table length {tbl_len} is not the '
                        f'{count * ENTRY}-byte entry table plus the {name_bytes}-byte '
                        f'name block')
    return {'data_offset': data_off, 'table_length': tbl_len}, members


def member_align(m, default=0x80):
    """Payload alignment for one member, CAPTURED from where the source put it.

    The cartridge does not use one alignment: measured across the eight archives,
    `.bcfnt` and `.bclim` payloads always start on a 0x80 boundary - GPU-facing
    data - while `.bclyt` and `.bclan` are packed at 4. Guessing a single value
    made three of ten archives rebuild smaller than their source. Deriving each
    member's requirement from its own recorded offset reproduces all ten and keeps
    working when a member changes size, because the REQUIREMENT does not move even
    though the offset does.
    """
    if m.source_offset is None:
        return default
    return 0x80 if m.source_offset % 0x80 == 0 else 4


def build(members, align=None, where='<archive>'):
    """Serialise members into a DARC archive.

    `members` must start with the root directory (path '') and be in the order the
    entry table should hold, which is what `parse` returns. `align` forces one
    alignment for every payload; when omitted each member keeps the alignment
    `member_align` derives from the source, which is what makes an untouched round
    trip byte-identical.
    """
    if not members or not members[0].is_dir or members[0].path != '':
        raise DarcError(f'{where}: the first member must be the root directory')

    index = {m.path: i for i, m in enumerate(members)}
    if len(index) != len(members):
        raise DarcError(f'{where}: duplicate paths in the member list')

    names = bytearray()
    name_off = {}
    for m in members:
        nm = '' if m.path == '' else m.name
        raw = nm.encode('utf-16-le') + b'\0\0'
        name_off[m.path] = len(names)
        names += raw
    table_len = len(members) * ENTRY + len(names)
    payload_at = HEADER + table_len

    # The payload region starts at align(header + table, 4) - measured on all ten
    # archives: 204, 208, 408, 1036 and 132. In every one of them the first member is
    # a 4-packed .bclyt, so the region origin and that member's own requirement
    # coincide; an archive whose first member needs 0x80 would have had its alignment
    # invariant broken by treating the first payload as a special case, so it is not
    # one - the region starts here and the first member is aligned like any other.
    payload_at = align4(payload_at)

    # Directory ranges: a directory owns every following entry whose path is under
    # it, which is exactly the contiguous block `parse` produced.
    end_index = {}
    for i, m in enumerate(members):
        if not m.is_dir:
            continue
        j = i + 1
        while j < len(members) and (m.path == '' or
                                    members[j].path.startswith(m.path + '/')):
            j += 1
        end_index[m.path] = j

    out = bytearray(HEADER + table_len)
    payload = bytearray()
    offsets = {}
    for m in members:
        if m.is_dir:
            continue
        a = align if align is not None else member_align(m)
        payload += b'\0' * ((-(payload_at + len(payload))) % a)
        offsets[m.path] = payload_at + len(payload)
        payload += m.data
    for i, m in enumerate(members):
        o = HEADER + i * ENTRY
        if m.is_dir:
            parent = 0 if m.path == '' else index[m.path.rsplit('/', 1)[0]
                                                 if '/' in m.path else '']
            struct.pack_into('<3I', out, o,
                             DIR_FLAG | name_off[m.path], parent, end_index[m.path])
        else:
            struct.pack_into('<3I', out, o,
                             name_off[m.path], offsets[m.path], len(m.data))
    out[HEADER + len(members) * ENTRY:HEADER + table_len] = names
    out += b'\0' * (payload_at - len(out))
    out += payload
    struct.pack_into('<4sHHIIIII', out, 0, MAGIC, BOM, HEADER, VERSION,
                     len(out), HEADER, table_len, payload_at)
    return bytes(out)


def replace(blob, path, data, where='<archive>'):
    """Return a new archive with one member's payload replaced.

    Sizes may differ: every offset and the declared file size are recomputed. This
    is the operation the font work needs, and it is deliberately the only mutation
    this module offers.
    """
    header, members = parse(blob, where)
    for m in members:
        if m.path == path.lstrip('/'):
            if m.is_dir:
                raise DarcError(f'{where}: {path} is a directory')
            m.data = data
            break
    else:
        raise DarcError(f'{where}: {path} is not in this archive '
                        f'({sum(1 for x in members if not x.is_dir)} members)')
    out = build(members, where=where)
    got = parse(out, where)[0]
    if got['table_length'] != header['table_length']:
        raise DarcError(f'{where}: the rebuild changed the table length '
                        f'{header["table_length"]} -> {got["table_length"]}, so the '
                        f'payload region moved for a reason other than the new size')
    return out

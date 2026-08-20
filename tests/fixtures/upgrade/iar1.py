"""IAR1: an index-archive container fixture. Test-only.

The second structural family, and the one whose failures look nothing like
FXR1's. Text lives in variable-length frames inside a fixed arena, so writing a
different length *moves* later frames and forces the index offsets, lengths and
CRCs to move with them. The characteristic defect here is not a clobbered
reserved byte but an index that no longer describes the data it points at.

Layout (all integers little-endian)::

    0x00  4  magic 'IAR1'
    0x04  2  version = 1
    0x06  2  entry_count
    0x08  4  index_offset = 0x20
    0x0C  4  entry_size   = 0x18
    0x10  4  data_offset  = 0x80
    0x14  4  data_end
    0x18  8  reserved, must stay zero
    ----------------------------- header ends at 0x20, protected
    0x20 + i*0x18:
      +0x00 4  stable id
      +0x04 4  frame offset, relative to data_offset
      +0x08 4  frame length
      +0x0C 2  text byte length
      +0x0E 2  flags
      +0x10 4  CRC-32 of the text bytes
      +0x14 4  reserved
    ----------------------------- index ends at 0x50
    0x50..0x80  reserved gap, protected
    0x80..0xC0  data arena, 0x40 bytes  <- relocation is confined here
    0xC0..0xD0  trailing sentinel, protected

Frame body::

    [u16 text_len][UTF-8 text][0x00 terminator][zero pad to 4-byte alignment]

The arena is fixed at 0x40 bytes so the file length never changes: relocation
stays a within-arena problem instead of becoming a length change, which is a
different failure with a different remedy.
"""

import zlib

MAGIC = b'IAR1'
VERSION = 1
HEADER_SIZE = 0x20
INDEX_OFFSET = 0x20
ENTRY_SIZE = 0x18
DATA_OFFSET = 0x80
ARENA_SIZE = 0x40
DATA_END = DATA_OFFSET + ARENA_SIZE
TAIL_SIZE = 16
TAIL_BYTE = 0x5A
TOTAL_SIZE = DATA_END + TAIL_SIZE
ALIGN = 4


class Iar1Error(ValueError):
    pass


def frame_size(text):
    """Bytes one frame occupies: length prefix, text, terminator, alignment."""
    body = 2 + len(text.encode('utf-8')) + 1
    return (body + ALIGN - 1) // ALIGN * ALIGN


def index_offset(index):
    return INDEX_OFFSET + index * ENTRY_SIZE


def protected_spans(entry_count):
    """Everything outside the index rows and the data arena."""
    index_end = INDEX_OFFSET + entry_count * ENTRY_SIZE
    return [
        (0, HEADER_SIZE, 'IAR1 header: magic, version, counts, offsets, reserved'),
        (index_end, DATA_OFFSET - index_end, 'reserved gap between index and data'),
        (DATA_END, TAIL_SIZE, 'trailing sentinel'),
    ]


def arena_span():
    """(offset, length) of the region relocation may rewrite."""
    return DATA_OFFSET, ARENA_SIZE


def index_span(entry_count):
    """(offset, length) covering every index row."""
    return INDEX_OFFSET, entry_count * ENTRY_SIZE


def build(records):
    """Serialise ``[(stable_id, text), ...]``.

    Frames are packed from the arena start in record order; leftover arena bytes
    are zeroed so an unwritten tail cannot carry stale data from a previous build.
    """
    count = len(records)
    if INDEX_OFFSET + count * ENTRY_SIZE > DATA_OFFSET:
        raise Iar1Error('%d entries do not fit before the data arena' % count)

    frames = bytearray(ARENA_SIZE)
    cursor = 0
    rows = []
    for stable_id, text in records:
        raw = text.encode('utf-8')
        size = frame_size(text)
        if cursor + size > ARENA_SIZE:
            raise Iar1Error(
                'record %d overflows the %d-byte arena' % (stable_id, ARENA_SIZE))
        frames[cursor:cursor + 2] = len(raw).to_bytes(2, 'little')
        frames[cursor + 2:cursor + 2 + len(raw)] = raw
        # terminator and alignment padding are already zero
        rows.append({
            'id': stable_id,
            'offset': cursor,
            'length': size,
            'text_len': len(raw),
            'crc': zlib.crc32(raw) & 0xFFFFFFFF,
        })
        cursor += size

    out = bytearray(TOTAL_SIZE)
    out[0:4] = MAGIC
    out[4:6] = VERSION.to_bytes(2, 'little')
    out[6:8] = count.to_bytes(2, 'little')
    out[8:12] = INDEX_OFFSET.to_bytes(4, 'little')
    out[12:16] = ENTRY_SIZE.to_bytes(4, 'little')
    out[16:20] = DATA_OFFSET.to_bytes(4, 'little')
    out[20:24] = DATA_END.to_bytes(4, 'little')
    # 0x18..0x1F reserved, already zero.

    for i, row in enumerate(rows):
        base = index_offset(i)
        out[base:base + 4] = row['id'].to_bytes(4, 'little')
        out[base + 4:base + 8] = row['offset'].to_bytes(4, 'little')
        out[base + 8:base + 12] = row['length'].to_bytes(4, 'little')
        out[base + 12:base + 14] = row['text_len'].to_bytes(2, 'little')
        out[base + 14:base + 16] = (0).to_bytes(2, 'little')
        out[base + 16:base + 20] = row['crc'].to_bytes(4, 'little')
        # +0x14 reserved, already zero.

    out[DATA_OFFSET:DATA_END] = frames
    out[DATA_END:] = bytes([TAIL_BYTE]) * TAIL_SIZE
    return bytes(out)


def parse(blob):
    """Read back, checking that the index actually describes the data.

    Every cross-field check here exists because relocation can break exactly one
    of them while the rest still look right.
    """
    if len(blob) != TOTAL_SIZE:
        raise Iar1Error('length %d is not %d' % (len(blob), TOTAL_SIZE))
    if blob[0:4] != MAGIC:
        raise Iar1Error('bad magic %r' % blob[0:4])
    if int.from_bytes(blob[4:6], 'little') != VERSION:
        raise Iar1Error('unsupported version')
    count = int.from_bytes(blob[6:8], 'little')
    if int.from_bytes(blob[8:12], 'little') != INDEX_OFFSET:
        raise Iar1Error('unexpected index offset')
    if int.from_bytes(blob[12:16], 'little') != ENTRY_SIZE:
        raise Iar1Error('unexpected entry size')
    if int.from_bytes(blob[16:20], 'little') != DATA_OFFSET:
        raise Iar1Error('unexpected data offset')
    if blob[DATA_END:] != bytes([TAIL_BYTE]) * TAIL_SIZE:
        raise Iar1Error('sentinel damaged')

    records = []
    for i in range(count):
        base = index_offset(i)
        stable_id = int.from_bytes(blob[base:base + 4], 'little')
        offset = int.from_bytes(blob[base + 4:base + 8], 'little')
        length = int.from_bytes(blob[base + 8:base + 12], 'little')
        text_len = int.from_bytes(blob[base + 12:base + 14], 'little')
        crc = int.from_bytes(blob[base + 16:base + 20], 'little')

        if offset + length > ARENA_SIZE:
            raise Iar1Error('entry %d frame runs past the arena' % i)
        frame = blob[DATA_OFFSET + offset:DATA_OFFSET + offset + length]
        declared = int.from_bytes(frame[0:2], 'little')
        if declared != text_len:
            raise Iar1Error(
                'entry %d: index says %d text bytes, frame says %d'
                % (i, text_len, declared))
        raw = frame[2:2 + text_len]
        if len(raw) != text_len:
            raise Iar1Error('entry %d frame is shorter than its text' % i)
        if frame[2 + text_len] != 0:
            raise Iar1Error('entry %d is not terminated' % i)
        if frame[3 + text_len:] != bytes(len(frame) - 3 - text_len):
            raise Iar1Error('entry %d has non-zero alignment padding' % i)
        actual_crc = zlib.crc32(raw) & 0xFFFFFFFF
        if actual_crc != crc:
            raise Iar1Error('entry %d CRC %08x does not match index %08x'
                            % (i, actual_crc, crc))
        records.append((stable_id, raw.decode('utf-8')))
    return records


def entries(blob):
    return {'iar1/%d' % stable_id: text for stable_id, text in parse(blob)}


def rebuild_with(blob, index, text):
    """Rewrite one entry's text, relocating everything after it.

    Relocation is why this family exists: the new text changes the frame size,
    which moves every later frame and forces their index rows to move with them.
    """
    records = parse(blob)
    if index >= len(records):
        raise Iar1Error('no entry %d' % index)
    stable_id, _ = records[index]
    records[index] = (stable_id, text)
    return build(records)

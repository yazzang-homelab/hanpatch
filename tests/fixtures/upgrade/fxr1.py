"""FXR1: a fixed-record container fixture. Test-only.

The first of two structural families. Records are fixed size, so a translation
is written in place and every byte outside a text slot must stay put. That makes
this the family where "wrote the right text, also clobbered a reserved byte" is
the natural failure, and where capacity is a hard ceiling rather than a
relocation problem.

Layout (all integers little-endian)::

    0x00  4  magic 'FXR1'
    0x04  2  version = 1
    0x06  2  record_count
    0x08  2  record_size = 0x20
    0x0A  2  header_size = 0x20
    0x0C  4  table_offset = 0x20
    0x10  4  boundary_end
    0x14 12  reserved, must stay zero
    ----------------------------- header ends at 0x20, protected
    0x20 + i*0x20:
      +0x00 4  stable id
      +0x04 1  flags
      +0x05 1  UTF-8 byte length (0..24)
      +0x06 2  reserved
      +0x08 24 NUL-padded UTF-8 text slot   <- the only writable span
    -----------------------------
    trailing 16 bytes: sentinel 0xA5, protected

These constants live here and nowhere else. Production code never learns them;
it only ever sees offsets, lengths and bytes.
"""

MAGIC = b'FXR1'
VERSION = 1
HEADER_SIZE = 0x20
RECORD_SIZE = 0x20
SLOT_OFFSET = 0x08
SLOT_SIZE = 24
TAIL_SIZE = 16
TAIL_BYTE = 0xA5


class Fxr1Error(ValueError):
    pass


def total_size(record_count):
    return HEADER_SIZE + record_count * RECORD_SIZE + TAIL_SIZE


def record_offset(index):
    return HEADER_SIZE + index * RECORD_SIZE


def slot_span(index):
    """(offset, length) of the writable text slot for one record."""
    return record_offset(index) + SLOT_OFFSET, SLOT_SIZE


def length_span(index):
    """(offset, length) of the declared-length byte for one record.

    Writing text changes how long that text is, so the length byte is part of
    the write surface rather than structure. Declaring it explicitly is the
    honest description: a plan that wrote it without saying so would be caught
    as an unregistered diff, and a plan that protected it could never write text
    at all.
    """
    return record_offset(index) + 5, 1


def writable_spans(index, owner_prefix='fxr1'):
    """Every span a text write for one record legitimately touches."""
    length_off, length_len = length_span(index)
    slot_off, slot_len = slot_span(index)
    return [
        (length_off, length_len, '%s/%d:length' % (owner_prefix, index)),
        (slot_off, slot_len, '%s/%d:text' % (owner_prefix, index)),
    ]


def protected_spans(record_count):
    """Spans no write may touch, each with the reason it is protected.

    The record id, flags and reserved bytes are structure: a writer that edits
    them is changing what the record *is* rather than what it says. The declared
    length byte sits between them and is deliberately excluded, because writing
    text must update it - see `length_span`.
    """
    spans = [(0, HEADER_SIZE, 'FXR1 header: magic, version, counts, reserved')]
    for i in range(record_count):
        base = record_offset(i)
        spans.append((base, 5, 'record %d id and flags' % i))
        spans.append((base + 6, 2, 'record %d reserved' % i))
    spans.append((total_size(record_count) - TAIL_SIZE, TAIL_SIZE,
                  'trailing sentinel'))
    return spans


def build(records):
    """Serialise ``[(stable_id, text), ...]`` into a container."""
    count = len(records)
    out = bytearray(total_size(count))
    out[0:4] = MAGIC
    out[4:6] = VERSION.to_bytes(2, 'little')
    out[6:8] = count.to_bytes(2, 'little')
    out[8:10] = RECORD_SIZE.to_bytes(2, 'little')
    out[10:12] = HEADER_SIZE.to_bytes(2, 'little')
    out[12:16] = HEADER_SIZE.to_bytes(4, 'little')
    out[16:20] = total_size(count).to_bytes(4, 'little')
    # 0x14..0x1F stay zero.

    for index, (stable_id, text) in enumerate(records):
        raw = text.encode('utf-8')
        if len(raw) > SLOT_SIZE:
            raise Fxr1Error('record %d needs %d bytes; the slot holds %d'
                            % (index, len(raw), SLOT_SIZE))
        base = record_offset(index)
        out[base:base + 4] = stable_id.to_bytes(4, 'little')
        out[base + 4] = 0
        out[base + 5] = len(raw)
        # +0x06..07 reserved, already zero.
        slot = base + SLOT_OFFSET
        out[slot:slot + SLOT_SIZE] = raw + bytes(SLOT_SIZE - len(raw))

    tail = total_size(count) - TAIL_SIZE
    out[tail:] = bytes([TAIL_BYTE]) * TAIL_SIZE
    return bytes(out)


def parse(blob):
    """Read a container back. Structural problems raise rather than guess."""
    if len(blob) < HEADER_SIZE + TAIL_SIZE:
        raise Fxr1Error('too short to hold a header and tail')
    if blob[0:4] != MAGIC:
        raise Fxr1Error('bad magic %r' % blob[0:4])
    version = int.from_bytes(blob[4:6], 'little')
    if version != VERSION:
        raise Fxr1Error('unsupported version %d' % version)
    count = int.from_bytes(blob[6:8], 'little')
    if int.from_bytes(blob[8:10], 'little') != RECORD_SIZE:
        raise Fxr1Error('unexpected record size')
    expected = total_size(count)
    if len(blob) != expected:
        raise Fxr1Error('length %d does not match %d records' % (len(blob), count))
    if blob[expected - TAIL_SIZE:] != bytes([TAIL_BYTE]) * TAIL_SIZE:
        raise Fxr1Error('sentinel damaged')

    records = []
    for index in range(count):
        base = record_offset(index)
        stable_id = int.from_bytes(blob[base:base + 4], 'little')
        declared = blob[base + 5]
        slot = base + SLOT_OFFSET
        raw = blob[slot:slot + SLOT_SIZE]
        if declared > SLOT_SIZE:
            raise Fxr1Error('record %d declares %d bytes' % (index, declared))
        # The declared length is authoritative. Trusting the NUL padding instead
        # would silently accept a slot whose declared length disagrees with its
        # content, which is exactly the corruption worth catching.
        text = raw[:declared].decode('utf-8')
        if raw[declared:] != bytes(SLOT_SIZE - declared):
            raise Fxr1Error('record %d has non-zero padding after its text' % index)
        records.append((stable_id, text))
    return records


def entries(blob):
    """``family/key -> text``, the shape the pipeline speaks."""
    return {'fxr1/%d' % stable_id: text for stable_id, text in parse(blob)}


def write_text(blob, index, text):
    """Write one slot, touching nothing else."""
    raw = text.encode('utf-8')
    if len(raw) > SLOT_SIZE:
        raise Fxr1Error('text needs %d bytes; the slot holds %d'
                        % (len(raw), SLOT_SIZE))
    out = bytearray(blob)
    base = record_offset(index)
    out[base + 5] = len(raw)
    slot = base + SLOT_OFFSET
    out[slot:slot + SLOT_SIZE] = raw + bytes(SLOT_SIZE - len(raw))
    return bytes(out)

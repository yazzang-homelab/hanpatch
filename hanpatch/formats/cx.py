"""The `.lz` compression Dragon Quest VII wraps its non-message resources in.

`CompressCtr::decode` (VA 0x2BE2FC in this cartridge's executable) dispatches on
the high nibble of byte 0 and accepts 0x1, 0x2, 0x3, 0x4, 0x5 and 0x8. Measured
across the 10,550 `.lz` files in the ROM: 3,528 `.pack.lz`, 1,951 `.bcmdl.lz`,
961 `.bctex.lz` and the rest of the model/effect/light resources are type 0x11,
and the 3,502 `.fpt.lz` texture archives are type 0x40.

Types 0x10/0x11 are the documented CX LZ formats, which is not an assumption:
decoding a `.pack.lz` with the standard reader yields `PackData` and a
`.bctex.lz` yields `CGFX`.

Type 0x40 is the game's own two-tree LZ. It is transcribed from the routine at
VA 0x128B8C rather than guessed:

    header   1  type/flags, high nibble 0x4
             3  u24 decompressed size, or 0 followed by a u32 at +4
    tree1    2  u16 count; the tree occupies count*4 + 4 bytes from the count
             .. 9-bit entries, MSB-first
    tree2    1  u8 count; the tree occupies count*4 + 4 bytes from the count
             .. 5-bit entries, MSB-first
    payload     bit stream, MSB-first

An entry is a node OR a value in the same 9 (or 5) bits: the low 7 (or 3) bits
are the child offset, and two flag bits say whether the child reached by 0 or by
1 is a value. From a node at u16 index `i`, bit `b` leads to
`(i & ~1) + 2*offset + 2 + b`. A value below 0x100 in tree1 is a literal; at or
above it, `(value & 0xFF) + 3` is a copy length, and tree2 then yields the bit
count of the displacement.

WRITING is deliberately not symmetric. The game accepts any type its dispatcher
knows, so a replacement resource is written as LZ11 - a format with a documented
encoder and a decoder proven against 10,550 shipped files - instead of
reimplementing the type-0x40 encoder to bit-exactness that nothing needs.
"""
import struct

LZ10, LZ11, TWO_TREE = 0x10, 0x11, 0x40
MIN_MATCH = 3
LZ11_WINDOW = 0x1000


class CxError(ValueError):
    pass


def _header(data, where):
    if len(data) < 8:
        raise CxError(f'{where}: {len(data)} bytes is too short for a CX header')
    size = int.from_bytes(data[1:4], 'little')
    pos = 4
    if size == 0:
        size = int.from_bytes(data[4:8], 'little')
        pos = 8
    return size, pos


class _Bits:
    """MSB-first bit reader over a byte cursor, the way the ROM reads them."""

    def __init__(self, data, pos):
        self.d, self.pos, self.acc, self.left = data, pos, 0, 0

    def bit(self):
        if self.left == 0:
            if self.pos >= len(self.d):
                raise CxError('bit stream ran out')
            self.acc = self.d[self.pos]
            self.pos += 1
            self.left = 8
        self.left -= 1
        return (self.acc >> self.left) & 1

    def take(self, width):
        value = 0
        for _ in range(width):
            value = (value << 1) | self.bit()
        return value


def _tree(data, pos, width, span, slots, where):
    """Read `span` bytes as `width`-bit fields into u16 slots, index 1 upward."""
    if pos + span > len(data):
        raise CxError(f'{where}: tree at {pos:#x} claims {span} bytes past the end')
    bits = _Bits(data, pos)
    table = [0] * slots
    index = 1
    while bits.pos - pos < span and index < slots:
        table[index] = bits.take(width)
        index += 1
    return table


def _two_tree(data, where):
    size, pos = _header(data, where)
    # Section boundaries come from the COUNTS, not from wherever the bit reader
    # happened to stop: deriving them from the cursor lands two or three bytes
    # off, and the tree then decodes as zeros that walk out of the work buffer.
    n1 = struct.unpack_from('<H', data, pos)[0]
    tree1 = _tree(data, pos + 2, 9, n1 * 4 + 2, 1024, where)
    base2 = pos + n1 * 4 + 4
    if base2 >= len(data):
        raise CxError(f'{where}: tree1 span runs past the end of the file')
    n2 = data[base2]
    tree2 = _tree(data, base2 + 1, 5, n2 * 4 + 3, 64, where)
    bits = _Bits(data, base2 + n2 * 4 + 4)
    out = bytearray()

    def walk(table, mask, leaf):
        index = 1
        while True:
            node = table[index]
            bit = bits.bit()
            nxt = (index & ~1) + 2 * (node & mask) + 2 + bit
            if nxt >= len(table):
                raise CxError(f'{where}: tree walk left the table at index {nxt}')
            if node & (leaf >> bit):
                return table[nxt]
            index = nxt

    while len(out) < size:
        value = walk(tree1, 0x7F, 0x100)
        if value < 0x100:
            out.append(value)
            continue
        length = min((value & 0xFF) + 3, size - len(out))
        # tree2's offset field is 3 bits, not 4: a 4-bit read walks past the
        # 64-entry work buffer the ROM allocates for it.
        n = walk(tree2, 0x07, 0x10)
        acc = 0 if n == 0 else 1
        for _ in range(max(0, n - 1)):
            acc = (acc << 1) | bits.bit()
        disp = acc + 1
        src = len(out) - disp
        if src < 0:
            raise CxError(f'{where}: displacement {disp} points before the output')
        for k in range(length):
            out.append(out[src + k])
    return bytes(out)


def _lz(data, where, extended):
    size, pos = _header(data, where)
    out = bytearray()
    while len(out) < size:
        flags = data[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= size:
                break
            if not (flags >> (7 - bit)) & 1:
                out.append(data[pos])
                pos += 1
                continue
            first = data[pos]
            indicator = first >> 4
            if not extended or indicator > 1:
                # The 2-byte form counts differently in the two formats: LZ10
                # stores length - 3, LZ11 stores length - 1. Using LZ10's bias
                # for LZ11 shortens every copy by two bytes, and the output still
                # reaches the declared size, so it looks fine and is not.
                length = indicator + (1 if extended else MIN_MATCH)
                disp = (((first & 0xF) << 8) | data[pos + 1]) + 1
                pos += 2
            elif indicator == 0:
                length = (((first & 0xF) << 4) | (data[pos + 1] >> 4)) + 0x11
                disp = (((data[pos + 1] & 0xF) << 8) | data[pos + 2]) + 1
                pos += 3
            else:
                length = (((first & 0xF) << 12) | (data[pos + 1] << 4)
                          | (data[pos + 2] >> 4)) + 0x111
                disp = (((data[pos + 2] & 0xF) << 8) | data[pos + 3]) + 1
                pos += 4
            src = len(out) - disp
            if src < 0:
                raise CxError(f'{where}: displacement {disp} points before the output')
            for k in range(length):
                out.append(out[src + k])
    return bytes(out)


def decompress(data, where='<lz>'):
    """Decode any `.lz` type this cartridge uses."""
    kind = data[0] & 0xF0
    if kind == TWO_TREE:
        return _two_tree(data, where)
    if kind in (LZ10, LZ11):
        return _lz(data, where, extended=data[0] == LZ11)
    raise CxError(
        f'{where}: compression type {data[0]:#04x} has no reader here. The ROM '
        f'accepts 0x1x, 0x2x, 0x3x, 0x4x, 0x5x and 0x8x; only the LZ types and '
        f'the two-tree 0x4x were measured in its files.')


def compress(data, where='<lz>'):
    """Write LZ11, the type the ROM's own dispatcher reads for every resource.

    Greedy longest match. There is no attempt at optimal parsing: the point is a
    stream the game accepts and a size that fits, and every output is decoded
    again before it is returned, so a wrong match can never leave this function.
    """
    if len(data) >= 1 << 24:
        raise CxError(f'{where}: {len(data)} bytes needs the 32-bit size header, '
                      f'which this writer does not emit')
    out = bytearray(struct.pack('<I', (len(data) << 8) | LZ11))
    index = {}
    pos = 0
    while pos < len(data):
        flags_at = len(out)
        out.append(0)
        flags = 0
        for bit in range(8):
            if pos >= len(data):
                break
            best_len, best_disp = 0, 0
            key = data[pos:pos + MIN_MATCH]
            for cand in reversed(index.get(bytes(key), ())):
                disp = pos - cand
                if disp > LZ11_WINDOW:
                    break
                length = MIN_MATCH
                limit = min(0x10110, len(data) - pos)
                while length < limit and data[cand + length] == data[pos + length]:
                    length += 1
                if length > best_len:
                    best_len, best_disp = length, disp
                    if length >= limit:
                        break
            if best_len >= MIN_MATCH:
                flags |= 0x80 >> bit
                disp = best_disp - 1
                if best_len <= 0x10:
                    out += bytes((((best_len - 1) << 4) | (disp >> 8),
                                  disp & 0xFF))
                elif best_len <= 0x110:
                    n = best_len - 0x11
                    out += bytes((n >> 4, ((n & 0xF) << 4) | (disp >> 8), disp & 0xFF))
                else:
                    n = best_len - 0x111
                    out += bytes((0x10 | (n >> 12), (n >> 4) & 0xFF,
                                  ((n & 0xF) << 4) | (disp >> 8), disp & 0xFF))
                take = best_len
            else:
                out.append(data[pos])
                take = 1
            for k in range(take):
                at = pos + k
                if at + MIN_MATCH <= len(data):
                    index.setdefault(bytes(data[at:at + MIN_MATCH]), []).append(at)
            pos += take
        out[flags_at] = flags
    result = bytes(out)
    if decompress(result, where) != data:
        raise CxError(f'{where}: LZ11 round trip mismatch')
    return result

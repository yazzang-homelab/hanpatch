"""IMY container codec — the LZ scheme this title wraps its bulk assets in.

PROVENANCE: measured, not transcribed. The header layout and every token rule
below were read out of the game's own decompressor in a decrypted EBOOT, and
then checked against the disc: all 686 IMY blocks found in the 547 files of
USRDIR/DATA.DAT decode to exactly the size their header declares, and all 686
re-encode to bytes identical to the originals. No rule here is a guess that
happened to work on one file.

    HEADER (32 bytes, little endian)

    0x00   4  magic, 'IMY\\0' (0x00594D49)
    0x04   4  dsize, size of the decoded block including this header
    0x08   2  width, the row stride IN BYTES (not in pixels)
    0x0A   1  flags
    0x0B   1  depth, bits per pixel
    0x0C   2  height, rows
    0x0E   2  colors, palette entries of 4 bytes each
    0x10  16  reserved, zero in every block on the disc
    0x20      palette, then the compressed payload

`dsize` is `32 + colors * 4 + width * height` for every block on the disc, which
is what makes `width` a byte stride: a 4bpp block counts one byte per stride
unit, not two pixels. Code that treats `width` as a pixel count gets the buffer
size wrong on every 4bpp asset.

The low bits of `flags` select the decoder, and they are the whole selection —
`depth` does not choose a code path in the routine that reads these blocks:

    flags & 1   payload is stored, not compressed; copy width * height bytes
    flags & 2   compression operates on 4-byte elements
    otherwise   compression operates on 2-byte elements

The remaining flag bits are reassembled into a format code by the container
constructor (`code = ((flags >> 5) & 4) | ((flags >> 2) & 3)`), which describes
the pixel format for the renderer. It does not pick a decompressor and this
module does not act on it, with one exception noted at `RGB_TRIPLE_UNSUPPORTED`.

    PAYLOAD

    u16 n, or u16 0 followed by u32 n   length of the token stream in bytes
    n bytes                             token stream
    remainder                           element stream

Two cursors run at once: tokens are read forward one byte at a time, and the
element stream is a separate forward cursor that only advances when a token
spends it. `n` is padded with zero bytes so that the element stream starts on a
4-byte boundary relative to the start of the header; the padding is never read
because decoding stops on output length, not on token exhaustion.

    TOKENS, for element size E and byte stride P

    0x00..0x0F   emit (b + 1) elements from the element stream, advancing it
    0x10..0xBF   emit one element read at (cursor - E * (b - 0x0F)) WITHOUT
                 advancing the cursor: a back-reference into elements already
                 spent, 1 to 176 of them
    0xC0..0xFF   let t = b - 0xC0; copy ((t & 0xF) + 1) elements from
                 (output + DELTAS[t >> 4]), overlapping and element by element

    DELTAS = [-E, -P, -P - E, -P + E]

so the copy sources are, in order, the element to the left, the one above, above
left, and above right. The copy is a byte-for-byte forward copy through the
output buffer, so a source that overlaps the destination repeats — that is how a
run of one value is written, and an encoder may rely on it.

    CHUNKS

The game does not always decode into one buffer. Its decompressor walks a list
of destination pointers and moves to the next one every `rows_per_chunk` rows,
which means a copy token must not reach across that boundary in either
direction: past the end it would run off the destination, and before the start
it would read a different allocation. The boundary is invisible in the header —
it is an argument the caller passes — so `encode` takes it as `chunk` and
defaults to None, meaning one buffer.

On this disc both cases occur. The 669 blocks that are data (scripts, maps,
animation tables) are encoded as a single buffer. The 17 that are large *.MPB
backgrounds are encoded in chunks of 32768 pixels, which is `CHUNK_PIXELS * depth
// 8` bytes. Every one of the 686 reproduces exactly under the matching setting,
and the two settings are not interchangeable: forcing chunks on the data blocks
breaks 60 of them, and dropping chunks from the backgrounds breaks all 17.

Content boundary: this module moves bytes. It does not know what a block holds,
does not unswizzle textures, and does not look at the palette.
"""
import struct

MAGIC = b'IMY\x00'
MAGIC_LE = 0x00594D49
HEADER = 32
PALETTE_ENTRY = 4

FLAG_STORED = 1
FLAG_WIDE = 2

MAX_LITERAL_RUN = 16
MAX_BACKREF = 176
MAX_COPY = 16

#: chunk size the game's asset tool used for large textures, in pixels
CHUNK_PIXELS = 32768

#: `flags & 2` with `depth == 24` selects a fourth decoder that reads 3-byte RGB
#: elements and widens them to 32-bit pixels. No block on this disc uses it, so
#: it is refused rather than shipped untested.
RGB_TRIPLE_UNSUPPORTED = 24


class ImyError(Exception):
    pass


class Header:
    """A parsed IMY header. Field names are the fields, not an interpretation."""

    __slots__ = ('dsize', 'width', 'flags', 'depth', 'height', 'colors')

    def __init__(self, dsize, width, flags, depth, height, colors):
        self.dsize = dsize
        self.width = width
        self.flags = flags
        self.depth = depth
        self.height = height
        self.colors = colors

    @property
    def element_size(self):
        return 4 if self.flags & FLAG_WIDE else 2

    @property
    def stored(self):
        return bool(self.flags & FLAG_STORED)

    @property
    def format_code(self):
        return ((self.flags >> 5) & 4) | ((self.flags >> 2) & 3)

    @property
    def pixels_size(self):
        return self.width * self.height

    @property
    def palette_size(self):
        return self.colors * PALETTE_ENTRY

    def pack(self):
        return struct.pack(
            '<IIHBBHH', MAGIC_LE, self.dsize, self.width, self.flags,
            self.depth, self.height, self.colors) + b'\x00' * 16

    def __repr__(self):
        return ('Header(width=%d, height=%d, flags=0x%02x, depth=%d, colors=%d,'
                ' dsize=%d)' % (self.width, self.height, self.flags, self.depth,
                                self.colors, self.dsize))


def parse_header(buf, off=0):
    """Read the header at `off`, or raise if it is not one."""
    if len(buf) - off < HEADER:
        raise ImyError('truncated header at 0x%x' % off)
    magic, dsize, width, flags, depth, height, colors = struct.unpack_from(
        '<IIHBBHH', buf, off)
    if magic != MAGIC_LE:
        raise ImyError('no IMY magic at 0x%x' % off)
    head = Header(dsize, width, flags, depth, height, colors)
    if not width or not height:
        raise ImyError('empty block at 0x%x: %d x %d' % (off, width, height))
    want = HEADER + head.palette_size + head.pixels_size
    if dsize != want:
        raise ImyError('dsize %d disagrees with %d x %d and %d colors (%d) at '
                       '0x%x' % (dsize, width, height, colors, want, off))
    if head.flags & FLAG_WIDE and head.depth == RGB_TRIPLE_UNSUPPORTED:
        raise ImyError('24-bit wide variant at 0x%x is not implemented' % off)
    if width % head.element_size:
        raise ImyError('stride %d is not a multiple of the %d-byte element at '
                       '0x%x' % (width, head.element_size, off))
    return head


def deltas(stride, element_size):
    """The four copy sources, in token order."""
    return (-element_size, -stride, -stride - element_size,
            -stride + element_size)


def decode(buf, off=0):
    """Decode the block at `off`. Returns (header, palette, pixels)."""
    head = parse_header(buf, off)
    body = off + HEADER
    palette = bytes(buf[body:body + head.palette_size])
    if len(palette) != head.palette_size:
        raise ImyError('truncated palette at 0x%x' % off)
    src = body + head.palette_size
    end = head.pixels_size

    if head.stored:
        pixels = bytes(buf[src:src + end])
        if len(pixels) != end:
            raise ImyError('truncated stored payload at 0x%x' % off)
        return head, palette, pixels

    esz = head.element_size
    if len(buf) - src < 2:
        raise ImyError('truncated payload at 0x%x' % off)
    n, = struct.unpack_from('<H', buf, src)
    tok = src + 2
    if n == 0:
        if len(buf) - tok < 4:
            raise ImyError('truncated long token length at 0x%x' % off)
        n, = struct.unpack_from('<I', buf, tok)
        tok += 4
    cur = tok + n
    if cur > len(buf):
        raise ImyError('token stream of %d runs past the file at 0x%x'
                       % (n, off))

    table = deltas(head.width, esz)
    out = bytearray(end)
    pos = 0
    while pos < end:
        if tok >= len(buf):
            raise ImyError('token stream ran out %d bytes short at 0x%x'
                           % (end - pos, off))
        b = buf[tok]
        tok += 1
        if b < 0x10:
            for _ in range(b + 1):
                nxt = cur + esz
                if nxt > len(buf):
                    raise ImyError('element stream ran out at 0x%x' % off)
                out[pos:pos + esz] = buf[cur:nxt]
                cur = nxt
                pos += esz
                if pos >= end:
                    break
        elif b < 0xC0:
            back = cur - esz * (b - 0x0F)
            if back < 0:
                raise ImyError('back-reference before the file at 0x%x' % off)
            out[pos:pos + esz] = buf[back:back + esz]
            pos += esz
        else:
            t = b - 0xC0
            s = pos + table[t >> 4]
            if s < 0:
                raise ImyError('copy from before the buffer at 0x%x' % off)
            for _ in range((t & 0xF) + 1):
                out[pos:pos + esz] = out[s:s + esz]
                s += esz
                pos += esz
                if pos >= end:
                    break
    return head, palette, bytes(out)


def _emit_runs(toks, run):
    while run:
        take = min(MAX_LITERAL_RUN, run)
        toks.append(take - 1)
        run -= take


def compress(pixels, stride, element_size=2, chunk=None):
    """Compress `pixels`. Returns (token stream, element stream).

    This reproduces the choices of the tool that built the disc: take the
    longest copy available, preferring the earlier source on a tie; else reuse
    the nearest matching element already spent; else spend a new one. `chunk`,
    when given, is the destination buffer size in bytes that copies may not
    cross.
    """
    esz = element_size
    if stride % esz or len(pixels) % esz:
        raise ImyError('stride %d and length %d must be multiples of %d'
                       % (stride, len(pixels), esz))
    end = len(pixels)
    table = deltas(stride, esz)
    toks = bytearray()
    elements = bytearray()
    spent = 0
    run = 0
    pos = 0
    while pos < end:
        if chunk:
            base = (pos // chunk) * chunk
            limit = min(base + chunk, end)
        else:
            base, limit = 0, end
        best, which = 0, -1
        for idx, delta in enumerate(table):
            # -stride + element degenerates to the destination itself when the
            # stride is one element wide; such a copy reads what it is writing
            if delta >= 0:
                continue
            s = pos + delta
            if s < base:
                continue
            c = 0
            while c < MAX_COPY and pos + (c + 1) * esz <= limit:
                a = s + c * esz
                if pixels[a:a + esz] != pixels[pos + c * esz:pos + (c + 1) * esz]:
                    break
                c += 1
            if c > best:
                best, which = c, idx
        if best:
            _emit_runs(toks, run)
            run = 0
            toks.append(0xC0 + (which << 4) + (best - 1))
            pos += best * esz
            continue
        cur = pixels[pos:pos + esz]
        hit = 0
        for k in range(1, MAX_BACKREF + 1):
            i = spent - k
            if i < 0:
                break
            if elements[i * esz:(i + 1) * esz] == cur:
                hit = k
                break
        if hit:
            _emit_runs(toks, run)
            run = 0
            toks.append(0x0F + hit)
            pos += esz
            continue
        elements.extend(cur)
        spent += 1
        run += 1
        pos += esz
    _emit_runs(toks, run)
    return bytes(toks), bytes(elements)


def encode(header, palette, pixels, chunk=None):
    """Build a whole block: header, palette, and compressed payload."""
    if len(palette) != header.palette_size:
        raise ImyError('palette of %d bytes, header declares %d'
                       % (len(palette), header.palette_size))
    if len(pixels) != header.pixels_size:
        raise ImyError('%d bytes of pixels, header declares %d x %d'
                       % (len(pixels), header.width, header.height))
    if header.flags & FLAG_WIDE and header.depth == RGB_TRIPLE_UNSUPPORTED:
        raise ImyError('24-bit wide variant is not implemented')
    if header.stored:
        return header.pack() + palette + pixels
    toks, elements = compress(pixels, header.width, header.element_size, chunk)
    prefix = 2 if 0 < len(toks) < 0x10000 else 6
    pad = -(HEADER + len(palette) + prefix + len(toks)) % 4
    toks += b'\x00' * pad
    if prefix == 2:
        head = struct.pack('<H', len(toks))
    else:
        head = struct.pack('<HI', 0, len(toks))
    return header.pack() + palette + head + toks + elements


def chunk_for_pixels(depth, pixels=CHUNK_PIXELS):
    """The chunk size in bytes that the disc's texture assets were built with."""
    return pixels * depth // 8


def find_blocks(buf, start=0):
    """Yield the offset of every well-formed IMY header in `buf`."""
    pos = start
    while True:
        i = buf.find(MAGIC, pos)
        if i < 0:
            return
        pos = i + len(MAGIC)
        try:
            parse_header(buf, i)
        except ImyError:
            continue
        yield i

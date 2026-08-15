"""ETC1 / ETC1A4 textures as Dragon Quest VII's CGFX resources store them.

A CGFX `TXOB` records a pixel format and a payload length. When the payload is
`width * height / 2` bytes the texture is ETC1, and when it is `width * height`
it is ETC1A4 - 4bpp colour with a 4bpp alpha plane. Those are the two formats
this cartridge's models use, and the title logo is one of them, so a localisation
that needs to touch title artwork needs this codec.

Every layout choice below was measured against ground truth rather than taken
from prose: the same texture was dumped from the GPU by an emulator, and the
parameters were fixed by requiring the decode of the ROM payload to reproduce
that dump. On the title atlas (`suuji`, 256x256 ETC1A4) all 4096 colour blocks
match exactly.

The layout:

  * The surface is a grid of 8x8 tiles in raster order.
  * Each tile holds four 4x4 blocks in the order (0,0), (4,0), (0,4), (4,4).
  * An ETC1A4 block is 8 bytes of alpha followed by 8 bytes of ETC1 colour; an
    ETC1 block is the 8 colour bytes alone. The alpha-first order was pinned by
    dumping the same payload through Azahar's renderer and requiring an exact
    RGBA match on all 65536 pixels of the title atlas: with colour read first
    the alpha plane lands one subtile late, which is precisely the artefact a
    mis-ordered write produces in-game (opaque boxes shifted 4px from their
    glyphs). Azahar's texture_decode.cpp agrees: it memcpys the alpha qword
    before the colour qword.
  * The colour qword is stored byte-reversed with respect to the ETC1 spec's
    big-endian block, so the spec's first word is the file's last four bytes.
  * Inside a block, pixel `i` is at `x = i >> 2`, `y = i & 3`.
  * Alpha is one nibble per pixel in that same pixel order, low nibble first,
    expanded to 8 bits by multiplying by 17.

`encode` is deliberately block-local: it re-encodes only the blocks whose pixels
changed and copies every other block's bytes verbatim. ETC1 is lossy, so a
wholesale re-encode of an untouched atlas would degrade artwork the change never
meant to touch.
"""

TILE = 8
BLOCK = 4
BLOCK_ORDER = ((0, 0), (4, 0), (0, 4), (4, 4))

# Per-table modifiers, indexed directly by the two-bit pixel index.
MODIFIERS = tuple((a, b, -a, -b) for a, b in (
    (2, 8), (5, 17), (9, 29), (13, 42),
    (18, 60), (24, 80), (33, 106), (47, 183)))


class TextureError(ValueError):
    pass


def _clamp(v):
    return 0 if v < 0 else (255 if v > 255 else v)


def _ext5(v):
    return (v << 3) | (v >> 2)


def _signed3(v):
    return v - 8 if v >= 4 else v


def bits_per_pixel(payload_len, width, height):
    """4 for ETC1, 8 for ETC1A4, refusing any other density."""
    px = width * height
    if payload_len * 2 == px:
        return 4
    if payload_len == px:
        return 8
    raise TextureError(
        f'{payload_len} bytes is neither ETC1 ({px // 2}) nor ETC1A4 ({px}) for '
        f'{width}x{height}')


def decode_block(colour):
    """One 8-byte ETC1 block as a 4x4 grid of (r, g, b)."""
    spec = colour[::-1]
    head = int.from_bytes(spec[0:4], 'big')
    index = int.from_bytes(spec[4:8], 'big')
    flip = bool(head & 0x1)
    diff = bool(head & 0x2)
    t1 = MODIFIERS[(head >> 5) & 0x7]
    t2 = MODIFIERS[(head >> 2) & 0x7]
    if diff:
        r1 = (head >> 27) & 0x1f
        g1 = (head >> 19) & 0x1f
        b1 = (head >> 11) & 0x1f
        c1 = (_ext5(r1), _ext5(g1), _ext5(b1))
        c2 = (_ext5((r1 + _signed3((head >> 24) & 0x7)) & 0x1f),
              _ext5((g1 + _signed3((head >> 16) & 0x7)) & 0x1f),
              _ext5((b1 + _signed3((head >> 8) & 0x7)) & 0x1f))
    else:
        c1 = (((head >> 28) & 0xf) * 17, ((head >> 20) & 0xf) * 17,
              ((head >> 12) & 0xf) * 17)
        c2 = (((head >> 24) & 0xf) * 17, ((head >> 16) & 0xf) * 17,
              ((head >> 8) & 0xf) * 17)
    out = [[None] * BLOCK for _ in range(BLOCK)]
    for i in range(16):
        x = i >> 2
        y = i & 3
        idx = (((index >> (i + 16)) & 1) << 1) | ((index >> i) & 1)
        sub = (y >= 2) if flip else (x >= 2)
        base = c2 if sub else c1
        mod = (t2 if sub else t1)[idx]
        out[y][x] = (_clamp(base[0] + mod), _clamp(base[1] + mod),
                     _clamp(base[2] + mod))
    return out


def _blocks(width, height, bpp):
    """(byte offset, x, y) for every 4x4 block, in payload order."""
    step = 16 if bpp == 8 else 8
    pos = 0
    for ty in range(0, height, TILE):
        for tx in range(0, width, TILE):
            for bx, by in BLOCK_ORDER:
                yield pos, tx + bx, ty + by
                pos += step


def decode(payload, width, height):
    """Return a PIL RGBA image for an ETC1 or ETC1A4 payload."""
    from PIL import Image
    bpp = bits_per_pixel(len(payload), width, height)
    if width % TILE or height % TILE:
        raise TextureError(f'{width}x{height} is not a whole number of 8x8 tiles')
    img = Image.new('RGBA', (width, height))
    px = img.load()
    for pos, bx, by in _blocks(width, height, bpp):
        c_off = 8 if bpp == 8 else 0
        colour = decode_block(payload[pos + c_off:pos + c_off + 8])
        alpha = payload[pos:pos + 8] if bpp == 8 else None
        for i in range(16):
            x = i >> 2
            y = i & 3
            if alpha is None:
                a = 255
            else:
                nib = alpha[i >> 1]
                a = ((nib & 0xf) if (i & 1) == 0 else (nib >> 4)) * 17
            r, g, b = colour[y][x]
            px[bx + x, by + y] = (r, g, b, a)
    return img


def _encode_block(pixels):
    """Encode a 4x4 grid of (r, g, b) into 8 ETC1 bytes.

    A single-subblock differential encoding with a per-subblock average base is
    used: it is the honest minimum for replacing flat artwork and text, and every
    block it writes decodes back through `decode_block`. It does not search
    flip/table space for the best fit, so it is not a general-purpose compressor -
    which is why `encode` only ever calls it for blocks that actually changed.
    """
    best = None
    for flip in (False, True):
        halves = ([[(x, y) for y in range(4) for x in range(2)],
                   [(x, y) for y in range(4) for x in range(2, 4)]] if not flip
                  else [[(x, y) for y in range(2) for x in range(4)],
                        [(x, y) for y in range(2, 4) for x in range(4)]])
        means = []
        for half in halves:
            n = len(half)
            means.append(tuple(sum(pixels[y][x][c] for x, y in half) // n
                               for c in range(3)))
        for t1 in range(8):
            for t2 in range(8):
                head = 0
                # individual mode: two independent 4-bit base colours
                base = []
                for m in means:
                    base.append(tuple(min(15, max(0, (c + 8) // 17)) for c in m))
                head |= base[0][0] << 28 | base[0][1] << 20 | base[0][2] << 12
                head |= base[1][0] << 24 | base[1][1] << 16 | base[1][2] << 8
                head |= (t1 << 5) | (t2 << 2) | (1 if flip else 0)
                index = 0
                err = 0
                for hi, half in enumerate(halves):
                    table = MODIFIERS[t1 if hi == 0 else t2]
                    c = tuple(v * 17 for v in base[hi])
                    for x, y in half:
                        want = pixels[y][x]
                        bi = None
                        bd = None
                        for k in range(4):
                            cand = (_clamp(c[0] + table[k]), _clamp(c[1] + table[k]),
                                    _clamp(c[2] + table[k]))
                            dd = sum((cand[j] - want[j]) ** 2 for j in range(3))
                            if bd is None or dd < bd:
                                bd, bi = dd, k
                        err += bd
                        i = x * 4 + y
                        index |= (bi & 1) << i
                        index |= ((bi >> 1) & 1) << (i + 16)
                if best is None or err < best[0]:
                    spec = head.to_bytes(4, 'big') + index.to_bytes(4, 'big')
                    best = (err, spec[::-1])
    return best[1]


def encode(image, original, width, height):
    """Return a payload for `image`, reusing `original` for unchanged blocks."""
    bpp = bits_per_pixel(len(original), width, height)
    if image.size != (width, height):
        raise TextureError(
            f'replacement is {image.size[0]}x{image.size[1]}, but this texture is '
            f'{width}x{height}; the layout that references it is in another file')
    rgba = image.convert('RGBA')
    px = rgba.load()
    out = bytearray(original)
    for pos, bx, by in _blocks(width, height, bpp):
        c_off = 8 if bpp == 8 else 0
        colour = decode_block(original[pos + c_off:pos + c_off + 8])
        want = [[None] * BLOCK for _ in range(BLOCK)]
        same = True
        for i in range(16):
            x = i >> 2
            y = i & 3
            r, g, b, a = px[bx + x, by + y]
            want[y][x] = (r, g, b)
            if colour[y][x] != (r, g, b):
                same = False
            if bpp == 8:
                nib = original[pos + (i >> 1)]
                have = ((nib & 0xf) if (i & 1) == 0 else (nib >> 4)) * 17
                if have != a:
                    same = False
        if same:
            continue
        out[pos + c_off:pos + c_off + 8] = _encode_block(want)
        if bpp == 8:
            alpha = bytearray(8)
            for i in range(16):
                x = i >> 2
                y = i & 3
                v = min(15, (px[bx + x, by + y][3] + 8) // 17)
                if (i & 1) == 0:
                    alpha[i >> 1] = (alpha[i >> 1] & 0xf0) | v
                else:
                    alpha[i >> 1] = (alpha[i >> 1] & 0x0f) | (v << 4)
            out[pos:pos + 8] = alpha
    return bytes(out)

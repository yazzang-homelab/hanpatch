"""MPB - the tiled image the title screen and shop backdrops are drawn from.

A `.MPB` member is a 64-byte header followed by one IMY block. The IMY block is
not the picture: it is a TILE ATLAS, a square texture holding the tiles side by
side, and the header carries the map that says where each tile lands. The
picture is assembled by walking that map.

    header
      0x00  'MAP\\0'
      0x04  u32  offset of the IMY block (64 on every member of this disc)
      0x0d  u8   bit depth of the atlas
      0x0e  u16  tiles in use
      0x10  u16  grid columns          0x12  u16  grid rows
      0x14  u16  tile width            0x16  u16  tile height
      0x18  u16  image width           0x1a  u16  image height
      0x20  u16[columns * rows]  the map: 0 is an empty cell, n draws tile n-1

The image size is SMALLER than the grid it is cut from - the title logo is
390x119 out of a 7x2 grid of 64px tiles, so the last column and the bottom row
are partly discarded. Those bounds live in the header, which is why a
replacement image has to keep the original's exact size: the engine reads the
size from here and would clip or stretch anything else.

The atlas pixels are PSP-SWIZZLED. A texture is stored as 16-byte by 8-row
blocks laid out one after another rather than as plain scanlines, so reading the
payload as rows produces a striped smear that still has the right colours - which
is exactly what it looked like before this was handled.
"""
import struct

from hanpatch.platforms.psp import imy

MAGIC = b'MAP\x00'
HEADER = 64
TILEMAP = 0x20

#: A tilemap entry is an index in its low bits and a flag in its high bit. The
#: flag's meaning is NOT established - it is set on most members of this disc
#: (`0x8001`, `0x8027`) and clear on the title logo - so it is masked off for
#: reading and preserved verbatim on write. Reading the entry whole made
#: `SOKANZUBG.MPB` ask for tile 32,807 and a 524,928-row atlas; with the mask its
#: highest tile lands exactly inside its real 256x640 atlas, which is what
#: identifies the split as correct rather than merely plausible.
TILE_FLAG = 0x8000
TILE_MASK = 0x7FFF

#: PSP texture swizzle granularity, in bytes and rows.
BLOCK_W = 16
BLOCK_H = 8


class MpbError(Exception):
    pass


class Layout:
    """What the header says about how the atlas becomes a picture."""

    __slots__ = ('imy_at', 'depth', 'tiles', 'cols', 'rows', 'tile_w',
                 'tile_h', 'width', 'height', 'tilemap')

    def __init__(self, blob):
        if len(blob) < HEADER or bytes(blob[:4]) != MAGIC:
            raise MpbError('not an MPB member')
        self.imy_at, = struct.unpack_from('<I', blob, 4)
        self.depth = blob[0x0d]
        (self.tiles, self.cols, self.rows, self.tile_w, self.tile_h,
         self.width, self.height) = struct.unpack_from('<7H', blob, 0x0e)
        cells = self.cols * self.rows
        if not cells or TILEMAP + cells * 2 > self.imy_at:
            raise MpbError('a %dx%d grid does not fit the header'
                           % (self.cols, self.rows))
        self.tilemap = list(struct.unpack_from('<%dH' % cells, blob, TILEMAP))

    @property
    def cells(self):
        return self.cols * self.rows

    def tile_of(self, entry):
        """The atlas tile an entry draws, or None for an empty cell."""
        index = entry & TILE_MASK
        return None if index == 0 else index - 1


def unswizzle(data, stride, height):
    """`data` as plain scanlines, undoing the PSP block order."""
    if stride % BLOCK_W or height % BLOCK_H:
        raise MpbError('%dx%d is not a whole number of %dx%d blocks'
                       % (stride, height, BLOCK_W, BLOCK_H))
    out = bytearray(len(data))
    per_row = stride // BLOCK_W
    src = 0
    for band in range(height // BLOCK_H):
        for bx in range(per_row):
            for y in range(BLOCK_H):
                dst = (band * BLOCK_H + y) * stride + bx * BLOCK_W
                out[dst:dst + BLOCK_W] = data[src:src + BLOCK_W]
                src += BLOCK_W
    return bytes(out)


def swizzle(data, stride, height):
    """The inverse of `unswizzle`."""
    if stride % BLOCK_W or height % BLOCK_H:
        raise MpbError('%dx%d is not a whole number of %dx%d blocks'
                       % (stride, height, BLOCK_W, BLOCK_H))
    out = bytearray(len(data))
    per_row = stride // BLOCK_W
    dst = 0
    for band in range(height // BLOCK_H):
        for bx in range(per_row):
            for y in range(BLOCK_H):
                src = (band * BLOCK_H + y) * stride + bx * BLOCK_W
                out[dst:dst + BLOCK_W] = data[src:src + BLOCK_W]
                dst += BLOCK_W
    return bytes(out)


def _rgba(palette, index):
    at = index * 4
    return tuple(palette[at:at + 4])


def decode(blob):
    """(layout, [[(r,g,b,a)]]) - the assembled picture, row-major.

    Only 8-bit atlases are handled, which is every member this title draws text
    or a logo into. A 4-bit member would need the nibble order established
    first, and guessing it would produce a picture that looks plausible and is
    wrong.
    """
    lay = Layout(blob)
    if lay.depth != 8:
        raise MpbError('%d-bit atlas is not implemented' % lay.depth)
    head, palette, pixels = imy.decode(blob, lay.imy_at)
    if head.colors * 4 != len(palette):
        raise MpbError('palette is %d bytes for %d colours'
                       % (len(palette), head.colors))
    flat = unswizzle(pixels, head.width, head.height)
    per_row = head.width // lay.tile_w
    out = [[(0, 0, 0, 0)] * lay.width for _ in range(lay.height)]
    for cell, entry in enumerate(lay.tilemap):
        tile = lay.tile_of(entry)
        if tile is None:
            continue
        sx = (tile % per_row) * lay.tile_w
        sy = (tile // per_row) * lay.tile_h
        if sy + lay.tile_h > head.height:
            raise MpbError('cell %d draws tile %d, which starts past the '
                           '%dx%d atlas' % (cell, tile, head.width,
                                            head.height))
        dx = (cell % lay.cols) * lay.tile_w
        dy = (cell // lay.cols) * lay.tile_h
        for y in range(lay.tile_h):
            ty = dy + y
            if ty >= lay.height:
                break
            row = out[ty]
            base = (sy + y) * head.width + sx
            for x in range(lay.tile_w):
                tx = dx + x
                if tx >= lay.width:
                    break
                row[tx] = _rgba(palette, flat[base + x])
    return lay, out


def encode(blob, image):
    """`blob` with `image` ({(x, y): (r,g,b,a)} as rows) drawn into its atlas.

    The header is untouched, so the tile map, grid and image bounds are the
    original's: a replacement is a repaint of the same layout, not a new one.

    The palette is rebuilt from the replacement's colours, so every atlas byte
    the tile map does not reach MUST be re-indexed to the colour it already had.
    Skipping that shipped a visible defect: this atlas is 256x256 while the
    picture is 390x119, so 19840 bytes (30.3%) lie outside the drawn bounds and
    all of them held old index 0 (opaque black) or 1 (transparent). In the
    rebuilt palette index 1 was the logo's olive vine colour, so 3456 of those
    bytes turned olive and the engine drew them as a 390px bar across the title
    screen - the exact width of the picture. `decode` clips at the picture
    height and therefore reported the round-trip pixel-exact, so nothing but
    booting the game could see it.

    Raises rather than dithering when the picture needs more colours than the
    atlas has slots. A silently quantised logo is a visible artefact that no
    later check would catch.
    """
    lay = Layout(blob)
    if lay.depth != 8:
        raise MpbError('%d-bit atlas is not implemented' % lay.depth)
    if len(image) != lay.height or any(len(r) != lay.width for r in image):
        raise MpbError('replacement is not %dx%d' % (lay.width, lay.height))
    head, palette, pixels = imy.decode(blob, lay.imy_at)
    flat = bytearray(unswizzle(pixels, head.width, head.height))

    order = []
    seen = {}
    for row in image:
        for px in row:
            if px not in seen:
                seen[px] = len(order)
                order.append(px)
    if len(order) > head.colors:
        raise MpbError('replacement uses %d colours, the atlas holds %d'
                       % (len(order), head.colors))

    written = bytearray(len(flat))
    drawn = bytearray(len(flat))
    per_row = head.width // lay.tile_w
    for cell, entry in enumerate(lay.tilemap):
        tile = lay.tile_of(entry)
        if tile is None:
            continue
        sx = (tile % per_row) * lay.tile_w
        sy = (tile // per_row) * lay.tile_h
        if sy + lay.tile_h > head.height:
            raise MpbError('cell %d draws tile %d, which starts past the '
                           '%dx%d atlas' % (cell, tile, head.width,
                                            head.height))
        dx = (cell % lay.cols) * lay.tile_w
        dy = (cell // lay.cols) * lay.tile_h
        for y in range(lay.tile_h):
            ty = dy + y
            base = (sy + y) * head.width + sx
            for x in range(lay.tile_w):
                tx = dx + x
                drawn[base + x] = 1
                if ty < lay.height and tx < lay.width:
                    flat[base + x] = seen[image[ty][tx]]
                    written[base + x] = 1

    # A byte a referenced tile DRAWS but the picture did not cover must keep the
    # COLOUR it had, which means a new index: its old index now names one of the
    # replacement's colours. Measured on `TITLE_LOGO.MPB`, whose 256x256 atlas
    # carries a 390x119 picture across a 448x128 grid: 3456 bytes are drawn past
    # the picture's edge and every one held index 1, transparent. The rebuilt
    # palette gave index 1 to the logo's olive vine, so the engine drew a 390px
    # olive bar - the picture's exact width - across the title screen.
    #
    # Bytes NO cell references are left alone. They cannot reach the screen, and
    # reserving palette slots for them is what makes a repaint fail: this atlas
    # has 16384 such bytes, and on a gradient atlas they would demand every one
    # of the 256 slots for colours nothing renders.
    n_img = len(order)
    kept = sorted({flat[pos] for pos in range(len(flat))
                   if drawn[pos] and not written[pos]})
    # Allocate every preserved colour BEFORE writing a single byte. Assigning as
    # they are discovered raises a bare ValueError from bytearray the moment an
    # index passes 255, which reads as a crash rather than as the palette being
    # full - and by then the payload is half rewritten.
    xlate = {}
    for old in kept:
        colour = _rgba(palette, old)
        new = seen.get(colour)
        if new is None:
            new = len(order)
            seen[colour] = new
            order.append(colour)
        xlate[old] = new
    if len(order) > head.colors:
        raise MpbError('the replacement uses %d colours and the atlas keeps %d '
                       'more outside the picture, %d in total, past the %d it '
                       'holds' % (n_img, len(order) - n_img,
                                  len(order), head.colors))
    for pos in range(len(flat)):
        if drawn[pos] and not written[pos]:
            flat[pos] = xlate[flat[pos]]

    newpal = bytearray(head.colors * 4)
    for px, i in seen.items():
        newpal[i * 4:i * 4 + 4] = bytes(px)
    body = imy.encode(head, bytes(newpal),
                      swizzle(bytes(flat), head.width, head.height))
    return bytes(blob[:lay.imy_at]) + body

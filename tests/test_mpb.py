"""MPB - the tiled image the title logo and shop backdrops are drawn from.

Synthetic cases cover the swizzle, the header and the encoder's refusals. The
corpus pass needs a real disc, so it walks every `.MPB` member of a directory
extracted from one and is skipped when that directory is absent - the same rule
`test_imy.py` uses.

    HANPATCH_PSP_EXTRACT=/path/to/extract python3 tests/test_mpb.py
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import mpb  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def skip(name, why):
    SKIP.append(name)
    print('  skip ' + name + ' (' + why + ')')


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


def test_swizzle():
    """The block order is a permutation, so the two directions must compose."""
    stride, height = 64, 32
    data = bytes((i * 7 + i // stride) & 0xFF for i in range(stride * height))
    plain = mpb.unswizzle(data, stride, height)
    case('unswizzle preserves length', len(plain) == len(data))
    case('unswizzle is a permutation', sorted(plain) == sorted(data))
    case('swizzle inverts unswizzle',
         mpb.swizzle(plain, stride, height) == data)
    case('unswizzle inverts swizzle',
         mpb.unswizzle(mpb.swizzle(data, stride, height), stride, height) == data)

    # The first block is the first BLOCK_W bytes of each of the first BLOCK_H
    # rows. Asserting that pins the direction, which a permutation check alone
    # cannot: swizzle and unswizzle are inverses even if both are backwards.
    want = b''.join(data[y * stride:y * stride + mpb.BLOCK_W]
                    for y in range(mpb.BLOCK_H))
    case('swizzle emits the first block first',
         mpb.swizzle(data, stride, height)[:mpb.BLOCK_W * mpb.BLOCK_H] == want)


def test_swizzle_refusals():
    data = bytes(64 * 32)
    case('swizzle refuses a partial block across',
         raises(mpb.MpbError, mpb.swizzle, data, 60, 32))
    case('swizzle refuses a partial block down',
         raises(mpb.MpbError, mpb.unswizzle, data, 64, 30))


def synthetic(cols=2, rows=1, tile=16, width=20, height=12, depth=8,
              tilemap=None, atlas=64, fill=None):
    """An MPB whose atlas is a plain gradient, for exercising the header path.

    The IMY block is written STORED so the test does not depend on the
    compressor; `imy.decode` reads the stored flag and returns the payload
    verbatim.
    """
    from hanpatch.platforms.psp import imy
    cells = cols * rows
    tilemap = list(range(1, cells + 1)) if tilemap is None else tilemap
    head = struct.pack('<4sI', mpb.MAGIC, mpb.HEADER)
    head += b'\x00' * (0x0d - len(head))
    head += bytes([depth])
    head += struct.pack('<7H', max(tilemap), cols, rows, tile, tile,
                        width, height)
    head += b'\x00' * (mpb.TILEMAP - len(head))
    head += struct.pack('<%dH' % cells, *tilemap)
    head += b'\x00' * (mpb.HEADER - len(head))

    # `fill` exists so a test can control how many DISTINCT indices the atlas
    # holds. The default gradient uses all 256, which is right for exercising
    # the palette but makes it impossible to test colour preservation outside
    # the picture: preserving 256 old colours plus any new one cannot fit in 256
    # slots, so the encoder would correctly refuse before the property could be
    # observed.
    pixels = (bytes(i & 0xFF for i in range(atlas * atlas)) if fill is None
              else bytes([fill]) * (atlas * atlas))
    palette = bytes()
    for i in range(256):
        palette += bytes((i, (i * 5) & 0xFF, (i * 11) & 0xFF, 0xFF))
    hdr = imy.Header(dsize=imy.HEADER + len(palette) + len(pixels),
                     width=atlas, height=atlas,
                     flags=imy.FLAG_STORED, depth=depth, colors=256)
    return head + imy.encode(hdr, palette, pixels)


def test_layout():
    blob = synthetic()
    lay = mpb.Layout(blob)
    case('layout reads the IMY offset', lay.imy_at == mpb.HEADER)
    case('layout reads the grid', (lay.cols, lay.rows) == (2, 1))
    case('layout reads the tile size', (lay.tile_w, lay.tile_h) == (16, 16))
    case('layout reads the image bounds', (lay.width, lay.height) == (20, 12))
    case('layout reads the tile map', lay.tilemap == [1, 2])
    case('layout counts cells', lay.cells == 2)

    case('layout refuses a non-MPB', raises(mpb.MpbError, mpb.Layout, b'nope' * 32))
    case('layout refuses a truncated header',
         raises(mpb.MpbError, mpb.Layout, blob[:8]))

    # A grid whose tile map would run past the IMY block is a header that
    # disagrees with itself; reading it would silently consume pixel bytes.
    bad = bytearray(blob)
    struct.pack_into('<H', bad, 0x10, 4096)
    case('layout refuses a grid that overruns the header',
         raises(mpb.MpbError, mpb.Layout, bytes(bad)))


def test_decode():
    blob = synthetic()
    lay, rows = mpb.decode(blob)
    case('decode returns the image height', len(rows) == lay.height)
    case('decode returns the image width',
         all(len(r) == lay.width for r in rows))
    case('decode returns RGBA tuples',
         all(len(px) == 4 for r in rows for px in r))

    # Cell 0 draws tile 0 from the atlas origin, so the top-left pixel is the
    # atlas's first pixel through the palette. That anchors the tile lookup.
    from hanpatch.platforms.psp import imy
    _, palette, _ = imy.decode(blob, lay.imy_at)
    case('decode maps the first pixel through the palette',
         rows[0][0] == tuple(palette[0:4]))

    # An empty cell stays transparent rather than drawing tile 0.
    holed = synthetic(cols=2, rows=1, tilemap=[0, 2])
    _, rows2 = mpb.decode(holed)
    case('decode leaves an empty cell transparent',
         rows2[0][0] == (0, 0, 0, 0))

    case('decode refuses a 4-bit atlas',
         raises(mpb.MpbError, mpb.decode, synthetic(depth=4)))


def test_encode():
    blob = synthetic()
    lay, rows = mpb.decode(blob)

    # The contract that matters for a replacement asset: whatever the encoder
    # does to the payload, decoding it again must give back the same picture.
    again = mpb.encode(blob, rows)
    lay2, rows2 = mpb.decode(again)
    case('encode round-trips the picture', rows2 == rows)
    case('encode preserves the header',
         again[:mpb.HEADER] == blob[:mpb.HEADER])
    case('encode preserves the image bounds',
         (lay2.width, lay2.height) == (lay.width, lay.height))

    # A repaint must actually land, and only inside the bounds.
    painted = [[(1, 2, 3, 255)] * lay.width for _ in range(lay.height)]
    _, back = mpb.decode(mpb.encode(blob, painted))
    case('encode writes the replacement pixels',
         all(px == (1, 2, 3, 255) for r in back for px in r))

    case('encode refuses the wrong height',
         raises(mpb.MpbError, mpb.encode, blob, rows[:-1]))
    case('encode refuses the wrong width',
         raises(mpb.MpbError, mpb.encode, blob,
                [r[:-1] for r in rows]))

    # More colours than the atlas has slots is the one failure a caller cannot
    # see in the output: quantising silently would ship a visibly banded logo.
    many = [[((x * 7 + y * 13) & 0xFF, x & 0xFF, y & 0xFF, 255)
             for x in range(lay.width)] for y in range(lay.height)]
    distinct = len({px for r in many for px in r})
    if distinct > 256:
        case('encode refuses more colours than the palette holds',
             raises(mpb.MpbError, mpb.encode, blob, many))
    else:
        skip('encode refuses more colours than the palette holds',
             'synthetic image only reaches %d colours' % distinct)


def test_encode_preserves_untouched_atlas():
    """A byte the tile map never names must keep its COLOUR, not its index.

    This shipped as a visible defect. `encode` rebuilds the palette from the
    replacement's own colours, but writes only the bytes inside the picture
    bounds - so every other byte kept an index that now pointed at a different
    colour. Measured on the Classic Dungeon X2 title logo: 19,840 of the 65,536
    atlas bytes (30.3%) are never written, 3,456 of them held old index 1, which
    was transparent, and the replacement's palette put its olive vine colour at
    index 1. The engine drew a 390px olive bar across the title screen.

    `decode` could not catch it: it clips at the picture height, so the
    round-trip was pixel-exact while the payload was wrong. The check therefore
    reads the atlas directly, old colour against new colour, over every byte a
    cell DRAWS - which is the set the engine can put on screen. Bytes in tiles
    no cell names are excluded deliberately: nothing renders them, and reserving
    palette slots for them is what pushed a 200-colour logo past 256.

    The atlas here is filled with index 0 deliberately. The painted colour takes
    index 0 in the rebuilt palette, so preserving the original colour REQUIRES
    rewriting those bytes to a different index - which is precisely what the
    defect failed to do.
    """
    from hanpatch.platforms.psp import imy

    def rgba(pal, i):
        return tuple(pal[i * 4:i * 4 + 4])

    blob = synthetic(fill=0)
    lay = mpb.Layout(blob)
    head, oldpal, oldpix = imy.decode(blob, lay.imy_at)
    oldflat = mpb.unswizzle(oldpix, head.width, head.height)

    paint = (9, 9, 9, 255)
    case('the fixture actually repaints (else the test proves nothing)',
         rgba(oldpal, 0) != paint)

    new = mpb.encode(blob, [[paint] * lay.width for _ in range(lay.height)])
    head2, newpal, newpix = imy.decode(new, lay.imy_at)
    newflat = mpb.unswizzle(newpix, head2.width, head2.height)

    # The drawn set, re-derived from the layout rather than borrowed from the
    # encoder: for each cell, the whole tile block it samples.
    per_row = head.width // lay.tile_w
    drawn = {}
    for cell, entry in enumerate(lay.tilemap):
        tile = lay.tile_of(entry)
        if tile is None:
            continue
        sx = (tile % per_row) * lay.tile_w
        sy = (tile // per_row) * lay.tile_h
        for y in range(lay.tile_h):
            for x in range(lay.tile_w):
                pos = (sy + y) * head.width + sx + x
                inside = (cell // lay.cols) * lay.tile_h + y < lay.height and \
                         (cell % lay.cols) * lay.tile_w + x < lay.width
                drawn[pos] = drawn.get(pos, False) or inside

    moved = [pos for pos, inside in drawn.items() if not inside
             and rgba(oldpal, oldflat[pos]) != rgba(newpal, newflat[pos])]
    case('a drawn byte outside the picture keeps its colour', not moved)
    repainted = sum(1 for pos, inside in drawn.items() if inside
                    and rgba(newpal, newflat[pos]) == paint)
    case('every drawn byte inside the picture carries the repaint',
         repainted == sum(1 for inside in drawn.values() if inside))
    case('the fixture leaves drawn bytes outside the picture to preserve',
         any(not inside for inside in drawn.values()))
    case('the original colour survives in the rebuilt palette',
         rgba(oldpal, 0) in {tuple(newpal[i * 4:i * 4 + 4])
                             for i in range(head2.colors)})
    case('the repaint still lands', mpb.decode(new)[1][0][0] == paint)

    # The cap must account for preserved colours too, or the encoder would
    # silently drop them once the palette filled up.
    full = synthetic(fill=0, atlas=64)
    many = []
    n = 0
    for _y in range(mpb.Layout(full).height):
        row = []
        for _x in range(mpb.Layout(full).width):
            row.append((n & 0xFF, (n >> 8) & 0xFF, 7, 255))
            n += 1
        many.append(row)
    distinct = len({px for r in many for px in r})
    if distinct == 256:
        case('encode refuses when picture + preserved colours overflow',
             raises(mpb.MpbError, mpb.encode, full, many))
    else:
        skip('encode refuses when picture + preserved colours overflow',
             'picture has %d distinct colours, not the 256 needed' % distinct)


def test_corpus():
    root = os.environ.get('HANPATCH_PSP_EXTRACT')
    if not root or not os.path.isdir(root):
        skip('corpus', 'set HANPATCH_PSP_EXTRACT to a directory of extracted files')
        return
    names = sorted(n for n in os.listdir(root) if n.upper().endswith('.MPB'))
    if not names:
        skip('corpus', 'no .MPB members in %s' % root)
        return
    seen = decoded = trips = 0
    other_depth = []
    bad = []
    for name in names:
        with open(os.path.join(root, name), 'rb') as fh:
            blob = fh.read()
        seen += 1
        try:
            lay, rows = mpb.decode(blob)
        except mpb.MpbError as exc:
            # 4-bit members are refused on purpose rather than guessed at.
            other_depth.append((name, str(exc)))
            continue
        if len(rows) != lay.height or any(len(r) != lay.width for r in rows):
            bad.append((name, 'size'))
            continue
        decoded += 1
        try:
            _, again = mpb.decode(mpb.encode(blob, rows))
        except mpb.MpbError as exc:
            bad.append((name, 'encode: %s' % exc))
            continue
        if again == rows:
            trips += 1
        else:
            bad.append((name, 'picture changed'))
    print('  corpus: %d members, %d decoded, %d round-tripped, %d refused'
          % (seen, decoded, trips, len(other_depth)))
    case('corpus: every 8-bit member decodes to its declared bounds',
         decoded + len(other_depth) == seen)
    case('corpus: every decoded member round-trips its picture',
         trips == decoded)
    for entry in bad[:10]:
        print('    ' + repr(entry))


def main():
    print('swizzle')
    test_swizzle()
    test_swizzle_refusals()
    print('header')
    test_layout()
    print('decode')
    test_decode()
    print('encode')
    test_encode()
    test_encode_preserves_untouched_atlas()
    print('corpus')
    test_corpus()
    print('\n%d passed, %d failed, %d skipped'
          % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        for name in FAIL:
            print('  FAIL ' + name)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

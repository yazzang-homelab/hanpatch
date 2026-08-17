"""Tests for the font archive, its swizzled sheets, and its metrics.

The synthetic cases build sheets and metrics byte by byte, so the swizzle, the
cell arithmetic, and the code-to-cell mapping are proved without a disc. The
corpus cases parse both real archives and rebuild them, which is what proves the
swizzle block size and the member padding are the game's and not ours.

Run: python3 tests/test_font.py
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import dsarc  # noqa: E402
from hanpatch.platforms.psp import font  # noqa: E402
from hanpatch.platforms.psp import imy  # noqa: E402

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


RAMP = b''.join(bytes([0xFF, 0xFF, 0xFF, i * 17]) for i in range(16))


def sheet_blob(coverage=None, stride=256, height=256):
    """A sheet whose pixels are whatever `coverage` says, swizzled."""
    flat = bytearray(stride * height)
    if coverage:
        for i, v in coverage.items():
            flat[i // 2] |= (v << 4) if i % 2 else v
    head = imy.Header(imy.HEADER + 64 + stride * height, stride, 0xA4, 4,
                      height, 16)
    blob = imy.encode(head, RAMP, font.swizzle(bytes(flat), stride, height))
    # members are padded to 4 bytes, as they are on the disc; a fixture that
    # skips the padding tests a format the game does not use
    return blob + b'\x00' * (-len(blob) % 4)


def archive(glyphs, sheets=1):
    members = [(font.METRICS, font.build_metrics(glyphs))]
    for i in range(sheets):
        members.append(('FONT_%02d.IMY' % i, sheet_blob()))
    return dsarc.build_idx(members)


# ----------------------------------------------------------------- swizzle

def test_swizzle():
    stride, height = 32, 16
    src = bytes((i * 7) & 0xFF for i in range(stride * height))
    sw = font.swizzle(src, stride, height)
    case('swizzle is reversible', font.unswizzle(sw, stride, height) == src)
    case('swizzle actually reorders', sw != src)

    # the first block is the first 16 bytes of each of the first 8 rows
    first = b''.join(src[r * stride:r * stride + 16] for r in range(8))
    case('a block is 16 bytes by 8 rows', sw[:128] == first)

    case('a surface that does not divide into blocks is refused',
         raises(font.FontError, font.swizzle, b'\x00' * 12, 12, 1))


# ----------------------------------------------------------------- metrics

def test_metrics():
    glyphs = [font.Glyph(0, 0x20, 0, 7), font.Glyph(1, 0x41, 1, 8),
              font.Glyph(2, 0x889F, 0, 15)]
    blob = font.build_metrics(glyphs)
    back = font.parse_metrics(blob)

    case('metrics round trip',
         [(g.code, g.bearing, g.advance) for g in back]
         == [(0x20, 0, 7), (0x41, 1, 8), (0x889F, 0, 15)])
    case('a one-byte code decodes from the low byte', back[1].char == 'A')
    case('a two-byte code decodes as Shift-JIS', back[2].char is not None)
    case('an illegal code reads as None, not as a crash',
         font.Glyph(0, 0x8100, 0, 8).char is None)
    case('a truncated metrics blob is refused',
         raises(font.FontError, font.parse_metrics, blob[:-2]))
    case('a count that disagrees with the length is refused',
         raises(font.FontError, font.parse_metrics,
                struct.pack('<I', 99) + blob[4:]))


def test_cells():
    # index 0 is the top-left cell; 31 ends the first row; 32 starts the second
    for index, origin in ((0, (0, 0)), (1, (16, 0)), (31, (496, 0)),
                          (32, (0, 16)), (511, (496, 240))):
        g = font.Glyph(index, 0x20, 0, 8)
        case('cell %d sits at %s' % (index, origin), g.origin == origin)
        case('cell %d is on sheet 0' % index, g.sheet == 0)

    case('cell 512 rolls onto the next sheet',
         (font.Glyph(512, 0x20, 0, 8).sheet,
          font.Glyph(512, 0x20, 0, 8).origin) == (1, (0, 0)))
    case('a sheet holds 512 cells', font.CELLS_PER_SHEET == 512)


# ------------------------------------------------------------------- sheets

def test_sheet_io():
    blob = sheet_blob({0: 15, 1: 3, 512: 9})
    header, palette, pixels = imy.decode(blob)
    sheet = font.Sheet(header, palette, pixels)

    case('a sheet is 512 by 256 pixels',
         (sheet.width, sheet.height) == (512, 256))
    case('coverage reads back per pixel, low nibble first',
         (sheet.coverage[0], sheet.coverage[1], sheet.coverage[512]) == (15, 3, 9))
    case('re-packing reproduces the stored pixels', sheet.pixels() == pixels)

    ink = bytes(range(16)) * 16
    sheet.write_cell(16, 16, ink)
    case('a written cell reads back', sheet.read_cell(16, 16) == ink)
    case('writing one cell leaves its neighbour alone',
         sheet.read_cell(0, 16) == bytes(font.CELL * font.CELL))
    case('coverage above 15 is refused',
         raises(font.FontError, sheet.write_cell, 0, 0, bytes([16]) * 256))
    case('a cell of the wrong size is refused',
         raises(font.FontError, sheet.write_cell, 0, 0, b'\x00' * 12))


def test_font():
    glyphs = [font.Glyph(0, 0x20, 0, 7), font.Glyph(1, 0x41, 1, 8)]
    blob = archive(glyphs)
    f = font.Font(blob)

    case('the archive parses into metrics and sheets',
         (len(f.glyphs), len(f.sheets)) == (2, 1))
    case('capacity is cells, not glyphs', f.capacity == 512)
    case('characters map to their glyphs', f.by_char()['A'].index == 1)

    ink = bytes([15]) * 256
    f.write(f.glyphs[1], ink)
    case('a glyph written through the font reads back',
         f.read(f.glyphs[1]) == ink)
    case('the neighbouring cell is untouched',
         f.read(f.glyphs[0]) == bytes(256))

    back = font.Font(f.build())
    case('an edited font survives a rebuild', back.read(back.glyphs[1]) == ink)
    case('rebuilding an unedited font reproduces it',
         font.Font(blob).build() == blob)

    # retargeting a cell is four bytes of metrics, nothing else
    f.glyphs[1].code = 0x42
    case('retargeting a cell changes only the code',
         font.Font(f.build()).by_char()['B'].index == 1)

    case('more glyphs than cells is refused',
         raises(font.FontError,
                font.Font, archive([font.Glyph(i, 0x20, 0, 8)
                                    for i in range(513)], sheets=1)))
    case('an archive without metrics is refused',
         raises(font.FontError, font.Font,
                dsarc.build_idx([('FONT_00.IMY', sheet_blob())])))


# ------------------------------------------------------------------- corpus

def test_corpus():
    root = os.environ.get('HANPATCH_PSP_EXTRACT')
    if not root or not os.path.isdir(root):
        skip('corpus: both font archives rebuild byte for byte',
             'set HANPATCH_PSP_EXTRACT to a directory of extracted files')
        return
    for name in ('FONT1.ARC', 'FONT2.ARC'):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            skip('corpus: %s' % name, 'not in the extract')
            continue
        with open(path, 'rb') as fh:
            data = fh.read()
        f = font.Font(data)
        case('corpus: %s rebuilds byte for byte' % name, f.build() == data)
        case('corpus: %s glyphs fit its sheets' % name,
             len(f.glyphs) <= f.capacity)
        case('corpus: %s metrics are in code order' % name,
             [g.code for g in f.glyphs[:8]] == sorted(g.code for g in f.glyphs[:8]))
        blank = bytes(font.CELL * font.CELL)
        case('corpus: %s space is an empty cell' % name,
             f.read(f.by_char()[' ']) == blank)
        case('corpus: %s A is not an empty cell' % name,
             f.read(f.by_char()['A']) != blank)
        print('  corpus: %s has %d glyphs in %d cells across %d sheets'
              % (name, len(f.glyphs), f.capacity, len(f.sheets)))


def main():
    print('swizzle')
    test_swizzle()
    print('metrics')
    test_metrics()
    test_cells()
    print('sheets')
    test_sheet_io()
    test_font()
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

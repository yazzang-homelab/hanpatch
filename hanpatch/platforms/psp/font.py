"""The title's font: metrics in FONT.BIN, glyphs in six IMY sheets.

PROVENANCE: measured from the disc. Both font archives parse, rebuild, and
compare byte identical against the originals, sheets included.

    ARCHIVE  FONT1.ARC / FONT2.ARC, a DSARCIDX (see `dsarc.py`)

    FONT.BIN        metrics, one 4-byte entry per glyph
    FONT_00.IMY     glyph sheet 0
    ...             six sheets in all

    FONT.BIN

    0x00   4  glyph count
    0x04      entries, 4 bytes each

    ENTRY

    0x00   2  Shift-JIS code; a one-byte character is stored in the low byte
    0x02   1  left side bearing, 0..9
    0x03   1  advance width in pixels, 1..15

There is no position field. A glyph's cell is its index in this table, and the
table is in code order, so the metrics ARE the code-to-cell map. Nothing else
records where a glyph lives, which is what makes retargeting a cell to a
different character a matter of rewriting four bytes.

    SHEETS

Each sheet is a 4bpp IMY block with a 16-entry palette, 256 bytes of stride and
256 rows - that is 512 by 256 PIXELS, because at 4bpp the IMY stride is bytes
and holds two pixels each. The palette is a straight alpha ramp: white at
sixteen levels of coverage, index n meaning n/15 opaque. These are antialiased
coverage masks, not colour images, so a glyph is a 16x16 array of 0..15.

Sheet pixels are stored SWIZZLED, in the PSP's texture order: blocks of 16 bytes
by 8 rows, laid out one after another. Read a sheet linearly and it decodes into
horizontal noise, which looks exactly like a broken decompressor and is not one.

Cells are 16x16 pixels, 32 across and 16 down, so 512 to a sheet and 3072 across
six. The disc uses 2725 of them.

Content boundary: this module addresses cells and reads coverage values. It does
not rasterise, does not know what a glyph depicts, and does not choose which
character should own a cell.
"""
import struct

from . import dsarc
from . import imy

METRICS = 'FONT.BIN'
CELL = 16
CELLS_ACROSS = 32
CELLS_DOWN = 16
CELLS_PER_SHEET = CELLS_ACROSS * CELLS_DOWN
LEVELS = 16
SWIZZLE_W = 16
SWIZZLE_H = 8


class FontError(Exception):
    pass


def unswizzle(src, stride, height):
    """Undo the PSP texture order: 16-byte by 8-row blocks laid out linearly."""
    if stride % SWIZZLE_W or height % SWIZZLE_H:
        raise FontError('a %dx%d surface does not divide into %dx%d blocks'
                        % (stride, height, SWIZZLE_W, SWIZZLE_H))
    out = bytearray(len(src))
    i = 0
    for by in range(height // SWIZZLE_H):
        for bx in range(stride // SWIZZLE_W):
            for row in range(SWIZZLE_H):
                o = (by * SWIZZLE_H + row) * stride + bx * SWIZZLE_W
                out[o:o + SWIZZLE_W] = src[i:i + SWIZZLE_W]
                i += SWIZZLE_W
    return bytes(out)


def swizzle(src, stride, height):
    """The inverse of `unswizzle`."""
    if stride % SWIZZLE_W or height % SWIZZLE_H:
        raise FontError('a %dx%d surface does not divide into %dx%d blocks'
                        % (stride, height, SWIZZLE_W, SWIZZLE_H))
    out = bytearray(len(src))
    i = 0
    for by in range(height // SWIZZLE_H):
        for bx in range(stride // SWIZZLE_W):
            for row in range(SWIZZLE_H):
                o = (by * SWIZZLE_H + row) * stride + bx * SWIZZLE_W
                out[i:i + SWIZZLE_W] = src[o:o + SWIZZLE_W]
                i += SWIZZLE_W
    return bytes(out)


class Glyph:
    """One entry of FONT.BIN. `code` is Shift-JIS, not Unicode."""

    __slots__ = ('index', 'code', 'bearing', 'advance')

    def __init__(self, index, code, bearing, advance):
        self.index = index
        self.code = code
        self.bearing = bearing
        self.advance = advance

    @property
    def raw(self):
        """The Shift-JIS bytes of this glyph's character, big-endian order."""
        if self.code < 0x100:
            return bytes([self.code])
        return bytes([(self.code >> 8) & 0xFF, self.code & 0xFF])

    @property
    def char(self):
        """The character, or None when the code is not legal Shift-JIS."""
        try:
            return self.raw.decode('shift_jis')
        except UnicodeDecodeError:
            return None

    @property
    def sheet(self):
        return self.index // CELLS_PER_SHEET

    @property
    def cell(self):
        return self.index % CELLS_PER_SHEET

    @property
    def origin(self):
        """(x, y) of this glyph's top-left pixel within its sheet."""
        c = self.cell
        return (c % CELLS_ACROSS * CELL, c // CELLS_ACROSS * CELL)

    def pack(self):
        return struct.pack('<HBB', self.code, self.bearing, self.advance)

    def __repr__(self):
        return 'Glyph(%d, code=0x%04X, %r, bearing=%d, advance=%d)' % (
            self.index, self.code, self.char, self.bearing, self.advance)


def parse_metrics(blob):
    count, = struct.unpack_from('<I', blob, 0)
    if 4 + count * 4 != len(blob):
        raise FontError('%s declares %d glyphs but is %d bytes'
                        % (METRICS, count, len(blob)))
    out = []
    for i in range(count):
        code, bearing, advance = struct.unpack_from('<HBB', blob, 4 + i * 4)
        out.append(Glyph(i, code, bearing, advance))
    return out


def build_metrics(glyphs):
    out = bytearray(struct.pack('<I', len(glyphs)))
    for g in glyphs:
        out += g.pack()
    return bytes(out)


class Sheet:
    """One glyph sheet, held unswizzled as one byte per pixel, 0..15."""

    def __init__(self, header, palette, pixels):
        self.header = header
        self.palette = palette
        self.width = header.width * 2          # two 4bpp pixels to a byte
        self.height = header.height
        flat = unswizzle(pixels, header.width, header.height)
        cov = bytearray(self.width * self.height)
        for i, b in enumerate(flat):
            cov[i * 2] = b & 0x0F
            cov[i * 2 + 1] = b >> 4
        self.coverage = cov

    def pixels(self):
        """Re-pack to swizzled 4bpp, the form the sheet is stored in."""
        flat = bytearray(len(self.coverage) // 2)
        for i in range(len(flat)):
            lo = self.coverage[i * 2] & 0x0F
            hi = self.coverage[i * 2 + 1] & 0x0F
            flat[i] = lo | (hi << 4)
        return swizzle(bytes(flat), self.header.width, self.header.height)

    def read_cell(self, x, y):
        out = bytearray(CELL * CELL)
        for row in range(CELL):
            o = (y + row) * self.width + x
            out[row * CELL:(row + 1) * CELL] = self.coverage[o:o + CELL]
        return bytes(out)

    def write_cell(self, x, y, values):
        if len(values) != CELL * CELL:
            raise FontError('a cell is %d values, got %d'
                            % (CELL * CELL, len(values)))
        if any(v > LEVELS - 1 for v in values):
            raise FontError('coverage runs 0..%d' % (LEVELS - 1))
        for row in range(CELL):
            o = (y + row) * self.width + x
            self.coverage[o:o + CELL] = values[row * CELL:(row + 1) * CELL]

    def encode(self):
        """The stored member: the IMY block, padded to a 4-byte boundary.

        Two of the twelve sheets on the disc carry two bytes of padding after
        the block, so a member is not always exactly its block. Dropping the pad
        changes the member's size field and the offsets of everything after it.
        """
        blob = imy.encode(self.header, self.palette, self.pixels())
        return blob + b'\x00' * (-len(blob) % 4)


class Font:
    """A whole font archive: metrics, sheets, and the cells they address."""

    def __init__(self, data):
        self.archive = dsarc.DsarcIdx(data)
        names = self.archive.names()
        if METRICS not in names:
            raise FontError('archive has no %s' % METRICS)
        self.sheet_names = [n for n in names if n != METRICS]
        self.glyphs = parse_metrics(self.archive.read(METRICS))
        self.sheets = []
        for name in self.sheet_names:
            blob = self.archive.read(name)
            header, palette, pixels = imy.decode(blob)
            if header.depth != 4:
                raise FontError('%s is %d bpp, expected 4' % (name, header.depth))
            self.sheets.append(Sheet(header, palette, pixels))
        need = (len(self.glyphs) + CELLS_PER_SHEET - 1) // CELLS_PER_SHEET
        if need > len(self.sheets):
            raise FontError('%d glyphs need %d sheets, archive has %d'
                            % (len(self.glyphs), need, len(self.sheets)))

    @property
    def capacity(self):
        return len(self.sheets) * CELLS_PER_SHEET

    def by_char(self):
        """{character: glyph}, skipping codes that are not legal Shift-JIS."""
        out = {}
        for g in self.glyphs:
            c = g.char
            if c is not None:
                out.setdefault(c, g)
        return out

    def read(self, glyph):
        x, y = glyph.origin
        return self.sheets[glyph.sheet].read_cell(x, y)

    def write(self, glyph, values):
        x, y = glyph.origin
        self.sheets[glyph.sheet].write_cell(x, y, values)

    def build(self):
        members = [(METRICS, build_metrics(self.glyphs))]
        for name, sheet in zip(self.sheet_names, self.sheets):
            members.append((name, sheet.encode()))
        return dsarc.build_idx(members, self.archive.ids,
                               self.archive.reserved)


class Metrics:
    """Width source for the layout gates, in the shape `wrap.char_width` needs.

    The 3DS reader exposes `char_to_index` / `width_of` / `def_cw`, and the
    layout core calls exactly those three. This presents the PSP font the same
    way so the core does not have to know which platform it is measuring, and
    so a width comes from the shipped font rather than from an assumed cell.

    A retargeted cell keeps the advance of the glyph it replaced, so a Hangul
    syllable measures as whatever kanji used to own that cell. Every one of them
    is 15 px on this disc, which is the full-width advance a syllable wants, but
    that is a measurement to re-check if the retarget pool ever changes.
    """

    def __init__(self, blob, hangul=None):
        self.font = Font(blob)
        self.by_char = {}
        for glyph in self.font.glyphs:
            ch = glyph.char
            if ch is not None:
                self.by_char.setdefault(ch, glyph)
        for ch, code in (hangul or {}).items():
            for glyph in self.font.glyphs:
                if glyph.code == code:
                    self.by_char[ch] = glyph
                    break
        widths = [g.advance for g in self.font.glyphs if g.advance]
        self.def_cw = max(set(widths), key=widths.count) if widths else CELL

    def char_to_index(self, ch):
        glyph = self.by_char.get(ch)
        return None if glyph is None else glyph.index

    def width_of(self, index):
        """(left, glyph width, advance) - the core reads the third."""
        glyph = self.font.glyphs[index]
        return (glyph.bearing, glyph.advance, glyph.advance)


def free_codes(glyphs, want):
    """Unused codes under lead bytes this font already proves the reader takes.

    The disc's own glyphs run past standard Shift-JIS - there are entries under
    lead bytes 0xF2, 0xF3 and 0xF5 - so the engine's reader is not limited to
    the standard ranges. Even so, new codes are drawn only from lead bytes that
    already carry glyphs, because those are the ones the shipped data proves are
    read as a two-byte character rather than as two singles.
    """
    used = {g.code for g in glyphs}
    leads = sorted({(g.code >> 8) & 0xFF for g in glyphs if g.code >= 0x100})
    out = []
    for lead in leads:
        for trail in range(0x40, 0xFD):
            if trail == 0x7F:
                continue
            code = (lead << 8) | trail
            if code not in used:
                out.append(code)
                if len(out) >= want:
                    return out
    return out


def append_glyphs(face, count):
    """Add `count` metrics entries pointing at cells the font does not use.

    The loader reads the glyph count out of FONT.BIN, allocates from it, and
    builds its own {code, index} array which it then SORTS, so appended entries
    need not be in code order and the table may grow. That was measured in the
    loader at EBOOT 0x1d02c; without it, appending would be a guess about the
    lookup and the empty cells would have to stay empty.
    """
    room = face.capacity - len(face.glyphs)
    if count > room:
        raise FontError('%d new entries but only %d empty cells'
                        % (count, room))
    codes = free_codes(face.glyphs, count)
    if len(codes) < count:
        raise FontError('only %d free codes for %d entries'
                        % (len(codes), count))
    added = []
    for code in codes:
        glyph = Glyph(len(face.glyphs), code, 0, CELL - 1)
        face.glyphs.append(glyph)
        added.append(glyph)
    return added

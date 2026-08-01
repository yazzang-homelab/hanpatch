"""BCFNT (CFNT v3) reader/writer.

Sheet pixels are stored in 3DS tiled (morton) order in one of several formats, and
which one is a per-title fact this module reads rather than assumes:

    fmt  4 = RGBA4444, 2 bytes/px. RGB is a shading mask the engine multiplies
             with the text colour; A is ink coverage. Crimson Shroud.
    fmt  8 = A8,       1 byte/px.  Coverage only, no shading mask.
    fmt 11 = A4,       4 bits/px.  Coverage only, two pixels per byte, LOW nibble
             first. That nibble order is the one thing here a round trip cannot
             prove, because `untile` and `tile` share the convention and a wrong
             one cancels on both sides; `nibble_order_agrees()` below checks it
             against the same glyph in an A8 font, where the byte order is
             unambiguous.

Measured on the two titles in this repo: Crimson Shroud ships fmt 4 at 64x64
sheets, Dragon Quest VII ships fmt 11 for its six `tbud_maru` text fonts and fmt 8
for `iwamaru_p15`, both at 1024x1024. An earlier version of this module hard-wired
fmt 4 in both the reader and the writer, so a DQ7 font raised IndexError on the
first sheet read - the A4 sheet is a quarter the size the RGBA4444 stride expects.
"""
import struct
from PIL import Image

# 3DS morton order within an 8x8 tile
TILE_ORDER = []
for i in range(64):
    x = ((i >> 0) & 1) | ((i >> 1) & 2) | ((i >> 2) & 4)
    y = ((i >> 1) & 1) | ((i >> 2) & 2) | ((i >> 3) & 4)
    TILE_ORDER.append((x, y))


#: bits per pixel for every format this module can read or write
FORMAT_BITS = {4: 16, 8: 8, 11: 4}
#: formats whose pixels carry a shading mask alongside coverage
FORMAT_HAS_RGB = {4}


def sheet_bytes(w, h, fmt):
    """Size of one sheet in bytes, or refuse an unknown format."""
    if fmt not in FORMAT_BITS:
        raise ValueError(f'sheet pixel format {fmt} is not one this reader has '
                         f'evidence for ({sorted(FORMAT_BITS)})')
    return w * h * FORMAT_BITS[fmt] // 8


def untile(raw, w, h, fmt):
    """Tiled sheet bytes -> PIL 'LA' (luminance = shading mask, alpha = coverage).

    For coverage-only formats the luminance channel is set to full, which is what
    an engine that ignores RGB effectively renders.
    """
    want = sheet_bytes(w, h, fmt)
    if len(raw) < want:
        raise ValueError(f'sheet is {len(raw)} bytes, format {fmt} needs {want} for '
                         f'{w}x{h}')
    img = Image.new('LA', (w, h))
    px = img.load()
    p = 0
    nib = 0
    for ty in range(0, h, 8):
        for tx in range(0, w, 8):
            for (ox, oy) in TILE_ORDER:
                if fmt == 4:
                    v = raw[p] | (raw[p + 1] << 8)
                    p += 2
                    px[tx + ox, ty + oy] = (((v >> 12) & 0xF) * 17, (v & 0xF) * 17)
                elif fmt == 8:
                    px[tx + ox, ty + oy] = (255, raw[p])
                    p += 1
                else:                       # fmt 11, A4: low nibble first
                    byte = raw[p + (nib >> 1)]
                    a = (byte & 0xF) if not (nib & 1) else (byte >> 4)
                    nib += 1
                    px[tx + ox, ty + oy] = (255, a * 17)
    return img


def nibble_order_agrees(a4_font, a8_font, sample):
    """Is the A4 low-nibble-first reading corroborated by an A8 font?

    A4 packs two horizontally adjacent pixels of each morton tile into one byte, so
    reading the nibbles in the wrong order swaps every adjacent PAIR - marginal at
    16px, destroyed in an 8x9 cell - and no round trip can see it, since the writer
    would swap them back. An A8 font has one byte per pixel and no such ambiguity,
    so the same glyph rendered in both is an independent witness.

    Returns (ok, score_as_read, score_swapped): `ok` is True when the coverage read
    as low-nibble-first correlates with the A8 glyph better than its pairwise-swapped
    variant does.
    """
    def coverage(f, ch):
        idx = f.char_to_index(ch)
        if idx is None:
            return None
        cell = f.glyph_image(idx)
        return cell.split()[-1]

    def swap_pairs(img):
        out = img.copy()
        px = out.load()
        w, h = out.size
        for y in range(h):
            for x in range(0, w - 1, 2):
                px[x, y], px[x + 1, y] = px[x + 1, y], px[x, y]
        return out

    def score(a, b):
        """Normalised agreement between two coverage images, scale-independent."""
        import statistics
        w = min(a.width, b.width)
        h = min(a.height, b.height)
        agree = 0
        total = 0
        for y in range(h):
            for x in range(w):
                ia = a.getpixel((x, y)) > 0
                ib = b.getpixel((x, y)) > 0
                agree += (ia == ib)
                total += 1
        return agree / total if total else 0.0

    same = swapped = 0.0
    seen = 0
    for ch in sample:
        ca = coverage(a4_font, ch)
        cb = coverage(a8_font, ch)
        if ca is None or cb is None:
            continue
        seen += 1
        same += score(ca, cb)
        swapped += score(swap_pairs(ca), cb)
    if not seen:
        return (None, 0.0, 0.0)
    return (same > swapped, same / seen, swapped / seen)


def untile_rgba(raw, w, h, fmt):
    """Tiled sheet bytes -> PIL 'RGBA' with the TRUE channels.

    Distinct from `untile`, which collapses to luminance+alpha: for a format that
    carries a shading mask the three channels are not equal, and callers that
    convert to 'LA' get a weighted mix of them. Collapsing early therefore changes
    the luminance a rebuild writes back, which is how a font that should have been
    byte-identical stopped being so.
    """
    want = sheet_bytes(w, h, fmt)
    if len(raw) < want:
        raise ValueError(f'sheet is {len(raw)} bytes, format {fmt} needs {want}')
    img = Image.new('RGBA', (w, h))
    px = img.load()
    p = 0
    nib = 0
    for ty in range(0, h, 8):
        for tx in range(0, w, 8):
            for (ox, oy) in TILE_ORDER:
                if fmt == 4:
                    v = raw[p] | (raw[p + 1] << 8)
                    p += 2
                    px[tx + ox, ty + oy] = (((v >> 12) & 15) * 17, ((v >> 8) & 15) * 17,
                                            ((v >> 4) & 15) * 17, (v & 15) * 17)
                elif fmt == 8:
                    px[tx + ox, ty + oy] = (255, 255, 255, raw[p])
                    p += 1
                else:
                    byte = raw[p + (nib >> 1)]
                    a = (byte & 0xF) if not (nib & 1) else (byte >> 4)
                    nib += 1
                    px[tx + ox, ty + oy] = (255, 255, 255, a * 17)
    return img


def tile(img, fmt):
    """PIL 'LA' -> tiled sheet bytes in `fmt`."""
    w, h = img.size
    if w % 8 or h % 8:
        raise ValueError(f'sheet {w}x{h} is not a multiple of 8')
    sheet_bytes(w, h, fmt)
    px = img.load()
    out = bytearray()
    nibbles = []
    for ty in range(0, h, 8):
        for tx in range(0, w, 8):
            for (ox, oy) in TILE_ORDER:
                l, a = px[tx + ox, ty + oy]
                if fmt == 4:
                    # Delegate the quantisation to the original writer rather than
                    # re-deriving it: `>> 4` and `// 17` agree only on multiples of
                    # 17, and an antialiased coverage value is not one, so a
                    # re-derivation would silently move the reference title's fonts.
                    n = l >> 4
                    out += struct.pack('<H', (n << 12) | (n << 8) | (n << 4) | (a >> 4))
                elif fmt == 8:
                    out.append(a)
                else:
                    nibbles.append(a // 17)
    if fmt == 11:
        for i in range(0, len(nibbles), 2):
            lo = nibbles[i]
            hi = nibbles[i + 1] if i + 1 < len(nibbles) else 0
            out.append((hi << 4) | lo)
    return bytes(out)


def untile_rgba4444(raw, w, h):
    """returns PIL 'LA' image (we only care about luminance+alpha)"""
    img = Image.new('LA', (w, h))
    px = img.load()
    p = 0
    for ty in range(0, h, 8):
        for tx in range(0, w, 8):
            for (ox, oy) in TILE_ORDER:
                v = raw[p] | (raw[p + 1] << 8)
                p += 2
                r = (v >> 12) & 0xF
                a = v & 0xF
                px[tx + ox, ty + oy] = (r * 17, a * 17)
    return img


def tile_rgba4444(img):
    """img: PIL 'LA' -> tiled RGBA4444 bytes"""
    w, h = img.size
    assert w % 8 == 0 and h % 8 == 0
    px = img.load()
    out = bytearray()
    for ty in range(0, h, 8):
        for tx in range(0, w, 8):
            for (ox, oy) in TILE_ORDER:
                l, a = px[tx + ox, ty + oy]
                n = l >> 4
                al = a >> 4
                v = (n << 12) | (n << 8) | (n << 4) | al
                out += struct.pack('<H', v)
    return bytes(out)


class Bcfnt:
    def __init__(self, data):
        self.raw = data
        magic, bom, hsz, self.ver, fsz, nblocks = struct.unpack('<4sHHIII', data[:0x14])
        assert magic == b'CFNT' and self.ver == 0x03000000
        self.hsz = hsz
        o = hsz
        self.finf = None
        self.tglp = None
        self.cwdh = []      # list of (start, end, [(l,g,c)...])
        self.cmap = []      # list of (start, end, type, payload)
        while o + 8 <= len(data):
            bm = data[o:o + 4]
            bs, = struct.unpack('<I', data[o + 4:o + 8])
            if bs == 0:
                break
            p = o + 8
            if bm == b'FINF':
                (self.font_type, self.linefeed, self.alter_idx, self.def_left,
                 self.def_gw, self.def_cw, self.encoding, _t, _c, _m,
                 self.height, self.width, self.ascent, _r) = struct.unpack('<BbH3BBIII4B', data[p:p + 0x18])
            elif bm == b'TGLP':
                (self.cell_w, self.cell_h, self.baseline, self.max_cw, self.sheet_size,
                 self.n_sheets, self.fmt, self.n_cols, self.n_rows,
                 self.sheet_w, self.sheet_h, sh_off) = struct.unpack('<4BIHHHHHHI', data[p:p + 0x18])
                self.sheets = [data[sh_off + i * self.sheet_size: sh_off + (i + 1) * self.sheet_size]
                               for i in range(self.n_sheets)]
            elif bm == b'CWDH':
                si, ei, nxt = struct.unpack('<HHI', data[p:p + 8])
                ws = []
                q = p + 8
                for i in range(ei - si + 1):
                    ws.append(struct.unpack('<bBb', data[q:q + 3]))
                    q += 3
                self.cwdh.append((si, ei, ws))
            elif bm == b'CMAP':
                s, e, mt, res, nxt = struct.unpack('<HHHHI', data[p:p + 0xc])
                q = p + 0xc
                if mt == 0:
                    payload = struct.unpack('<H', data[q:q + 2])[0]
                elif mt == 1:
                    payload = list(struct.unpack('<%dH' % (e - s + 1), data[q:q + 2 * (e - s + 1)]))
                else:
                    cnt, = struct.unpack('<H', data[q:q + 2])
                    payload = [struct.unpack('<HH', data[q + 2 + i * 4:q + 6 + i * 4]) for i in range(cnt)]
                self.cmap.append((s, e, mt, payload))
            o += bs

    # ---- lookups -------------------------------------------------------
    def char_to_index(self, ch):
        c = ord(ch)
        for s, e, mt, payload in self.cmap:
            if not (s <= c <= e):
                continue
            if mt == 0:
                return payload + (c - s)
            if mt == 1:
                v = payload[c - s]
                if v != 0xFFFF:
                    return v
            else:
                for cc, idx in payload:
                    if cc == c:
                        return idx
        return None

    def width_of(self, idx):
        for s, e, ws in self.cwdh:
            if s <= idx <= e:
                return ws[idx - s]
        return (self.def_left, self.def_gw, self.def_cw)

    def sheet_image(self, i):
        return untile(self.sheets[i], self.sheet_w, self.sheet_h, self.fmt)

    def glyph_image(self, idx):
        per = self.n_cols * self.n_rows
        sh, rem = divmod(idx, per)
        row, col = divmod(rem, self.n_cols)
        img = self.sheet_image(sh)
        x = col * (self.cell_w + 1)
        y = row * (self.cell_h + 1)
        return img.crop((x, y, x + self.cell_w, y + self.cell_h))


def build(cell_w, cell_h, baseline, linefeed, height, width, ascent,
          glyphs, encoding=1, font_type=1, alter_idx=0,
          sheet_w=256, sheet_h=256, fmt=4):
    """glyphs: ordered list of (codepoint, PIL 'LA' image cell_w x cell_h, (left, glyph_w, char_w)).
    Index 0 should be the fallback/space glyph."""
    n_cols = sheet_w // (cell_w + 1)
    n_rows = sheet_h // (cell_h + 1)
    per = n_cols * n_rows
    n_sheets = (len(glyphs) + per - 1) // per
    sheets = []
    for s in range(n_sheets):
        img = Image.new('LA', (sheet_w, sheet_h), (0, 0))
        for k in range(per):
            gi = s * per + k
            if gi >= len(glyphs):
                break
            row, col = divmod(k, n_cols)
            img.paste(glyphs[gi][1], (col * (cell_w + 1), row * (cell_h + 1)))
        sheets.append(tile(img, fmt))
    sheet_size = len(sheets[0])

    widths = [g[2] for g in glyphs]
    # CMAP: split into contiguous runs -> TABLE blocks; leftovers -> SCAN
    cmap_entries = sorted((cp, i) for i, (cp, _, _) in enumerate(glyphs))
    runs = []
    for cp, i in cmap_entries:
        if runs and cp == runs[-1][-1][0] + 1:
            runs[-1].append((cp, i))
        else:
            runs.append([(cp, i)])
    blocks = []
    scan = []
    for run in runs:
        if len(run) >= 8:
            blocks.append(('TABLE', run))
        else:
            scan.extend(run)
    if scan:
        blocks.append(('SCAN', sorted(scan)))

    # ---- serialize -----------------------------------------------------
    def blk(magic, body):
        size = 8 + len(body)
        pad = (-size) % 4
        return magic + struct.pack('<I', size + pad) + body + b'\0' * pad

    finf_off_ph = 0
    # build blocks after FINF to know offsets
    hdr_size = 0x14
    finf_size = 8 + 0x18
    tglp_off = hdr_size + finf_size            # offset of TGLP block start
    tglp_body_len = 0x18
    sheet_data_off = tglp_off + 8 + tglp_body_len
    # 0x80-align sheet data like the originals
    pad_to = (-sheet_data_off) % 0x80
    sheet_data_off += pad_to
    tglp_blk_size = 8 + tglp_body_len + pad_to + sheet_size * n_sheets

    cwdh_off = tglp_off + tglp_blk_size
    cwdh_body = struct.pack('<HHI', 0, len(widths) - 1, 0)
    for (l, g, c) in widths:
        cwdh_body += struct.pack('<bBb', l, g, c)
    cwdh_blk = blk(b'CWDH', cwdh_body)

    cmap_off = cwdh_off + len(cwdh_blk)
    cmap_blks = []
    off = cmap_off
    bodies = []
    for kind, run in blocks:
        if kind == 'TABLE':
            body = struct.pack('<HHHHI', run[0][0], run[-1][0], 1, 0, 0)
            body += b''.join(struct.pack('<H', i) for _, i in run)
        else:
            body = struct.pack('<HHHHI', run[0][0], run[-1][0], 2, 0, 0)
            body += struct.pack('<H', len(run))
            body += b''.join(struct.pack('<HH', cp, i) for cp, i in run)
        bodies.append(body)
    # patch next-offsets
    offs = []
    cur = cmap_off
    sizes = []
    for body in bodies:
        size = 8 + len(body)
        size += (-size) % 4
        sizes.append(size)
        offs.append(cur)
        cur += size
    out_cmaps = []
    for i, body in enumerate(bodies):
        nxt = offs[i + 1] + 8 if i + 1 < len(bodies) else 0
        body = body[:8] + struct.pack('<I', nxt) + body[12:]
        out_cmaps.append(blk(b'CMAP', body))

    finf = struct.pack('<BbH3BBIII4B', font_type, linefeed, alter_idx,
                       glyphs[0][2][0], glyphs[0][2][1], glyphs[0][2][2],
                       encoding, tglp_off + 8, cwdh_off + 8, cmap_off + 8,
                       height, width, ascent, 0)
    finf_blk = blk(b'FINF', finf)

    tglp_body = struct.pack('<4BIHHHHHHI', cell_w, cell_h, baseline, max(w[2] for w in widths),
                            sheet_size, n_sheets, fmt, n_cols, n_rows,
                            sheet_w, sheet_h, sheet_data_off)
    tglp_blk = b'TGLP' + struct.pack('<I', tglp_blk_size) + tglp_body + b'\0' * pad_to + b''.join(sheets)

    n_blocks = 2 + 1 + len(out_cmaps)
    total = hdr_size + len(finf_blk) + len(tglp_blk) + len(cwdh_blk) + sum(len(c) for c in out_cmaps)
    head = struct.pack('<4sHHIII', b'CFNT', 0xFEFF, hdr_size, 0x03000000, total, n_blocks)
    return head + finf_blk + tglp_blk + cwdh_blk + b''.join(out_cmaps)

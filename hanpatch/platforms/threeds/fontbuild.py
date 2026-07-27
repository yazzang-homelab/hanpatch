"""Build Korean-capable BCFNT fonts by extending the game's originals.

Existing glyph cells (latin/symbols) are copied verbatim from the original
font so ASCII rendering is untouched; Hangul syllables are rendered from a
TTF and appended.
"""
import os
import struct
import sys
from PIL import Image, ImageDraw, ImageFont

from hanpatch import config
from hanpatch.platforms.threeds import bcfnt
from hanpatch.platforms.threeds.bcfnt import Bcfnt, TILE_ORDER

# NeoDunggeunmo (OFL-1.1) is a 16px bitmap-style Hangul face. At the 18x19 /
# 14x16 cells this game uses, a 1-bit pixel face stays crisp and high-contrast,
# while an antialiased gothic turns to mush and reads as a grey blob.
def _ttf(default='fonts/neodgm.ttf'):
    """TTF to render the target script from, resolved against the project."""
    return config.p(config.prof('ttf') or default)


KO_TTF = None  # resolved per build from the profile ("ttf": "fonts/....ttf")
KO_TTF_AA = '/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf'
CRISP = True
# per-font (ttf, px size, crisp) — sizes are the cell-fitting sizes
FONT_SPEC = {
    'font_text': (KO_TTF, 16, True),
    'font_system': (KO_TTF, 14, True),
}


def font_spec():
    """FONT_SPEC with the profile's TTF and any per-font size overrides."""
    ttf = _ttf()
    spec = {}
    over = config.prof('font_px') or {}
    for name, (_, size, crisp) in FONT_SPEC.items():
        spec[name] = (ttf, int(over.get(name, size)), crisp)
    return spec

# The sheets are RGBA4444: A is ink coverage and RGB is a shading mask that the
# engine multiplies with the text colour. Measured from the shipped fonts:
#   font_text   -> RGB is white in the glyph body and dark on the antialiased
#                  rim, which is what gives English text its outlined look.
#   font_system -> RGB is white everywhere; only A carries the glyph.
# Writing 255-coverage instead (the naive "inverse") makes the body pure black
# and the rim light, i.e. a flat black glyph with no outline.
SHADE_LUT = {
    'font_text': [15, 9, 8, 7, 6, 5, 3, 3, 3, 4, 4, 6, 7, 10, 12, 15],
    'font_system': [15] * 16,
    'font_ruby': [15] * 16,
}


def ksx1001_syllables():
    """The 2350 KS X 1001 hangul syllables, in Unicode order."""
    import codecs
    out = []
    for cp in range(0xAC00, 0xD7A4):
        ch = chr(cp)
        try:
            b = ch.encode('euc-kr')
        except UnicodeEncodeError:
            continue
        if len(b) == 2 and 0xB0 <= b[0] <= 0xC8 and 0xA1 <= b[1] <= 0xFE:
            out.append(ch)
    return out


def sheet_rgba(f, i):
    raw = f.sheets[i]
    img = Image.new('RGBA', (f.sheet_w, f.sheet_h))
    px = img.load()
    p = 0
    for ty in range(0, f.sheet_h, 8):
        for tx in range(0, f.sheet_w, 8):
            for ox, oy in TILE_ORDER:
                v = raw[p] | (raw[p + 1] << 8)
                p += 2
                px[tx + ox, ty + oy] = (((v >> 12) & 15) * 17, ((v >> 8) & 15) * 17,
                                        ((v >> 4) & 15) * 17, (v & 15) * 17)
    return img


def original_glyphs(f):
    """-> (list of (codepoint, RGBA cell image, (l,g,c)), set of covered cps)"""
    cps = {}
    for s, e, mt, payload in f.cmap:
        if mt == 0:
            for c in range(s, e + 1):
                cps.setdefault(c, payload + (c - s))
        elif mt == 1:
            for c in range(s, e + 1):
                v = payload[c - s]
                if v != 0xFFFF:
                    cps.setdefault(c, v)
        else:
            for c, idx in payload:
                cps.setdefault(c, idx)
    per = f.n_cols * f.n_rows
    sheets = {}
    out = []
    for cp in sorted(cps, key=lambda c: cps[c]):
        idx = cps[cp]
        sh, rem = divmod(idx, per)
        row, col = divmod(rem, f.n_cols)
        if sh not in sheets:
            sheets[sh] = sheet_rgba(f, sh)
        x = col * (f.cell_w + 1)
        y = row * (f.cell_h + 1)
        cell = sheets[sh].crop((x, y, x + f.cell_w, y + f.cell_h))
        out.append((cp, cell, f.width_of(idx)))
    return out


def render_hangul(ch, font, cell_w, cell_h, baseline, px_size, lut=None,
                  crisp=False):
    """Render `ch` into an RGBA cell using the shipped font's own encoding:
    A = ink coverage, RGB = SHADE_LUT[coverage]."""
    lut = lut or SHADE_LUT['font_text']
    pad = 8
    tmp = Image.new('L', (cell_w + 2 * pad, cell_h + 2 * pad), 0)
    d = ImageDraw.Draw(tmp)
    d.text((pad, pad + baseline), ch, font=font, fill=255, anchor='ls')
    if crisp:
        # pixel fonts must stay 1-bit; antialiasing turns them to mush at 16px
        tmp = tmp.point(lambda v: 255 if v >= 110 else 0)
    bbox = tmp.getbbox()
    cov = Image.new('L', (cell_w, cell_h), 0)
    if bbox:
        x0, y0, x1, y1 = bbox
        w = x1 - x0
        h = y1 - y0
        # centre horizontally inside the advance box, keep vertical position
        crop = tmp.crop((x0, 0, x1, tmp.height))
        # target: ink starts at floor((advance - w)/2)
        adv = cell_w - 1
        dx = max(0, (adv - w) // 2)
        cov.paste(crop.crop((0, pad, w, pad + cell_h)), (dx, 0))
        gw = min(cell_w, dx + w)
        left = 0
    else:
        gw = 0
        left = 0
    shade = cov.point(lambda v: lut[min(15, v * 16 // 256)] * 17)
    rgba = Image.merge('RGBA', (shade, shade, shade, cov))
    return rgba, (left, gw, cell_w - 1)


def build_font(src_path, out_path, extra_chars, px_size, sheet_w=256, sheet_h=256,
               ttf=KO_TTF, lut=None, crisp=False):
    f = Bcfnt(open(src_path, 'rb').read())
    glyphs = original_glyphs(f)
    have = {cp for cp, _, _ in glyphs}
    font = ImageFont.truetype(ttf, px_size)
    added = 0
    for ch in extra_chars:
        cp = ord(ch)
        if cp in have:
            continue
        img, w = render_hangul(ch, font, f.cell_w, f.cell_h, f.baseline, px_size,
                               lut, crisp)
        glyphs.append((cp, img, w))
        have.add(cp)
        added += 1
    data = bcfnt.build(f.cell_w, f.cell_h, f.baseline, f.linefeed, f.height,
                       f.width, f.ascent, glyphs, encoding=f.encoding,
                       font_type=f.font_type, alter_idx=f.alter_idx,
                       sheet_w=sheet_w, sheet_h=sheet_h)
    open(out_path, 'wb').write(data)
    return len(glyphs), added, len(data)


def used_chars(paths=None):
    """Every codepoint that appears in the finished Korean text."""
    import glob
    import json
    import re
    out = set()
    files = set()
    for p in (paths or (config.out('tm.json'),)):
        files.update(glob.glob(p))
    files.update(glob.glob(config.out('tm_*.json')))
    for p in sorted(files):
        try:
            d = json.load(open(p))
        except (OSError, ValueError):
            continue
        for v in d.values():
            if isinstance(v, str):
                out.update(re.sub(r'<[^>\n]*>', '', v))
    out.discard('\n')
    return out


EXTRA_PUNCT = (list('　·…‘’“”―－∼※①②③④⑤⑥⑦⑧⑨「」『』〈〉《》【】')
               + [chr(c) for c in range(0x3131, 0x3164)])


def build_all(verbose=True):
    """Build every font in FONT_SPEC from the profile's source/target paths.

    Extra glyphs are added on demand: any non-ASCII character the finished
    translation actually uses but the base set lacks, so the ROM can never
    render a missing glyph.
    """
    chars = ksx1001_syllables()
    extra = list(EXTRA_PUNCT)
    have = set(chars) | set(extra)
    missing = sorted(c for c in used_chars() if c not in have and ord(c) > 0x7E)
    if missing and verbose:
        print(f'adding {len(missing)} extra glyphs used by the translation: '
              f'{"".join(missing[:60])}')
    extra += missing
    src = [config.p(x) for x in config.prof('font_src')]
    dst = [config.p(x) for x in config.prof('font_out')]
    by_name = {}
    for path in src:
        by_name[os.path.splitext(os.path.basename(path))[0]] = path
    out_by_name = {}
    for path in dst:
        out_by_name[os.path.splitext(os.path.basename(path))[0]] = path
    results = []
    for name, (ttf, size, crisp) in font_spec().items():
        if name not in by_name or name not in out_by_name:
            continue
        os.makedirs(os.path.dirname(out_by_name[name]), exist_ok=True)
        n, a, sz = build_font(by_name[name], out_by_name[name], chars + extra,
                              size, ttf=ttf, lut=SHADE_LUT[name], crisp=crisp)
        if verbose:
            print(f'{name}: {n} glyphs (+{a}), {sz} bytes')
        results.append({'font': name, 'glyphs': n, 'added': a, 'bytes': sz})
    return results


if __name__ == '__main__':
    build_all()

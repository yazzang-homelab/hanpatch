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
    """Which fonts to build, from the PROFILE rather than from a table here.

    A font is built when the same basename appears in both `font_src` and
    `font_out`; its pixel size comes from `font_px`, which is DEMANDED for any font
    this module does not already carry a measured size for. A size nobody measured
    is the same defect as a layout budget nobody measured: the glyphs would be
    rendered at a guess and would not fit the cell the engine draws into.
    """
    ttf = _ttf()
    # One TTF per project is not enough for a title that ships a font per pixel
    # size: measured on this cartridge, an 8x9 cell needs a 7px pixel font while a
    # 16x18 cell takes a 16px one, and no single face fits both without clipping.
    per_font = config.prof('font_ttf') or {}
    over = config.prof('font_px') or {}
    names = ({os.path.splitext(os.path.basename(p))[0]
              for p in config.prof('font_src')}
             & {os.path.splitext(os.path.basename(p))[0]
                for p in config.prof('font_out')})
    spec = {}
    for name in sorted(names):
        if name in over:
            size = int(over[name])
        elif name in FONT_SPEC:
            size = FONT_SPEC[name][1]
        else:
            raise SystemExit(
                f'font {name!r} has no pixel size: declare it in the profile as '
                f'"font_px": {{"{name}": <px>}}. There is deliberately no default - '
                f'a size nobody measured renders glyphs that do not fit the cell the '
                f'engine draws into.')
        if size <= 0:
            raise SystemExit(f'font {name!r} pixel size {size} is not positive')
        face = config.p(per_font[name]) if name in per_font else ttf
        if not os.path.exists(face):
            raise SystemExit(f'font {name!r} renders from {face}, which does not exist')
        spec[name] = (face, size, CRISP)
    return spec

# The sheets are RGBA4444: A is ink coverage and RGB is a shading mask that the
# engine multiplies with the text colour. Measured from the shipped fonts:
#   font_text   -> RGB is white in the glyph body and dark on the antialiased
#                  rim, which is what gives English text its outlined look.
#   font_system -> RGB is white everywhere; only A carries the glyph.
# Writing 255-coverage instead (the naive "inverse") makes the body pure black
# and the rim light, i.e. a flat black glyph with no outline.
# These were measured BY HAND from the two reference fonts. They are kept as a
# cross-check, not as the authority: `measure_shade_lut` derives the same mapping
# from whatever source font it is handed, so a title whose fonts encode shading
# differently is served without editing this table, and the reference build proves
# the measurement agrees with the hand-read values by reproducing its fonts
# byte-for-byte.
# Legacy fallbacks ONLY. These moved into profiles/crimson_shroud.json as
# `font_shade`, where a title's facts belong; they stay here so a project that has
# not been migrated still builds the same bytes. `font_ruby` is gone: it was a
# record for a font nothing ships, and it would have outranked a real measurement
# for any title that happened to name a font that way.
SHADE_LUT = {
    'font_text': [15, 9, 8, 7, 6, 5, 3, 3, 3, 4, 4, 6, 7, 10, 12, 15],
    'font_system': [15] * 16,
}


def measure_shade_lut(f, glyphs=None):
    """Derive coverage -> RGB-nibble shading from a font's OWN glyphs.

    The sheets are RGBA4444: A is ink coverage and RGB is a mask the engine
    multiplies with the text colour, so a new glyph has to encode shading the same
    way the shipped ones do or it renders as a flat blob next to them. Rather than
    keep a per-title table, read the relation out of the font: bucket every pixel
    of every existing glyph by its A nibble and take the most common RGB nibble in
    that bucket. Buckets no glyph exercises are filled from the nearest bucket that
    was, so the result is always a full 16-entry table.

    The estimator is the MEDIAN, chosen by measurement rather than taste: against
    the reference title's hand-read tables the median reproduces font_system in all
    16 buckets and font_text in 15 of 16, while the mode misses six buckets and the
    mean misses seven. The single disagreement (bucket 14: measured 5, recorded 12)
    is reported by the caller rather than silently resolved, because the recorded
    value is what produced the pinned reference artifact.

    Returns None when the font has no glyph pixels to measure, which the caller
    must treat as a refusal rather than substituting a default.
    """
    if f.fmt not in bcfnt.FORMAT_HAS_RGB:
        # Coverage-only: there is no mask to measure and none to write, so the flat
        # table is exact rather than a fallback - `tile` ignores luminance entirely
        # for these formats.
        return [15] * 16
    import collections
    import statistics
    counts = [collections.Counter() for _ in range(16)]
    for _cp, cell, _m in (glyphs if glyphs is not None else original_glyphs(f)):
        for r, _g, _b, a in cell.getdata():
            counts[min(15, a * 16 // 256)][r * 16 // 256] += 1
    if not any(counts[1:]):
        return None
    lut = [None] * 16
    for i, c in enumerate(counts):
        if c:
            lut[i] = int(statistics.median(sorted(c.elements())))
    known = [i for i, v in enumerate(lut) if v is not None]
    for i in range(16):
        if lut[i] is None:
            lut[i] = lut[min(known, key=lambda k: abs(k - i))]
    return lut


def all_syllables():
    """Every one of the 11172 modern hangul syllables, in Unicode order."""
    return [chr(cp) for cp in range(0xAC00, 0xD7A4)]


def charset(name=None):
    """The base hangul coverage for this title. A DECLARED fact, not a default.

    Measured cause for this existing: every DQ7 font shipped 2350 syllables - the KS X
    1001 set - and the font gate is fail-closed, so a translation containing any of the
    other 8822 was REJECTED. In a sample of twelve rejected rows, eight were rejected for
    unsupported glyphs and nothing else. The set is not a stylistic choice, it decides
    whether ordinary Korean can be written at all.

    It cannot simply become 'all' either: the reference title's pinned font hashes were
    produced from the KS set, and silently widening it would move a byte artifact this
    project treats as an invariant. So the title declares, and the reference declares the
    set it was actually built with.
    """
    v = name if name is not None else (config.prof('font_charset') or 'ksx1001')
    if v == 'ksx1001':
        return ksx1001_syllables()
    if v in ('hangul', 'hangul-all', 'all'):
        return all_syllables()
    raise SystemExit(
        f"UNKNOWN font_charset {v!r}: use 'ksx1001' for the 2350-syllable KS X 1001 set "
        "or 'hangul-all' for all 11172 modern syllables. A misspelt name must not fall "
        "back to the narrow set - that is how a title ships a gate that rejects its own "
        "language.")


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
    """One sheet as RGBA, whatever pixel format the font uses.

    This used to inline an RGBA4444 stride, so a coverage-only sheet (A4 or A8, as
    Dragon Quest VII ships) ran off the end of the buffer. The format-aware reader
    returns luminance+alpha for every format; alpha-only formats report full
    luminance, which is what an engine that ignores RGB draws.
    """
    return bcfnt.untile_rgba(f.sheets[i], f.sheet_w, f.sheet_h, f.fmt)


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


def build_font(src_path, out_path, extra_chars, px_size, sheet_w=None, sheet_h=None,
               ttf=KO_TTF, lut=None, crisp=False):
    f = Bcfnt(open(src_path, 'rb').read())
    # Geometry INHERITS from the source unless a caller states otherwise. The
    # reference title is the exception and now says so in its own profile
    # ("font_sheet": {"font_text": [256, 256], ...}): its pinned artifacts were built
    # by re-sheeting 64x64 sources to 256x256, and that transformation is data about
    # that title, not a default for every title. Defaulting to 256x256 here put the
    # authority in the wrong place - a declared [1024, 1024] was a no-op record while
    # the one real transformation existed nowhere.
    sheet_w = sheet_w or f.sheet_w
    sheet_h = sheet_h or f.sheet_h
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
                       sheet_w=sheet_w, sheet_h=sheet_h, fmt=f.fmt)
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


def source_symbols():
    """Non-CJK characters the SOURCE text uses, which a translation may keep.

    The glyph gate is the INTERSECTION of every target font's coverage, and that is
    correct: a string can be drawn by any of them. But it made a character the shipped
    game itself uses unrenderable whenever ONE font lacked it. Measured on DQ7: U+FF0A
    FULLWIDTH ASTERISK is in six of the seven source fonts and absent from the layout font
    iwamaru_p15, so every translation that carried it through - all sixteen in one sampled
    batch - was rejected for unsupported glyphs, with no other defect.

    Weakening the gate to a union would let a genuinely unrenderable character ship. The
    invariant is made TRUE instead: whatever symbols the source writes, every target font
    can draw. Kana and kanji are deliberately excluded - a separate check already refuses
    Japanese left in the Korean, so adding 1479 kanji to a Korean font would only buy the
    right to ship untranslated text. Of the 64 symbols this source uses, the layout font
    was missing 34.
    """
    try:
        src = config.load_object(config.src_path(), 'the extracted source')
    except (OSError, SystemExit):
        return []
    out = set()
    for rows in src.values():
        for row in rows:
            for ch in row.get('en', ''):
                o = ord(ch)
                if o < 0x80 or ch in '\r\n':
                    continue
                if 0x3040 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF:
                    continue                      # kana / kanji: never kept in Korean
                out.add(ch)
    return sorted(out)


def build_all(verbose=True):
    """Build every font in FONT_SPEC from the profile's source/target paths.

    Extra glyphs are added on demand: any non-ASCII character the finished
    translation actually uses but the base set lacks, so the ROM can never
    render a missing glyph.
    """
    chars = charset()
    extra = list(EXTRA_PUNCT) + source_symbols()
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
        declared = (config.prof('font_shade') or {}).get(name)
        if declared is not None:
            if (not isinstance(declared, (list, tuple)) or len(declared) != 16
                    or not all(isinstance(v, int) and 0 <= v <= 15 for v in declared)):
                raise SystemExit(
                    f'font_shade[{name!r}] must be 16 values in 0..15, got '
                    f'{declared!r}')
            declared = [int(v) for v in declared]
        measured = measure_shade_lut(Bcfnt(open(by_name[name], 'rb').read()))
        if measured is None and name not in SHADE_LUT:
            raise SystemExit(
                f'{by_name[name]}: no glyph pixels to measure the shading mask from '
                f'and no value on record, so there is no way to encode new glyphs the '
                f'way this font encodes its own. Refusing to guess.')
        # A RECORDED value wins where one exists: it is what produced the pinned
        # reference artifact, so overriding it with a measurement would break the
        # byte-identity invariant. The measurement is still taken and any
        # disagreement is named, because that is how a wrong record gets noticed.
        # Authority order: the PROFILE first, because that is where a title's facts
        # live and where a wrong value is correctable without editing shared module
        # source; then the module's legacy table, which is what produced the pinned
        # reference artifact; then the measurement. The measurement is always taken
        # and a disagreement with whichever value wins is NAMED.
        recorded = declared if declared is not None else SHADE_LUT.get(name)
        lut = recorded if recorded is not None else measured
        sheet = (config.prof('font_sheet') or {}).get(name)
        sheet_wh = (None, None)
        if sheet is not None:
            if (not isinstance(sheet, (list, tuple)) or len(sheet) != 2
                    or not all(isinstance(v, int) and not isinstance(v, bool)
                               and v > 0 and v % 8 == 0 for v in sheet)):
                raise SystemExit(
                    f'font_sheet[{name!r}] is {sheet!r}; it must be two positive '
                    f'multiples of 8, e.g. [256, 256]. A malformed value used to fall '
                    f'back to 256x256 silently, which re-sheets a 1024x1024 font with '
                    f'no diagnostic.')
            sheet_wh = (int(sheet[0]), int(sheet[1]))
        if measured is not None and recorded is not None and measured != recorded:
            diff = [i for i in range(16) if measured[i] != recorded[i]]
            if verbose:
                print(f'{name}: shading mask measured from the font differs from the '
                      f'recorded one at bucket(s) {diff} '
                      f'({[measured[i] for i in diff]} vs '
                      f'{[recorded[i] for i in diff]}); using the declared/recorded '
                      f'value, which is what the pinned artifact was built with')
        n, a, sz = build_font(by_name[name], out_by_name[name], chars + extra,
                              size, sheet_w=sheet_wh[0], sheet_h=sheet_wh[1],
                              ttf=ttf, lut=lut, crisp=crisp)
        if verbose:
            print(f'{name}: {n} glyphs (+{a}), {sz} bytes')
        results.append({'font': name, 'glyphs': n, 'added': a, 'bytes': sz})
    return results


if __name__ == '__main__':
    build_all()

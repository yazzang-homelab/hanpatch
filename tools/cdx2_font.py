#!/usr/bin/env python3
"""Bake Hangul glyphs into the title's font by retargeting cells it does not use.

    python3 tools/cdx2_font.py <extract dir> <work dir> [--ttf F] [--px N]
                               [--charset-from FILE] [--limit N]

The font maps a Shift-JIS code to a cell by the code's position in FONT.BIN, so
there are two ways to gain a cell and only one of them is free.

RETARGETING an existing entry changes nothing structural: the count, the order
and every code stay exactly as they were, and only the pixels in the cell
change. Whatever the engine's lookup does, it does the same thing afterwards.
The Korean text then travels through the game as the Shift-JIS codes of the
cells that were retargeted, which is what the emitted mapping is for.

APPENDING an entry would need the 347 empty cells to be described by new table
rows, and that is not free: it depends on whether the engine's lookup tolerates
codes that are out of order, which has not been measured. This tool does not
append. If a translation ever needs more than the cells retargeting provides,
measure the lookup first.

A cell may be retargeted only when nothing the game renders uses its character.
That set is built from text located STRUCTURALLY - script records and database
fields whose geometry the records themselves prove - never from scanning for
byte pairs that look like Shift-JIS. That scan was tried and is useless here:
binary data yields 8994 distinct legal-looking codes against 2725 real glyphs,
which would leave 3 reclaimable cells and no translation.
"""
import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hanpatch.platforms.psp import dsarc  # noqa: E402
from hanpatch.platforms.psp import font as fontmod  # noqa: E402
from hanpatch.platforms.psp import sdt  # noqa: E402

FONT_ARCHIVES = ('FONT1.ARC', 'FONT2.ARC')
DATABASE = 'DATABASE.DAT'
HEADERS = (4, 8, 16)
HANGUL_FIRST, HANGUL_LAST = 0xAC00, 0xD7A3


def field_at(record, off):
    end = record.find(b'\x00', off)
    if end < 0 or end == off:
        return None
    try:
        return record[off:end].decode('shift_jis')
    except UnicodeDecodeError:
        return None


def table_geometry(blob):
    """(header, stride, field offsets) that the records themselves prove."""
    if len(blob) < 8:
        return None
    count, = struct.unpack_from('<I', blob, 0)
    if not count:
        return None
    best = None
    for head in HEADERS:
        body = len(blob) - head
        if body <= 0 or body % count or body // count < 4:
            continue
        stride = body // count
        records = [blob[head + i * stride:head + (i + 1) * stride]
                   for i in range(count)]
        found = []
        for off in range(stride - 1):
            hits = jp = 0
            for r in records:
                s = field_at(r, off)
                if s is None:
                    continue
                hits += 1
                if any('\u3040' <= c <= '\u30ff' or '\u4e00' <= c <= '\u9fff'
                       for c in s):
                    jp += 1
            if hits >= count * 0.9 and jp >= count * 0.5:
                found.append(off)
        pruned = [o for i, o in enumerate(found)
                  if i == 0 or o - found[i - 1] > 1]
        if pruned and (best is None or len(pruned) > len(best[2])):
            best = (head, stride, pruned)
    return best


def database_characters(path):
    """Every character in a structurally located database field."""
    with open(path, 'rb') as fh:
        arc = dsarc.Dsarc(sdt.Sdt(fh.read()).payload)
    used = set()
    solved, unsolved = [], []
    for member in arc:
        blob = arc.read(member.name)
        geom = table_geometry(blob)
        if not geom:
            unsolved.append(member.name)
            continue
        head, stride, fields = geom
        count, = struct.unpack_from('<I', blob, 0)
        for i in range(count):
            record = blob[head + i * stride:head + (i + 1) * stride]
            for off in fields:
                s = field_at(record, off)
                if s:
                    used.update(s)
        solved.append(member.name)
    return used, solved, unsolved


def script_characters(work):
    path = os.path.join(work, 'text_src.json')
    if not os.path.isfile(path):
        raise SystemExit('no text_src.json in %s; run cdx2_corpus.py first' % work)
    with open(path) as fh:
        src = json.load(fh)
    used = set()
    for rows in src.values():
        for row in rows:
            used.update(row['en'])
    return used


def rasterise(ttf, px, cell, chars):
    """Render each character to a cell of 0..15 coverage values."""
    from PIL import Image, ImageDraw, ImageFont
    face = ImageFont.truetype(ttf, px)
    out = {}
    for ch in chars:
        img = Image.new('L', (cell, cell), 0)
        draw = ImageDraw.Draw(img)
        box = draw.textbbox((0, 0), ch, font=face)
        x = (cell - (box[2] - box[0])) // 2 - box[0]
        y = (cell - (box[3] - box[1])) // 2 - box[1]
        draw.text((x, y), ch, font=face, fill=255)
        out[ch] = bytes(v * 15 // 255 for v in img.tobytes())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('extract')
    ap.add_argument('work')
    ap.add_argument('--ttf', default='/mnt/ssd256/offloaded/root-tmp/'
                                     'fontquest/work/Galmuri11_Bold.ttf')
    ap.add_argument('--px', type=int, default=14)
    ap.add_argument('--charset-from',
                    help='a UTF-8 file whose Hangul syllables are the charset; '
                         'without it a provisional set is used and labelled so')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args(argv)

    script = script_characters(args.work)
    db, solved, unsolved = database_characters(
        os.path.join(args.extract, DATABASE))
    used = script | db
    print('used characters: script %d, database %d, union %d'
          % (len(script), len(db), len(used)))
    print('database members: %d solved, %d unsolved (%s)'
          % (len(solved), len(unsolved), ', '.join(unsolved[:6])))

    fonts = {}
    for name in FONT_ARCHIVES:
        with open(os.path.join(args.extract, name), 'rb') as fh:
            fonts[name] = fontmod.Font(fh.read())
    first = fonts[FONT_ARCHIVES[0]]
    spare = [g for g in first.glyphs
             if g.char is not None and g.char not in used]
    print('cells: %d total, %d filled, %d empty, %d retargetable'
          % (first.capacity, len(first.glyphs),
             first.capacity - len(first.glyphs), len(spare)))

    if args.charset_from:
        with open(args.charset_from, encoding='utf-8') as fh:
            text = fh.read()
        charset = sorted({c for c in text
                          if HANGUL_FIRST <= ord(c) <= HANGUL_LAST})
        provisional = False
    else:
        charset = [chr(c) for c in range(HANGUL_FIRST, HANGUL_FIRST + len(spare))]
        provisional = True
    if args.limit:
        charset = charset[:args.limit]
    if len(charset) > len(spare):
        raise SystemExit('charset needs %d cells, %d are retargetable; '
                         'reduce the translation or measure the engine lookup '
                         'before appending entries'
                         % (len(charset), len(spare)))
    print('charset: %d syllables%s'
          % (len(charset), ' (PROVISIONAL, not from a translation)'
             if provisional else ''))

    glyphs = rasterise(args.ttf, args.px, fontmod.CELL, charset)
    blank = sum(1 for v in glyphs.values() if not any(v))
    if blank:
        raise SystemExit('%d syllables rendered blank from %s'
                         % (blank, args.ttf))

    mapping = {}
    for name, f in fonts.items():
        pool = [g for g in f.glyphs if g.char is not None and g.char not in used]
        for ch, glyph in zip(charset, pool):
            f.write(glyph, glyphs[ch])
            mapping.setdefault(ch, glyph.code)
        out = os.path.join(args.work, name)
        with open(out, 'wb') as fh:
            fh.write(f.build())
        print('wrote %s (%d cells retargeted)' % (out, len(charset)))

    path = os.path.join(args.work, 'font_map.json')
    with open(path, 'w') as fh:
        json.dump({'provisional': provisional, 'ttf': args.ttf, 'px': args.px,
                   'cells_retargeted': len(charset),
                   'map': {c: mapping[c] for c in charset}},
                  fh, ensure_ascii=False, indent=1)
    print('wrote %s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())

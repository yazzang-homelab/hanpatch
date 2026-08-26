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
import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hanpatch import config  # noqa: E402
from hanpatch.platforms.psp import dbtbl  # noqa: E402
from hanpatch.platforms.psp import dsarc  # noqa: E402
from hanpatch.platforms.psp import eboot as ebootmod  # noqa: E402
from hanpatch.platforms.psp import font as fontmod  # noqa: E402
from hanpatch.platforms.psp import ldt  # noqa: E402
from hanpatch.platforms.psp import sdt  # noqa: E402

FONT_ARCHIVES = ('FONT1.ARC', 'FONT2.ARC')
DATABASE = 'DATABASE.DAT'
EBOOT_INPUT = 'EBOOT.elf'
EBOOT_FAMILY = 'eboot.elf'
OPENING = 'OPENING.LDT'
OPENING_FAMILY = 'asset__OPENING.LDT'
DB_PREFIX = 'db__'
HEADERS = (4, 8, 16)
HANGUL_FIRST, HANGUL_LAST = 0xAC00, 0xD7A3

#: Advance written onto every retargeted cell. The metrics field holds 1..15 and
#: the renderer steps scale*(advance+1), so 15 is exactly the 16px cell - the
#: full-width advance a Hangul syllable occupies. A retargeted cell inherits the
#: advance of the glyph it replaced, and once the retarget pool grew past
#: full-width kanji it started handing syllables 3px and 5px cells.
ADVANCE_FULL = 15

#: Shift-JIS lead bytes whose cells may be retargeted. A cell's CODE, not its
#: metrics, is what the engine's text reader uses to decide the glyph is
#: full-width, so a syllable written into a cell outside these rows renders
#: wrong however correct the bitmap and advance are.
#:
#: Measured on the title screen: every syllable that rendered correctly sat in
#: 0x88-0x91, and `기` - placed at 0x81F3 in the symbol row - rendered as a
#: sliver even with 96 inked pixels and advance 15. A single-byte code is worse
#: and needs no test: the cell is halfwidth by definition, so a 16px syllable is
#: clipped to 8px. The previous bake put 25 syllables in single-byte cells and
#: 101 more in the symbol, kana, Greek and Cyrillic rows.
#:
#: The kanji rows are the full Shift-JIS kanji planes, including the ones past
#: the standard range that this disc's own glyphs already occupy.
KANJI_LEADS = frozenset(range(0x88, 0xA0)) | frozenset(range(0xE0, 0xF0))


def fullwidth_kanji_code(code):
    """Is `code` a two-byte code in a row the reader draws full-width?"""
    return (code >> 8) in KANJI_LEADS


#: Sheets whose CLUT is PROVEN on the device to render `INK_INDEX` as white.
#:
#: Measured, not reasoned about. A diagnostic build put six sheet-0 syllables,
#: six sheet-3 syllables and six known-good sheet-2 syllables into the three
#: title-menu rows and the frame was read back: sheet 3 drew 388 white pixels,
#: sheet 2 drew 303, and sheet 0 drew ZERO - the row was blank, with only the
#: 48 pixels of menu-box frame that appear identically in all three rows.
#:
#: Sheet 0 is the one sheet whose shipped FONT2 glyphs ink at 2 rather than 1
#: (31894 pixels at 2 against 28 at 1), so index 1 there is a CLUT slot the
#: original art barely touches and the engine draws as nothing. Baking it anyway
#: cost 31 cells holding `가 거 것 게 건 걸 검 개 같` - among the most common
#: syllables in Korean, so the defect would have been visible in most sentences
#: while every gate reported the build clean: byte budgets, font coverage and
#: the encoder all pass on an invisible glyph.
#:
#: Sheets 4 and 5 are excluded because they are UNPROVEN, not because they are
#: known bad. Sheets 1-3 hold 1514 retargetable cells against 1069 syllables, so
#: there is no reason to spend an unmeasured sheet.
PROVEN_SHEETS = frozenset((1, 2, 3))


def retargetable(glyph, used):
    """May `glyph`'s cell be repainted as a Hangul syllable?

    Four conditions, each one a defect that shipped when it was missing: the
    cell must hold a character (an empty cell has no code the reader resolves),
    that character must not be one the patch still renders, the code must be
    a two-byte kanji-row code so the reader draws it full-width, and the cell
    must live on a sheet whose CLUT is proven to render the ink index visibly.
    """
    if glyph.char is None or glyph.char in used:
        return False
    if glyph.sheet not in PROVEN_SHEETS:
        return False
    return fullwidth_kanji_code(glyph.code)


def common_retargetable_codes(fonts, used):
    """Codes whose cells are safe to repaint in every runtime font archive."""
    pools = [
        {g.code for g in face.glyphs if retargetable(g, used)}
        for face in fonts
    ]
    return sorted(set.intersection(*pools)) if pools else []


def bitmap_digest(values):
    """Stable identity for the 16x16 coverage values a map entry claims."""
    return hashlib.sha256(bytes(values)).hexdigest()


def readback_mismatches(face, mapping, expected):
    """Mapped syllables whose rebuilt cell is not their rasterised bitmap."""
    by_code = {g.code: g for g in face.glyphs}
    bad = []
    for ch, code in mapping.items():
        glyph = by_code.get(code)
        if glyph is None or ch not in expected:
            bad.append(ch)
            continue
        if bytes(face.read(glyph)) != bytes(expected[ch]):
            bad.append(ch)
    return bad


#: The CLUT index that renders pure white, read off the device with solid
#: single-index blocks (see `rasterise`). Every other index in the low range is a
#: hue, so this is the only value a monochrome glyph may use.
INK_INDEX = 1

#: Share of a sheet's non-zero pixels the commonest value must hold for the
#: sheet to count as one-bit. Measured: FONT2 sheet 0 is 99.9% ink 2, while
#: every FONT1 sheet spreads its commonest value over barely a quarter.
INK_DOMINANCE = 0.95

#: Coverage at or above this becomes ink. A pixel font at its native size is
#: already almost binary, so the threshold only decides the handful of partial
#: pixels on a diagonal.
INK_THRESHOLD = 128


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


def uncovered_references(strings, owned):
    """Structural strings left after corpus-owned occurrences are removed."""
    counts = {}
    for text in owned:
        counts[text] = counts.get(text, 0) + 1
    out = []
    for text in strings:
        if counts.get(text, 0):
            counts[text] -= 1
        else:
            out.append(text)
    return out


def row_skipped(family, key, skip_keys):
    """True when this corpus row is a profile-declared skip slot.

    Profile `skip_keys` are the eboot name-entry palette and firmware dialogs,
    stored as leaf `off*` offsets. OPENING.LDT narration uses the same `off%x`
    scheme, so a global leaf match would hold back a prologue slot that shares
    an offset with a palette row. Leaf keys therefore bind only to `eboot.elf`;
    a qualified `family/key` still skips that exact row.
    """
    skip_keys = set(skip_keys)
    if '%s/%s' % (family, key) in skip_keys:
        return True
    return key in skip_keys and family == EBOOT_FAMILY


def database_characters(path, skip=()):
    """Every character in a structurally located database field.

    `skip` is preferably {member: [source occurrence, ...]}. Only those exact
    occurrences are owned by the corpus and may surrender their source cells;
    every other structural field still renders its original bytes. A legacy
    iterable of member names remains accepted for callers that already proved
    whole-member ownership, but the title build passes occurrence data because
    one covered member can still contain held-back symbol or Japanese fields.
    """
    with open(path, 'rb') as fh:
        arc = dsarc.Dsarc(sdt.Sdt(fh.read()).payload)
    used = set()
    covered = set(skip)
    solved, unsolved = [], []
    for member in arc:
        blob = arc.read(member.name)
        owned = ([row.jp for row in dbtbl.strings(blob)]
                 if member.name in covered else [])
        if isinstance(skip, dict):
            owned = list(skip.get(member.name, ()))
        geom = table_geometry(blob)
        if not geom:
            unsolved.append(member.name)
            continue
        head, stride, fields = geom
        count, = struct.unpack_from('<I', blob, 0)
        references = []
        for i in range(count):
            record = blob[head + i * stride:head + (i + 1) * stride]
            for off in fields:
                s = field_at(record, off)
                if s:
                    references.append(s)
        for text in uncovered_references(references, owned):
            used.update(text)
        solved.append(member.name)
    return used, solved, unsolved



def shipped_characters(work, skip_keys=()):
    """Every character the PATCHED game renders, per family, from the corpus.

    The preserve-set decides which cells may be retargeted, and basing it on the
    SOURCE is wrong once a domain is actually translated: it protects a cell for
    a kanji the patch has just deleted. Measured here - preserving the source of
    both text domains left 1493 free cells against a corpus that needs more,
    while the same corpus needs no cell for Japanese it no longer contains.

    So a row contributes what it SHIPS: its Korean when it has a translation,
    its source when it does not. Before anything is translated that is exactly
    the old source-based set, so this generalises the previous rule rather than
    loosening it. Rows that legitimately ship as source - asset identifiers the
    engine resolves - keep their cells by the same rule.
    """
    path = os.path.join(work, 'text_src.json')
    if not os.path.isfile(path):
        raise SystemExit('no text_src.json in %s; run cdx2_corpus.py first' % work)
    with open(path) as fh:
        src = json.load(fh)
    used = set()
    skip_keys = set(skip_keys)
    shipped = untranslated = 0
    for family, rows in src.items():
        tmpath = os.path.join(work, 'ko', 'tm_%s.json' % family)
        tmap = {}
        if os.path.isfile(tmpath):
            with open(tmpath) as fh:
                tmap = json.load(fh)
        # Database occurrence ownership is counted by corpus_db_sources /
        # database_characters. Those rows still feed the preserve-set, but the
        # shipped/source counters are the non-table text domains - script,
        # executable, prologue - so a skipped eboot palette row is not mixed
        # with an untranslated ITEM.DAT field in the same fixture.
        count_rows = not family.startswith(DB_PREFIX)
        for row in rows:
            en = row['en']
            key = row.get('key', '')
            ko = None if row_skipped(family, key, skip_keys) else tmap.get(en)
            if isinstance(ko, str) and ko:
                used.update(ko)
                if count_rows:
                    shipped += 1
            else:
                used.update(en)
                if count_rows:
                    untranslated += 1
    return used, shipped, untranslated


def corpus_db_members(work):
    """DATABASE.DAT member names the corpus carries text for."""
    path = os.path.join(work, 'text_src.json')
    if not os.path.isfile(path):
        return set()
    with open(path) as fh:
        src = json.load(fh)
    return {f[len('db__'):] for f in src if f.startswith('db__')}


def profile_input(name):
    """Resolve one declared project input, failing if its promise is broken."""
    declared = (config.prof('build_inputs') or {}).get(name)
    if not declared:
        return None
    path = config.p(declared)
    if not os.path.isfile(path):
        raise SystemExit('profile declares %s at %s, but the file is missing; '
                         'font cells cannot be reclaimed without scanning it'
                         % (name, path))
    return path


def corpus_eboot_offsets(work):
    """Executable string offsets already represented in the source corpus."""
    with open(os.path.join(work, 'text_src.json')) as fh:
        src = json.load(fh)
    out = set()
    for row in src.get(EBOOT_FAMILY, ()):
        key = str(row.get('key', ''))
        if not key.startswith('off'):
            continue
        try:
            out.add(int(key[3:], 16))
        except ValueError:
            continue
    return out


def unowned_eboot_characters(path, work):
    """Characters referenced by executable slots the corpus cannot rewrite."""
    with open(path, 'rb') as fh:
        refs = ebootmod.reference_strings(fh.read())
    owned = corpus_eboot_offsets(work)
    live = [(off, text) for off, _raw, text in refs if off not in owned]
    used = {ch for _off, text in live for ch in text}
    return used, len(refs), len(live)


def decode_extracted_asset(raw):
    """SDT-wrapped archive member or a plain payload."""
    try:
        return sdt.Sdt(raw).payload
    except sdt.SdtError:
        return raw


def corpus_opening_offsets(work):
    """Prologue string offsets already represented in the source corpus."""
    with open(os.path.join(work, 'text_src.json')) as fh:
        src = json.load(fh)
    out = set()
    for row in src.get(OPENING_FAMILY, ()):
        key = str(row.get('key', ''))
        if not key.startswith('off'):
            continue
        try:
            out.add(int(key[3:], 16))
        except ValueError:
            continue
    return out


def unowned_opening_characters(path, work):
    """Characters referenced by OPENING.LDT slots the corpus cannot rewrite."""
    with open(path, 'rb') as fh:
        refs = ldt.reference_strings(decode_extracted_asset(fh.read()))
    owned = corpus_opening_offsets(work)
    live = []
    for row in refs:
        try:
            off = int(row.key[3:], 16)
        except ValueError:
            live.append(row)
            continue
        if off not in owned:
            live.append(row)
    used = {ch for row in live for ch in row.jp}
    return used, len(refs), len(live)


def corpus_db_sources(work):
    """Source occurrences the database injector is allowed to replace."""
    path = os.path.join(work, 'text_src.json')
    with open(path) as fh:
        src = json.load(fh)
    out = {}
    for family, rows in src.items():
        if not family.startswith('db__'):
            continue
        out[family[len('db__'):]] = [
            row['en'] for row in rows if row.get('en')
        ]
    return out


def rasterise(ttf, px, cell, chars):
    """Render each character to a cell of 1-bit ink: 0 or INK_INDEX.

    NOT antialiased, and that is measured rather than stylistic. A cell value is
    an index into a 16-entry CLUT that the ENGINE uploads - it is not the
    coverage of an alpha ramp. Reading it back off the device with solid blocks
    of one index per cell:

        index  1 -> (255, 255, 255)   pure white
        index 14 -> (247, 247,   0)   yellow
        index 15 -> (247,   0, 247)   magenta

    The CLUT cycles hue, so an antialiased glyph does not fade at its edges - it
    paints them in different COLOURS. That is exactly what shipped: syllables
    rendered with magenta and cyan fringes around a yellow core while the
    Japanese text beside them was clean white, and no change to the palette
    stored in the sheet could affect it because the engine never reads that
    palette.

    One bit at the white index is therefore the only faithful bake. Nothing is
    lost: at its native size a pixel font like Galmuri produces two levels
    anyway, and the shipped Japanese glyphs are drawn from the same CLUT.
    """
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
        out[ch] = bytes(INK_INDEX if v >= INK_THRESHOLD else 0
                        for v in img.tobytes())
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

    skip_keys = config.prof('skip_keys') or ()
    used, n_ko, n_src = shipped_characters(args.work, skip_keys=skip_keys)
    covered = corpus_db_sources(args.work)
    db, solved, unsolved = database_characters(
        os.path.join(args.extract, DATABASE), skip=covered)
    used |= db

    eboot_path = profile_input(EBOOT_INPUT)
    eboot_used, eboot_refs, eboot_unowned = set(), 0, 0
    if eboot_path:
        eboot_used, eboot_refs, eboot_unowned = unowned_eboot_characters(
            eboot_path, args.work)
        used |= eboot_used

    opening_path = os.path.join(args.extract, OPENING)
    with open(os.path.join(args.work, 'text_src.json')) as fh:
        has_opening_corpus = OPENING_FAMILY in json.load(fh)
    if has_opening_corpus and not os.path.isfile(opening_path):
        raise SystemExit(
            'corpus carries %s but %s is missing; prologue leftovers cannot '
            'be preserved without scanning the asset' % (OPENING_FAMILY, opening_path))
    opening_used, opening_refs, opening_unowned = set(), 0, 0
    if os.path.isfile(opening_path):
        opening_used, opening_refs, opening_unowned = unowned_opening_characters(
            opening_path, args.work)
        used |= opening_used

    print('preserve-set from the shipped corpus: %d rows ship Korean, %d ship '
          'source, %d characters preserved (+%d database characters)'
          % (n_ko, n_src, len(used), len(db)))
    print('database members: %d solved, %d unsolved (%s)'
          % (len(solved), len(unsolved), ', '.join(unsolved[:6])))
    if eboot_path:
        print('executable references: %d Japanese C strings, %d outside the '
              'corpus, %d characters protected'
              % (eboot_refs, eboot_unowned, len(eboot_used)))
    if os.path.isfile(opening_path):
        print('opening references: %d Japanese C strings, %d outside the '
              'corpus, %d characters protected'
              % (opening_refs, opening_unowned, len(opening_used)))

    fonts = {}
    for name in FONT_ARCHIVES:
        with open(os.path.join(args.extract, name), 'rb') as fh:
            fonts[name] = fontmod.Font(fh.read())
    first = fonts[FONT_ARCHIVES[0]]
    spare = common_retargetable_codes(fonts.values(), used)
    print('cells: %d total, %d filled, %d empty, %d retargetable in the kanji '
          'area of every font'
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

    mapping = {ch: code for ch, code in zip(charset, spare)}
    for name, f in fonts.items():
        by_code = {g.code: g for g in f.glyphs}
        for ch, code in mapping.items():
            glyph = by_code.get(code)
            if glyph is None or not retargetable(glyph, used):
                raise SystemExit('%s cannot safely host %r at %#x'
                                 % (name, ch, code))
            f.write(glyph, glyphs[ch])
            # A cell carries its own metrics, and the cell now holds a Hangul
            # syllable rather than the glyph it was baked for. Leaving the old
            # advance behind is not cosmetic: the engine steps the cursor by
            # scale * (advance + 1) and clips to it, so a 16px syllable in the
            # cell that used to hold a halfwidth mark renders as a sliver.
            # Measured on the previous bake: 180 of 880 syllables clipped, the
            # worst at 3px of 16 - visible on the title screen as a menu entry
            # missing its last letter.
            glyph.advance = ADVANCE_FULL
            glyph.bearing = 0
        # The metrics are what the engine reads, so the check belongs on the
        # rebuilt font rather than on the objects just mutated.
        rebuilt = fontmod.Font(f.build())
        codes = {glyph.code for glyph in rebuilt.glyphs}
        by_code = {glyph.code: glyph for glyph in rebuilt.glyphs}
        drift = readback_mismatches(rebuilt, mapping, glyphs)
        if drift:
            raise SystemExit(
                '%d mapped cells in %s do not hold their claimed syllable (%s)'
                % (len(drift), name, ''.join(drift[:8])))
        clipped = sorted(ch for ch, code in mapping.items()
                         if code in codes
                         and by_code[code].advance != ADVANCE_FULL)
        if clipped:
            raise SystemExit(
                '%d retargeted cells in %s kept a narrow advance (%s): a '
                'syllable there renders clipped'
                % (len(clipped), name, ''.join(clipped[:8])))
        # The advance is only half of the width story. The code decides how the
        # renderer READS the byte pair, and a syllable handed a halfwidth or
        # symbol-row code is drawn narrow no matter what its metrics say.
        # A retargeted cell must hold ONLY the ink index the device proved is
        # white. Every other low index is a hue in the CLUT the engine uploads
        # for these sheets, so an antialiased edge is not a soft edge - it is a
        # coloured one. This check is what would have caught the rainbow menu
        # immediately.
        wrong = sorted(ch for ch, code in mapping.items()
                       if code in by_code
                       and set(v for v in rebuilt.read(by_code[code]) if v)
                       - {INK_INDEX})
        if wrong:
            raise SystemExit(
                '%d syllables in %s hold an index other than %d (%s): the '
                'engine draws those as an arbitrary colour'
                % (len(wrong), name, INK_INDEX, ''.join(wrong[:8])))
        stray = sorted(ch for ch, code in mapping.items()
                       if not fullwidth_kanji_code(code))
        if stray:
            raise SystemExit(
                '%d syllables in %s were given codes outside the fullwidth '
                'kanji rows (%s): the renderer draws those narrow'
                % (len(stray), name, ''.join(stray[:8])))
        # The code being full-width says the cell is drawn wide; it says nothing
        # about whether the SHEET's palette renders ink at all. Sheet 0 satisfies
        # every other check here and still draws nothing, so the sheet has to be
        # asserted on the rebuilt font rather than trusted from the pool filter.
        unproven = sorted(ch for ch, code in mapping.items()
                          if code in by_code
                          and by_code[code].sheet not in PROVEN_SHEETS)
        if unproven:
            raise SystemExit(
                '%d syllables in %s landed on a sheet whose palette is not '
                'proven to render ink (%s): a syllable there is invisible on '
                'the device while every other check passes'
                % (len(unproven), name, ''.join(unproven[:8])))
        out = os.path.join(args.work, name)
        with open(out, 'wb') as fh:
            fh.write(rebuilt.build())
        print('wrote %s (%d cells retargeted, advance set to %d)'
              % (out, len(charset), ADVANCE_FULL))

    path = os.path.join(args.work, 'font_map.json')
    with open(path, 'w') as fh:
        json.dump({'provisional': provisional, 'ttf': args.ttf, 'px': args.px,
                   'cells_retargeted': len(charset),
                   'bitmap_sha256': {c: bitmap_digest(glyphs[c])
                                     for c in charset},
                   'map': {c: mapping[c] for c in charset}},
                  fh, ensure_ascii=False, indent=1)
    print('wrote %s' % path)
    return 0


if __name__ == '__main__':
    sys.exit(main())

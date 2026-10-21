"""Cell-selection rules for the Classic Dungeon X2 Hangul bake.

Every case here is a defect that SHIPPED or nearly shipped, and each one passed
every other gate at the time: byte budgets, font coverage, the encoder and
`hanpatch verify` all report clean on a glyph that the device draws wrong or not
at all. Only reading pixels back off the emulator caught them, so the rules they
produced are pinned here rather than re-derived.

Run: python3 tests/test_cdx2_font.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import cdx2_font as cf  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def skip(name, why):
    SKIP.append(name)
    print('  skip ' + name + ' (' + why + ')')


class Cell:
    """The three fields `retargetable` reads off a real glyph."""

    def __init__(self, char, code, sheet):
        self.char, self.code, self.sheet = char, code, sheet


def test_fullwidth_rows():
    """Only two-byte kanji-row codes are drawn full-width.

    The first bake put 25 syllables in single-byte cells - halfwidth by
    definition, so a 16px syllable was clipped to 8px - and 101 more in the
    symbol, kana, Greek and Cyrillic rows, where `기` at 0x81F3 rendered as a
    sliver despite carrying 96 inked pixels and advance 15.
    """
    case('single-byte code is not fullwidth', not cf.fullwidth_kanji_code(0x41))
    case('symbol row 0x81 is not fullwidth', not cf.fullwidth_kanji_code(0x81F3))
    case('kana row 0x82 is not fullwidth', not cf.fullwidth_kanji_code(0x82A6))
    case('kanji row 0x88 is fullwidth', cf.fullwidth_kanji_code(0x88D8))
    case('kanji row 0x91 is fullwidth', cf.fullwidth_kanji_code(0x9140))
    # The disc's own glyphs occupy rows past the standard kanji range, so the
    # bake may use them; excluding them cost cells the corpus needed.
    case('extended row 0xE0 is fullwidth', cf.fullwidth_kanji_code(0xE040))
    case('row 0xA0 between the two planes is not fullwidth',
         not cf.fullwidth_kanji_code(0xA0B1))


def test_proven_sheets():
    """A syllable may only land on a sheet measured to render ink visibly.

    Sheet 0 is the one sheet whose shipped FONT2 glyphs ink at index 2 rather
    than 1, so index 1 there is a CLUT slot the original art barely touches. A
    diagnostic build put six sheet-0, six sheet-3 and six known-good sheet-2
    syllables into the three title-menu rows: sheet 3 drew 388 white pixels,
    sheet 2 drew 303, and sheet 0 drew ZERO. That cost 31 cells holding
    `가 거 것 게 건 걸 검 개 같` - among the commonest syllables in Korean.
    """
    case('sheet 0 is not proven', 0 not in cf.PROVEN_SHEETS)
    case('sheet 1 is proven', 1 in cf.PROVEN_SHEETS)
    case('sheet 2 is proven', 2 in cf.PROVEN_SHEETS)
    case('sheet 3 is proven', 3 in cf.PROVEN_SHEETS)
    # 4 and 5 are excluded for being UNMEASURED, not for being known bad.
    # Sheets 1-3 hold 1514 retargetable cells against 1069 syllables.
    case('sheet 4 is not proven', 4 not in cf.PROVEN_SHEETS)
    case('sheet 5 is not proven', 5 not in cf.PROVEN_SHEETS)


def test_retargetable():
    used = {'漢'}
    ok = Cell('物', 0x8AAA, 2)
    case('a filled fullwidth cell on a proven sheet is retargetable',
         cf.retargetable(ok, used))
    case('an empty cell is refused',
         not cf.retargetable(Cell(None, 0x8AAA, 2), used))
    case('a character the patch still renders is refused',
         not cf.retargetable(Cell('漢', 0x8AAA, 2), used))
    case('a single-byte cell is refused',
         not cf.retargetable(Cell('A', 0x41, 2), used))
    case('a symbol-row cell is refused',
         not cf.retargetable(Cell('・', 0x8145, 2), used))
    # The defect this file exists for: every other field is valid and only the
    # sheet is wrong, so nothing but a device read would have caught it.
    case('an unproven sheet is refused even when the code is fullwidth',
         not cf.retargetable(Cell('物', 0x8AAA, 0), used))
    case('unproven sheet 4 is refused',
         not cf.retargetable(Cell('物', 0x8AAA, 4), used))


def test_ink_index():
    """Coverage values are CLUT indices, not an alpha ramp.

    Read off the device with solid single-index blocks: index 1 is
    (255,255,255), index 14 is (247,247,0) yellow, index 15 is (247,0,247)
    magenta. An antialiased 0..14 glyph therefore paints a hue cycle rather than
    grey, which is why the menu rendered yellow-cored against the white the
    Japanese draws.
    """
    case('ink index is the white slot', cf.INK_INDEX == 1)
    case('ink index is not a hue', cf.INK_INDEX not in (14, 15))
    case('threshold binarises coverage', 0 < cf.INK_THRESHOLD < 255)
    case('dominance demands a near-pure sheet', cf.INK_DOMINANCE >= 0.9)


def test_advance():
    """A retargeted cell must be given the fullwidth advance.

    A cell keeps the advance of the glyph it replaced unless it is set, and the
    replaced kanji advances vary: 180 of 880 syllables clipped, the worst at 3px
    of a 16px cell.
    """
    case('fullwidth advance is set', cf.ADVANCE_FULL == 15)


def _bake_font():
    """The TTF and pixel size a real bake used, or (None, None).

    Read from a project's own `font_map.json` rather than hard-coded: the bake
    records what it used, so the test measures the actual configuration instead
    of one a reader of this file guessed. `HANPATCH_TTF` overrides for a box that
    has the font somewhere else.
    """
    env = os.environ.get('HANPATCH_TTF', '')
    if env and os.path.exists(env):
        return env, int(os.environ.get('HANPATCH_TTF_PX', '14'))
    for cand in ('/mnt/ssd256/hanpatch-cdx2/work/font_map.json',
                 os.path.join(ROOT, 'work', 'font_map.json')):
        if not os.path.exists(cand):
            continue
        try:
            with open(cand) as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        ttf, px = doc.get('ttf'), doc.get('px')
        if ttf and px and os.path.exists(ttf):
            return ttf, int(px)
    return None, None


def test_rasterise_is_one_bit():
    """The bake must emit only 0 or INK_INDEX - never an intermediate value.

    This is the check that would have caught the colour defect at bake time. An
    antialiased glyph spreads coverage over indices 0..14, and this title's CLUT
    for those sheets is a SEVEN-HUE CYCLE rather than an alpha ramp - index 14 is
    yellow, 15 magenta - so partial coverage renders as colour, not as a paler
    white. Every gate passed on it: the bytes were right, the charset was
    covered, and the glyph was legible. Only the rendered pixels were wrong.
    """
    ttf, px = _bake_font()
    if not ttf:
        skip('rasterise: one bit per pixel', 'no bake font on this box')
        return
    cells = cf.rasterise(ttf, px, 16, '가나다한글')
    vals = set()
    for buf in cells.values():
        vals.update(bytes(buf))
    case('rasterise emits only 0 and the ink index',
         vals <= {0, cf.INK_INDEX})
    case('rasterise actually inked something', cf.INK_INDEX in vals)
    # A cell that is all ink or all background means the rasteriser produced a
    # block or a blank, both of which look like "a glyph" to every later check.
    solid = [ch for ch, buf in cells.items()
             if len(set(bytes(buf))) < 2]
    case('every rasterised cell has both ink and background', not solid)


def test_map_readback():
    """A map is valid only when every runtime font holds its claimed bitmap."""

    class Face:
        def __init__(self, glyphs, cells=None):
            self.glyphs = glyphs
            self.cells = cells or {}

        def read(self, glyph):
            return self.cells[glyph.code]

    first = Face([
        Cell('甲', 0x8840, 2),
        Cell('乙', 0x8841, 2),
        Cell('丙', 0x8842, 2),
    ])
    second = Face([
        Cell('甲', 0x8840, 2),
        Cell('丙', 0x8842, 2),
        Cell('丁', 0x8843, 2),
    ])
    case('map uses only codes safe in both runtime fonts',
         cf.common_retargetable_codes((first, second), set())
         == [0x8840, 0x8842])

    expected = {
        '가': bytes([1]) * 256,
        '나': bytes([2]) * 256,
        '다': bytes([3]) * 256,
    }
    mapping = {'가': 0x8840, '나': 0x8841, '다': 0x8842}
    glyphs = [
        Cell('甲', 0x8840, 2),
        Cell('乙', 0x8841, 2),
        Cell('丙', 0x8842, 2),
    ]
    drifted = Face(glyphs, {
        0x8840: expected['가'],
        0x8841: expected['가'],
        0x8842: expected['다'],
    })
    case('a rank-early cell is rejected while its neighbours remain valid',
         cf.readback_mismatches(drifted, mapping, expected) == ['나'])

    corrected = Face(glyphs, {
        0x8840: expected['가'],
        0x8841: expected['나'],
        0x8842: expected['다'],
    })
    case('an exact map-to-bitmap assignment passes',
         not cf.readback_mismatches(corrected, mapping, expected))


def test_source_reference_ownership():
    """Corpus ownership is per occurrence, and skipped rows still ship source."""
    remaining = cf.uncovered_references(
        ['決定', '決定', '情報'], ['決定'])
    case('corpus ownership removes occurrences rather than whole members',
         remaining == ['決定', '情報'])
    used = set(''.join(remaining))
    case('a still-referenced kanji cell is not retargetable',
         not cf.retargetable(Cell('決', 0x8840, 2), used))

    import tempfile
    with tempfile.TemporaryDirectory() as work:
        os.makedirs(os.path.join(work, 'ko'))
        source = {
            'eboot.elf': [
                {'key': 'off248bd3', 'en': 'かな特', 'jp': ''},
            ],
            'db__ITEM.DAT': [
                {'key': 'r0f0', 'en': '決定', 'jp': ''},
            ],
        }
        with open(os.path.join(work, 'text_src.json'), 'w') as fh:
            json.dump(source, fh, ensure_ascii=False)
        case('database ownership mirrors extracted source occurrences',
             cf.corpus_db_sources(work)
             == {'ITEM.DAT': ['決定']})
        with open(os.path.join(work, 'ko', 'tm_eboot.elf.json'), 'w') as fh:
            json.dump({'かな特': '가'}, fh, ensure_ascii=False)
        preserved, shipped, untranslated = cf.shipped_characters(
            work, skip_keys={'off248bd3'})
    case('a skipped palette row preserves source despite stale TM',
         set('かな特') <= preserved and '가' not in preserved)
    case('a skipped palette row is accounted as source',
         shipped == 0 and untranslated == 1)


def main():
    print('fullwidth rows')
    test_fullwidth_rows()
    print('proven sheets')
    test_proven_sheets()
    print('cell selection')
    test_retargetable()
    print('map readback')
    test_map_readback()
    print('source ownership')
    test_source_reference_ownership()
    print('ink')
    test_ink_index()
    test_advance()
    print('rasteriser')
    test_rasterise_is_one_bit()
    print('\n%d passed, %d failed, %d skipped'
          % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        for name in FAIL:
            print('  FAIL ' + name)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

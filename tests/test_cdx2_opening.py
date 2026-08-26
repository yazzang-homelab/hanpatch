"""Classic Dungeon X2 prologue extraction and LDT writeback regressions.

The script record `OPENING.LDT` is an operand. The six narration lines the
player reads live in that asset's payload and must be extracted separately.

Run: python3 tests/test_cdx2_opening.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.adapters.cdx2 import (  # noqa: E402
    ClassicDungeonX2, OPENING_FAMILY, opening_rows,
)
from hanpatch.platforms.psp import ldt, sdt  # noqa: E402

PASS, FAIL = [], []

# SYNTHETIC narration, not the game's script. This repository is PUBLIC and the
# title's text is not ours to publish; the shapes are what the parser cares about,
# so the fixture reproduces them - six lines, mixed kana and kanji, the wide space
# the asset uses as a word break, corner brackets, the three-dot ellipsis and the
# em dash - without carrying one line of the original.
PROLOGUE = (
    'いろいろな 記号が、',
    'いろいろな 場所から あつまり、',
    'しかし、「この」 見本文には 「意味」が なかった。',
    '読んだら さいご、にどと 戻れない・・・',
    'そんな 見本文に、今日も ひとつ、',
    'あらたに 加えられた行が あった――',
)


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def raises(exc, fn, *args):
    try:
        fn(*args)
    except exc:
        return True
    except Exception:
        return False
    return False


def payload(lines=PROLOGUE):
    out = bytearray(b'\x01\x02\x00')
    out += b'DICANM6090\x00'                 # operand, not narration
    out += '見本'.encode('shift_jis') + b'\x00'  # kanji-only binary lookalike
    for text in lines:
        out += text.encode('shift_jis') + b'\x00'
    out += b'\xff\x00'
    return bytes(out)


def test_extracts_the_asset_not_the_operand():
    rows = opening_rows(payload())
    case('six narration lines are emitted', [row.jp for row in rows] == list(PROLOGUE))
    case('the corpus family names the asset container',
         OPENING_FAMILY == 'asset__OPENING.LDT')
    case('keys are stable payload offsets', all(row.key.startswith('off') for row in rows))
    case('budgets are the exact source slot lengths',
         all(row.budget == len(row.jp.encode('shift_jis')) for row in rows))
    case('ASCII handles and kanji-only cells are not writable narration',
         all(row.jp not in ('DICANM6090', '見本') for row in rows))
    refs = [row.jp for row in ldt.reference_strings(payload())]
    case('a kanji-only LDT cell still owns its font cells',
         '見本' in refs)
    case('writable narration remains a subset of referenced strings',
         all(line in refs for line in PROLOGUE))


def test_whole_cells_and_writeback():
    whole = b'%s ' + '見本データ'.encode('shift_jis') + b'\x00'
    got = ldt.strings(whole)
    case('an ASCII prefix remains part of the whole C string',
         len(got) == 1 and got[0].jp == '%s 見本データ')

    blob = payload()
    rows = opening_rows(blob)
    first, second = rows[0], rows[1]
    replacement = '記号が、'.encode('shift_jis')
    rebuilt, applied = ldt.build(blob, {first.key: replacement})
    case('one LDT slot is rewritten', applied == 1)
    case('the asset length is unchanged', len(rebuilt) == len(blob))
    case('readback uses the stable offset after translation',
         ldt.stored(rebuilt, [first.key])[first.key] == replacement)
    case('the following narration slot is untouched',
         ldt.stored(rebuilt, [second.key])[second.key]
         == second.jp.encode('shift_jis'))
    case('an overlong replacement is refused',
         raises(ldt.LdtError, ldt.build, blob,
                {first.key: b'X' * (first.budget + 1)}))
    case('an unknown offset is refused',
         raises(ldt.LdtError, ldt.build, blob, {'offdead': b'X'}))


def test_six_lines_are_a_floor_not_an_optional_domain():
    case('five lines fail closed',
         raises(ldt.LdtError, opening_rows, payload(PROLOGUE[:5])))


class Entry:
    def __init__(self, compressed, decompressed=0):
        self.compressed = compressed
        self.decompressed = decompressed


class Fs:
    def __init__(self, blob, entry):
        self.blob = blob
        self.entry = entry

    def find(self, _name):
        return self.entry

    def read(self, _name):
        return self.blob


def test_compressed_and_plain_assets_share_one_payload_reader():
    raw = payload()
    case('plain LDT payload is passed through',
         ClassicDungeonX2._asset_payload(Fs(raw, Entry(False)), 'x.LDT')
         == (None, raw))

    padded = raw + b'\x00' * (-len(raw) % 16)
    wrapped = sdt.build(padded, stride=16, rows=4)
    box, decoded = ClassicDungeonX2._asset_payload(
        Fs(wrapped, Entry(True, len(padded))), 'x.LDT')
    case('compressed LDT payload is decoded structurally', decoded == padded)
    case('an unchanged compressed payload rebuilds identically',
         box.build(decoded) == wrapped)


def main():
    print('opening extraction')
    test_extracts_the_asset_not_the_operand()
    print('slot writeback')
    test_whole_cells_and_writeback()
    test_six_lines_are_a_floor_not_an_optional_domain()
    print('container wrapper')
    test_compressed_and_plain_assets_share_one_payload_reader()
    print('\n%d passed, %d failed' % (len(PASS), len(FAIL)))
    if FAIL:
        for name in FAIL:
            print('  FAIL ' + name)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

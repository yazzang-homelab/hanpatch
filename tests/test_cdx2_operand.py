"""An engine identifier is not a line, and translating one crashes the game.

`dsf.scan` cannot tell a filename operand from dialogue: both are a
length-prefixed Shift-JIS string sitting inline in the bytecode. The Korean
build of 2026-08-20 translated 29 of them - `OPENING.LDT`, `ANMPARTY.LDT`,
`ANMVITER.LDT` - and New Game died a few seconds after the BGM prompt with a
write through a null pointer, because the loader was asked for a member that does
not exist. Bisected to one record: chunk 966, `story\\demo1001_00.dsf` @130,
`OPENING.LDT`; reverting that single record makes the prologue play.

The rule is membership of the archive's own name list. These cases pin why it
cannot be a shape rule instead: every ASCII-only spelling such a rule would have
to catch is also legal player text on this disc.

Run: python3 tests/test_cdx2_operand.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.adapters.cdx2 import is_operand  # noqa: E402

#: A slice of the real DATA.DAT name list, including the three that shipped
#: translated.
ASSETS = {
    'OPENING.LDT', 'ANMPARTY.LDT', 'ANMVITER.LDT', 'ANIME000.IDT',
    'W_RM11.DAT', 'BAR_ED.MPB', 'SCRIPT.SDT', 'DATABASE.DAT',
    'STAFFROLL.TXT', 'FONT1.ARC',
}

#: Player text that a shape rule would have swallowed. `3`, `HP/MP` and `...`
#: are records the corpus really holds, and a held-back record ships Japanese.
TEXT = (
    'サンプルワード',
    'またあとで～。',
    'つづきは あとで 見せるよ！',
    '3',
    'ＯＫ',
    'Lv. 99',
    'HP/MP',
    '...',
    'yes?',
    'OPENING',              # the name without its extension is not a member
    'opening.ldt',          # the loader's names are exact; case is not a match
    'story\\demo1001_00.dsf',   # a chunk name, not an archive member
)

PASS, FAIL = [], []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


print('a record naming an archive member is an operand')
for text in sorted(ASSETS):
    case('operand %r' % text, is_operand(text, ASSETS))

print('everything else is player text')
for text in TEXT:
    case('text %r' % text, not is_operand(text, ASSETS))

print('an empty name list holds nothing back')
case('no assets', not is_operand('OPENING.LDT', set()))

print('%d passed, %d failed' % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)

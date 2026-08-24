"""How DATABASE.DAT text is found, pinned against the ways it was found wrong.

Every case here is a defect that SHIPPED in the Korean build of 2026-08-20: 398
strings still Japanese and 274 rendered as Hangul garbage, none of them
mistranslated - all of them invisible to the reader. The blobs are synthetic but
each one reproduces the exact shape that hid real text.

Run: python3 tests/test_dbtbl_fields.py
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import dbtbl  # noqa: E402

PASS, FAIL = [], []


def case(name, ok, detail=None):
    (PASS if ok else FAIL).append(name)
    note = '' if ok or detail is None else '  <- %r' % (detail,)
    print(('  ok   ' if ok else '  FAIL ') + name + note)


def sjis(text):
    return text.encode('cp932')


def member(count, stride, records, header=16):
    """A record member: u32 count, header padding, then fixed-stride records."""
    blob = bytearray(struct.pack('<I', count) + b'\x00' * (header - 4))
    for rec in records:
        padded = rec + b'\x00' * (stride - len(rec))
        assert len(padded) == stride, (len(rec), stride)
        blob += padded
    return bytes(blob)


def field(text, width):
    raw = sjis(text)
    return raw + b'\x00' * (width - len(raw))


print('a numeric field with no zero byte must not swallow the text behind it')
# MONSTER.DAT: `01 00 b8 0b` sits in front of every monster name. A walk that
# skips to the next NUL read `b8 0b 83 68 …`, failed, and lost all 295 names.
rec = lambda name, desc: (b'\x01\x00\xb8\x0b' + field(name, 20) + field(desc, 40))
blob = member(3, 64, [rec('サンプルネコA', 'ぬすむ。'),
                      rec('サンプルネコB', 'とっしんする。'),
                      rec('サンプルネコC', 'かたい。')])
found = {s.text for s in dbtbl.record_slots(blob)}
case('monster names are found', 'サンプルネコB' in found, sorted(found)[:4])
case('descriptions are found too', 'かたい。' in found, sorted(found)[:4])

print('text behind a signed id is found, and the id is not written over')
# DIFFICULTY.DAT: s16 id then `DEFが99`. Reading from rel 0 decodes in a third of
# the records because the id is often a valid lead byte; rel 2 decodes in all.
recs = [struct.pack('<h', -99 + i) + field('DEFが%d' % (99 - i), 30)
        for i in range(6)]
blob = member(6, 32, recs)
slots = dbtbl.record_slots(blob)
case('the string is read from the text field',
     all((s.offset - 16) % 32 == 2 for s in slots),
     [s.offset for s in slots])
case('every record is seen', len(slots) == 6, len(slots))

print('a misaligned shape must lose to an aligned one that explains as much')
# CHAR.DAT: header 4 / stride 159 explained eight strings and so did header 16 /
# stride 156, but the first read three bytes into every one of them, and picking
# by count alone took it: `サンプルン` came out as `[トン`.
recs = [b'\x01\x00\xd0\x07\x00\x07\x01\x00' + field('ヴィッダー', 20)
        + field('マノアカズの 管理人。', 100),
        b'\x02\x00\xda\x07\x00\x07\x02\x00' + field('サンプルン', 20)
        + field('見本語の説明文。', 100)]
blob = member(2, 128, recs)
texts = {s.text for s in dbtbl.record_slots(blob)}
case('names are whole', 'サンプルン' in texts, sorted(texts))
case('no shifted duplicate', not any(t.startswith('[') for t in texts), sorted(texts))
case('slot count equals string count', len(dbtbl.record_slots(blob)) == 4,
     len(dbtbl.record_slots(blob)))

print('the NEC row decodes, because this disc writes with it')
# `弱点②④⑥` is on every armour description. Python's shift_jis cannot read the
# circled digits; cp932 can. 267 strings were dropped by that one choice.
blob = member(1, 48, [field('もえそうなよろい　弱点④', 48)])
case('circled digits survive',
     dbtbl.record_slots(blob) and '④' in dbtbl.record_slots(blob)[0].text,
     [s.text for s in dbtbl.record_slots(blob)])

print('a short mixed line is a line')
# The old ratio test wanted Japanese to outnumber the rest 3:1 and dropped these.
for text in ('SP+30マナ15増', 'CRT+5 弱点②', 'DEFが99'):
    blob = member(1, 48, [field(text, 48)])
    case('kept %r' % text, bool(dbtbl.record_slots(blob)))

print('bytes that only look like text are not offered')
# `劔>"` came out of a level curve and `\x80迄` out of an effect table. The proof
# they are not text: `" > < \\ ^ | ` { } ~ @ # ;` occur zero times across the
# 11279 strings this project has already translated.
for raw in (b'\x8ek>"', b'\x80\x8f}', b'\x94\x7f'):
    case('rejected %r' % raw, dbtbl._text(raw) is None, repr(dbtbl._text(raw)))

print('a budget never reaches into the next field')
blob = member(2, 64, [field('なまえ', 16) + field('せつめい', 48),
                      field('あ', 16) + field('い', 48)])
slots = dbtbl.record_slots(blob)
byoff = {s.offset: s for s in slots}
first = min(byoff)
case('short name keeps its own field only',
     all(s.budget < 16 or s.offset % 64 != 16 for s in slots),
     [(s.offset, s.budget) for s in slots])
# Hangul reaches the disc as retargeted Shift-JIS cells, so a write is just
# bytes here: the point is that it lands and that the neighbouring field keeps
# its own text.
written = dbtbl.build(blob, {'off%d' % first: b'\x88\x9f\x88\xa0'})
case('a write lands', written != blob)
case('the neighbouring field is intact',
     'せつめい' in {s.text for s in dbtbl.record_slots(written)},
     sorted(s.text for s in dbtbl.record_slots(written)))

print('rebuilding with no edits returns the same bytes')
case('records', dbtbl.build(blob, {}) == blob)

print('%d passed, %d failed' % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)

"""What a stranger's patch tells us about our own ROM.

Run:  python3 tests/test_patchprobe.py

The fixture is synthetic because the claim under test is not "this works on
Dragon Quest VII" - it is "when a translator moves text, the diff says so, and
when nothing structural happened, the diff does not invent structure". A
synthetic image is the only one where we know the right answer in advance, so
a false positive here cannot hide behind a real ROM's complexity.

The negative cases matter more than the positive ones. A probe that labels
everything a pointer table would pass a test suite made only of pointer
tables, and would then send the fingerprinter chasing noise on every title.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch import patchprobe  # noqa: E402

PASS = []
FAIL = []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def sec(title):
    print()
    print(title)


# --------------------------------------------------------------- the fixture

HEADER = 0x000
TABLE = 0x100
TEXT = 0x200
FONT = 0x1300
END = 0x2300

ENTRIES = 64

JP = ['ぼうけんのしょをえらんでください', 'つよさをみる', 'そうびをととのえる',
      'まほうをつかう', 'どうぐをつかう', 'たたかう', 'にげる', 'ためる']
KO = ['모험의 서를 선택해 주세요', '강함을 봅니다', '장비를 갖춥니다',
      '마법을 사용한다', '도구를 사용한다', '싸운다', '도망친다', '힘을 모은다']


def _build(strings, encoding, font_fill):
    """One image, laid out the way a cartridge lays one out.

    Header, then a pointer table, then the strings it points at, then a font.
    Sixty-four unchanged bytes sit between the text and the font so the
    segmenter is not asked to guess where one ends - real images pad to
    alignment for the same reason.
    """
    image = bytearray(END)
    image[HEADER:TABLE] = bytes(range(0x40)) * 4

    blob = bytearray()
    offsets = []
    for i in range(ENTRIES):
        offsets.append(TEXT + len(blob))
        blob += strings[i % len(strings)].encode(encoding) + b'\x00'
    assert TEXT + len(blob) < FONT - 64, 'fixture text overruns the font'

    for i, off in enumerate(offsets):
        image[TABLE + i * 4:TABLE + (i + 1) * 4] = off.to_bytes(4, 'little')
    image[TEXT:TEXT + len(blob)] = blob

    for i in range(FONT, END):
        image[i] = font_fill(i - FONT)
    return bytes(image)


OLD = _build(JP, 'shift_jis', lambda i: (i * 7) & 0x1F)
NEW = _build(KO, 'euc-kr', lambda i: (i * 7) & 0x7F)

DOC = patchprobe.probe(OLD, NEW, rom_id='fixture/synthetic/one',
                       source={'host': 'example.invalid', 'licence': None})
BY_LABEL = {}
for r in DOC['regions']:
    BY_LABEL.setdefault(r['label'], []).append(r)


def covering(offset):
    for r in DOC['regions']:
        if r['start'] <= offset < r['end']:
            return r
    return None


# ------------------------------------------------------------------ the cases

sec('the diff finds the three structures a translation must touch')
case('the images are the same size, so offsets mean what they say',
     DOC['alignment'] == 'same_size')
case('the pointer table is found where it was put',
     (covering(TABLE) or {}).get('label') == 'pointer_table')
case('the text bank is found where it was put',
     (covering(TEXT) or {}).get('label') == 'text_bank')
# The font's first bytes survive the patch - a few cells happen to hold the
# same values in both images - so the region starts a little inside the block.
# Asking for the exact offset would be asking the diff to know a boundary it
# cannot see, so this asks only that the font is found inside the font.
font = next((r for r in DOC['regions']
             if r['label'] == 'font' and FONT <= r['start'] < END), None)
case('the font is found inside the font block', font is not None)
case('the unchanged header is not a region at all', covering(0) is None)

sec('the pointer table is read, not guessed')
table = covering(TABLE)
ev = (table or {}).get('evidence', {})
case('it recovers the width the fixture used', ev.get('width') == 4)
case('it recovers the endianness the fixture used', ev.get('endian') == 'little')
case('it counts every entry', ev.get('entries') == ENTRIES)
case('it sees that the targets moved',
     ev.get('shifted_fraction', 0) > patchprobe.MIN_SHIFTED_FRACTION)
case('it proves the table by where the pointers aim, not by their shape',
     ev.get('targets_in_changed_fraction') == 1.0)
case('it reclaims the unmoved first entry the diff never saw',
     ev.get('unmoved_head_entries') == 1
     and ev.get('moved_entries') == ENTRIES - 1)
case('the reclaimed table starts at the real table, not at the first diff',
     (table or {}).get('start') == TABLE)
case('the table stops where the text begins',
     (table or {}).get('end') == TEXT)
case('the first target is the start of the text bank',
     ev.get('first_target') == TEXT)

case('one merged region is split into the two structures it held',
     [r['label'] for r in DOC['regions'][:2]] == ['pointer_table', 'text_bank']
     and DOC['regions'][0]['end'] == DOC['regions'][1]['start'])

sec('the text bank is a language flip, not just a change')
text = covering(TEXT)
tev = (text or {}).get('evidence', {})
case('it measured Japanese before', tev.get('source_script_rate', 0) > 0.25)
case('it measured Korean after', tev.get('target_script_rate', 0) > 0.25)
case('it checked that Korean was not already there',
     tev.get('target_script_rate_before', 1) < tev.get('source_script_rate', 0))

sec('nothing is asserted as knowledge')
case('every region is asserted, never higher',
     all(r['status'] == patchprobe.STATUS for r in DOC['regions']))
case('the document says a measurement has not happened yet',
     DOC['provenance']['independent_measurement'] is False)
case('the document records which of our dumps it came from',
     len(DOC['provenance']['old_sha256']) == 64)

sec('their bytes do not leave the machine that applied the patch')
leaks = patchprobe._no_payload(DOC)
case('the emitted document holds no bytes from either image', leaks == [])
case('the guard would catch a leak if somebody added one',
     patchprobe._no_payload({'regions': [{'raw': b'\x00\x01'}]})
     == ['/regions/0/raw'])
case('probe refuses to return a document carrying payload',
     'contains_third_party_bytes' in DOC['provenance']
     and DOC['provenance']['contains_third_party_bytes'] is False)

sec('a probe that learned little says so')
case('the unexplained byte count is reported',
     'unexplained_bytes' in DOC['summary'])
case('the summary counts every label, including the empty ones',
     set(DOC['summary']['labels']) == set(patchprobe.LABELS))
case('bytes changed is the sum of the regions',
     DOC['summary']['bytes_changed']
     == sum(r['length'] for r in DOC['regions']))

sec('noise is not structure')
noise_old = bytes((i * 31 + 7) & 0xFF for i in range(0x4000))
noise_new = bytearray(noise_old)
for i in range(0x1000, 0x1400):
    noise_new[i] = (noise_new[i] + 0x11) & 0xFF
noise = patchprobe.probe(noise_old, bytes(noise_new), rom_id='fixture/noise/one',
                         source={'host': 'example.invalid'})
case('a random rewrite produces no pointer table',
     all(r['label'] != 'pointer_table' for r in noise['regions']))
case('a random rewrite produces no text bank',
     all(r['label'] != 'text_bank' for r in noise['regions']))
case('it is reported as unexplained rather than dropped',
     noise['summary']['unexplained_bytes'] > 0)

sec('a table too short to be evidence is refused')
short_old = bytearray(0x400)
short_new = bytearray(0x400)
for i in range(4):
    short_old[0x100 + i * 4:0x104 + i * 4] = (0x200 + i * 8).to_bytes(4, 'little')
    short_new[0x100 + i * 4:0x104 + i * 4] = (0x200 + i * 12).to_bytes(4, 'little')
short = patchprobe.probe(bytes(short_old), bytes(short_new),
                         rom_id='fixture/short/one', source={})
case('four ascending numbers are not a pointer table',
     all(r['label'] != 'pointer_table' for r in short['regions']))

sec('a table nobody moved is not a translation artefact')
still = bytearray(OLD)
still[TEXT] = (still[TEXT] + 1) & 0xFF
same = patchprobe.probe(OLD, bytes(still), rom_id='fixture/still/one', source={})
case('an unmoved table is not reported', 'pointer_table' not in
     {r['label'] for r in same['regions']})

sec('a rebuild is refused, not diffed at the wrong offsets')
grown = patchprobe.probe(OLD, OLD + b'\x00' * 16, rom_id='fixture/grown/one',
                         source={})
case('a size change is named', grown['alignment'] == 'size_changed')
case('no regions are invented from a shifted image', grown['regions'] == [])
try:
    patchprobe.segment(OLD, OLD + b'\x00')
    refused = False
except ValueError:
    refused = True
case('segment refuses a mismatched pair outright rather than truncating',
     refused)

sec('the output is a search order, not an answer')
cands = patchprobe.candidates(DOC)
case('the pointer table becomes a candidate', len(cands) == 1)
case('the candidate carries the shape, not the contents',
     set(cands[0]) == {'at', 'width', 'endian', 'stride', 'entries',
                       'confidence', 'status'})
case('candidates stay asserted', cands[0]['status'] == patchprobe.STATUS)
case('nothing is a candidate when nothing was found',
     patchprobe.candidates(noise) == [])

sec('two runs agree')
again = patchprobe.probe(OLD, NEW, rom_id='fixture/synthetic/one',
                         source={'host': 'example.invalid', 'licence': None})
case('the same pair produces the same document', again == DOC)


print()
print('%d passed, %d failed' % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)

"""Tests for the script table/data pair and its inline text records.

The synthetic cases build a pair byte by byte, so the record shape, the 16-byte
chunk alignment, and the rewrite arithmetic are proved without a disc. The
corpus cases parse the real pair and rebuild it, which is the only evidence that
the two length relations hold across every string the title ships rather than
across the handful a synthetic file would contain.

Run: python3 tests/test_dsf.py
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import dsarc  # noqa: E402
from hanpatch.platforms.psp import dsf  # noqa: E402
from hanpatch.platforms.psp import sdt  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def skip(name, why):
    SKIP.append(name)
    print('  skip ' + name + ' (' + why + ')')


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


def record(text):
    return struct.pack('<HH', len(text) + 3, len(text)) + text + b'\x00'


def pair(chunks):
    """Build a (table, data) pair from [(id, name_field, chunk bytes)]."""
    data = bytearray()
    offsets = []
    for _, _, body in chunks:
        offsets.append(len(data))
        data += body
        data += b'\x00' * (-len(data) % dsf.ALIGN)
    table = bytearray(struct.pack('<I', len(chunks)) + b'\x00' * 12)
    for (id_, field, _), off in zip(chunks, offsets):
        table += struct.pack('<II', id_, off) + field
    return bytes(table), bytes(data)


def field(name, tail=b''):
    raw = name.encode('ascii') + b'\x00'
    return raw + tail + b'\x00' * (dsf.NAME - len(raw) - len(tail))


# ------------------------------------------------------------------ records

def test_records():
    body = b'\x17\x09' + record(b'ABCDEF') + b'\x0a\x00\x04' \
        + record(b'GH') + b'\x00\x01'
    table, data = pair([(7, field('a.dsf'), body)])
    s = dsf.Script(table, data)

    recs = s.records()
    case('both records are found', [r.text for r in recs] == [b'ABCDEF', b'GH'])
    case('a record is located by chunk and offset',
         recs[0].chunk == 0 and recs[0].start == 2)
    case('the key names the chunk and the offset', recs[0].key == '0:2')

    case('the size field is the length plus three',
         struct.unpack_from('<H', body, 2)[0] ==
         struct.unpack_from('<H', body, 4)[0] + 3)

    # a string's own bytes must not produce a second, overlapping match
    case('records do not overlap', len(recs) == 2)

    case('an unterminated record is not a record',
         dsf.scan(struct.pack('<HH', 9, 6) + b'ABCDEF' + b'\x01') == [])
    case('a size that disagrees with the length is not a record',
         dsf.scan(struct.pack('<HH', 12, 6) + b'ABCDEF\x00') == [])
    case('a record holding a NUL is not a record',
         dsf.scan(struct.pack('<HH', 9, 6) + b'AB\x00DEF\x00') == [])
    case('illegal Shift-JIS is not a record',
         dsf.scan(struct.pack('<HH', 7, 4) + b'\x81\xff\x81\xff\x00') == [])


def test_rebuild():
    chunks = [(1, field('a.dsf'), b'\x01\x02' + record(b'AAAA')),
              (2, field('b.dsf'), record(b'BB') + b'\x99'),
              (3, field('a.dsf'), b'no text here')]
    table, data = pair(chunks)
    s = dsf.Script(table, data)

    t2, d2 = s.build()
    case('rebuilding with no edits reproduces the table', t2 == table)
    case('rebuilding with no edits reproduces the data', d2 == data)
    case('a chunk with no records is carried through',
         s.chunk_bytes(2).startswith(b'no text here'))
    case('chunks that share a name are separate chunks',
         [c.name for c in s.chunks] == ['a.dsf', 'b.dsf', 'a.dsf'])
    case('the id is a key, not the index', [c.id for c in s.chunks] == [1, 2, 3])

    # every chunk begins on a 16-byte boundary
    case('chunks are 16-byte aligned',
         all(c.offset % dsf.ALIGN == 0 for c in s.chunks))


def test_edits():
    chunks = [(1, field('a.dsf'), b'\x01\x02' + record(b'AAAA') + b'\x03'),
              (2, field('b.dsf'), record(b'BB'))]
    table, data = pair(chunks)
    s = dsf.Script(table, data)
    first = s.records()[0]

    grown = s.build({first.key: b'X' * 40})
    back = dsf.Script(*grown)
    case('a longer replacement survives the rebuild',
         back.records()[0].text == b'X' * 40)
    case('the length fields are rewritten to match',
         struct.unpack_from('<HH', back.chunk_bytes(0), first.start)
         == (43, 40))
    case('the following chunk moves and is still intact',
         back.records()[1].text == b'BB')
    case('the bytecode around a replacement is untouched',
         back.chunk_bytes(0).startswith(b'\x01\x02')
         and back.chunk_bytes(0)[first.start + 4 + 40 + 1] == 3)

    shrunk = dsf.Script(*s.build({first.key: b'Z'}))
    case('a shorter replacement survives too',
         shrunk.records()[0].text == b'Z')
    case('chunks stay aligned after a length change',
         all(c.offset % dsf.ALIGN == 0 for c in shrunk.chunks))

    case('an edit naming no record is refused, not ignored',
         raises(dsf.ScriptError, s.build, {'9:99': b'X'}))
    case('a replacement holding a NUL is refused',
         raises(dsf.ScriptError, s.build, {first.key: b'A\x00B'}))


def test_damage():
    table, data = pair([(1, field('a.dsf'), record(b'AA'))])

    case('a table too short for a header is refused',
         raises(dsf.ScriptError, dsf.Script, b'\x01\x00', data))

    empty = bytearray(table)
    struct.pack_into('<I', empty, 0, 0)
    case('a table declaring no chunks is refused',
         raises(dsf.ScriptError, dsf.Script, bytes(empty), data))

    liar = bytearray(table)
    struct.pack_into('<I', liar, 0, 999)
    case('a chunk count the table cannot hold is refused',
         raises(dsf.ScriptError, dsf.Script, bytes(liar), data))

    past = bytearray(table)
    struct.pack_into('<I', past, dsf.HEADER + 4, len(data) + 64)
    case('a chunk offset past the data file is refused',
         raises(dsf.ScriptError, dsf.Script, bytes(past), data))


def test_name_field_tail():
    """The bytes after the name are data, not padding, and must survive."""
    table, data = pair([(1, field('a.dsf', b'\x00\x01\x01'), record(b'AA'))])
    s = dsf.Script(table, data)
    case('a name with a non-zero tail still reads as the name',
         s.chunks[0].name == 'a.dsf')
    case('the tail after the name survives a rebuild', s.build()[0] == table)


# ------------------------------------------------------------------- corpus

def test_corpus():
    root = os.environ.get('HANPATCH_PSP_EXTRACT')
    path = os.path.join(root or '', 'SCRIPT.SDT')
    if not root or not os.path.isfile(path):
        skip('corpus: the real script pair rebuilds byte for byte',
             'set HANPATCH_PSP_EXTRACT to a directory of extracted files')
        return
    with open(path, 'rb') as fh:
        arc = dsarc.Dsarc(sdt.Sdt(fh.read()).payload)
    total = 0
    for tname, dname in (('SCRIPT.TBL', 'SCRIPT.DAT'),
                         ('AISCRIPT.TBL', 'AISCRIPT.DAT')):
        table, data = arc.read(tname), arc.read(dname)
        s = dsf.Script(table, data)
        built = s.build()
        case('corpus: %s rebuilds byte for byte' % tname, built[0] == table)
        case('corpus: %s rebuilds byte for byte' % dname, built[1] == data)
        recs = s.records()
        total += len(recs)
        case('corpus: every %s record obeys both length relations' % dname,
             all(struct.unpack_from('<HH', s.chunk_bytes(r.chunk), r.start)
                 == (len(r.text) + 3, len(r.text)) for r in recs))
        case('corpus: every %s record is legal Shift-JIS' % dname,
             all(_decodes(r.text) for r in recs))
        print('  corpus: %s has %d chunks and %d records'
              % (dname, len(s), len(recs)))
    case('corpus: the script holds records at all', total > 0)


def _decodes(raw):
    try:
        raw.decode('shift_jis')
        return True
    except UnicodeDecodeError:
        return False


def main():
    print('records')
    test_records()
    print('rebuild')
    test_rebuild()
    test_edits()
    test_damage()
    test_name_field_tail()
    print('corpus')
    test_corpus()
    print('\n%d passed, %d failed, %d skipped'
          % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        for name in FAIL:
            print('  FAIL ' + name)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

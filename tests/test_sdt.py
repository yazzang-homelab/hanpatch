"""Tests for the two container layers above the IMY codec.

Synthetic first: archives and containers this file builds byte by byte, so the
alignment rules, the straddling payload, and the refusals are proved without a
disc. Then a corpus layer that parses the real file and rebuilds it, which is
the only thing that proves the alignment constants are the game's and not ours.
It is skipped, and reported as skipped, when the extract is absent.

Run: python3 tests/test_sdt.py
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import dsarc  # noqa: E402
from hanpatch.platforms.psp import imy  # noqa: E402
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


# ------------------------------------------------------------------- dsarc

def test_dsarc():
    members = [('FIRST.DAT', b'a' * 700), ('sub\\SECOND.TBL', b'b' * 12),
               ('THIRD.DAT', b'')]
    blob = dsarc.build(members)
    arc = dsarc.Dsarc(blob)

    case('archive round trips through build and parse',
         [(m.name, arc.read(m.name)) for m in arc] == members)
    case('member order is the order given', arc.names() ==
         ['FIRST.DAT', 'sub\\SECOND.TBL', 'THIRD.DAT'])
    case('a backslash in a name is kept, not rewritten',
         'sub\\SECOND.TBL' in arc.names())
    case('a zero-length member is a member, not an absence',
         arc.read('THIRD.DAT') == b'')

    case('every member starts on a 512-byte boundary',
         all(m.offset % dsarc.ALIGN == 0 for m in arc))
    case('the table is padded out before the first member',
         arc.members[0].offset >= dsarc.HEADER + len(members) * dsarc.ENTRY)
    case('sizes are exact, so padding is never part of a member',
         arc.members[0].size == 700)
    case('the archive itself is padded to the alignment',
         len(blob) % dsarc.ALIGN == 0)

    case('rebuilding a parsed archive reproduces it',
         dsarc.build(arc.contents(), arc.reserved) == blob)


def test_dsarc_damage():
    good = dsarc.build([('A.DAT', b'x' * 8)])

    case('a file without the magic is refused',
         raises(dsarc.DsarcError, dsarc.Dsarc, b'DSARC XX' + good[8:]))
    case('a file shorter than the header is refused',
         raises(dsarc.DsarcError, dsarc.Dsarc, good[:12]))

    liar = bytearray(good)
    struct.pack_into('<I', liar, 8, 9999)
    case('a member count the file cannot hold is refused',
         raises(dsarc.DsarcError, dsarc.Dsarc, bytes(liar)))

    past = bytearray(good)
    struct.pack_into('<I', past, dsarc.HEADER + dsarc.NAME, len(good) * 2)
    case('a member running past the archive is refused',
         raises(dsarc.DsarcError, dsarc.Dsarc, bytes(past)))

    case('a name too long for the field is refused on build',
         raises(dsarc.DsarcError, dsarc.build, [('N' * 40 + '.DAT', b'')]))


# --------------------------------------------------------------------- sdt

def test_sdt():
    stride, rows = 16, 4
    span = stride * rows
    payload = bytes((i * 7 + (i // 16)) & 0xFF for i in range(span * 2 + 32))
    blob = sdt.build(payload, stride, rows)
    box = sdt.Sdt(blob)

    case('payload survives the split and rejoin', box.payload == payload)
    case('the payload is cut into full blocks plus a remainder',
         len(box) == 3)
    case('every block but the last holds a whole grid',
         [h.height for h in box.headers] == [rows, rows, 2])
    case('geometry is reported from the first block',
         (box.stride, box.rows) == (stride, rows))
    case('rebuilding reproduces the container', box.build() == blob)

    case('blocks start where the directory says',
         all(blob[o:o + 4] == imy.MAGIC for o in box.offsets))
    case('every block starts 4-byte aligned',
         all(o % 4 == 0 for o in box.offsets))
    case('the directory sits immediately before the first block',
         box.offsets[0] == sdt.HEADER + len(box) * 4)

    # a payload that straddles a boundary must still come back whole
    arc = dsarc.build([('BIG.DAT', bytes(range(256)) * 6),
                       ('SMALL.DAT', b'tail')])
    box2 = sdt.Sdt(sdt.build(arc, 16, 4))
    case('an archive straddling many blocks parses after rejoining',
         dsarc.Dsarc(box2.payload).read('SMALL.DAT') == b'tail')
    case('a straddling archive would not parse from one block alone',
         len(box2) > 1)

    grown = payload + b'\x00' * (stride * 3)
    case('a longer payload is re-cut into more blocks',
         len(sdt.Sdt(box.build(grown))) == 4)
    case('a re-cut container still yields the new payload',
         sdt.Sdt(box.build(grown)).payload == grown)


def test_sdt_damage():
    good = sdt.build(b'x' * 64, 16, 4)

    case('a file too short for a directory is refused',
         raises(sdt.SdtError, sdt.Sdt, b'\x01\x00\x00\x00'))

    empty = bytearray(good)
    struct.pack_into('<I', empty, 0, 0)
    case('a directory declaring no blocks is refused',
         raises(sdt.SdtError, sdt.Sdt, bytes(empty)))

    liar = bytearray(good)
    struct.pack_into('<I', liar, 0, 9999)
    case('a block count the file cannot hold is refused',
         raises(sdt.SdtError, sdt.Sdt, bytes(liar)))

    moved = bytearray(good)
    struct.pack_into('<I', moved, sdt.HEADER, 4096)
    case('a first block that is not where the directory ends is refused',
         raises(sdt.SdtError, sdt.Sdt, bytes(moved)))

    case('a payload tail that is not whole rows is refused',
         raises(sdt.SdtError, sdt.build, b'x' * 65, 16, 4))
    case('a non-positive geometry is refused',
         raises(sdt.SdtError, sdt.build, b'', 0, 4))


# ------------------------------------------------------------------ corpus

def test_corpus():
    root = os.environ.get('HANPATCH_PSP_EXTRACT')
    path = os.path.join(root or '', 'SCRIPT.SDT')
    if not root or not os.path.isfile(path):
        skip('corpus: the real container rebuilds byte for byte',
             'set HANPATCH_PSP_EXTRACT to a directory of extracted files')
        return
    with open(path, 'rb') as fh:
        data = fh.read()
    box = sdt.Sdt(data)
    case('corpus: the container rebuilds byte for byte', box.build() == data)
    arc = dsarc.Dsarc(box.payload)
    case('corpus: the payload is an archive that parses', len(arc) > 0)
    case('corpus: the archive rebuilds byte for byte',
         dsarc.build(arc.contents(), arc.reserved) == box.payload)
    case('corpus: no member runs past the payload',
         all(m.offset + m.size <= len(box.payload) for m in arc))
    print('  corpus: %d blocks, %d bytes of payload, %d members: %s'
          % (len(box), len(box.payload), len(arc), ', '.join(arc.names())))


def main():
    print('dsarc')
    test_dsarc()
    test_dsarc_damage()
    print('sdt')
    test_sdt()
    test_sdt_damage()
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

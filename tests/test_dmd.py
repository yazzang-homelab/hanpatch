"""Tests for the `.DMD` container reader.

Every case builds a container byte by byte, so the header relations and the
fail-closed rules are proved without a disc. That matters here: a reader whose
only evidence is a synthetic fixture can pass while returning nothing from the
real asset, so the disc-side evidence for this module is recorded in `dmd.py` as
a measurement (96 of 96 members decode, each to the length PSPFS records,
1,201,348 bytes in total) and re-checked by `hanpatch extract`, not asserted
here where no ROM is available.

Run: python3 tests/test_dmd.py
"""
import gzip
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import dmd  # noqa: E402

PASS, FAIL, SKIP = [], [], []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc:
        return True
    except Exception:
        return False
    return False


def build(payloads, kind=dmd.KIND, offsets=None):
    """A well-formed container over `payloads`, or a broken one via `offsets`."""
    members = [gzip.compress(p, mtime=0) for p in payloads]
    table_end = dmd.HEADER + len(members) * 4
    if offsets is None:
        offsets = []
        at = table_end
        for member in members:
            offsets.append(at)
            at += len(member)
    head = struct.pack('<II', len(members), kind)
    head += struct.pack('<%dI' % len(offsets), *offsets)
    return head + b''.join(members)


def test_header():
    blob = build([b'A' * 40])
    count, kind, offsets = dmd.parse_header(blob)
    case('a single-block header reads back its count and kind',
         count == 1 and kind == dmd.KIND)
    case('the block starts after the offset table',
         offsets == [dmd.HEADER + 4])

    blob = build([b'A' * 40, b'B' * 40, b'C' * 40])
    count, _kind, offsets = dmd.parse_header(blob)
    case('three blocks give three ascending offsets',
         count == 3 and offsets == sorted(offsets) and len(set(offsets)) == 3)

    case('kind is carried, not validated',
         dmd.parse_header(build([b'x' * 8], kind=0xdeadbeef))[1] == 0xdeadbeef)


def test_payload_is_concatenated():
    parts = [b'first-', b'second-', b'third']
    case('the payload is every block in order',
         dmd.decode(build(parts)) == b''.join(parts))
    case('blocks() keeps the split visible',
         dmd.blocks(build(parts)) == parts)
    # A record straddling a boundary is the reason decode() concatenates: the
    # halves are meaningless alone and only the join carries the string.
    halves = [b'\x82\xbf\x82\xaa AAA', b'BBB \x82\xbf\x82\xaa']
    joined = dmd.decode(build(halves))
    case('a value split across two blocks survives the join',
         b'AAABBB' in joined)


def test_expect_guards_the_decoder():
    blob = build([b'z' * 100])
    case('a matching expected size is accepted',
         dmd.decode(blob, expect=100) == b'z' * 100)
    case('a mismatched expected size is refused',
         raises(dmd.DmdError, dmd.decode, blob, 99))


def test_fails_closed():
    case('an empty buffer is refused',
         raises(dmd.DmdError, dmd.parse_header, b''))
    case('a zero block count is refused',
         raises(dmd.DmdError, dmd.parse_header,
                struct.pack('<III', 0, dmd.KIND, 12)))
    case('an offset table past the end is refused',
         raises(dmd.DmdError, dmd.parse_header,
                struct.pack('<II', 4, dmd.KIND) + b'\x00' * 4))

    good = build([b'q' * 30])
    case('an offset inside the offset table is refused',
         raises(dmd.DmdError, dmd.parse_header,
                build([b'q' * 30], offsets=[dmd.HEADER])))
    case('an offset past the end is refused',
         raises(dmd.DmdError, dmd.parse_header,
                build([b'q' * 30], offsets=[len(good) + 1])))
    case('a descending offset pair is refused',
         raises(dmd.DmdError, dmd.parse_header,
                build([b'a' * 20, b'b' * 20], offsets=[80, 40])))

    # The header can be right and the body still be another format.
    body = build([b'w' * 20])
    _c, _k, offs = dmd.parse_header(body)
    broken = bytearray(body)
    broken[offs[0]:offs[0] + 2] = b'ZZ'
    case('a block without gzip magic is refused, not skipped',
         raises(dmd.DmdError, dmd.decode, bytes(broken)))

    broken = bytearray(body)
    broken[offs[0] + 12] ^= 0xff
    case('a corrupt deflate stream is refused, not truncated',
         raises(dmd.DmdError, dmd.decode, bytes(broken)))


def main():
    print('header')
    test_header()
    print('payload')
    test_payload_is_concatenated()
    print('expected size')
    test_expect_guards_the_decoder()
    print('fails closed')
    test_fails_closed()
    print('\n%d passed, %d failed, %d skipped'
          % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        for name in FAIL:
            print('  FAIL ' + name)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""Tests for gzip image surfaces inside `.LDT` containers.

Synthetic cases build containers byte by byte, so the header arithmetic, the
splice and the 4bpp/swizzle involutions are proved without a disc. The corpus
case is the one that matters for reinjection: it takes the real narration block,
rebuilds the container without changing a pixel, and demands every surface come
back unchanged. gzip is not byte-stable, so the assertion is decoded
equivalence - the criterion the graphics-text strategy sets for formats with
multiple legal encodings.

Run: python3 tests/test_ldtimg.py
"""
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import ldtimg  # noqa: E402

PASS, FAIL, SKIP = [], [], []

#: The shipped narration surface, when a checkout of the title repo is present.
CORPUS = '/mnt/ssd256/hanpatch-cdx2/extracted/OPENING.LDT'
NARRATION_BLOCK = 1
STRIDE, HEIGHT = 256, 128


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


def build(payloads, pad=b''):
    """A container with `len(payloads)` gzip members and a correct header."""
    members = b''
    for p in payloads:
        obj = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
        members += obj.compress(p) + obj.flush()
    head = struct.pack('<I', len(payloads)) + b'\x00' * 12
    out = bytearray(head + b'\x00' * (ldtimg.HEADER - len(head)) + members + pad)
    struct.pack_into('<I', out, ldtimg.PAYLOAD_SIZE_AT,
                     len(out) - ldtimg.HEADER)
    return bytes(out)


def test_header():
    blob = build([b'A' * 2048])
    case('payload size equals length minus header',
         ldtimg.payload_size(blob) == len(blob) - ldtimg.HEADER)
    case('a consistent header is reported consistent',
         ldtimg.header_consistent(blob))
    broken = bytearray(blob)
    struct.pack_into('<I', broken, ldtimg.PAYLOAD_SIZE_AT, 1)
    case('a wrong payload size is reported inconsistent',
         not ldtimg.header_consistent(bytes(broken)))
    case('a truncated header is refused',
         raises(ldtimg.LdtImageError, ldtimg.payload_size, b'\x00' * 8))


def test_blocks():
    parts = [b'X' * 2048, b'Y' * 4096]
    blob = build(parts)
    found = ldtimg.blocks(blob)
    case('every member is found in file order',
         [d for _s, _e, d in found] == parts)
    case('member end is where the inflater stopped',
         all(e > s for s, e, _ in found)
         and found[0][1] <= found[1][0])
    case('sub-floor members are skipped',
         ldtimg.blocks(build([b'tiny'])) == [])


def test_4bpp_involution():
    packed = bytes(range(256))
    case('unpack then pack is identity',
         ldtimg.pack_4bpp(ldtimg.unpack_4bpp(packed)) == packed)
    case('low nibble comes first',
         ldtimg.unpack_4bpp(b'\x21')[0] == 1
         and ldtimg.unpack_4bpp(b'\x21')[1] == 2)
    case('an out-of-range index is refused',
         raises(ldtimg.LdtImageError, ldtimg.pack_4bpp, bytes([16, 0])))
    case('an odd pixel count is refused',
         raises(ldtimg.LdtImageError, ldtimg.pack_4bpp, bytes([1])))


def test_surface_involution():
    surface = bytes((i * 7 + i // 16) % 256 for i in range(STRIDE * HEIGHT))
    pixels = ldtimg.decode_surface(surface, STRIDE, HEIGHT)
    case('a 4bpp surface decodes to two pixels per byte',
         len(pixels) == len(surface) * 2)
    case('decode then encode is identity',
         ldtimg.encode_surface(pixels, STRIDE, HEIGHT) == surface)
    case('8bpp decode then encode is identity',
         ldtimg.encode_surface(
             ldtimg.decode_surface(surface, STRIDE, HEIGHT, depth=8),
             STRIDE, HEIGHT, depth=8) == surface)
    case('a block too small for the geometry is refused',
         raises(ldtimg.LdtImageError, ldtimg.decode_surface,
                surface[:16], STRIDE, HEIGHT))
    case('an unsupported depth is refused',
         raises(ldtimg.LdtImageError, ldtimg.decode_surface,
                surface, STRIDE, HEIGHT, 16))


def test_rebuild_fixes_the_header():
    parts = [b'X' * 2048, b'Y' * 4096]
    blob = build(parts, pad=b'\x00\x00\x00')
    # A payload that compresses differently, so the length must move.
    replacement = bytes((i * 31) % 256 for i in range(4096))
    out = ldtimg.rebuild(blob, 1, replacement)
    case('the rebuilt header declares the new length',
         ldtimg.header_consistent(out))
    found = ldtimg.blocks(out)
    case('the rebuilt container still holds both members', len(found) == 2)
    case('the untouched member is unchanged', found[0][2] == parts[0])
    case('the replaced member decodes to the replacement',
         found[1][2] == replacement)
    case('trailing bytes survive the splice', out.endswith(b'\x00\x00\x00'))
    case('an out-of-range block index is refused',
         raises(ldtimg.LdtImageError, ldtimg.rebuild, blob, 9, replacement))


def test_corpus_round_trip():
    if not os.path.exists(CORPUS):
        skip('the shipped narration block rebuilds unchanged',
             'no extracted OPENING.LDT on this host')
        return
    blob = open(CORPUS, 'rb').read()
    case('the shipped header is consistent', ldtimg.header_consistent(blob))
    found = ldtimg.blocks(blob)
    if len(found) <= NARRATION_BLOCK:
        case('the shipped container holds the narration block', False)
        return
    decoded = found[NARRATION_BLOCK][2]
    pixels = ldtimg.decode_surface(decoded, STRIDE, HEIGHT)
    again = ldtimg.encode_surface(pixels, STRIDE, HEIGHT)
    case('the shipped surface survives decode/encode exactly',
         again == decoded[:STRIDE * HEIGHT])

    out = ldtimg.rebuild(blob, NARRATION_BLOCK, decoded)
    case('an unmodified rebuild fixes the header',
         ldtimg.header_consistent(out))
    rebuilt = ldtimg.blocks(out)
    case('an unmodified rebuild keeps every member',
         len(rebuilt) == len(found))
    case('an unmodified rebuild changes no pixel of any surface',
         all(rebuilt[i][2] == found[i][2] for i in range(len(found))))
    # gzip is not byte-stable, which is WHY the assertion above is decoded
    # equivalence. Measured on this block: the shipped stream is 3,513 bytes and
    # a level-9 re-encode of the identical payload is 3,485. Pin that the bytes
    # really do differ while the decode does not, so nobody later "fixes" the
    # criterion to byte equality and fails a correct rebuild.
    case('re-encoding differs in bytes while decoding identically',
         out != blob
         and ldtimg.blocks(out)[NARRATION_BLOCK][2] == decoded)


def main():
    print('header')
    test_header()
    print('blocks')
    test_blocks()
    print('4bpp')
    test_4bpp_involution()
    print('surface')
    test_surface_involution()
    print('rebuild')
    test_rebuild_fixes_the_header()
    print('corpus')
    test_corpus_round_trip()
    print('\n%d passed, %d failed, %d skipped'
          % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        for name in FAIL:
            print('  FAIL ' + name)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

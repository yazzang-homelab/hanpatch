"""IMY codec tests.

Two layers, and the split matters.

The synthetic layer builds payloads token by token and asserts what each token
means: a literal run spends the element stream, a back-reference does not, a copy
overlaps itself, the four copy sources are left / up / up-left / up-right. These
run anywhere and prove the rules this module claims to implement.

The corpus layer proves those rules are the game's rules, which no synthetic
image can. It walks every IMY block in a directory of files extracted from the
disc, decodes it, re-encodes it, and demands the bytes come back identical. It is
skipped when that directory is absent, because the disc is not ours to ship;
point HANPATCH_PSP_EXTRACT at one and it runs. A skip is reported as a skip, not
as a pass.

Run: python3 tests/test_imy.py
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import imy  # noqa: E402

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


def block(width, height, flags, depth, colors, payload, palette=b''):
    """Assemble a block by hand so tests can write illegal ones too."""
    dsize = imy.HEADER + colors * 4 + width * height
    head = struct.pack('<IIHBBHH', imy.MAGIC_LE, dsize, width, flags, depth,
                       height, colors) + b'\x00' * 16
    return head + palette + payload


def payload(tokens, elements):
    return struct.pack('<H', len(tokens)) + bytes(tokens) + bytes(elements)


# ------------------------------------------------------------------ tokens

def test_tokens():
    # a literal run of 4 elements, then one more of 1
    blob = block(8, 1, 0x00, 8, 0, payload([0x03, 0x00],
                                           b'\x01\x02\x03\x04\x05\x06\x07\x08'))
    head, pal, out = imy.decode(blob)
    case('literal run spends the element stream in order',
         out == b'\x01\x02\x03\x04\x05\x06\x07\x08')
    case('a run of n emits n+1 elements', head.element_size == 2)

    # 0x10 is the element most recently spent; it does not advance the cursor
    blob = block(10, 1, 0x00, 8, 0, payload([0x01, 0x10, 0x10, 0x00],
                                            b'AABB' b'ZZ'))
    head, pal, out = imy.decode(blob)
    case('back-reference 0x10 repeats the last element spent',
         out == b'AABB' b'BB' b'BB' b'ZZ')

    # 0x11 reaches one further back
    blob = block(6, 1, 0x00, 8, 0, payload([0x01, 0x11], b'AABB'))
    case('back-reference distance counts elements, not bytes',
         imy.decode(blob)[2] == b'AABB' b'AA')

    # copy source 0: the element to the left, overlapping so it repeats
    blob = block(8, 1, 0x00, 8, 0, payload([0x00, 0xC0 + 2], b'AB'))
    case('copy source 0 is the element to the left and overlaps',
         imy.decode(blob)[2] == b'AB' * 4)

    # copy source 1: the element above
    blob = block(4, 2, 0x00, 8, 0, payload([0x01, 0xC0 + 0x11], b'ABCD'))
    case('copy source 1 is the row above',
         imy.decode(blob)[2] == b'ABCD' b'ABCD')

    # copy source 2: above left. Row 0 is AB CD, row 1 starts at CD's column.
    blob = block(4, 2, 0x00, 8, 0, payload([0x01, 0x00, 0xC0 + 0x20],
                                           b'ABCD' b'EF'))
    case('copy source 2 is up-left',
         imy.decode(blob)[2] == b'ABCD' b'EF' b'AB')

    # copy source 3: above right
    blob = block(4, 2, 0x00, 8, 0, payload([0x01, 0xC0 + 0x30, 0x00],
                                           b'ABCD' b'EF'))
    case('copy source 3 is up-right',
         imy.decode(blob)[2] == b'ABCD' b'CD' b'EF')

    # a copy that would overrun the buffer is cut off at the end, not refused
    blob = block(4, 1, 0x00, 8, 0, payload([0x00, 0xC0 + 15], b'AB'))
    case('a copy is truncated by the end of the buffer',
         imy.decode(blob)[2] == b'ABAB')


def test_widths():
    # flags & 2 makes every element 4 bytes wide, including the copy deltas
    blob = block(16, 1, 0x02, 8, 0, payload([0x00, 0xC0 + 2], b'ABCD'))
    head, pal, out = imy.decode(blob)
    case('flags & 2 selects 4-byte elements', head.element_size == 4)
    case('4-byte copy source 0 steps back four bytes', out == b'ABCD' * 4)

    blob = block(4, 1, 0x01, 8, 0, b'WXYZ')
    head, pal, out = imy.decode(blob)
    case('flags & 1 stores the payload uncompressed', out == b'WXYZ')
    case('a stored block reports itself stored', head.stored)


def test_header():
    pal = b'\x01\x02\x03\x04' * 3
    blob = block(4, 2, 0x00, 8, 3, payload([0x03], b'ABCDEFGH'), pal)
    head, got, out = imy.decode(blob)
    case('the palette sits between header and payload', got == pal)
    case('palette size is counted in dsize',
         head.dsize == imy.HEADER + 12 + 8)
    case('width is a byte stride, not a pixel count', head.pixels_size == 8)

    case('format code is reassembled from the scattered flag bits',
         imy.Header(0, 0, 0x94, 8, 0, 0).format_code == 5)
    case('a zero format code is the plain case',
         imy.Header(0, 0, 0x10, 8, 0, 0).format_code == 0)


def test_damage():
    good = block(4, 1, 0x00, 8, 0, payload([0x01], b'ABCD'))

    case('a block without the magic is refused',
         raises(imy.ImyError, imy.decode, b'JMY\x00' + good[4:]))

    short = bytearray(good)
    struct.pack_into('<I', short, 4, 999)
    case('a dsize that disagrees with the dimensions is refused',
         raises(imy.ImyError, imy.decode, bytes(short)))

    case('a header cut off mid-field is refused',
         raises(imy.ImyError, imy.decode, good[:20]))

    case('a zero dimension is refused rather than decoding to nothing',
         raises(imy.ImyError, imy.decode,
                block(0, 4, 0x00, 8, 0, payload([0x01], b'ABCD'))))

    case('an element stream that runs out is refused',
         raises(imy.ImyError, imy.decode,
                block(64, 1, 0x00, 8, 0, payload([0x0F], b'AB'))))

    case('a token stream that runs out is refused',
         raises(imy.ImyError, imy.decode,
                block(64, 1, 0x00, 8, 0, payload([], b'AB'))))

    case('a copy from before the buffer is refused',
         raises(imy.ImyError, imy.decode,
                block(4, 1, 0x00, 8, 0, payload([0xC0 + 0x10], b''))))

    case('a back-reference reaching before the file is refused',
         raises(imy.ImyError, imy.decode,
                block(4, 1, 0x00, 8, 0, payload([0xBF], b'AB'))))

    case('a stride that is not a multiple of the element is refused',
         raises(imy.ImyError, imy.decode,
                block(3, 1, 0x00, 8, 0, payload([0x00], b'AB\x00'))))

    case('the unimplemented 24-bit variant is refused, not guessed',
         raises(imy.ImyError, imy.decode,
                block(8, 1, 0x02, imy.RGB_TRIPLE_UNSUPPORTED, 0,
                      payload([0x01], b'ABCDEFGH'))))


# ----------------------------------------------------------------- encoder

def synthetic_images():
    """Shapes that exercise every token, without needing a disc."""
    rng = 0x12345678
    def nxt():
        nonlocal rng
        rng = (rng * 1103515245 + 12345) & 0xFFFFFFFF
        return (rng >> 16) & 0xFF

    yield 'flat', 64, 8, bytes(64 * 8)
    yield 'rows repeat', 64, 8, (bytes(range(64))) * 8
    yield 'noise', 64, 8, bytes(nxt() for _ in range(64 * 8))
    # a few distinct values, which is what drives back-references
    yield 'palette-ish', 64, 8, bytes((nxt() % 5) * 40 for _ in range(64 * 8))
    # a single row, so there is no row above to copy from
    yield 'one row', 64, 1, bytes(nxt() for _ in range(64))
    # long runs, which is what drives 16-element copies
    yield 'runs', 64, 8, b''.join(bytes([nxt()]) * 37 for _ in range(14))[:512]


def test_roundtrip():
    for name, width, height, pixels in synthetic_images():
        for flags, esz in ((0x00, 2), (0x02, 4)):
            head = imy.Header(imy.HEADER + len(pixels), width, flags, 8,
                              height, 0)
            blob = imy.encode(head, b'', pixels)
            back = imy.decode(blob)[2]
            case('roundtrip %s at %d-byte elements' % (name, esz),
                 back == pixels)

    # the same, chunked, which changes which copies are legal
    pixels = bytes(range(64)) * 8
    head = imy.Header(imy.HEADER + len(pixels), 64, 0x00, 8, 8, 0)
    chunked = imy.encode(head, b'', pixels, chunk=128)
    case('a chunked stream still decodes to the same pixels',
         imy.decode(chunked)[2] == pixels)
    case('chunking costs bytes, so it is not the default',
         len(chunked) > len(imy.encode(head, b'', pixels)))

    toks, elements = imy.compress(pixels, 64, 2, chunk=128)
    pos = 0
    crossings = 0
    tok = 0
    while pos < len(pixels):
        b = toks[tok]
        tok += 1
        if b < 0x10:
            pos += 2 * (b + 1)
        elif b < 0xC0:
            pos += 2
        else:
            t = b - 0xC0
            src = pos + imy.deltas(64, 2)[t >> 4]
            if src < (pos // 128) * 128 or pos + 2 * ((t & 0xF) + 1) > (pos // 128 + 1) * 128:
                crossings += 1
            pos += 2 * ((t & 0xF) + 1)
    case('no copy crosses a chunk boundary when chunking is on', crossings == 0)


def test_encoder_refusals():
    head = imy.Header(imy.HEADER + 16, 8, 0x00, 8, 2, 0)
    case('a palette of the wrong size is refused',
         raises(imy.ImyError, imy.encode, head, b'\x00\x00\x00\x00', b'x' * 16))
    case('pixels of the wrong size are refused',
         raises(imy.ImyError, imy.encode, head, b'', b'x' * 15))
    case('a stored block encodes to its bytes unchanged',
         imy.encode(imy.Header(imy.HEADER + 16, 8, 0x01, 8, 2, 0), b'',
                    b'x' * 16).endswith(b'x' * 16))


def test_padding():
    """The element stream must start 4-byte aligned from the header."""
    for width in (2, 4, 6, 8, 10):
        pixels = bytes(range(width)) * 4
        head = imy.Header(imy.HEADER + len(pixels), width, 0x00, 8, 4, 0)
        blob = imy.encode(head, b'', pixels)
        n, = struct.unpack_from('<H', blob, imy.HEADER)
        start = imy.HEADER + 2 + n
        case('element stream is 4-byte aligned at stride %d' % width,
             start % 4 == 0)
        case('padding does not change the pixels at stride %d' % width,
             imy.decode(blob)[2] == pixels)


# ------------------------------------------------------------------ corpus

def test_corpus():
    root = os.environ.get('HANPATCH_PSP_EXTRACT')
    if not root or not os.path.isdir(root):
        skip('corpus: every block on the disc reproduces byte for byte',
             'set HANPATCH_PSP_EXTRACT to a directory of extracted files')
        return
    seen = decoded = exact = 0
    bad = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        with open(path, 'rb') as fh:
            data = fh.read()
        for off in imy.find_blocks(data):
            seen += 1
            try:
                head, pal, pixels = imy.decode(data, off)
            except imy.ImyError as exc:
                bad.append((name, off, 'decode', str(exc)))
                continue
            if len(pixels) != head.pixels_size:
                bad.append((name, off, 'size', len(pixels)))
                continue
            decoded += 1
            for chunk in (None, imy.chunk_for_pixels(head.depth)):
                built = imy.encode(head, pal, pixels, chunk=chunk)
                if data[off:off + len(built)] == built:
                    exact += 1
                    break
            else:
                bad.append((name, off, 'encode', head))
    print('  corpus: %d blocks, %d decoded, %d byte-identical'
          % (seen, decoded, exact))
    case('corpus: every block decodes to its declared size', decoded == seen)
    case('corpus: every block re-encodes byte for byte', exact == seen)
    for entry in bad[:10]:
        print('    ' + repr(entry))


def main():
    print('tokens')
    test_tokens()
    test_widths()
    print('header')
    test_header()
    test_damage()
    print('encoder')
    test_roundtrip()
    test_encoder_refusals()
    test_padding()
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

"""Tests for PSPFS_V1 and the ISO write path.

The synthetic cases build archives byte by byte, so the padding rule, the
storage order, and the compressed-size refusal are proved without a disc.

The corpus case is the one that matters: it parses the real 371 MB archive out
of the retail image, rebuilds it from its own members, writes it back, and
demands the whole 550 MB image come back byte identical. An adapter that cannot
pass that has not earned the right to inject anything, and the two facts this
file documents - a 20-byte name field and a padding rule that always advances -
were both found by this test failing.

Run: python3 tests/test_pspfs.py     (corpus: set HANPATCH_PSP_ISO)
"""
import hashlib
import mmap
import os
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import iso9660  # noqa: E402
from hanpatch.platforms.psp import pspfs  # noqa: E402

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


def archive(files, order=None):
    """Build one by hand: [(name, decompressed, data)], stored in `order`."""
    count = len(files)
    order = order if order is not None else list(range(count))
    offsets = {}
    cursor = pspfs.pad_to(pspfs.HEADER + count * pspfs.ENTRY)
    for i in order:
        offsets[i] = cursor
        cursor = pspfs.pad_to(cursor + len(files[i][2]))
    out = bytearray(pspfs.MAGIC + struct.pack('<II', count, 0))
    for i, (name, dec, blob) in enumerate(files):
        raw = name.encode('ascii')
        out += raw + b'\x00' * (pspfs.NAME - len(raw))
        out += struct.pack('<III', dec, len(blob), offsets[i])
    out += b'\x00' * (cursor - len(out))
    for i in order:
        out[offsets[i]:offsets[i] + len(files[i][2])] = files[i][2]
    return bytes(out)


# ------------------------------------------------------------------ padding

def test_padding():
    case('padding always advances past a full sector',
         pspfs.pad_to(512) == 1024)
    case('a partial sector rounds up', pspfs.pad_to(513) == 1024)
    case('a byte short of the boundary still gets one byte',
         pspfs.pad_to(1023) == 1024)
    case('zero advances to the first boundary', pspfs.pad_to(0) == 512)


def test_parse():
    blob = archive([('A.DAT', 0, b'a' * 600), ('B.DAT', 4096, b'b' * 10)])
    fs = pspfs.Pspfs(blob)

    case('files parse in table order', fs.names() == ['A.DAT', 'B.DAT'])
    case('contents read back', fs.read('A.DAT') == b'a' * 600)
    case('a decompressed size marks a file compressed',
         (fs.find('A.DAT').compressed, fs.find('B.DAT').compressed)
         == (False, True))
    case('the decompressed size is kept', fs.find('B.DAT').decompressed == 4096)
    case('the second file starts after the padding rule',
         fs.find('B.DAT').offset == pspfs.pad_to(fs.find('A.DAT').offset + 600))
    case('an unknown name is refused', raises(pspfs.PspfsError, fs.read, 'X'))


def test_identity_and_order():
    # storage order deliberately differs from table order, as on the disc
    files = [('A.DAT', 0, b'a' * 600), ('B.DAT', 0, b'b' * 700),
             ('C.DAT', 0, b'c' * 512)]
    blob = archive(files, order=[2, 0, 1])
    fs = pspfs.Pspfs(blob)

    case('storage order is not table order', fs.order == [2, 0, 1])
    case('an unchanged archive rebuilds byte for byte', fs.build() == blob)
    case('a file of exactly one sector still gets padding after it',
         fs.find('A.DAT').offset
         == pspfs.pad_to(fs.find('C.DAT').offset + 512))


def test_replace():
    blob = archive([('A.DAT', 0, b'a' * 600), ('B.DAT', 0, b'b' * 10)])
    fs = pspfs.Pspfs(blob)

    grown = pspfs.Pspfs(fs.build({'A.DAT': b'x' * 5000}))
    case('a grown file is stored whole', grown.read('A.DAT') == b'x' * 5000)
    case('the file after it moves', grown.find('B.DAT').offset
         > pspfs.Pspfs(blob).find('B.DAT').offset)
    case('the untouched file is intact', grown.read('B.DAT') == b'b' * 10)

    shrunk = pspfs.Pspfs(fs.build({'A.DAT': b'x'}))
    case('a shrunk file is stored whole', shrunk.read('A.DAT') == b'x')
    case('the archive shrinks with it', len(shrunk.data) < len(blob))

    case('replacing an unknown file is refused',
         raises(pspfs.PspfsError, fs.build, {'NOPE.DAT': b''}))


def test_compressed_refusal():
    blob = archive([('S.DAT', 8192, b's' * 100)])
    fs = pspfs.Pspfs(blob)

    case('replacing a compressed file without its decompressed size is refused',
         raises(pspfs.PspfsError, fs.build, {'S.DAT': b'x' * 50}))

    ok = pspfs.Pspfs(fs.build({'S.DAT': b'x' * 50}, {'S.DAT': 4096}))
    case('supplying the decompressed size is accepted',
         ok.find('S.DAT').decompressed == 4096)
    case('an unchanged compressed file keeps its size',
         pspfs.Pspfs(fs.build()).find('S.DAT').decompressed == 8192)


def test_damage():
    blob = archive([('A.DAT', 0, b'a' * 8)])
    case('a file without the magic is refused',
         raises(pspfs.PspfsError, pspfs.Pspfs, b'NOTFS_V1' + blob[8:]))
    case('a buffer shorter than the header is refused',
         raises(pspfs.PspfsError, pspfs.Pspfs, blob[:8]))

    liar = bytearray(blob)
    struct.pack_into('<I', liar, 8, 9999)
    case('a count the buffer cannot hold is refused',
         raises(pspfs.PspfsError, pspfs.Pspfs, bytes(liar)))

    past = bytearray(blob)
    struct.pack_into('<I', past, pspfs.HEADER + pspfs.NAME + 8, len(blob) * 4)
    case('a file running past the archive is refused',
         raises(pspfs.PspfsError, pspfs.Pspfs, bytes(past)))


# ------------------------------------------------------------------- corpus

def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b''):
            h.update(chunk)
    return h.hexdigest()


def test_corpus():
    src = os.environ.get('HANPATCH_PSP_ISO')
    if not src or not os.path.isfile(src):
        skip('corpus: the retail image rebuilds byte for byte',
             'set HANPATCH_PSP_ISO to the disc image')
        return
    with iso9660.Iso.from_path(src) as iso:
        entry = iso.find('/PSP_GAME/USRDIR/DATA.DAT')
        if entry is None:
            skip('corpus: identity rebuild', 'image has no USRDIR/DATA.DAT')
            return
        base, size = entry.offset, entry.size

    with open(src, 'rb') as fh:
        buf = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        fs = pspfs.Pspfs(buf[base:base + size])
        rebuilt = fs.build()
        original = hashlib.sha256(buf[base:base + size]).hexdigest()
        buf.close()

    case('corpus: the archive rebuilds byte for byte',
         hashlib.sha256(rebuilt).hexdigest() == original)
    case('corpus: the rebuild is the same length', len(rebuilt) == size)
    print('  corpus: %d files, %d compressed, %d bytes'
          % (len(fs), sum(1 for f in fs if f.compressed), size))

    out = os.path.join(tempfile.gettempdir(), 'hanpatch-identity.iso')
    try:
        iso9660.write(src, out, {'/PSP_GAME/USRDIR/DATA.DAT': rebuilt})
        case('corpus: the whole image rebuilds byte for byte',
             _sha(out) == _sha(src))
        case('corpus: the image keeps its length',
             os.path.getsize(out) == os.path.getsize(src))
    finally:
        if os.path.exists(out):
            os.unlink(out)


def main():
    print('padding')
    test_padding()
    print('archive')
    test_parse()
    test_identity_and_order()
    test_replace()
    test_compressed_refusal()
    test_damage()
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

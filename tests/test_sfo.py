"""Tests for `PARAM.SFO` parsing and in-place title replacement.

Synthetic blocks are built byte by byte so the header arithmetic, the entry
table and the fit refusal are proved without a disc. The corpus case uses the
shipped block when a checkout is present: it asserts the real layout, replaces
the title, and demands the file size and every offset stay put - the property
that makes this patch safe.

Run: python3 tests/test_sfo.py
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import sfo  # noqa: E402

PASS, FAIL, SKIP = [], [], []

#: The shipped block, when the title repo is on this host.
CORPUS = '/mnt/ssd256/hanpatch-cdx2/extracted/PARAM.SFO'


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


def build(pairs):
    """A minimal SFO. `pairs` is `[(key, fmt, value_bytes, max)]`."""
    count = len(pairs)
    key_table = b''
    key_offs = []
    for key, _fmt, _val, _mx in pairs:
        key_offs.append(len(key_table))
        key_table += key.encode('ascii') + b'\0'
    # Key table is padded so the data table starts aligned, as shipped blocks do.
    pad = (-len(key_table)) % 4
    key_table += b'\0' * pad

    data_table = b''
    data_offs = []
    for _key, _fmt, val, mx in pairs:
        data_offs.append(len(data_table))
        data_table += val + b'\0' * (mx - len(val))

    header_size = struct.calcsize('<4sIIII') + count * 16
    key_off = header_size
    data_off = key_off + len(key_table)
    out = bytearray()
    out += struct.pack('<4sIIII', sfo.MAGIC, 0x101, key_off, data_off, count)
    for i, (_key, fmt, val, mx) in enumerate(pairs):
        out += struct.pack('<HHIII', key_offs[i], fmt, len(val), mx, data_offs[i])
    out += key_table
    out += data_table
    return bytes(out)


def sample():
    return build([
        ('BOOTABLE', sfo.FMT_U32, struct.pack('<I', 1), 4),
        ('DISC_ID', sfo.FMT_UTF8, b'TEST00001\0', 16),
        ('TITLE', sfo.FMT_UTF8, 'ゲーム名'.encode('utf-8') + b'\0', 128),
    ])


def test_parse():
    s = sfo.Sfo(sample())
    case('entry count matches the header', len(s.entries) == 3)
    case('keys are read from the key table',
         [e.key for e in s.entries] == ['BOOTABLE', 'DISC_ID', 'TITLE'])
    case('a u32 entry decodes to int', s.value('BOOTABLE') == 1)
    case('a utf-8 entry decodes without its NUL',
         s.value('DISC_ID') == 'TEST00001')
    case('a non-ascii title round-trips through utf-8',
         s.value('TITLE') == 'ゲーム名')
    case('items() returns every pair', len(s.items()) == 3)
    case('a missing key is refused', raises(sfo.SfoError, s.value, 'NOPE'))
    case('find() returns None for a missing key', s.find('NOPE') is None)


def test_rejects_malformed():
    case('a short block is refused',
         raises(sfo.SfoError, sfo.Sfo, b'\x00PSF'))
    case('a wrong magic is refused',
         raises(sfo.SfoError, sfo.Sfo, b'BAD!' + bytes(16)))
    truncated = sample()[:40]
    case('an entry table running past the block is refused',
         raises(sfo.SfoError, sfo.Sfo, truncated))


def test_write_value():
    original = sample()
    s = sfo.Sfo(original)
    out = s.write_value('TITLE', '클래식 던전 X2')
    case('the block size never changes', len(out) == len(original))
    again = sfo.Sfo(out)
    case('the new title reads back', again.value('TITLE') == '클래식 던전 X2')
    case('length counts the trailing NUL',
         again.find('TITLE').length
         == len('클래식 던전 X2'.encode('utf-8')) + 1)
    case('max is untouched', again.find('TITLE').max == 128)
    case('other entries are unchanged',
         again.value('BOOTABLE') == 1 and again.value('DISC_ID') == 'TEST00001')
    case('every data offset is unchanged',
         [e.data_at for e in again.entries] == [e.data_at for e in s.entries])

    # No tail of the longer old value may survive inside the field.
    long_first = sfo.Sfo(original).write_value('TITLE', 'あ' * 20)
    short_next = sfo.Sfo(long_first).write_value('TITLE', '짧음')
    field = sfo.Sfo(short_next).find('TITLE')
    tail = short_next[field.data_at + field.length:field.data_at + field.max]
    case('the field is cleared past the new value', tail == b'\0' * len(tail))

    case('a value larger than max is refused',
         raises(sfo.SfoError, s.write_value, 'TITLE', 'あ' * 60))
    case('a u32 entry refuses a string write',
         raises(sfo.SfoError, s.write_value, 'BOOTABLE', 'nope'))
    case('a missing key is refused',
         raises(sfo.SfoError, s.write_value, 'NOPE', 'x'))


def test_corpus():
    if not os.path.exists(CORPUS):
        skip('the shipped block patches without moving anything',
             'no extracted PARAM.SFO on this host')
        return
    blob = open(CORPUS, 'rb').read()
    s = sfo.Sfo(blob)
    title = s.find('TITLE')
    case('the shipped TITLE reserves more than it uses',
         title is not None and title.max > title.length)
    out = s.write_value('TITLE', '클래식 던전 X2')
    case('the shipped block keeps its size', len(out) == len(blob))
    again = sfo.Sfo(out)
    case('the shipped block reads back the new title',
         again.value('TITLE') == '클래식 던전 X2')
    case('every other shipped value is unchanged',
         [(k, v) for k, v in again.items() if k != 'TITLE']
         == [(k, v) for k, v in s.items() if k != 'TITLE'])
    case('the shipped entry table keeps every offset',
         [e.data_at for e in again.entries] == [e.data_at for e in s.entries])


def main():
    print('parse')
    test_parse()
    print('malformed')
    test_rejects_malformed()
    print('write')
    test_write_value()
    print('corpus')
    test_corpus()
    print('\n%d passed, %d failed, %d skipped'
          % (len(PASS), len(FAIL), len(SKIP)))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

"""PSP container tests — ISO 9660 volumes and PBP wrappers.

No disc is involved. Every image here is **synthesised**: sectors this file
writes byte by byte, with a directory tree of our own choosing. That is enough to
prove the things that actually break — both-endian agreement, the sector-padding
rule for directory extents, version-suffix handling, and PBP's implied section
sizes — none of which depend on the contents of any title.

The modules under test are transcribed from published specifications rather than
measured from a dumped image, so these tests prove the transcription is
self-consistent. They do not prove a retail UMD agrees; that needs an image.

Run: python3 tests/test_psp.py
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import iso9660  # noqa: E402
from hanpatch.platforms.psp import pbp  # noqa: E402

PASS, FAIL = [], []


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


# ---------------------------------------------------------------- synthesis

SECTOR = iso9660.SECTOR


def both32(value):
    return struct.pack('<I', value) + struct.pack('>I', value)


def both16(value):
    return struct.pack('<H', value) + struct.pack('>H', value)


def dir_record(name, lba, size, is_dir):
    """One directory record. `name` is raw identifier bytes."""
    length = 33 + len(name)
    if length % 2:
        length += 1
    rec = bytearray(length)
    rec[0] = length
    rec[1] = 0                                   # no extended attribute record
    rec[2:10] = both32(lba)
    rec[10:18] = both32(size)
    rec[25] = 0x02 if is_dir else 0x00
    rec[28:32] = both16(1)
    rec[32] = len(name)
    rec[33:33 + len(name)] = name
    return bytes(rec)


def dir_extent(entries, self_lba, parent_lba):
    """A directory extent: '.', '..', then `entries`, padded to a sector."""
    out = bytearray()
    out += dir_record(b'\x00', self_lba, SECTOR, True)
    out += dir_record(b'\x01', parent_lba, SECTOR, True)
    for rec in entries:
        out += rec
    if len(out) > SECTOR:
        raise AssertionError('fixture directory overflows one sector')
    out += b'\x00' * (SECTOR - len(out))
    return bytes(out)


def build_iso():
    """A four-level volume with the layout a PSP title actually uses.

        /PSP_GAME/SYSDIR/EBOOT.BIN      (sector 21)
        /UMD_DATA.BIN                   (sector 22)
    """
    eboot = b'EBOOT-PAYLOAD-' + bytes(range(64))
    umd_data = b'UMD-DATA-' + b'\xAA' * 100

    sysdir = dir_extent([dir_record(b'EBOOT.BIN;1', 21, len(eboot), False)], 20, 19)
    psp_game = dir_extent([dir_record(b'SYSDIR', 20, SECTOR, True)], 19, 18)
    root = dir_extent([
        dir_record(b'PSP_GAME', 19, SECTOR, True),
        dir_record(b'UMD_DATA.BIN;1', 22, len(umd_data), False),
    ], 18, 18)

    # primary volume descriptor
    pvd = bytearray(SECTOR)
    pvd[0] = iso9660.VD_PRIMARY
    pvd[1:6] = iso9660.STD_ID
    pvd[6] = 1
    pvd[0x28:0x48] = b'CLADUN_FIXTURE'.ljust(32, b' ')
    pvd[0x50:0x58] = both32(23)
    pvd[0x80:0x84] = both16(SECTOR)
    pvd[0x9C:0x9C + 34] = dir_record(b'\x00', 18, SECTOR, True)

    term = bytearray(SECTOR)
    term[0] = iso9660.VD_TERMINATOR
    term[1:6] = iso9660.STD_ID
    term[6] = 1

    image = bytearray(b'\x00' * (SECTOR * iso9660.SYSTEM_AREA_SECTORS))
    image += pvd
    image += term
    image += root
    image += psp_game
    image += sysdir
    image += eboot.ljust(SECTOR, b'\x00')
    image += umd_data.ljust(SECTOR, b'\x00')
    return bytes(image), eboot, umd_data


# ---------------------------------------------------------------- iso 9660

def test_iso():
    image, eboot, umd_data = build_iso()
    iso = iso9660.Iso(image)

    case('iso: volume identifier is trimmed of its padding',
         iso.volume_id == 'CLADUN_FIXTURE')
    case('iso: logical block size is read as 2048', iso.block_size == SECTOR)
    case('iso: volume space size is read', iso.volume_space == 23)

    paths = sorted(e.path for e in iso.walk())
    case('iso: walk finds every entry and no others',
         paths == ['/PSP_GAME', '/PSP_GAME/SYSDIR',
                   '/PSP_GAME/SYSDIR/EBOOT.BIN', '/UMD_DATA.BIN'])

    case("iso: walk does not emit '.' or '..'",
         not any(p.endswith('/.') or p.endswith('/..') for p in paths))

    boot = iso.find('/PSP_GAME/SYSDIR/EBOOT.BIN')
    case('iso: find locates a nested file', boot is not None and not boot.is_dir)
    case('iso: find is case-insensitive',
         iso.find('/psp_game/sysdir/eboot.bin') is not None)
    case('iso: find tolerates a missing leading slash',
         iso.find('PSP_GAME/SYSDIR/EBOOT.BIN') is not None)
    case('iso: find returns None for an absent path',
         iso.find('/PSP_GAME/NOPE.BIN') is None)

    case('iso: version suffix is stripped from the path',
         boot.path == '/PSP_GAME/SYSDIR/EBOOT.BIN')
    case('iso: raw identifier keeps the version suffix for a rebuild',
         boot.raw_name == b'EBOOT.BIN;1')

    case('iso: file extent reads back byte for byte', iso.read(boot) == eboot)
    case('iso: a second file reads back byte for byte',
         iso.read(iso.find('/UMD_DATA.BIN')) == umd_data)

    case('iso: entry offset is its LBA times the sector size',
         boot.offset == boot.lba * SECTOR == iso9660.offset_of(boot.lba))
    case('iso: reading a directory is refused',
         raises(iso9660.IsoError, iso.read, iso.find('/PSP_GAME')))

    case('iso: size matches the record, not the padded sector',
         boot.size == len(eboot))

    # directories are yielded before the files they contain, so a consumer can
    # create a tree in walk order without buffering
    order = [e.path for e in iso.walk()]
    case('iso: a directory is yielded before its contents',
         order.index('/PSP_GAME') < order.index('/PSP_GAME/SYSDIR')
         < order.index('/PSP_GAME/SYSDIR/EBOOT.BIN'))


def test_iso_damage():
    """The failure modes worth having: each must be refused, not guessed at."""
    image, _, _ = build_iso()

    bad_std = bytearray(image)
    bad_std[SECTOR * 16 + 1:SECTOR * 16 + 6] = b'XX001'
    case('iso: a wrong standard identifier is refused',
         raises(iso9660.IsoError, iso9660.Iso, bytes(bad_std)))

    # corrupt only the big-endian half of the root extent pointer
    at = SECTOR * 16 + 0x9C + 2 + 4
    bad_be = bytearray(image)
    struct.pack_into('>I', bad_be, at, 999)
    case('iso: a both-endian mismatch is refused rather than trusting one half',
         raises(iso9660.IsoError, iso9660.Iso, bytes(bad_be)))

    truncated = image[:SECTOR * 17]
    case('iso: an image ending before the descriptor terminator is refused',
         raises(iso9660.IsoError, iso9660.Iso, truncated))

    no_pvd = bytearray(image)
    no_pvd[SECTOR * 16] = iso9660.VD_TERMINATOR
    case('iso: a volume with no primary descriptor is refused',
         raises(iso9660.IsoError, iso9660.Iso, bytes(no_pvd)))

    odd_block = bytearray(image)
    odd_block[0x80 + SECTOR * 16:0x84 + SECTOR * 16] = both16(800)
    case('iso: a logical block size other than 2048 is refused',
         raises(iso9660.IsoError, iso9660.Iso, bytes(odd_block)))

    case('iso: a negative LBA is refused',
         raises(iso9660.IsoError, iso9660.offset_of, -1))


def test_iso_padding():
    """A directory extent spanning two sectors, with a record that would
    straddle the boundary. The standard forbids straddling, so the tail of the
    first sector is zero padding and the reader must skip to the next one."""
    names = [b'FILE%02d.DAT;1' % i for i in range(40)]
    records = [dir_record(n, 100 + i, 16, False) for i, n in enumerate(names)]

    out = bytearray()
    out += dir_record(b'\x00', 18, SECTOR * 2, True)
    out += dir_record(b'\x01', 18, SECTOR * 2, True)
    for rec in records:
        if len(out) % SECTOR + len(rec) > SECTOR:
            out += b'\x00' * (SECTOR - len(out) % SECTOR)
        out += rec
    out += b'\x00' * (SECTOR * 2 - len(out))
    case('padding fixture really does span two sectors', len(out) == SECTOR * 2)

    pvd = bytearray(SECTOR)
    pvd[0] = iso9660.VD_PRIMARY
    pvd[1:6] = iso9660.STD_ID
    pvd[6] = 1
    pvd[0x28:0x48] = b'PAD'.ljust(32, b' ')
    pvd[0x50:0x58] = both32(20)
    pvd[0x80:0x84] = both16(SECTOR)
    pvd[0x9C:0x9C + 34] = dir_record(b'\x00', 18, SECTOR * 2, True)

    term = bytearray(SECTOR)
    term[0] = iso9660.VD_TERMINATOR
    term[1:6] = iso9660.STD_ID
    term[6] = 1

    image = bytearray(b'\x00' * (SECTOR * 16)) + pvd + term + out
    image += b'\x00' * SECTOR * 120

    found = sorted(e.path for e in iso9660.Iso(bytes(image)).walk())
    case('iso: records after a padded sector boundary are still found',
         found == sorted('/FILE%02d.DAT' % i for i in range(40)))


# -------------------------------------------------------------------- pbp

def test_pbp():
    parts = {
        'PARAM.SFO': b'SFO-' + b'\x01' * 60,
        'ICON0.PNG': b'PNG-' + b'\x02' * 30,
        'DATA.PSP': b'PSP-' + b'\x03' * 200,
        'DATA.PSAR': b'PSAR-' + b'\x04' * 500,
    }
    blob = pbp.build(parts)
    parsed = pbp.Pbp(blob)

    case('pbp: magic and header survive a build/parse round trip',
         blob[:4] == pbp.MAGIC)
    for name, payload in parts.items():
        case('pbp: %s round trips byte for byte' % name,
             parsed.read(name) == payload)

    case('pbp: an unsupplied section is zero length, not absent',
         parsed.sections['SND0.AT3'].empty and parsed.read('SND0.AT3') == b'')
    case('pbp: every section in the format is present',
         set(parsed.sections) == set(pbp.SECTIONS))
    case('pbp: iteration follows header order',
         [s.name for s in parsed] == list(pbp.SECTIONS))

    # the last section's size is implied by end of file, which is the one size
    # that is not a subtraction of two offsets
    last = parsed.sections['DATA.PSAR']
    case('pbp: the final section runs to end of file',
         last.offset + last.size == len(blob))

    case('pbp: sections are laid out back to back with no gaps',
         all(a.offset + a.size == b.offset
             for a, b in zip(list(parsed), list(parsed)[1:])))

    case('pbp: an unknown section name is refused on build',
         raises(pbp.PbpError, pbp.build, {'README.TXT': b''}))
    case('pbp: an unknown section name is refused on read',
         raises(pbp.PbpError, parsed.read, 'README.TXT'))


def test_pbp_damage():
    blob = bytearray(pbp.build({'DATA.PSP': b'x' * 32}))

    bad_magic = bytearray(blob)
    bad_magic[0:4] = b'PBP\x00'
    case('pbp: wrong magic is refused',
         raises(pbp.PbpError, pbp.Pbp, bytes(bad_magic)))

    case('pbp: a file shorter than the header is refused',
         raises(pbp.PbpError, pbp.Pbp, bytes(blob[:pbp.HEADER - 1])))

    backwards = bytearray(blob)
    struct.pack_into('<I', backwards, 0x08 + 4, pbp.HEADER)
    struct.pack_into('<I', backwards, 0x08, pbp.HEADER + 16)
    case('pbp: decreasing offsets are refused rather than yielding a negative size',
         raises(pbp.PbpError, pbp.Pbp, bytes(backwards)))

    past_end = bytearray(blob)
    struct.pack_into('<I', past_end, 0x08 + 28, len(blob) + 4096)
    case('pbp: an offset past end of file is refused',
         raises(pbp.PbpError, pbp.Pbp, bytes(past_end)))

    inside_header = bytearray(blob)
    struct.pack_into('<I', inside_header, 0x08, 4)
    case('pbp: an offset pointing into the header is refused',
         raises(pbp.PbpError, pbp.Pbp, bytes(inside_header)))


def main():
    print('iso 9660')
    test_iso()
    test_iso_damage()
    test_iso_padding()
    print('pbp')
    test_pbp()
    test_pbp_damage()
    print('\n%d passed, %d failed' % (len(PASS), len(FAIL)))
    if FAIL:
        for name in FAIL:
            print('  FAIL ' + name)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())

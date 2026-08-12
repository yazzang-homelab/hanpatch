"""Adversarial tests for the Luma3DS LayeredFS pack.

Run:  python3 tests/test_luma.py

A LayeredFS pack is the shape that reaches real hardware, and nothing on the
console checks it. If the pack is missing a file the game shows Japanese there;
if the title id is wrong Luma patches nothing and looks identical to "the patch
does not work"; if the IPS is malformed the executable is corrupted at boot.
So every case here is a pack that must not be believed.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch import config  # noqa: E402
from hanpatch.platforms import threeds  # noqa: E402
from hanpatch.platforms.threeds import blz, luma  # noqa: E402

PASS = []
FAIL = []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def sec(title):
    print()
    print(title)


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except SystemExit as e:
        return str(e)
    return None


TMP = tempfile.mkdtemp(prefix='hanpatch-luma-test-')

sec('IPS over the decompressed executable')
base = bytes(4096)
case('no change is a patch with no records',
     luma.ips(base, base) == b'PATCH' + b'EOF')

one = bytearray(base)
one[0x123] = 0xFF
one = bytes(one)
p = luma.ips(base, one)
case('one changed byte round-trips', luma.apply_ips(base, p) == one)
case('one changed byte is one record', len(p) == 5 + 3 + 2 + 1 + 3)

two = bytearray(base)
two[0x10:0x14] = b'ABCD'
two[0x18:0x1C] = b'EFGH'      # 4-byte gap: cheaper inside one record
two = bytes(two)
p = luma.ips(base, two)
case('changes separated by a small gap round-trip',
     luma.apply_ips(base, p) == two)
case('a small gap is kept inside one record, not paid for twice',
     p.count(b'ABCD') == 1 and len(p) < 5 + 2 * (3 + 2 + 4) + 3 + 8)

far = bytearray(base)
far[0x10] = 1
far[0x800] = 2
far = bytes(far)
case('changes far apart round-trip', luma.apply_ips(base, luma.ips(base, far))
     == far)

big = bytearray(base)
big[0x100:0x100 + 0x20000] = bytes(range(256)) * 512   # past one record's size
big = bytes(big[:len(base)] if len(big) > len(base) else big)
long_src = bytes(0x30000)
long_dst = bytearray(long_src)
long_dst[0x100:0x100 + 0x20000] = (bytes(range(256)) * 512)[:0x20000]
long_dst = bytes(long_dst)
case('a change longer than one IPS record is split and round-trips',
     luma.apply_ips(long_src, luma.ips(long_src, long_dst)) == long_dst)

case('a length change is refused, not truncated',
     'length change' in (raises(luma.ips, base, base + b'x') or ''))
case('past the 16 MB IPS limit is refused',
     '16 MB' in (raises(luma.ips, bytes(1 << 24 | 1), bytes(1 << 24 | 1)) or ''))

# 'EOF' is a valid three-byte offset (0x454F46). A record starting there would
# end the patch early and silently drop everything after it.
eof_src = bytearray(0x454F46 + 16)
eof_dst = bytearray(eof_src)
eof_dst[0x454F46] = 0x7F
p = luma.ips(bytes(eof_src), bytes(eof_dst))
case('a record at the offset spelling "EOF" does not end the patch',
     luma.apply_ips(bytes(eof_src), p) == bytes(eof_dst))

sec('title id comes from the ROM, not from a config field')
hdr = os.path.join(TMP, 'ncch_header.bin')
blob = bytearray(0x200)
blob[0x100:0x104] = b'NCCH'
blob[0x118:0x120] = (0x0004000000065E00).to_bytes(8, 'little')
with open(hdr, 'wb') as f:
    f.write(bytes(blob))
case('the program id is read as 16 upper-case hex',
     luma.title_id(hdr) == '0004000000065E00')
bad = os.path.join(TMP, 'not-ncch.bin')
with open(bad, 'wb') as f:
    f.write(bytes(0x200))
case('a file that is not an NCCH header is refused',
     'not an NCCH header' in (raises(luma.title_id, bad) or ''))
case('a missing header is refused rather than guessed',
     'run extract first' in (raises(luma.title_id,
                                    os.path.join(TMP, 'nope.bin')) or ''))


class FakeAdapter:
    """A staged tree with every case the packer has to get right."""

    def __init__(self, root):
        self.root = root
        self.romfs_dir = os.path.join(root, 'extracted', 'romfs')
        self.stage_dir = os.path.join(root, 'build', 'romfs')
        os.makedirs(os.path.join(self.romfs_dir, 'MESS'))
        os.makedirs(os.path.join(self.romfs_dir, 'MOVIE'))
        os.makedirs(os.path.join(self.romfs_dir, 'LAYOUT'))
        self.write(self.romfs_dir, 'MESS/same.fpt', b'unchanged')
        self.write(self.romfs_dir, 'MESS/text.fpt', b'japanese')
        self.write(self.romfs_dir, 'MOVIE/big.mov', b'x' * 4096)
        self.write(self.romfs_dir, 'LAYOUT/font.arc', b'japanese font')

        os.makedirs(os.path.join(self.stage_dir, 'MESS'))
        self.write(self.stage_dir, 'MESS/same.fpt', b'unchanged')
        self.write(self.stage_dir, 'MESS/text.fpt', b'korean!!')
        self.write(self.stage_dir, 'MESS/new.fpt', b'added by the patch')
        # untouched directories are staged as links, exactly like the real one
        os.symlink(os.path.join(self.romfs_dir, 'MOVIE'),
                   os.path.join(self.stage_dir, 'MOVIE'))
        os.symlink(os.path.join(self.romfs_dir, 'LAYOUT'),
                   os.path.join(self.stage_dir, 'LAYOUT'))
        # the font write lands in the source tree through that link
        self.write(self.romfs_dir, 'LAYOUT/font.arc', b'korean font!')

    @staticmethod
    def write(base, rel, data):
        path = os.path.join(base, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)

    def stage(self, entries):
        return {'romfs': self.stage_dir,
                'exefs': {'.code': blz.compress(PATCHED_CODE)},
                'rewritten': ['LAYOUT/font.arc'],
                'stats': {}}


sec('what goes in the pack')
PROJ = os.path.join(TMP, 'proj')
os.makedirs(PROJ)
ad = FakeAdapter(PROJ)
SOURCE_CODE = bytes(0x2000)
PATCHED_CODE = bytearray(SOURCE_CODE)
PATCHED_CODE[0x40:0x44] = b'HOOK'
PATCHED_CODE = bytes(PATCHED_CODE)
PROJ2 = os.path.join(TMP, 'proj2')
ad2 = FakeAdapter(PROJ2)
FakeAdapter.write(os.path.join(PROJ2, 'extracted'), 'exefs/.code',
                  blz.compress(SOURCE_CODE))
with open(os.path.join(PROJ2, config.PROJECT_FILE), 'w') as f:
    f.write('{"title": "Fake", "platform": "threeds", "adapter": "dq7",'
            ' "target": "ko", "profile": "profile.json", "rom": "game.3ds"}')
with open(os.path.join(PROJ2, 'profile.json'), 'w') as f:
    f.write('{}')
config.set_root(PROJ2)
OUT = os.path.join(TMP, 'sd')
rep = luma.pack(ad2, {}, OUT, tid='0004000000065E00', quiet=True)
packed = sorted(rep['files'])
case('changed and new files are packed',
     'MESS/text.fpt' in packed and 'MESS/new.fpt' in packed)
case('an unchanged file is not packed', 'MESS/same.fpt' not in packed)
case('a symlinked directory is never walked into',
     not any(p.startswith('MOVIE/') for p in packed))
case('a file rewritten through the staged symlink is still packed',
     'LAYOUT/font.arc' in packed)
root = os.path.join(OUT, 'luma', 'titles', '0004000000065E00')
case('the pack sits under luma/titles/<TID>/romfs',
     os.path.exists(os.path.join(root, 'romfs', 'MESS', 'text.fpt')))
case('the packed bytes are the staged bytes, not the source bytes',
     open(os.path.join(root, 'romfs', 'MESS', 'text.fpt'), 'rb').read()
     == b'korean!!')
case('the packed font is the rewritten one',
     open(os.path.join(root, 'romfs', 'LAYOUT', 'font.arc'), 'rb').read()
     == b'korean font!')
case('code.ips is written', os.path.exists(os.path.join(root, 'code.ips')))
with open(os.path.join(root, 'code.ips'), 'rb') as f:
    ips_bytes = f.read()
case('code.ips patches the DECOMPRESSED executable',
     luma.apply_ips(SOURCE_CODE, ips_bytes) == PATCHED_CODE)
case('a README tells the operator what to switch on',
     'Enable game patching' in open(os.path.join(root, 'README.txt'),
                                    encoding='utf-8').read())
case('the README carries the title id it needs to match',
     '0004000000065E00' in open(os.path.join(root, 'README.txt'),
                                encoding='utf-8').read())

case('an adapter with no stage() is refused, not worked around',
     'has no stage()' in (raises(luma.pack, object(), {}, OUT,
                                 tid='0' * 16) or ''))

sec('the pack must equal the rebuilt RomFS')
# Build a real RomFS out of the staged tree and check the pack against it. This
# is the claim a console depends on: the same bytes a rebuilt ROM would carry.
IMAGE = os.path.join(TMP, 'romfs.bin')
FLAT = os.path.join(TMP, 'flat')
shutil.copytree(ad2.stage_dir, FLAT, symlinks=False)
threeds.build_romfs(FLAT, IMAGE)
v = luma.verify_against_rom(root, IMAGE, quiet=True)
case('every packed file is found in the image and matches',
     v['mismatched'] == 0 and not v['missing'] and v['checked'] == len(packed))

# and it must notice when they differ
with open(os.path.join(root, 'romfs', 'MESS', 'text.fpt'), 'wb') as f:
    f.write(b'TAMPERED')
v = luma.verify_against_rom(root, IMAGE, quiet=True)
case('a tampered packed file is reported, not passed', v['mismatched'] == 1)
with open(os.path.join(root, 'romfs', 'MESS', 'stray.fpt'), 'wb') as f:
    f.write(b'not in the image')
v = luma.verify_against_rom(root, IMAGE, quiet=True)
case('a file the image does not have is reported as missing',
     v['missing'] == ['MESS/stray.fpt'])

shutil.rmtree(TMP, ignore_errors=True)

print()
print(f'{len(PASS)} passed, {len(FAIL)} failed')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
sys.exit(1 if FAIL else 0)

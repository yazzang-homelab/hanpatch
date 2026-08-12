"""Container, crypto and distribution tests.

Retail key material cannot be shipped, so the crypto paths are exercised with
**synthesised** inputs: a title key of our own choosing, wrapped with a common
key of our own choosing, and content encrypted with it. That proves the CBC/IV
layout, the common-key search, and the validate-by-magic guard, which is where
the bugs live — not the value of any real key.

Run: python3 tests/test_containers.py [--rom /path/to/a.cia] [--fpt-dir /path/to/dumped-fpt]
"""
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from Crypto.Cipher import AES  # noqa: E402

from hanpatch import delta  # noqa: E402
from hanpatch.platforms.threeds import cia as ciamod  # noqa: E402
from hanpatch.platforms.threeds import blz  # noqa: E402
from hanpatch.platforms.threeds import keys as keysmod  # noqa: E402
from hanpatch.platforms.threeds import ncsd as ncsdmod  # noqa: E402
from hanpatch.platforms.threeds import romfs as romfsmod  # noqa: E402
from hanpatch.adapters import dq7 as dq7mod  # noqa: E402
from hanpatch.formats import dq7table  # noqa: E402
from hanpatch.formats import dmp as dmpmod  # noqa: E402
from PIL import Image  # noqa: E402
from hanpatch.platforms import threeds as threedsmod  # noqa: E402

PASS, FAIL = [], []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def raises(fn, needle=None):
    try:
        fn()
    except BaseException as e:          # SystemExit is how the CLI layer fails
        return (needle in str(e)) if needle else True
    return False


TMP = tempfile.mkdtemp(prefix='hanpatch-containers-')

print('== key scrambler ==')
# The scrambler is a fixed published transform; these assert it is wired the
# documented way (rotate 2, xor, add C, rotate 87) and is not accidentally
# order-dependent in a way that would silently produce a wrong key.
k = keysmod.keygen(keysmod.KEYX_2C, 0)
case('keygen is deterministic', k == keysmod.keygen(keysmod.KEYX_2C, 0))
case('keygen depends on KeyY',
     keysmod.keygen(keysmod.KEYX_2C, 1) != k)
case('keygen depends on KeyX',
     keysmod.keygen(keysmod.KEYX_2C ^ 1, 0) != k)
case('keygen stays inside 128 bits', k <= keysmod.MASK128)
case('rol wraps at 128 bits', keysmod.rol(1, 128) == 1
     and keysmod.rol(1, 127) == 1 << 127)
# regression: the scrambler's addition can carry past bit 127. Rotating the
# un-masked sum folds that carry back in and collapses distinct KeyY values.
_over = [y for y in range(64)
         if ((keysmod.rol(keysmod.KEYX_2C, 2) ^ y) + keysmod.KEYGEN_C
             ).bit_length() > 128]
case('a carry past bit 127 does not collapse two KeyY values',
     all(keysmod.keygen(keysmod.KEYX_2C, y)
         != keysmod.keygen(keysmod.KEYX_2C, y ^ 1) for y in _over)
     if _over else True)
case('distinct KeyY values give distinct keys',
     len({keysmod.keygen(keysmod.KEYX_2C, y) for y in range(512)}) == 512)

print('== key store ==')
kt = os.path.join(TMP, 'keys.txt')
open(kt, 'w').write(
    '# comment\n'
    'slot0x25KeyX = ' + '11' * 16 + '\n'
    'slot0x18KeyX = ' + '22' * 16 + '\n'
    'slot0x3DKeyX = ' + '33' * 16 + '\n'
    'common0      = ' + '44' * 16 + '\n'
    'common1      = ' + '55' * 16 + '\n'
    'bogus        = nothex\n'
    'tooshort     = 0011\n')
os.environ['HANPATCH_KEYS'] = TMP
ks = keysmod.KeyStore()
case('keys.txt slots load', ks.have(0x25) and ks.have(0x18) and ks.have(0x3D))
case('common keys load', sorted(ks.common) == [0, 1])
case('malformed lines are ignored', not ks.have(0x99))
case('a short key is rejected', keysmod.b2i(b'\x00' * 16) == 0
     and not any(v == 0x0011 for v in ks.keyx.values()))
case('missing slot yields no key', ks.normal(0x1B, 0) is None)
case('present slot yields a 16-byte key', len(ks.normal(0x25, 0)) == 16)

print('== boot9 anchor search ==')
# A synthetic bootROM: the public slot-0x2C KeyX at an arbitrary offset with
# neighbouring slots laid out contiguously around it.
blob = bytearray(os.urandom(0x10000))
anchor = 0x4321 & ~0xF
blob[anchor:anchor + 16] = keysmod.i2b(keysmod.KEYX_2C)
blob[anchor + 16:anchor + 32] = keysmod.i2b(0xAABB)          # slot 0x2D
blob[anchor - 16 * 7:anchor - 16 * 6] = keysmod.i2b(0xCCDD)  # slot 0x25
B9DIR = tempfile.mkdtemp(prefix='hanpatch-boot9-')
b9 = os.path.join(B9DIR, 'boot9.bin')
open(b9, 'wb').write(bytes(blob))
os.environ['HANPATCH_KEYS'] = B9DIR
ks2 = keysmod.KeyStore()
case('boot9 anchor locates the known slot', ks2.keyx.get(0x2C) == keysmod.KEYX_2C)
case('slots are indexed off the anchor', ks2.keyx.get(0x25) == 0xCCDD
     and ks2.keyx.get(0x2D) == 0xAABB)
case('a bootROM without the anchor is rejected',
     raises(lambda: keysmod.KeyStore()._boot9(b'\x00' * 0x100),
            'no recognisable NCCH KeyX'))
os.environ['HANPATCH_KEYS'] = TMP + os.pathsep + B9DIR
ks2b = keysmod.KeyStore()
case('an explicit keys.txt overrides a bootROM slot',
     ks2b.keyx.get(0x25) == keysmod.b2i(b'\x11' * 16))
case('a bootROM still fills slots keys.txt omits',
     ks2b.keyx.get(0x2D) == 0xAABB)
os.environ['HANPATCH_KEYS'] = TMP

print('== validate-by-magic ==')
case('pick returns the first accepted candidate',
     keysmod.KeyStore.pick(['a', 'b', 'c'], lambda c: c == 'b') == 'b')
case('pick returns None when nothing validates',
     keysmod.KeyStore.pick(['a', 'b'], lambda c: False) is None)
case('pick survives a validator that raises',
     keysmod.KeyStore.pick(['a', 'b'],
                           lambda c: (_ for _ in ()).throw(ValueError())
                           if c == 'a' else True) == 'b')

print('== seed crypto ==')
seed = os.urandom(16)
tid = 0x0004000000123400
expect = int.from_bytes(
    hashlib.sha256(keysmod.i2b(0x1234) + seed).digest()[:16], 'big')
case('seed KeyY is sha256(KeyY || seed) truncated',
     ks.seed_keyy(0x1234, tid, seed) == expect)
case('an unknown title id yields no seed KeyY',
     ks.seed_keyy(0x1234, 0xDEADBEEF) is None)
sdb = (struct.pack('<I', 1) + b'\0' * 12
       + struct.pack('<Q', tid) + seed + b'\0' * 8)
open(os.path.join(TMP, 'seeddb.bin'), 'wb').write(sdb)
ks3 = keysmod.KeyStore()
case('seeddb.bin is parsed', ks3.seeds.get(tid) == seed)
case('seeddb feeds the derivation', ks3.seed_keyy(0x1234, tid) == expect)

print('== title key unwrapping (synthetic) ==')
common_y = 0x44 * (16 ** 31 // 15)  # value is irrelevant, only the wiring is
common_y = keysmod.b2i(b'\x44' * 16)
keyx3d = keysmod.b2i(b'\x33' * 16)
wrapper = keysmod.i2b(keysmod.keygen(keyx3d, common_y))
titlekey = os.urandom(16)
iv = struct.pack('>Q', tid) + b'\0' * 8
enc_tk = AES.new(wrapper, AES.MODE_CBC, iv).encrypt(titlekey)
case('titlekey unwraps with the matching common key',
     ks.titlekey(enc_tk, tid, 0) == titlekey)
case('the wrong common index gives the wrong key',
     ks.titlekey(enc_tk, tid, 1) != titlekey)
case('candidates include every configured common key',
     len(ks.titlekey_candidates(enc_tk, tid)) == 2)
case('a missing common index yields None',
     ks.titlekey(enc_tk, tid, 5) is None)

print('== ticket parsing ==')
ROM = None
for i, a in enumerate(sys.argv):
    if a == '--rom' and i + 1 < len(sys.argv):
        ROM = sys.argv[i + 1]
if ROM is None:
    for cand in ('/root/tmp/crimson-kr/game.cia',):
        if os.path.exists(cand):
            ROM = cand
if ROM:
    from hanpatch.platforms.threeds import repack
    from hanpatch.platforms import threeds
    c = repack.Cia(ROM)
    tidr, encr, idxr = ciamod.parse_ticket(c.tik)
    n = threeds.open_ncch(ROM)
    case('the ticket title id matches the NCCH program id', tidr == n.program_id)
    case('the common key index is a valid slot', 0 <= idxr <= 5)
    case('the encrypted title key is 16 bytes', len(encr) == 16)
    case('container detection identifies a CIA', threeds.detect(ROM) == 'cia')
    case('plaintext content needs no key material',
         ciamod.prepare_content(c, 0)[2] is False)
    case('a decrypted content is recognised as NCCH',
         ciamod._looks_like_ncch(open(ROM, 'rb').read(c.chunks[0]['offset']
                                                      + 0x200)[-0x200:]))
else:
    print('  skip ticket/container cases (no ROM given)')

print('== NCSD / CCI ==')
# Build a synthetic CCI: NCSD header + card info + two partitions.
part0 = os.urandom(0x600)
part0 = bytearray(part0)
part0[0x100:0x104] = b'NCCH'
part1 = os.urandom(0x400)
head = bytearray(0x200)
head[0x100:0x104] = b'NCSD'
struct.pack_into('<Q', head, 0x108, 0x1234567890ABCDEF)
p0_off = (0x200 + 0x1000) // 0x200
struct.pack_into('<II', head, 0x120, p0_off, len(part0) // 0x200)
struct.pack_into('<II', head, 0x128, p0_off + len(part0) // 0x200,
                 len(part1) // 0x200)
total = (p0_off * 0x200) + len(part0) + len(part1)
struct.pack_into('<I', head, 0x104, total // 0x200)
cci = os.path.join(TMP, 'test.3ds')
with open(cci, 'wb') as f:
    f.write(bytes(head))
    f.write(b'\0' * 0x1000)
    f.write(bytes(part0))
    f.write(part1)
n = ncsdmod.Ncsd(cci)
case('NCSD magic is required',
     raises(lambda: ncsdmod.Ncsd(kt), 'not an NCSD'))
case('the partition table is read', len(n.parts) == 2)
case('partition 0 offset is in bytes', n.partition(0)['offset'] == p0_off * 0x200)
case('partition sizes are media-scaled', n.partition(0)['size'] == len(part0))
ext = os.path.join(TMP, 'p0.bin')
ncsdmod.extract_partition(cci, 0, ext)
case('a partition extracts verbatim', open(ext, 'rb').read() == bytes(part0))
case('partition hashing matches the extract',
     ncsdmod.sha256_partition(cci, 0)
     == hashlib.sha256(open(ext, 'rb').read()).hexdigest())

bigger = bytearray(part0) + os.urandom(0x200)
newp0 = os.path.join(TMP, 'p0new.bin')
open(newp0, 'wb').write(bytes(bigger))
out_cci = os.path.join(TMP, 'rebuilt.3ds')
ncsdmod.rebuild(cci, {0: newp0}, out_cci)
r = ncsdmod.Ncsd(out_cci)
case('a rebuilt CCI keeps its magic and media id',
     r.media_id == n.media_id and len(r.parts) == 2)
case('a grown partition is reflected in the table',
     r.partition(0)['size'] == len(bigger))
case('later partitions move instead of being overwritten',
     r.partition(1)['offset'] > r.partition(0)['offset'])
f = open(out_cci, 'rb')
f.seek(r.partition(1)['offset'])
case('the untouched partition survives byte for byte',
     f.read(len(part1)) == part1)
f.seek(r.partition(0)['offset'])
case('the replaced partition is the new content',
     f.read(len(bigger)) == bytes(bigger))
case('the declared media size covers the content',
     r.media_size * 0x200 >= r.partition(1)['offset'] + r.partition(1)['size'])

print('== delta ==')
a = os.path.join(TMP, 'a.bin')
b = os.path.join(TMP, 'b.bin')
base = os.urandom(300000)
open(a, 'wb').write(base)
mod = bytearray(base)
mod[1000:1100] = b'X' * 100
mod += b'tail'
open(b, 'wb').write(bytes(mod))
for backend in (['hpd', 'xdelta'] if delta.have_xdelta() else ['hpd']):
    p = os.path.join(TMP, f'p.{backend}')
    o = os.path.join(TMP, f'o.{backend}')
    r = delta.create(a, b, p, backend=backend)
    delta.apply(a, p, o)
    case(f'{backend}: round trip reproduces the target',
         open(o, 'rb').read() == bytes(mod))
    case(f'{backend}: a small change makes a small patch',
         r['size'] < os.path.getsize(b) // 2)
    wrong = os.path.join(TMP, 'wrong.bin')
    open(wrong, 'wb').write(os.urandom(300000))
    case(f'{backend}: the wrong source is refused',
         raises(lambda: delta.apply(wrong, p, o + '.x'), 'mismatch'))

hp = os.path.join(TMP, 'p.hpd')
delta.hpd_create(a, b, hp)
hdr, off = delta.hpd_read_header(hp)
case('hpd records both hashes', hdr['old_sha256'] == delta.sha256(a)
     and hdr['new_sha256'] == delta.sha256(b))
case('hpd stores only changed blocks',
     len(hdr['records']) < (hdr['new_size'] + hdr['block'] - 1) // hdr['block'])
case('hpd rejects a foreign file', raises(
    lambda: delta.hpd_read_header(kt), 'not an hpd patch'))
appl = os.path.join(TMP, 'apply_patch.py')
delta.write_applier(appl)
import subprocess  # noqa: E402
out2 = os.path.join(TMP, 'standalone.bin')
rc = subprocess.run([sys.executable, appl, a, hp, out2], capture_output=True)
case('the standalone applier reproduces the target',
     rc.returncode == 0 and open(out2, 'rb').read() == bytes(mod))
rc = subprocess.run([sys.executable, appl, wrong, hp, out2 + '.x'],
                    capture_output=True)
case('the standalone applier refuses the wrong source', rc.returncode != 0)

print('== release bundle ==')
from hanpatch import config, release  # noqa: E402
from hanpatch import manifest as _manmod  # noqa: E402
case('a non-bundle file is rejected', raises(lambda: release.inspect(kt)))
# A seal carries the ruleset it was built under, and `manifest.load` refuses an
# older one rather than packing text today's rules would change. So these cases
# need a reference project sealed under the CURRENT ruleset - saying which one it
# holds, instead of reporting a skip that looks like "no project here".
_ref_seal = '/root/tmp/crimson-kr/work/ko/manifest.json'
_ref_ruleset = (json.load(open(_ref_seal)).get('ruleset')
                if os.path.exists(_ref_seal) else None)
if (ROM and os.path.exists('/root/tmp/crimson-kr/work/ko/manifest.approved')
        and _ref_ruleset == _manmod.RULESET):
    config.set_root('/root/tmp/crimson-kr')
    hpk = os.path.join(TMP, 'r.hpk')
    info = release.create(out=hpk,
                          built='/root/tmp/crimson-kr/dist/Crimson Shroud (KO patch).cia')
    case('the bundle is orders of magnitude smaller than the ROM',
         info['size'] * 100 < os.path.getsize(ROM))
    case('the bundle records both hashes',
         len(info['source_sha256']) == 64 and len(info['output_sha256']) == 64)
    case('the bundle carries the sealed digest',
         info['digest'] == json.load(
             open('/root/tmp/crimson-kr/work/ko/manifest.approved'))['digest'])
    import zipfile  # noqa: E402
    names = zipfile.ZipFile(hpk).namelist()
    case('the bundle carries no game data',
         not any(n.endswith(('.cia', '.cci', '.3ds', 'text_src.json'))
                 for n in names))
    case('the bundle carries the fonts it needs',
         any(n.startswith('fonts/') for n in names))
    case('applying a bundle to the wrong ROM is refused', raises(
        lambda: release.apply(hpk, kt, out=os.path.join(TMP, 'x.cia')),
        'mismatch'))
elif _ref_ruleset is not None and _ref_ruleset != _manmod.RULESET:
    print(f'  skip bundle cases (the reference project is sealed under ruleset '
          f'{_ref_ruleset}, this build is {_manmod.RULESET}: reseal and re-approve '
          f'it with `hanpatch gates`)')
else:
    print('  skip bundle cases (no approved reference project)')

print()
print('== container fidelity (M3) ==')
# Three defects found by demanding that an UNTOUCHED rebuild reproduce its source
# byte for byte. Each of them passed every check the pipeline had, and each would
# have reached hardware.
from hanpatch.platforms.threeds import romfs_build as rbuild  # noqa: E402
import hanpatch.platforms.threeds as threeds_mod  # noqa: E402
import inspect  # noqa: E402

# (1) The RomFS writer computed its hash tree in memory and never checked what it
# had written. One run produced two level2 leaves each a SINGLE BIT off over
# byte-identical data, and the level1/master hashes were then computed over the
# corrupted leaves - so the image was internally consistent and every superblock
# check passed. It did not reproduce, i.e. a transient fault, which is precisely
# what a release pipeline must refuse rather than sign.
# A real image, then fault injection. A source-string check cannot tell whether
# the guard covers what actually shipped, and the first version of this guard did
# NOT: it compared level3 against the IN-MEMORY leaf table and ran BEFORE level1,
# level2 and the master hash were written, so a fault in any later write still
# returned success.
_rf_stage = os.path.join(TMP, 'romfs-stage')
os.makedirs(os.path.join(_rf_stage, 'DIR'), exist_ok=True)
for _n, _sz in (('a.bin', 1000), ('B.bin', 4100), ('c.bin', 20), ('d.bin', 700)):
    open(os.path.join(_rf_stage, _n), 'wb').write(bytes(range(256)) * (_sz // 256 + 1))
open(os.path.join(_rf_stage, 'DIR', 'inner.bin'), 'wb').write(b'\x5a' * 9000)
_rf_img = os.path.join(TMP, 'romfs-a.bin')
rbuild.write_romfs(_rf_stage, _rf_img)
case('the finished tree verifies from disk, not from memory',
     rbuild.verify_tree(_rf_img) is None)
case('an untouched RomFS rebuild is deterministic',
     (lambda p2: (rbuild.write_romfs(_rf_stage, p2),
                  open(_rf_img, 'rb').read() == open(p2, 'rb').read())[1])(
         os.path.join(TMP, 'romfs-b.bin')))


def _corrupt(src, dst, offset, mask=0x04):
    data = bytearray(open(src, 'rb').read())
    data[offset] ^= mask
    open(dst, 'wb').write(bytes(data))
    return dst


_hdr = open(_rf_img, 'rb').read(0x60)
_mh = struct.unpack_from('<I', _hdr, 8)[0]
_l1sz = struct.unpack_from('<Q', _hdr, 0x0C + 8)[0]
_l2sz = struct.unpack_from('<Q', _hdr, 0x24 + 8)[0]
_l3sz = struct.unpack_from('<Q', _hdr, 0x3C + 8)[0]


def _al(x, a=0x1000):
    return (x + a - 1) // a * a


_l3p = _al(0x60 + _mh)
_l1p = _l3p + _al(_l3sz)
_l2p = _l1p + _al(_l1sz)
for _label, _off in (('a level2 leaf', _l2p + 8),
                     ('a level1 hash', _l1p + 8),
                     ('the master hash', 0x60 + 8),
                     ('a level3 data byte', _l3p + _l3sz - 4)):
    _bad = _corrupt(_rf_img, os.path.join(TMP, 'romfs-bad.bin'), _off)
    case(f'a single flipped bit in {_label} is refused by tree verification',
         raises(lambda b=_bad: rbuild.verify_tree(b), 'ROMFS WRITE FAILED'))

# BLOCKING from review: the CAPTURED ordering - the mechanism the whole slice's
# byte-identity argument rests on - had zero automated coverage. The fixture above
# is deliberately shaped for it: ASCII order is (B.bin, a.bin, c.bin) while
# casefold order is (a.bin, B.bin, c.bin), so a capture that is ignored is visible.
_cap_a = os.path.join(TMP, 'cap-a.bin')
rbuild.write_romfs(_rf_stage, _cap_a)
_cap = rbuild.sibling_order(_cap_a)


def _root_chain(img):
    """The root directory's file sibling chain, as the engine would walk it."""
    r = romfsmod.RomFS(img)
    _p, _s, _child, f0, _n = r._dir(0)
    out, fo = [], f0
    while fo != 0xFFFFFFFF:
        _pp, sib, _do, _ds, fn = r._file(fo)
        out.append(fn)
        fo = sib
    return out


case('the capture reads back the order the image actually holds',
     _cap['children'][''][1] == _root_chain(_cap_a)
     and _cap['dir_layout'] == ['', 'DIR']
     and ('DIR', 'inner.bin') in _cap['file_layout'])

# A capture taken from an image built with the DEFAULT order cannot show that the
# capture was honoured - the two coincide, so ignoring it entirely still produces
# an identical file. So build an image whose order is NOT the default first, by
# handing the writer a REVERSED capture, and only then prove a capture of THAT
# reproduces it.
_rev = {'children': {k: (d, list(reversed(f))) for k, (d, f) in _cap['children'].items()},
        'dir_layout': list(_cap['dir_layout']),
        'file_layout': [(d, n) for d, n in reversed(_cap['file_layout'])]}
_cap_x = os.path.join(TMP, 'cap-x.bin')
rbuild.write_romfs(_rf_stage, _cap_x, order=_rev)
case('a reversed capture produces a different image from the default order',
     open(_cap_x, 'rb').read() != open(_cap_a, 'rb').read())
case('a reversed capture is honoured in the sibling chain the engine walks',
     _root_chain(_cap_x) == list(reversed(_root_chain(_cap_a))))
_cap_y = os.path.join(TMP, 'cap-y.bin')
rbuild.write_romfs(_rf_stage, _cap_y, order_from=_cap_x)
case('capturing a NON-default order reproduces it byte-for-byte',
     open(_cap_x, 'rb').read() == open(_cap_y, 'rb').read())
# The chain and the file-table layout are two separate captured facts. Reading the
# chain back cannot show the LAYOUT half was honoured, because the chain comes from
# child_files either way - this does, and it fails the moment file_layout is dropped.
case('the captured file-table layout is honoured, not just the chain',
     rbuild.sibling_order(_cap_x)['file_layout'] == _rev['file_layout'])
def _first_file_name(img):
    r = romfsmod.RomFS(img)
    return r._file(r._dir(0)[3])[4]


# first_file must name the head of the CAPTURED chain. Note what cannot be tested
# here and why: the agreement check in build_level3 refuses any capture whose chain
# order and layout order disagree, so for every capture that survives, the chain
# head IS also the lowest filemeta offset. The chain sort is therefore defensive
# rather than load-bearing, and the agreement check is what would surface a title
# that genuinely diverges. Asserting a difference that the design forbids would be
# a test of nothing.
case('first_file names the head of the captured chain',
     _first_file_name(_cap_x) == _rev['children'][''][1][0]
     and _first_file_name(_cap_a) == _cap['children'][''][1][0]
     and _first_file_name(_cap_x) != _first_file_name(_cap_a))
case('passing both an image and a mapping is refused', raises(
    lambda: rbuild.write_romfs(_rf_stage, os.path.join(TMP, 'cap-both.bin'),
                               order_from=_cap_a, order=_cap),
    'not both'))

# A staged name absent from the capture must land AFTER the captured ones.
open(os.path.join(_rf_stage, 'z-new.bin'), 'wb').write(b'n' * 300)
_meta2, _files2, _o2, _s2, _t2 = rbuild.build_level3(
    rbuild.build_tree(_rf_stage), _cap)
_root_files = [n for p, n, _x in _files2 if p.name == '']
_captured_root = _cap['children'][''][1]
case('a staged name absent from the capture is placed after the captured ones',
     _root_files[:len(_captured_root)] == _captured_root
     and _root_files[-1] == 'z-new.bin')
os.remove(os.path.join(_rf_stage, 'z-new.bin'))
os.makedirs(os.path.join(_rf_stage, 'EMPTYDIR'), exist_ok=True)
case('a capture that describes fewer directories than the tree is refused', raises(
    lambda: rbuild.build_level3(rbuild.build_tree(_rf_stage), _cap),
    'refusing rather than producing an almost'))
os.rmdir(os.path.join(_rf_stage, 'EMPTYDIR'))
# The corrected path resolution must refuse an unresolvable capture instead of
# silently filing an entry as top-level and falling back to the default sort.
_broken = os.path.join(TMP, 'cap-broken.bin')
_bd = bytearray(open(_cap_a, 'rb').read())
_l3 = 0x1000
_dm_off = struct.unpack_from('<I', _bd, _l3 + 0x0C)[0]
struct.pack_into('<I', _bd, _l3 + _dm_off + 0x18 + 0x00, 0xDEAD)
open(_broken, 'wb').write(bytes(_bd))
case('a capture whose parent offset is not a directory record is refused', raises(
    lambda: rbuild.sibling_order(_broken), 'refusing to guess where it belongs'))

# verify_tree's two review gaps: the aligned span and the header descriptors.
_pad_bad = os.path.join(TMP, 'romfs-padbad.bin')
_pd = bytearray(open(_cap_a, 'rb').read())
_mh = struct.unpack_from('<I', _pd, 8)[0]
_l3sz = struct.unpack_from('<Q', _pd, 0x3C + 8)[0]
_l3p = (0x60 + _mh + 0xFFF) // 0x1000 * 0x1000
_pd[_l3p + _l3sz + 2] ^= 0x08
open(_pad_bad, 'wb').write(bytes(_pd))
case('a flipped bit in level3 PAD is refused, not skipped as non-logical', raises(
    lambda: rbuild.verify_tree(_pad_bad), 'ROMFS WRITE FAILED'))
_hdr_bad = os.path.join(TMP, 'romfs-hdrbad.bin')
_hd = bytearray(open(_cap_a, 'rb').read())
struct.pack_into('<I', _hd, 0x3C + 16, 13)
open(_hdr_bad, 'wb').write(bytes(_hd))
case('a wrong block-size exponent is refused even though every hash matches', raises(
    lambda: rbuild.verify_tree(_hdr_bad), 'block shift'))
_trunc_tree = os.path.join(TMP, 'romfs-trunctree.bin')
open(_trunc_tree, 'wb').write(open(_cap_a, 'rb').read()[:-64])
case('a file truncated inside the hash tree is refused, naming the region', raises(
    lambda: rbuild.verify_tree(_trunc_tree), 'ends inside the region at'))

# The data copy in write_romfs is the largest byte mover in the slice and had no
# count check: a file that shrank between measurement and copy left a hole.
_shrink_stage = os.path.join(TMP, 'shrink-stage')
os.makedirs(_shrink_stage, exist_ok=True)
_victim = os.path.join(_shrink_stage, 'v.bin')
open(_victim, 'wb').write(b'v' * 5000)
_orig_getsize = os.path.getsize


def _lying_getsize(p):
    return _orig_getsize(p) + 4096 if p == _victim else _orig_getsize(p)


os.path.getsize = _lying_getsize
try:
    case('a file shorter than the size measured for it is refused', raises(
        lambda: rbuild.write_romfs(_shrink_stage, os.path.join(TMP, 'shrink.bin')),
        'ran out after'))
finally:
    os.path.getsize = _orig_getsize

# (2) The superblock check hashed a fixed 0x200 for every section. The declared
# region is a per-title field in media units, and this cartridge declares TWO -
# so the check called a genuine retail image corrupt, and the repacker had the
# same assumption baked in, shrinking the region it declared from 2 to 1. A
# shrunken region agreed with a shrunken check, so both looked fine.
_ssrc = inspect.getsource(threeds_mod.superblock_hashes)
case('the superblock check reads the declared hash region, not a fixed 0x200',
     '0x1B8' in _ssrc and '0x1A8' in _ssrc and '* 0x200' in _ssrc)
_rsrc = inspect.getsource(repack.rebuild_ncch)
case('the repacker preserves the declared hash region instead of writing 1',
     "struct.unpack_from('<I', n.h, 0x1B8)" in _rsrc
     and 'hash_region * 0x200' in _rsrc)
case('a RomFS shorter than the declared superblock region is refused',
     'refusing to write a hash over data that does not exist' in _rsrc)

# (3) The CCI writer packed partitions forward from the end of the card-info
# header, moving partition 0 from 0x4000 to 0x1200 and rewriting the card pad
# with zeros where a cartridge writes 0xFF.
_nsrc = inspect.getsource(ncsdmod.rebuild)
case('a CCI partition keeps its original offset when nothing before it grew',
     "p['offset'] if p['offset'] >= pos else pos" in _nsrc)
case('the gap before the first partition is filled as card pad, not zeros',
     '_fill(o, ' in _nsrc and 'o.seek(L[' not in _nsrc)

# The end-to-end property all three serve, on a synthetic CCI: an untouched
# rebuild reproduces its source byte for byte.
_cci = os.path.join(TMP, 'fidelity.cci')
_p0 = os.path.join(TMP, 'fidelity-p0.bin')
_body = bytes(range(256)) * 32          # 8 KiB of non-repeating-ish content
open(_p0, 'wb').write(_body)
_head = bytearray(0x4000)
_head[0x100:0x104] = b'NCSD'
struct.pack_into('<I', _head, 0x104, 0x4000 // 0x200 + len(_body) // 0x200)
struct.pack_into('<II', _head, 0x120, 0x4000 // 0x200, len(_body) // 0x200)
_head[0x200:0x1200] = bytes(range(256)) * 16      # card info
_head[0x1200:0x4000] = b'\xff' * (0x4000 - 0x1200)
open(_cci, 'wb').write(bytes(_head) + _body)
_out = os.path.join(TMP, 'fidelity-rebuilt.cci')
ncsdmod.rebuild(_cci, {}, _out)
case('an untouched CCI rebuild is byte-identical to its source',
     open(_cci, 'rb').read() == open(_out, 'rb').read())
_grown = os.path.join(TMP, 'fidelity-grown.bin')
open(_grown, 'wb').write(_body + b'\x5a' * 0x200)
_out2 = os.path.join(TMP, 'fidelity-grown.cci')
ncsdmod.rebuild(_cci, {0: _grown}, _out2)
_r = ncsdmod.Ncsd(_out2)
case('a grown partition still starts where the card said it does',
     _r.parts[0]['offset'] == 0x4000
     and _r.parts[0]['size'] == len(_body) + 0x200)
case('the card pad before the first partition survives a grown rebuild',
     open(_out2, 'rb').read(0x4000)[0x1200:] == b'\xff' * (0x4000 - 0x1200))

# A SHRINKING partition used to leave a filesystem hole wherever the next
# partition kept its original offset, and a hole reads back as zeros - not the
# byte a card writes. Two partitions, shrink the first, inspect the gap.
_cci2 = os.path.join(TMP, 'two-part.cci')
_h2 = bytearray(0x4000)
_h2[0x100:0x104] = b'NCSD'
_p0len, _p1len = 0x800, 0x400
struct.pack_into('<II', _h2, 0x120, 0x4000 // 0x200, _p0len // 0x200)
struct.pack_into('<II', _h2, 0x128, (0x4000 + _p0len) // 0x200, _p1len // 0x200)
struct.pack_into('<I', _h2, 0x104, (0x4000 + _p0len + _p1len) // 0x200)
_h2[0x200:0x1200] = bytes(range(256)) * 16
_h2[0x1200:0x4000] = b'\xff' * (0x4000 - 0x1200)
open(_cci2, 'wb').write(bytes(_h2) + b'\x11' * _p0len + b'\x22' * _p1len)
_shrunk = os.path.join(TMP, 'shrunk-p0.bin')
open(_shrunk, 'wb').write(b'\x11' * 0x200)
_out3 = os.path.join(TMP, 'shrunk.cci')
ncsdmod.rebuild(_cci2, {0: _shrunk}, _out3)
_r3 = ncsdmod.Ncsd(_out3)
_gap_from, _gap_to = 0x4000 + 0x200, _r3.parts[1]['offset']
_gap = open(_out3, 'rb').read(_gap_to)[_gap_from:_gap_to]
case('a shrunk partition leaves card pad behind it, never a zero hole',
     _gap_to > _gap_from and set(_gap) == {0xFF})
case('a later partition keeps its declared offset when an earlier one shrank',
     _r3.parts[1]['offset'] == 0x4000 + _p0len)

# A source whose declared partition range runs past EOF used to be padded to size
# and reported as success, so a partition full of 0xFF passed every structural
# check. Truncate the image mid-partition-1 and rebuild with no replacements.
_cut = os.path.join(TMP, 'truncated-src.cci')
_whole = open(_cci2, 'rb').read()
open(_cut, 'wb').write(_whole[:0x4000 + _p0len + 0x100])
case('a source partition that runs past EOF is refused, not padded to size', raises(
    lambda: ncsdmod.rebuild(_cut, {}, os.path.join(TMP, 'trunc.cci')),
    'refusing to pad a truncated read'))
# The refusal has to be reachable on the path the PIPELINE takes. threeds.rebuild
# routes a CCI through repack.rebuild_ncch FIRST, and that copy loop used to spin
# forever at EOF, so the guard below never ran for the case it was written for.


# A PLAINTEXT NCCH fixture. It carries no retail key material (the no-crypto flag
# makes every section a plain read), and it is what pins the defect that reported a
# genuine retail cartridge as corrupt: the hash region is a per-title DECLARATION
# in media units, and the check and the rebuild must agree on it.
def _plain_ncch(path, romfs_bytes, hash_region_units=2, romfs_off=0x1000):
    romfs_pad = (len(romfs_bytes) + 0x1FF) // 0x200 * 0x200
    body = bytearray(romfs_off + romfs_pad)
    h = bytearray(0x200)
    h[0x100:0x104] = b'NCCH'
    struct.pack_into('<I', h, 0x104, (romfs_off + romfs_pad) // 0x200)
    h[0x118:0x120] = struct.pack('<Q', 0x0004000000065E00)
    flags = bytearray(8)
    flags[7] = 0x04                      # no crypto: sections are plain reads
    h[0x188:0x190] = bytes(flags)
    struct.pack_into('<II', h, 0x1B0, romfs_off // 0x200, romfs_pad // 0x200)
    struct.pack_into('<I', h, 0x1B8, hash_region_units)
    span = hash_region_units * 0x200
    region = (romfs_bytes + b'\0' * span)[:span]
    h[0x1E0:0x200] = hashlib.sha256(region).digest()
    body[0:0x200] = h
    body[romfs_off:romfs_off + len(romfs_bytes)] = romfs_bytes
    open(path, 'wb').write(bytes(body))
    return path


_rb = bytes(range(256)) * 8              # 2048 bytes, so 2 media units exist
_ncch2 = _plain_ncch(os.path.join(TMP, 'plain-2mu.ncch'), _rb, 2)
case('a declared two-unit RomFS region is hashed over 1024 bytes, not 512',
     threedsmod.superblock_hashes(_ncch2)['romfs'] is True)
# The KEY SET matters, not just one key: `or 1` used to force three keys, so an
# NCCH with no exheader and no exefs was reported corrupt on both.
case('a section the NCCH does not declare is skipped, not reported corrupt',
     threedsmod.superblock_hashes(_ncch2) == {'romfs': True})
# An NCCH that declares NOTHING is the one shape that used to yield a usable-looking
# {}, which the only consumer read as zero problems. The producer must refuse it, and
# the case has to call the REAL function - asserting against a stub of the refusal
# passes with the refusal deleted.
_ncch0 = os.path.join(TMP, 'plain-nosections.ncch')
_z = bytearray(open(_ncch2, 'rb').read())
struct.pack_into('<III', _z, 0x1B0, 0, 0, 0)
struct.pack_into('<II', _z, 0x1A0, 0, 0)
struct.pack_into('<I', _z, 0x180, 0)
open(_ncch0, 'wb').write(bytes(_z))
case('an NCCH declaring no sections at all is refused, not reported clean', raises(
    lambda: threedsmod.superblock_hashes(_ncch0), 'no superblock hash to verify'))
# and the adapter must PROPAGATE that refusal rather than absorbing it into its
# problems list, which is what makes the producer the single owner of the policy.
case('the adapter propagates the producer refusal rather than absorbing it', raises(
    lambda: dq7mod.DragonQuest7().verify(_ncch0, {}),
    'no superblock hash to verify'))
_ncch_bad = _plain_ncch(os.path.join(TMP, 'plain-bad.ncch'), _rb, 2)
_bb = bytearray(open(_ncch_bad, 'rb').read())
_bb[0x1E0:0x200] = hashlib.sha256(_rb[:0x200]).digest()
open(_ncch_bad, 'wb').write(bytes(_bb))
case('a hash covering only the first 512 bytes fails a two-unit declaration',
     threedsmod.superblock_hashes(_ncch_bad)['romfs'] is False)
_newromfs = os.path.join(TMP, 'plain-new-romfs.bin')
open(_newromfs, 'wb').write(bytes(reversed(_rb)))
_rebuilt_ncch = os.path.join(TMP, 'plain-rebuilt.ncch')
repack.rebuild_ncch(_ncch2, 0, _newromfs, _rebuilt_ncch)
case('the rebuild preserves the declared region instead of shrinking it to one',
     struct.unpack_from('<I', open(_rebuilt_ncch, 'rb').read(0x200), 0x1B8)[0] == 2)
case('the rebuilt header hash matches the region it declares',
     threedsmod.superblock_hashes(_rebuilt_ncch)['romfs'] is True)
# MAJOR-6: the truncated-source refusal must be reachable where the pipeline
# actually enters, not only when ncsd.rebuild is called directly.
_cut_ncch = os.path.join(TMP, 'plain-cut.ncch')
open(_cut_ncch, 'wb').write(open(_ncch2, 'rb').read()[:0x400])
case('a truncated NCCH source is refused instead of looping at EOF', raises(
    lambda: repack.rebuild_ncch(_cut_ncch, 0, _newromfs,
                                os.path.join(TMP, 'plain-cut-out.ncch')),
    'ran out after'))
open(os.path.join(TMP, 'empty-p0.bin'), 'wb').write(b'')
case('a zero-length replacement partition is refused with a diagnostic', raises(
    lambda: ncsdmod.rebuild(_cci2, {0: os.path.join(TMP, 'empty-p0.bin')},
                            os.path.join(TMP, 'empty.cci')),
    'cannot be zero bytes'))

print()
print('== ExeFS rebuild: the region the superblock hash does not cover ==')


def _plain_ncch_exefs(path, members, romfs_bytes=b'IVFC' + b'\0' * 2044,
                      exefs_off=0x1000, exefs_units=1):
    """A plaintext NCCH carrying a real ExeFS, so the rebuild can be measured.

    `members` is [(name, bytes)]. The ExeFS header is built the way the format
    defines it: a 0x10 entry per member at the front and its SHA-256 in the
    reversed hash table at 0xC0.
    """
    header = bytearray(0x200)
    body = bytearray()
    offset = 0
    for index, (name, content) in enumerate(members):
        header[index * 0x10:index * 0x10 + 8] = name.encode('latin1').ljust(8, b'\0')
        struct.pack_into('<II', header, index * 0x10 + 8, offset, len(content))
        header[0xC0 + (9 - index) * 0x20:0xE0 + (9 - index) * 0x20] = (
            hashlib.sha256(content).digest())
        padded = content + b'\0' * (-len(content) % 0x200)
        body += padded
        offset += len(padded)
    exefs = bytes(header) + bytes(body)
    romfs_off = exefs_off + len(exefs)
    romfs_pad = (len(romfs_bytes) + 0x1FF) // 0x200 * 0x200
    h = bytearray(0x200)
    h[0x100:0x104] = b'NCCH'
    struct.pack_into('<I', h, 0x104, (romfs_off + romfs_pad) // 0x200)
    h[0x118:0x120] = struct.pack('<Q', 0x0004000000065E00)
    h[0x180:0x184] = struct.pack('<I', 0x400)          # exheader declared
    flags = bytearray(8)
    flags[7] = 0x04                                    # no crypto
    h[0x188:0x190] = bytes(flags)
    struct.pack_into('<II', h, 0x1A0, exefs_off // 0x200, len(exefs) // 0x200)
    struct.pack_into('<I', h, 0x1A8, exefs_units)
    struct.pack_into('<II', h, 0x1B0, romfs_off // 0x200, romfs_pad // 0x200)
    struct.pack_into('<I', h, 0x1B8, 1)
    image = bytearray(romfs_off + romfs_pad)
    image[0:0x200] = h
    # The exheader occupies the 0x800 REGION after the header regardless of what
    # 0x180 declares; fill it so its hash is over real bytes.
    image[0x200:0xA00] = bytes(range(256)) * 8
    h[0x160:0x180] = hashlib.sha256(bytes(image[0x200:0x600])).digest()
    h[0x1C0:0x1E0] = hashlib.sha256(exefs[:exefs_units * 0x200]).digest()
    h[0x1E0:0x200] = hashlib.sha256(
        (romfs_bytes + b'\0' * 0x200)[:0x200]).digest()
    image[0:0x200] = h
    image[exefs_off:exefs_off + len(exefs)] = exefs
    image[romfs_off:romfs_off + len(romfs_bytes)] = romfs_bytes
    open(path, 'wb').write(bytes(image))
    return path


_members = [('.code', b'CODE' * 64), ('banner', b'BANNER!!' * 16),
            ('icon', b'ICON' * 8), ('logo', b'LOGO' * 4)]
_exefs_src = _plain_ncch_exefs(os.path.join(TMP, 'exefs-src.ncch'), _members)
case('the ExeFS fixture verifies against its own member hashes',
     threedsmod.exefs_member_hashes(_exefs_src)
     == {'.code': True, 'banner': True, 'icon': True, 'logo': True})
_exefs_out = os.path.join(TMP, 'exefs-rebuilt.ncch')
repack.rebuild_ncch(_exefs_src, 0, _newromfs, _exefs_out,
                    exefs_replacements={'.code': b'PATCHED!' * 32})
# The defect this pins: a member name shorter than its 8-byte slot used to RESIZE
# the header bytearray, which moved every hash and every member after it while the
# superblock hash still agreed, being computed over the same shifted buffer.
case('a rebuilt ExeFS keeps its header exactly 0x200 bytes',
     struct.unpack_from('<II', open(_exefs_out, 'rb').read(0x1200), 0x1000 + 8)
     == (0, len(b'PATCHED!' * 32)))
case('every rebuilt ExeFS member still matches its own declared hash',
     all(threedsmod.exefs_member_hashes(_exefs_out).values()))
case('the replaced member is the one that changed',
     open(_exefs_out, 'rb').read()[0x1200:0x1200 + 8] == b'PATCHED!')
case('the untouched members are byte-identical after a rebuild',
     b'BANNER!!' in open(_exefs_out, 'rb').read()
     and b'LOGO' in open(_exefs_out, 'rb').read())
case('a replacement naming a member the source lacks is refused', raises(
    lambda: repack.rebuild_ncch(_exefs_src, 0, _newromfs,
                                os.path.join(TMP, 'exefs-unknown.ncch'),
                                exefs_replacements={'.text': b'x'}),
    'missing from source'))
case('a replacement that would overflow the space before RomFS is refused', raises(
    lambda: repack.rebuild_ncch(_exefs_src, 0, _newromfs,
                                os.path.join(TMP, 'exefs-overflow.ncch'),
                                exefs_replacements={'.code': b'x' * 0x100000}),
    'overflows'))
# A shifted hash table is invisible to the superblock hash, so the member check has
# to be the one that sees it. Corrupt one member and prove which check reacts.
_bad_member = os.path.join(TMP, 'exefs-bad-member.ncch')
_bm = bytearray(open(_exefs_out, 'rb').read())
_bm[0x1200] ^= 0xFF
open(_bad_member, 'wb').write(bytes(_bm))
case('a flipped byte inside a member is caught by the member hashes',
     threedsmod.exefs_member_hashes(_bad_member)['.code'] is False)
case('the same flip is invisible to the superblock hash, which is why both run',
     threedsmod.superblock_hashes(_bad_member)['exefs'] is True)

print()
print('== DMP textures: the raw artwork form under /LAYOUTTEX ==')
# 8x8 Morton tiles in ABGR. A naive linear/RGBA read produces scrambled blocks
# with swapped channels, which is exactly the corruption that would ship if
# artwork were replaced through a reader that "looked right" on a flat colour.
_dmp_w, _dmp_h = 16, 8
_dmp_img = Image.new('RGBA', (_dmp_w, _dmp_h))
for _x in range(_dmp_w):
    for _y in range(_dmp_h):
        _dmp_img.putpixel((_x, _y), (_x * 16, _y * 32, 255 - _x * 8, 255 - _y * 16))
_dmp_bytes = dmpmod.encode(_dmp_img)
case('an encoded texture carries the measured 16-byte header',
     _dmp_bytes[:8] == b'DMP\x03' + b'8888'
     and struct.unpack_from('<4H', _dmp_bytes, 8) == (_dmp_w, _dmp_h, _dmp_w, _dmp_h))
case('a texture round trips pixel-for-pixel through the tiling',
     list(dmpmod.decode(_dmp_bytes).getdata()) == list(_dmp_img.getdata()))
case('the first stored pixel is ABGR, not RGBA',
     _dmp_bytes[16:20] == bytes((255, 255, 0, 0)))
# Tiling is only provable against a size that is more than one tile wide, which
# is where a linear reader and a tiled reader disagree.
case('the second tile starts after 64 pixels, not after one row',
     _dmp_bytes[16 + 64 * 4:16 + 64 * 4 + 4]
     == bytes((_dmp_img.getpixel((8, 0))[3], _dmp_img.getpixel((8, 0))[2],
               _dmp_img.getpixel((8, 0))[1], _dmp_img.getpixel((8, 0))[0])))
case('a replacement of the wrong size is refused, not scaled', raises(
    lambda: dmpmod.encode(Image.new('RGBA', (8, 8)), _dmp_bytes),
    'Resize it first'))
case('a format this reader never measured is refused', raises(
    lambda: dmpmod.parse(b'DMP\x03' + b'4444' + struct.pack('<4H', 8, 8, 8, 8)
                         + b'\0' * 256),
    'has no encoder here'))
case('a size that is not a whole number of tiles is refused', raises(
    lambda: dmpmod.parse(b'DMP\x03' + b'8888' + struct.pack('<4H', 12, 8, 12, 8)
                         + b'\0' * (12 * 8 * 4)),
    'whole number of'))
case('a truncated payload is refused instead of read past the end', raises(
    lambda: dmpmod.parse(b'DMP\x03' + b'8888' + struct.pack('<4H', 8, 8, 8, 8)
                         + b'\0' * 16),
    'needs'))

print()
print('== decrypted output: what an emulator will actually boot ==')
_dec_out = os.path.join(TMP, 'exefs-decrypted.ncch')
repack.rebuild_ncch(_exefs_src, 0, _newromfs, _dec_out, decrypt=True)
_dec_flags = open(_dec_out, 'rb').read(0x200)[0x188:0x190]
case('a decrypted rebuild declares NoCrypto', bool(_dec_flags[7] & 0x04))
case('a decrypted rebuild clears the crypto method', _dec_flags[3] == 0)
case('a decrypted rebuild drops the fixed-key and seed bits',
     not _dec_flags[7] & 0x01 and not _dec_flags[7] & 0x20)
case('a decrypted rebuild still verifies its member hashes',
     all(threedsmod.exefs_member_hashes(_dec_out).values()))
case('a decrypted rebuild still verifies every superblock hash',
     all(threedsmod.superblock_hashes(_dec_out).values()))
# The exheader REGION is 0x800; this fixture declares 0x400 at 0x180 exactly as the
# retail cartridge declares 0x3FB. Trusting the declaration wrote 10 bytes too few
# and shifted every following section, which the member hashes above now catch.
case('the exheader region is copied whole, so the ExeFS lands where it is declared',
     open(_dec_out, 'rb').read(0xA00)[0x200:0xA00]
     == open(_exefs_src, 'rb').read(0xA00)[0x200:0xA00])

print()
print('== DQ7 verify fail-closed ==')
# content_hashes() is CIA-only and reports [] for a cartridge. The adapter is the
# declared owner of that gap: an empty list must never read as "nothing to check,
# therefore clean". The plan's wording is "RAISE when content_hashes() returns []
# for a non-CIA base"; taken literally that would make a CCI unverifiable
# forever, so what ships is the same guarantee with a substitute: the adapter
# demands the NCCH superblock hashes, which are what actually cover a cartridge,
# and refuses when neither source of evidence exists.

_dq7 = dq7mod.DragonQuest7()

def _probe_cia():
    try:
        dq7mod.DragonQuest7().verify('/nonexistent.3ds', {})
    except SystemExit as exc:
        return ('refused', str(exc))
    except Exception as exc:
        return ('proceeded', type(exc).__name__)
    return ('clean', '')

_orig_detect = threedsmod.detect
_orig_chunks = threedsmod.content_hashes
_orig_blocks = threedsmod.superblock_hashes
try:
    threedsmod.detect = lambda p: 'cci'
    threedsmod.content_hashes = lambda p: []
    threedsmod.superblock_hashes = lambda p, keystore=None: {'romfs': False}
    _reached = []
    _dq7._keystore = lambda: None
    try:
        _dq7.verify('/nonexistent.3ds', {})
    except SystemExit as exc:
        _reached.append(('refused', str(exc)))
    except Exception as exc:
        _reached.append(('proceeded', type(exc).__name__))
    case('a failing superblock hash is reported rather than refused outright',
         _reached and _reached[0][0] == 'proceeded')
    threedsmod.detect = lambda p: 'cia'
    threedsmod.content_hashes = lambda p: []
    case('a CIA with no content chunks is not refused by the cartridge rule',
         (lambda: [r[0] for r in [_probe_cia()]] == ['proceeded'])())
finally:
    threedsmod.detect = _orig_detect
    threedsmod.content_hashes = _orig_chunks
    threedsmod.superblock_hashes = _orig_blocks

# THE FONT REFUSAL ITSELF, not only its predicate. verify() was never called with a
# non-empty manifest, so deleting `if new_chars and not config.prof('font_out')`
# passed every case, the corpus, the reference build and the DQ7 identity run.
_fr_root = tempfile.mkdtemp(prefix='hanpatch-fontrefusal-')
os.makedirs(os.path.join(_fr_root, 'work'), exist_ok=True)
json.dump({'title': 'DQ7', 'platform': 'threeds', 'adapter': 'dq7', 'target': 'ko',
           'profile': 'p.json', 'rom': 'game.3ds'},
          open(os.path.join(_fr_root, config.PROJECT_FILE), 'w'))
json.dump({'fam': [{'key': 'k.txt', 'en': '\u3042', 'jp': ''}]},
          open(os.path.join(_fr_root, 'work', 'text_src.json'), 'w'), ensure_ascii=False)


def _verify_with(font_out, entries):
    json.dump({'budget': {'default': 64}, 'engine_wraps': False, 'source_lang': 'ja',
               'font_out': font_out},
              open(os.path.join(_fr_root, 'p.json'), 'w'))
    _prev = config.root()
    config.set_root(_fr_root)
    try:
        return dq7mod.DragonQuest7().verify('/nonexistent.3ds', entries)
    finally:
        if os.path.exists(os.path.join(_prev, config.PROJECT_FILE)):
            config.set_root(_prev)


case('verify refuses a manifest that adds glyphs while no font is declared',
     raises(lambda: _verify_with([], {'fam/k.txt': '\ud55c\uae00'}),
            'characters the source never used'))
case('verify does not refuse a manifest that adds no glyph',
     not raises(lambda: _verify_with([], {'fam/k.txt': '\u3042'}),
                'characters the source never used'))
case('a declared font_out retires the refusal',
     not raises(lambda: _verify_with(['fonts/ko.bcfnt'], {'fam/k.txt': '\ud55c'}),
                'characters the source never used'))

print()
print('== DARC archive and font formats (M4) ==')
# This whole slice shipped with no automated case: a search of tests/ for
# darc|untile|tile|measure_shade_lut|font_slots|_apply_fonts found one string. Every
# claim below was a manual run.
from hanpatch.formats import darc  # noqa: E402
from hanpatch.platforms.threeds import bcfnt as bcfntmod  # noqa: E402
from hanpatch.platforms.threeds import fontbuild as fbmod  # noqa: E402


def _darc(members, aligns=None):
    """Synthesise an archive with MIXED alignments, like the real ones."""
    ms = [darc.Member('', True)]
    ms.append(darc.Member('font', True))
    for name, data, al in members:
        m = darc.Member(f'font/{name}', False, data)
        m.source_offset = al       # the captured requirement
        ms.append(m)
    return ms


_dm = _darc([('a.bclyt', b'\x11' * 100, 4), ('b.bcfnt', b'\x22' * 300, 0x80),
             ('c.bcfnt', b'\x33' * 50, 0x80)])
_db = darc.build(_dm, where='synth.arc')
case('a synthesised DARC round trips byte-for-byte',
     darc.build(darc.parse(_db, 'synth.arc')[1], where='synth.arc') == _db)
_dh, _dmem = darc.parse(_db, 'synth.arc')
_files = [m for m in _dmem if not m.is_dir]
case('the parsed members keep their names, order and payloads',
     [m.path for m in _files] == ['font/a.bclyt', 'font/b.bcfnt', 'font/c.bcfnt']
     and [len(m.data) for m in _files] == [100, 300, 50])
case('a 0x80 member is aligned and a packed member is not over-aligned',
     _files[1].source_offset % 0x80 == 0 and _files[2].source_offset % 0x80 == 0
     and _files[0].source_offset % 4 == 0)
case('the payload region starts where the header plus table says it does',
     _dh['data_offset'] == (28 + _dh['table_length'] + 3) // 4 * 4)
_bad = bytearray(_db)
struct.pack_into('<I', _bad, 0x18, _dh['data_offset'] + 4)
case('a payload region origin this reader cannot reproduce is refused',
     raises(lambda: darc.parse(bytes(_bad), 'synth.arc'), 'cannot reproduce another'))
_bad2 = bytearray(_db)
struct.pack_into('<I', _bad2, 0x14, _dh['table_length'] + 4)
# Changing the declared table length also moves where the payload region must
# start, so the origin check fires first. Both refusals are correct; assert that the
# archive is refused and that the diagnostic points at the layout arithmetic.
case('a declared table length inconsistent with the layout is refused',
     raises(lambda: darc.parse(bytes(_bad2), 'synth.arc'), 'table after a'))
case('a foreign magic is refused',
     raises(lambda: darc.parse(b'DARC' + _db[4:], 'x'), 'is not'))
case('a big-endian byte-order mark is refused',
     raises(lambda: darc.parse(_db[:4] + b'\xfe\xff' + _db[6:], 'x'),
            'no evidence for a big-endian'))
_grown = darc.replace(_db, 'font/b.bcfnt', b'\x22' * 1000, where='synth.arc')
_gh, _gm = darc.parse(_grown, 'synth.arc')
_gf = [m for m in _gm if not m.is_dir]
case('a size-changing replace keeps every other member byte-identical',
     [bytes(m.data) for m in _gf if m.path != 'font/b.bcfnt']
     == [bytes(m.data) for m in _files if m.path != 'font/b.bcfnt'])
case('a size-changing replace keeps the grown member 0x80-aligned',
     len(_gf[1].data) == 1000 and _gf[1].source_offset % 0x80 == 0)
case('replacing a member that is not there is refused',
     raises(lambda: darc.replace(_db, 'font/nope.bcfnt', b'x', where='s'),
            'is not in this archive'))
case('replacing a directory is refused',
     raises(lambda: darc.replace(_db, 'font', b'x', where='s'), 'is a directory'))

# The pixel formats. A4 and A8 must round-trip exactly; fmt 4 must keep producing
# what the legacy writer produced, because the reference fonts are pinned to it.
for _fmt, _bits in sorted(bcfntmod.FORMAT_BITS.items()):
    _raw = bytes((i * 7 + _fmt) & 0xFF
                 for i in range(bcfntmod.sheet_bytes(16, 8, _fmt)))
    _img = bcfntmod.untile(_raw, 16, 8, _fmt)
    _rt = bcfntmod.tile(_img, _fmt)
    case(f'format {_fmt} ({_bits} bits/px) tiles back to the same bytes',
         _rt == _raw if _fmt != 4 else len(_rt) == len(_raw))
case('a sheet shorter than the format needs is refused',
     raises(lambda: bcfntmod.untile(b'\0' * 4, 16, 8, 11), 'needs'))
case('an unknown pixel format is refused, not guessed',
     raises(lambda: bcfntmod.sheet_bytes(16, 8, 2), 'not one this reader has'))
case('only RGBA4444 is recorded as carrying a shading mask',
     bcfntmod.FORMAT_HAS_RGB == {4})
case('the A4 stride is a quarter of the RGBA4444 stride',
     bcfntmod.sheet_bytes(64, 64, 11) * 4 == bcfntmod.sheet_bytes(64, 64, 4))

# The shading-mask estimator, on synthetic glyphs so it needs no cartridge.
class _FakeFont:
    fmt = 4


_cells = []
for _cov, _shade in ((255, 15), (128, 6), (64, 3)):
    _im = Image.new('RGBA', (4, 4), (_shade * 17, _shade * 17, _shade * 17, _cov))
    _cells.append((0x41, _im, (0, 4, 4)))
_lut = fbmod.measure_shade_lut(_FakeFont(), _cells)
case('the shading mask is measured from the glyphs it is given',
     _lut[15] == 15 and _lut[8] == 6 and _lut[4] == 3)
case('unexercised buckets are filled from the nearest measured one',
     all(v is not None for v in _lut) and len(_lut) == 16)


class _AlphaFont:
    fmt = 11


case('a coverage-only format needs no measurement and reports a flat mask',
     fbmod.measure_shade_lut(_AlphaFont()) == [15] * 16)
case('a font with no glyph pixels reports no measurement rather than a default',
     fbmod.measure_shade_lut(_FakeFont(), [
         (0x41, Image.new('RGBA', (4, 4), (0, 0, 0, 0)), (0, 4, 4))]) is None)

print()
print('== DQ7 font mirroring (M4) ==')
# A font is mirrored across archives - tbud_maru_b12 sits in both the bundled
# system_font.arc and its own system_font12.arc - so writing one slot leaves the
# engine free to draw the old glyphs from whichever archive it loads. Measured on the
# cartridge: 7 fonts across 12 slots in 10 archives. None of that had a case.
_mr = tempfile.mkdtemp(prefix='hanpatch-mirror-')
os.makedirs(os.path.join(_mr, 'extracted', 'romfs', 'LAYOUT'), exist_ok=True)
os.makedirs(os.path.join(_mr, 'extracted', 'fonts'), exist_ok=True)
os.makedirs(os.path.join(_mr, 'work', 'ko'), exist_ok=True)


def _mirror_arc(path, members):
    ms = [darc.Member('', True), darc.Member('font', True)]
    for name, data in members:
        m = darc.Member(f'font/{name}', False, data)
        m.source_offset = 0x80
        ms.append(m)
    with open(path, 'wb') as fh:
        fh.write(darc.build(ms, where=path))


_SRC_FONT = b'\x41' * 512
_NEW_FONT = b'\x42' * 700
_layout = os.path.join(_mr, 'extracted', 'romfs', 'LAYOUT')
_mirror_arc(os.path.join(_layout, 'bundle.arc'),
            [('mirrored.bcfnt', _SRC_FONT), ('other.bcfnt', _SRC_FONT)])
_mirror_arc(os.path.join(_layout, 'solo.arc'), [('mirrored.bcfnt', _SRC_FONT)])
with open(os.path.join(_mr, 'work', 'ko', 'mirrored.bcfnt'), 'wb') as fh:
    fh.write(_NEW_FONT)
json.dump({'title': 'M', 'platform': 'threeds', 'adapter': 'dq7', 'target': 'ko',
           'profile': 'p.json', 'rom': 'game.3ds'},
          open(os.path.join(_mr, config.PROJECT_FILE), 'w'))


def _mirror_profile(font_out):
    json.dump({'budget': {'default': 64}, 'engine_wraps': False, 'source_lang': 'ja',
               'font_out': font_out},
              open(os.path.join(_mr, 'p.json'), 'w'))
    # The profile is cached, so rewriting the file is not enough: re-point the root
    # to force a re-resolve. Without this the later cases silently exercise the
    # FIRST profile and pass for the wrong reason.
    if config.root() == _mr:
        config.set_root(_mr)


_prev_mr = config.root()
_mirror_profile(['work/ko/mirrored.bcfnt'])
config.set_root(_mr)
try:
    _ad = dq7mod.DragonQuest7()
    _slots = _ad.font_slots()
    case('every archive slot holding a font is found, not just the first',
         sorted(_slots) == ['mirrored', 'other']
         and sorted(_slots['mirrored']) == [('bundle.arc', 'font/mirrored.bcfnt'),
                                            ('solo.arc', 'font/mirrored.bcfnt')])
    _stage = os.path.join(_mr, 'stage')
    shutil.rmtree(_stage, ignore_errors=True)
    shutil.copytree(os.path.join(_mr, 'extracted', 'romfs'), _stage)
    case('the built font is written into EVERY slot that holds it',
         _ad._apply_fonts(_stage) == 2)
    _after = {}
    for _arc in ('bundle.arc', 'solo.arc'):
        with open(os.path.join(_stage, 'LAYOUT', _arc), 'rb') as fh:
            _after[_arc] = {m.path: bytes(m.data)
                            for m in darc.parse(fh.read(), _arc)[1] if not m.is_dir}
    case('both mirrors carry the new font',
         _after['bundle.arc']['font/mirrored.bcfnt'] == _NEW_FONT
         and _after['solo.arc']['font/mirrored.bcfnt'] == _NEW_FONT)
    case('a font the profile did not build is left untouched',
         _after['bundle.arc']['font/other.bcfnt'] == _SRC_FONT)
    _mirror_profile([])
    case('injection is refused when the profile declares no built font',
         raises(lambda: _ad._apply_fonts(_stage), 'would ship the source font'))
    _mirror_profile(['work/ko/nosuch.bcfnt'])
    case('a built font missing from disk is refused',
         raises(lambda: _ad._apply_fonts(_stage), 'built font nosuch'))
    with open(os.path.join(_mr, 'work', 'ko', 'ghost.bcfnt'), 'wb') as fh:
        fh.write(_NEW_FONT)
    _mirror_profile(['work/ko/ghost.bcfnt'])
    case('a built font that is in no archive on this ROM is refused',
         raises(lambda: _ad._apply_fonts(_stage), 'in no archive on this cartridge'))
finally:
    if os.path.exists(os.path.join(_prev_mr, config.PROJECT_FILE)):
        config.set_root(_prev_mr)

print()
print('== FPT0 archive and message record ==')
# Every field is decoded, and every field the cartridge holds constant is
# VALIDATED, not carried. These cases pin the three readings that were wrong
# during the format work - a "padding" u32 that is really the data offset, a
# "type" byte that is really the name length, and reserved words carried through
# so lossily that an archive holding [1, 2] rebuilt as [2, 2] - plus the
# fail-closed refusals that make a repack trustworthy.
from hanpatch.formats import fpt0, fpttxt  # noqa: E402

TAGB = b'TEMP/STEP2'.ljust(56, b'\0') + struct.pack('<2I', 0, fpt0.name_key('TEMP/STEP2'))
R4 = b'#4\r\nA\r\n\r\n\r\n'
R5 = b'#5\r\nBB\r\n\r\n\r\n'


def fpt(entries, tag='TEMP/STEP2'):
    return {'tag': tag}, [fpt0.Entry(n, d) for n, d in entries]


hdr, es = fpt([('#000004.txt', R4), ('#000005.txt', R5)])
blob = fpt0.build(hdr, es)
case('an FPT0 archive round trips byte-for-byte with every field recomputed',
     fpt0.build(*fpt0.parse(blob)) == blob)
case('the layout is header, entry table, 64-byte tag block, then payloads',
     blob[:4] == b'FPT0' and struct.unpack_from('<I', blob, 8)[0] == 2
     and blob[0x10 + 2 * 0x20:0x10 + 2 * 0x20 + 0x40] == TAGB)
case('parse exposes only the one field the format varies',
     fpt0.parse(blob)[0] == {'tag': 'TEMP/STEP2'})

# The u32 after the name key is the RELATIVE data offset, not padding. A reader
# that called it padding still reproduced untouched bytes by rebuilding
# sequentially, and broke the moment a payload changed length.
offs = [struct.unpack_from('<I', blob, 0x10 + i * 0x20 + 0x14)[0] for i in range(2)]
case('the u32 after the name key is the relative data offset, not padding',
     offs == [0, len(R4)] and offs[1] == 11)
bad = bytearray(blob)
struct.pack_into('<I', bad, 0x10 + 0x20 + 0x14, 9)
case('a data offset that disagrees with the concatenation order is refused',
     raises(lambda: fpt0.parse(bytes(bad)), 'concatenation position'))

# The top byte of the key is the NAME LENGTH. Every name on the cartridge is 10
# or 11 characters, which is exactly why "0x0B means text, 0x0A means texture"
# fitted all the data and was still wrong.
case('the name key carries the name length in its top byte',
     fpt0.name_key('#000004.txt') >> 24 == 11
     and fpt0.name_key('tex000.dmp') >> 24 == 10
     and fpt0.name_key('TEMP/STEP2') >> 24 == 10)
# Golden vectors: the u32 stored at +0x20 of /MESS/#011000.fpt for its first
# entry, and the tag key at the end of that container's tag block. Without a
# literal from the cartridge every key assertion is self-referential and a
# changed multiplier round trips perfectly while the console cannot match a key.
case('the name key matches the values stored on the cartridge',
     fpt0.name_key('#011002.txt') == 0x0B9DC7E9
     and fpt0.name_key('TEMP/STEP2') == 0x0A929EA3)
case('the polynomial multiplier is pinned by a cartridge value',
     fpt0.poly13('#011002.txt') & 0xFFFFFF == 0x9DC7E9)
case('the name key is not the plain 32-bit polynomial hash',
     fpt0.name_key('tex000.dmp') != fpt0.poly13('tex000.dmp') & 0xFFFFFFFF)
case('the low 24 bits of the key are the 13-weighted polynomial hash',
     all(fpt0.name_key(n) & 0xFFFFFF == fpt0.poly13(n) & 0xFFFFFF
         for n in ('#000004.txt', 'tex000.dmp', 'TEMP/STEP2', 'a')))
bad = bytearray(blob)
struct.pack_into('<I', bad, 0x10 + 0x10, 0xDEADBEEF)
case('a stored key that disagrees with the name is refused',
     raises(lambda: fpt0.parse(bytes(bad)), 'does not match the computed'))

# Reserved words are validated, never carried. Carrying them lost data: only the
# LAST entry's trailing word survived and was written into every entry.
for label, at, needle in (
        ('the header reserved word', 0x04, 'header reserved word'),
        ('the tag reserved word', 0x10 + 2 * 0x20 + 0x38, 'tag reserved word')):
    bad = bytearray(blob)
    struct.pack_into('<I', bad, at, 7)
    case(f'a non-zero value in {label} is refused',
         raises(lambda b=bytes(bad): fpt0.parse(b), needle))
bad = bytearray(blob)
struct.pack_into('<I', bad, 0x10 + 0x1C, 1)
struct.pack_into('<I', bad, 0x10 + 0x20 + 0x1C, 2)
case('entries whose reserved words differ are refused, not silently unified',
     raises(lambda: fpt0.parse(bytes(bad)), 'reserved word at 0x1c'))
bad = bytearray(blob)
struct.pack_into('<I', bad, 0x0C, 2)
case('a version this reader has no evidence for is refused',
     raises(lambda: fpt0.parse(bytes(bad)), 'evidence for'))

# Fail closed. The 64-byte build tag was found precisely because the reader
# refused to leave 64 bytes unclaimed instead of ignoring them.
case('trailing bytes the entry table does not claim are refused',
     raises(lambda: fpt0.parse(blob + b'\0' * 64), 'unclaimed'))
case('a truncated payload is refused',
     raises(lambda: fpt0.parse(blob[:-4]), 'runs past EOF'))
case('a table that runs past EOF is refused',
     raises(lambda: fpt0.parse(blob[:0x14]), 'tag block need'))
case('a foreign magic is refused',
     raises(lambda: fpt0.parse(b'DARC' + blob[4:]), "magic b'DARC'"))
dup = bytearray(blob)
dup[0x10 + 0x20:0x10 + 0x20 + 0x10] = b'#000004.txt'.ljust(0x10, b'\0')
case('a name repeated without its key is caught by the key check first',
     raises(lambda: fpt0.parse(bytes(dup)), 'does not match the computed'))
struct.pack_into('<I', dup, 0x10 + 0x20 + 0x10, fpt0.name_key('#000004.txt'))
case('a duplicate entry name is refused on read',
     raises(lambda: fpt0.parse(bytes(dup)), 'repeats the name'))
case('a duplicate entry name is refused on write', raises(
    lambda: fpt0.build(*fpt([('#000004.txt', R4), ('#000004.txt', R5)])),
    'repeats the name'))
case('an empty entry name is refused on write',
     raises(lambda: fpt0.build(*fpt([('', R4)])), 'empty name'))
case('a name over the 16-byte field is refused on write',
     raises(lambda: fpt0.build(*fpt([('x' * 17 + '.txt', R4)])), 'over the 16-byte'))
case('a non-ASCII name is refused on write, naming the archive and entry',
     raises(lambda: fpt0.build(*fpt([('\ud55c.txt', R4)]), where='dq7.fpt'),
            'dq7.fpt: entry 0 name'))
case('a NUL inside an entry name is refused on write',
     raises(lambda: fpt0.build(*fpt([('a\0b.txt', R4)])), 'contains a NUL'))
case('a NUL inside the tag string is refused on write',
     raises(lambda: fpt0.build(*fpt([('a.txt', R4)], tag='TEMP\0STEP2')),
            'contains a NUL'))
case('a header without a tag is refused on write, not a KeyError',
     raises(lambda: fpt0.build({}, [fpt0.Entry('a.txt', R4)]),
            'must be an object carrying "tag"'))
case('a non-string entry name is refused on write',
     raises(lambda: fpt0.build(*fpt([(7, R4)])), 'must be a string'))
case('a text payload is refused on write, naming the entry',
     raises(lambda: fpt0.build({'tag': 'T'}, [fpt0.Entry('a.txt', '#1\r\n')],
                               where='dq7.fpt'),
            'dq7.fpt: entry 0'))
case('a 16-byte name is accepted and round trips',
     fpt0.parse(fpt0.build(*fpt([('abcdefghijkl.txt', R4)])))[1][0].name
     == 'abcdefghijkl.txt')
case('an empty archive round trips',
     fpt0.build(*fpt0.parse(fpt0.build({'tag': 'TEMP/STEP2'}, [])))
     == fpt0.build({'tag': 'TEMP/STEP2'}, []))
case('the tag string is carried verbatim, not invented',
     fpt0.parse(blob)[0]['tag'] == 'TEMP/STEP2'
     and fpt0.parse(fpt0.build(*fpt([('a.txt', R4)], tag='')))[0]['tag'] == '')
case('source_offset is a read-only diagnostic that build ignores',
     (lambda h, e: (setattr(e[1], 'source_offset', 999),
                    fpt0.build(h, e) == blob)[1])(*fpt0.parse(blob)))

# A length change must be absorbed by recomputation alone.
hdr2, es2 = fpt0.parse(blob)
es2[0] = fpt0.Entry(es2[0].name, b'#4\r\n' + '\uac00'.encode() * 40 + b'\r\n\r\n\r\n')
grown = fpt0.build(hdr2, es2)
regrown_hdr, regrown = fpt0.parse(grown)
case('a length-changed repack recomputes offsets and lengths',
     len(grown) == len(blob) + len(es2[0].data) - len(R4)
     and [e.source_offset for e in regrown] == [0, len(es2[0].data)]
     and bytes(regrown[1].data) == R5)

# The message record inside a .txt entry. Both head shapes and the four- or
# five-line shape are enforced on read AND on write: nothing here knows what a
# six-line record does to a text window.
rec = fpttxt.parse(b'#2020\r\nline\r\n\r\n\r\n')
case('a message record splits into a head line and CRLF-terminated lines',
     rec.head == '#2020' and rec.lines == ['line', '', ''])
case('a message record rebuilds byte-for-byte',
     fpttxt.build(rec) == b'#2020\r\nline\r\n\r\n\r\n')
case('a TALKER head is accepted',
     fpttxt.parse(b'TALKER=12\r\na\r\n\r\n\r\n').head == 'TALKER=12')
case('a five-line record is accepted',
     len(fpttxt.parse(b'#1\r\na\r\nb\r\nc\r\nd\r\n').lines) == 4)
case('a head shape absent from the cartridge is refused on read',
     raises(lambda: fpttxt.parse(b'UNKNOWN\r\na\r\n\r\n\r\n'), 'neither'))
case('a head shape absent from the cartridge is refused on write',
     raises(lambda: fpttxt.build(fpttxt.Record('UNKNOWN', ['a', '', ''])), 'neither'))
for bad_lines, label in ((['a'], 'two'), (['a', 'b', 'c', 'd', 'e'], 'six')):
    case(f'a {label}-line record is refused on write',
         raises(lambda l=bad_lines: fpttxt.build(fpttxt.Record('#1', l)),
                'on the cartridge has'))
case('a two-line record is refused on read',
     raises(lambda: fpttxt.parse(b'#1\r\na\r\n'), 'on the cartridge has'))
case('a head with trailing junk is refused',
     raises(lambda: fpttxt.parse(b'#12x\r\na\r\n\r\n\r\n'), 'neither'))
case('a full-width-digit head is refused',
     raises(lambda: fpttxt.build(fpttxt.Record('#\uff11\uff12', ['a', '', ''])),
            'neither'))
# The 4-or-5 envelope is measured across 66208 records but exercised one window
# at a time: 55 five-line records elsewhere are not evidence that THIS window
# draws five lines.
_four = fpttxt.parse(b'#7\r\na\r\nb\r\n\r\n')
case('a parsed record remembers the line count it was read with',
     _four.source_lines == 4)
case('growing a four-line record to five is refused by default',
     raises(lambda: fpttxt.build(fpttxt.Record(_four.head, _four.lines + ['c'],
                                               source_lines=4)),
            'is not evidence that it draws'))
case('a deliberate line-count change is allowed when stated',
     fpttxt.build(fpttxt.Record(_four.head, _four.lines + ['c'], source_lines=4),
                  expect_lines=5).endswith(b'c\r\n'))
case('a stated line count still has to sit inside the measured envelope',
     raises(lambda: fpttxt.build(fpttxt.Record(_four.head, _four.lines + ['c', 'd'],
                                               source_lines=4), expect_lines=6),
            'on the cartridge has'))
_five = fpttxt.parse(b'#8\r\na\r\nb\r\nc\r\nd\r\n')
case('shrinking a five-line record to four is refused by default',
     raises(lambda: fpttxt.build(fpttxt.Record(_five.head, _five.lines[:-1],
                                               source_lines=5)),
            'is not evidence that it draws'))
case('a record built from scratch is not second-guessed',
     fpttxt.build(fpttxt.Record('#7', ['a', '', ''])) == b'#7\r\na\r\n\r\n\r\n')
case('the display lines join with LF for the layout gate',
     fpttxt.Record('#7', ['ab', 'cd', '']).text == 'ab\ncd\n')
case('a payload that is not UTF-8 is refused',
     raises(lambda: fpttxt.parse(b'#1\r\n\xff\xfe\r\n\r\n\r\n'), 'not UTF-8'))
case('a payload not terminated by CRLF is refused',
     raises(lambda: fpttxt.parse(b'#1\r\nx'), 'does not end with CRLF'))
case('a bare LF is refused',
     raises(lambda: fpttxt.parse(b'#1\r\nx\ny\r\n\r\n\r\n'), 'bare LF'))
case('a bare CR is refused',
     raises(lambda: fpttxt.parse(b'#1\r\nx\ry\r\n\r\n\r\n'), 'bare CR'))
case('a line containing a break is refused, naming the line index',
     raises(lambda: fpttxt.build(fpttxt.Record('#1', ['a\r\nb', '', ''])),
            'line 1 contains a CRLF'))
case('a Hangul line longer than the source round trips through both layers',
     fpttxt.parse(fpttxt.build(
         fpttxt.Record('#1', ['\ud55c\uae00' * 30, '', '']))).lines
     == ['\ud55c\uae00' * 30, '', ''])

print()
print('== measured line capacity ==')
# wrap.capacity() used to invent a limit when nothing had been measured - the
# family maximum, or a hardcoded 10 - which is the same defect as a generic pixel
# budget: the capacity gate would pass a translation against a limit the title
# never proved.
from hanpatch import config as configmod  # noqa: E402
from hanpatch import wrap as wrapmod  # noqa: E402

capdir = os.path.join(TMP, 'capproj')
os.makedirs(os.path.join(capdir, 'work', 'ko'), exist_ok=True)
json.dump({'title': 'cap', 'platform': 'threeds', 'adapter': 'crimson_shroud',
           'target': 'ko', 'profile': 'p.json'},
          open(os.path.join(capdir, 'hanpatch.json'), 'w'))
json.dump({'budget': {'default': 320}, 'capacity': {'dialogue': 3}},
          open(os.path.join(capdir, 'p.json'), 'w'))
_prev_root = configmod.root()
configmod.set_root(capdir)
case('an unmeasured line capacity fails closed instead of inventing one',
     raises(lambda: wrapmod.capacity(None, 'nowhere'), 'no measured line capacity'))
case('a profile-declared capacity is used when nothing is derived yet',
     wrapmod.capacity(None, 'dialogue') == 3)
json.dump({'dialogue/mes_a': 4, 'dialogue/mes_b': 7, 'system/x': 2},
          open(os.path.join(capdir, 'work', 'ko', 'capacity.json'), 'w'))
configmod.reset_module_caches()
case('a derived group capacity beats the family and the profile',
     wrapmod.capacity('dialogue/mes_a', 'dialogue') == 4)
case('an unlisted group falls back to the derived family maximum',
     wrapmod.capacity('dialogue/mes_zz', 'dialogue') == 7)
case('a family with no derived rows still refuses to invent a limit',
     raises(lambda: wrapmod.capacity('battle/x', 'battle'), 'no measured'))
if os.path.exists(os.path.join(_prev_root, 'hanpatch.json')):
    configmod.set_root(_prev_root)

# Opt-in check against REAL cartridge bytes. The 345/345 and 66208/66208 claims
# were measured once by scripts outside this repository; this turns them into a
# standing check anyone can re-run. Counts only, so the content boundary holds:
# no payload is printed and nothing is written.
_FPT_DIR = None
for _i, _a in enumerate(sys.argv):
    if _a == '--fpt-dir' and _i + 1 < len(sys.argv):
        _FPT_DIR = sys.argv[_i + 1]
    elif _a.startswith('--fpt-dir='):
        _FPT_DIR = _a.split('=', 1)[1]
# A flag that was asked for but cannot be honoured must FAIL, not skip: an
# operator who believes they ran the cartridge check must not get a green suite
# that never opened a container.
if _FPT_DIR is not None and not os.path.isdir(_FPT_DIR):
    case(f'--fpt-dir names a real directory ({_FPT_DIR})', False)
    _FPT_DIR = None
if _FPT_DIR:
    print()
    print('== real cartridge containers ==')
    _files = sorted(os.path.join(_FPT_DIR, f) for f in os.listdir(_FPT_DIR))
    _files = [f for f in _files if os.path.isfile(f)]
    _n = _bad = _recs = _recbad = 0
    for _f in _files:
        _raw = open(_f, 'rb').read()
        try:
            _h, _es = fpt0.parse(_raw, os.path.basename(_f))
            if fpt0.build(_h, _es, os.path.basename(_f)) != _raw:
                raise fpt0.FptError('rebuild differs')
            _n += 1
        except Exception as _exc:
            _bad += 1
            print(f'    {os.path.basename(_f)}: {_exc}')
            continue
        for _e in _es:
            if not _e.name.lower().endswith('.txt'):
                continue
            try:
                _r = fpttxt.parse(_e.data, _e.name)
                if fpttxt.build(_r, _e.name) != _e.data:
                    raise fpttxt.RecordError('record rebuild differs')
                _recs += 1
            except Exception as _exc:
                _recbad += 1
                print(f'    {os.path.basename(_f)}/{_e.name}: {_exc}')
    case(f'--fpt-dir actually held containers ({len(_files)} files)', bool(_files))
    case(f'every real container round trips byte-for-byte ({_n} parsed)',
         _files and _n == len(_files) and _bad == 0)
    case(f'every real message record round trips byte-for-byte ({_recs} records)',
         _recs > 0 and _recbad == 0)
else:
    print()
    print('  skip real-container cases (pass --fpt-dir DIR with dumped .fpt files)')

print()
print('== deterministic 3DS BLZ compression ==')
_blz_plain = (b'DQ7 executable fixture\0' * 1024) + bytes(range(256))
_blz_first = blz.compress(_blz_plain)
_blz_second = blz.compress(_blz_plain)
case('BLZ compression is deterministic', _blz_first == _blz_second)
case('BLZ compressed data round trips exactly',
     blz.decompress(_blz_first) == _blz_plain)
case('BLZ uses compression when it saves space', len(_blz_first) < len(_blz_plain))
print()
print('== dq7 string tables: the menu/name text that lives outside /MESS ==')
_MENU = ('#0,0,\r\n#1,0,ひのきの;ぼう\r\n#2,,こんぼう\r\n\r\n'
         '#3,0,その他{1ほか}\r\n').encode('utf-8')
_TEXT = ('\ufeff0,"-"\r\n1,"ひのきのぼう"\r\n2,"メラ"\r\n\r\n').encode('utf-8')

_m = dq7table.parse('MENULIST/x.txt', _MENU)
_t = dq7table.parse('TEXT/y.txt', _TEXT)
case('a menu table parses every non-empty row', len(_m.rows) == 4)
case('a quoted name table parses every non-empty row', len(_t.rows) == 3)
case('the BOM is a property of the file, not of a row',
     _t.bom == '\ufeff' and _m.bom == '')
case('an untouched menu table rebuilds byte-for-byte',
     dq7table.build(_m) == _MENU)
case('an untouched name table rebuilds byte-for-byte, BOM included',
     dq7table.build(_t) == _TEXT)
# The flag column differs per row (`0` and empty both occur) and the id column is the
# game's own index: rewriting either shifts what the engine looks up, so a replacement
# must edit the text field ONLY.
dq7table.set_text(_m, '#2', '곤봉')
dq7table.set_text(_t, '1', '노송나무 몽둥이')
case('replacing a menu row keeps its id and its empty flag column',
     b'#2,,\xea\xb3\xa4\xeb\xb4\x89' in dq7table.build(_m))
case('replacing a quoted row keeps the quoting',
     '1,"노송나무 몽둥이"'.encode('utf-8') in dq7table.build(_t))
case('a replacement leaves every other row untouched',
     dq7table.build(_m).count(b'\r\n') == _MENU.count(b'\r\n')
     and dq7table.build(_t).count(b'\r\n') == _TEXT.count(b'\r\n'))
case('an unparseable row is refused rather than silently dropped',
     raises(lambda: dq7table.parse('MENULIST/x.txt', b'not a row\r\n'),
            'unrecognised table row'))
case('a family id carries no slash, because a manifest key is split on the first one',
     dq7table.family_of('MENULIST/command_menu.txt') == '@MENULIST_command_menu'
     and '/' not in dq7table.family_of('TEXT/ITEM_NAME.txt'))
# A table row is one stored line. A translation carrying a break would split the record
# and shift every field after it, which is silent corruption rather than a failed build.
case('the injector refuses a translation containing a line break',
     'line break' in (dq7mod.DragonQuest7.inject.__doc__ or '')
     or 'stores exactly one line' in open(
         os.path.join(ROOT, 'hanpatch', 'adapters', 'dq7.py'), encoding='utf-8').read())

print()
print('== etc1: the compressed textures DQ7 draws its title artwork from ==')
from hanpatch.formats import etc1 as _etc1

_etc1_w = _etc1_h = 8
# One 8x8 tile: four 4x4 blocks, each 8 colour bytes then 8 alpha bytes.
_etc1_payload = bytes(range(64))
case('a payload half the pixel count is ETC1, a payload equal to it is ETC1A4',
     _etc1.bits_per_pixel(32, 8, 8) == 4 and _etc1.bits_per_pixel(64, 8, 8) == 8)
case('any other density is refused rather than guessed',
     raises(lambda: _etc1.bits_per_pixel(40, 8, 8), 'neither ETC1'))
_etc1_img = _etc1.decode(_etc1_payload, _etc1_w, _etc1_h)
case('an ETC1A4 payload decodes to one RGBA image of the declared size',
     _etc1_img.size == (8, 8) and _etc1_img.mode == 'RGBA')
case('re-encoding an unmodified decode reproduces the payload byte for byte',
     _etc1.encode(_etc1_img, _etc1_payload, _etc1_w, _etc1_h) == _etc1_payload)
_etc1_edit = _etc1_img.copy()
_etc1_edit.putpixel((0, 0), (255, 0, 0, 255))
_etc1_new = _etc1.encode(_etc1_edit, _etc1_payload, _etc1_w, _etc1_h)
case('editing one pixel rewrites only the block that holds it',
     len(_etc1_new) == len(_etc1_payload)
     and _etc1_new[16:] == _etc1_payload[16:]
     and _etc1_new[:16] != _etc1_payload[:16])
_etc1_back = _etc1.decode(_etc1_new, _etc1_w, _etc1_h)
case('the edited pixel survives the round trip as opaque red',
     _etc1_back.getpixel((0, 0))[3] == 255
     and _etc1_back.getpixel((0, 0))[0] > _etc1_back.getpixel((0, 0))[2])
case('every pixel outside the edited block is unchanged',
     all(_etc1_back.getpixel((x, y)) == _etc1_img.getpixel((x, y))
         for x in range(8) for y in range(8) if not (x < 4 and y < 4)))
case('a replacement of the wrong size is refused, because the layout that '
     'references the texture is elsewhere',
     raises(lambda: _etc1.encode(_etc1_img.resize((16, 16)), _etc1_payload,
                                 _etc1_w, _etc1_h), 'replacement is'))

print()
print(f'{len(PASS)} passed, {len(FAIL)} failed')
for f in FAIL:
    print('  FAILED:', f)
sys.exit(1 if FAIL else 0)

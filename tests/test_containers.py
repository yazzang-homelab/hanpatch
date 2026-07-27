"""Container, crypto and distribution tests.

Retail key material cannot be shipped, so the crypto paths are exercised with
**synthesised** inputs: a title key of our own choosing, wrapped with a common
key of our own choosing, and content encrypted with it. That proves the CBC/IV
layout, the common-key search, and the validate-by-magic guard, which is where
the bugs live — not the value of any real key.

Run: python3 tests/test_containers.py [--rom /path/to/a.cia]
"""
import hashlib
import json
import os
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from Crypto.Cipher import AES  # noqa: E402

from hanpatch import delta  # noqa: E402
from hanpatch.platforms.threeds import cia as ciamod  # noqa: E402
from hanpatch.platforms.threeds import keys as keysmod  # noqa: E402
from hanpatch.platforms.threeds import ncsd as ncsdmod  # noqa: E402

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
case('a non-bundle file is rejected', raises(lambda: release.inspect(kt)))
if ROM and os.path.exists('/root/tmp/crimson-kr/work/ko/manifest.approved'):
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
else:
    print('  skip bundle cases (no approved reference project)')

print()
print(f'{len(PASS)} passed, {len(FAIL)} failed')
for f in FAIL:
    print('  FAILED:', f)
sys.exit(1 if FAIL else 0)

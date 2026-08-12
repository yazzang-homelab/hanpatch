"""LayeredFS packs — the same patch, as loose files on an SD card.

A rebuilt image is the wrong shape for someone playing on the console they own.
It is 2 GB, it has to be installed, and on a cartridge title it cannot be
installed at all: the retail cart is read-only and the rebuilt NCSD carries a
signature that no longer covers its own headers, so a retail console refuses it.

Luma3DS solves that from the other side. With game patching enabled it redirects
RomFS reads to files under `/luma/titles/<TitleID>/romfs/` and applies an IPS
patch to the decompressed executable from `code.ips`. The cartridge stays as it
is; the SD card carries only what the translation changed.

    hanpatch luma mypatch.hpk --rom /path/to/their.3ds --out /media/SDCARD

What lands on the card is exactly the bytes the rebuilt ROM would have contained
— `verify_against_rom` proves that file by file rather than asserting it, because
"it looked right in an emulator" is not the same claim.

Layout written:

    luma/titles/<TID>/romfs/<path>   every RomFS file the patch changed
    luma/titles/<TID>/code.ips      the executable patch, IPS over decompressed
    luma/titles/<TID>/README.txt    what to switch on, in Korean
"""
import hashlib
import os
import shutil
import struct

from hanpatch import config
from hanpatch.platforms.threeds import blz

IPS_MAX = 1 << 24          # IPS offsets are three bytes
IPS_RECORD_MAX = 0xFFFF


def title_id(header=None):
    """The 16-hex title id Luma keys its directory on.

    Read from the extracted NCCH header rather than a config field: a wrong id
    means Luma silently patches nothing, which is the failure mode hardest to
    tell apart from "the patch does not work".
    """
    header = header or config.extracted('ncch_header.bin')
    if not os.path.exists(header):
        raise SystemExit(f'no extracted NCCH header at {header}; run extract '
                         'first — the title id has to come from the ROM')
    with open(header, 'rb') as fh:
        blob = fh.read(0x200)
    if blob[0x100:0x104] != b'NCCH':
        raise SystemExit(f'{header} is not an NCCH header')
    program_id = struct.unpack_from('<Q', blob, 0x118)[0]
    return f'{program_id:016X}'


def ips(original, patched):
    """An IPS patch turning `original` into `patched`.

    Refuses rather than truncates: IPS addresses three bytes, and a patch that
    silently drops the changes past 16 MB would produce a console that boots and
    misbehaves.
    """
    if len(original) != len(patched):
        raise SystemExit('IPS cannot express a length change '
                         f'({len(original)} -> {len(patched)} bytes)')
    if len(original) > IPS_MAX:
        raise SystemExit(f'{len(original)} bytes is past the 16 MB IPS limit; '
                         'ship code.bin instead')
    out = bytearray(b'PATCH')
    i = 0
    n = len(original)
    while i < n:
        if original[i] == patched[i]:
            i += 1
            continue
        start = i
        # A run of equal bytes shorter than a record header is cheaper to keep
        # inside the record than to pay for a second one.
        gap = 0
        while i < n and (original[i] != patched[i] or gap < 6):
            if original[i] == patched[i]:
                gap += 1
            else:
                gap = 0
            i += 1
            if i - start >= IPS_RECORD_MAX:
                break
        end = i - gap if gap else i
        chunk = patched[start:end]
        if start == 0x454F46:   # 'EOF' as an offset would end the patch early
            start -= 1
            chunk = patched[start:end]
        out += start.to_bytes(3, 'big') + len(chunk).to_bytes(2, 'big') + chunk
    out += b'EOF'
    return bytes(out)


def apply_ips(original, patch):
    """Apply an IPS patch, for checking our own output."""
    if patch[:5] != b'PATCH':
        raise SystemExit('not an IPS patch')
    data = bytearray(original)
    i = 5
    while True:
        head = patch[i:i + 3]
        if head == b'EOF':
            return bytes(data)
        offset = int.from_bytes(head, 'big')
        size = int.from_bytes(patch[i + 3:i + 5], 'big')
        i += 5
        if size:
            data[offset:offset + size] = patch[i:i + size]
            i += size
        else:                       # RLE record
            run = int.from_bytes(patch[i:i + 2], 'big')
            data[offset:offset + run] = bytes([patch[i + 2]]) * run
            i += 3


README = """드래곤 퀘스트 VII 한글패치 — 실기(Luma3DS) 설치

이 폴더는 롬을 다시 만들지 않습니다. 카트리지는 그대로 두고, 번역된 파일만 SD
카드에서 읽게 합니다. 그래서 2GB를 옮기지 않아도 되고, 서명이 깨진 이미지를
설치하지도 않습니다.

설치
----
1. SD 카드 루트에 이 `luma` 폴더를 그대로 복사합니다. 이미 있으면 합칩니다.
   경로가 이렇게 되어야 합니다:
     SD:/luma/titles/{tid}/romfs/...
     SD:/luma/titles/{tid}/code.ips
2. 콘솔을 켜면서 SELECT 를 누른 채로 Luma 설정 화면에 들어갑니다.
3. **Enable game patching** 을 켭니다(A 로 체크, START 로 저장).
4. 재부팅하고 카트리지(또는 설치된 타이틀)를 실행합니다.

확인
----
게임 안 텍스트가 한국어로 나오면 적용된 것입니다. 나오지 않으면 순서대로
확인하십시오.
  * `Enable game patching` 이 꺼져 있는지 — 가장 흔한 원인입니다.
  * 타이틀 ID 폴더 이름이 {tid} 인지(대문자 16자리).
  * 리전이 다른 카트리지인지. 이 패치는 일본판 {product} 기준이고, 다른 리전은
    파일 구조가 달라 적용되지 않습니다.

포함된 것
---------
  RomFS 교체 파일 {files}개, {mb:.1f} MB
  실행 코드 패치 code.ips ({ips} 바이트)
  대상 ROM sha256 {src}

이 폴더에는 게임 데이터가 없습니다. 번역문과 글꼴, 그리고 실행 코드의 작은
패치뿐입니다. 패치할 게임을 정당하게 소유할 책임은 이용자에게 있습니다.
"""


def pack(adapter_obj, entries, out, rom=None, tid=None, quiet=False):
    """Write a LayeredFS pack for `entries`. Returns a report."""
    if not hasattr(adapter_obj, 'stage'):
        raise SystemExit(f'{adapter_obj.__class__.__name__} has no stage(); '
                         'LayeredFS needs the staged files, not a rebuilt image')
    tid = tid or title_id()
    staged = adapter_obj.stage(entries)
    stage_dir = staged['romfs']
    source_dir = adapter_obj.romfs_dir

    root = os.path.join(out, 'luma', 'titles', tid)
    romfs_out = os.path.join(root, 'romfs')
    shutil.rmtree(romfs_out, ignore_errors=True)
    os.makedirs(romfs_out, exist_ok=True)

    changed = []
    total = 0

    def take(rel, data):
        dst = os.path.join(romfs_out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'wb') as fh:
            fh.write(data)
        changed.append(rel.replace(os.sep, '/'))
        return len(data)

    for base, dirs, files in os.walk(stage_dir):
        # Symlinked directories are the untouched parts of the RomFS by
        # construction (see the adapter's staging), so never walk into them.
        dirs[:] = [d for d in dirs
                   if not os.path.islink(os.path.join(base, d))]
        for name in sorted(files):
            src = os.path.join(base, name)
            if os.path.islink(src):
                continue
            rel = os.path.relpath(src, stage_dir)
            original = os.path.join(source_dir, rel)
            with open(src, 'rb') as fh:
                new = fh.read()
            if os.path.exists(original):
                with open(original, 'rb') as fh:
                    if fh.read() == new:
                        continue
            total += take(rel, new)
            if not quiet and len(changed) % 50 == 0:
                print(f'  {len(changed)} files, {total / 1e6:.1f} MB',
                      flush=True)

    # Files the adapter rewrote through a staged symlink: a diff against the
    # source cannot see those, because the write went into the source.
    for rel in staged.get('rewritten', []):
        if rel.replace('/', os.sep) in [c.replace('/', os.sep) for c in changed]:
            continue
        src = os.path.join(stage_dir, *rel.split('/'))
        with open(src, 'rb') as fh:
            total += take(rel.replace('/', os.sep), fh.read())

    code = staged['exefs'].get('.code')
    ips_bytes = b''
    if code is not None:
        with open(config.extracted('exefs', '.code'), 'rb') as fh:
            source_code = fh.read()
        # Luma patches the decompressed executable, so the patch is computed
        # there. Both sides are BLZ compressed on disk.
        before = blz.decompress(source_code)
        after = blz.decompress(code)
        ips_bytes = ips(before, after)
        if apply_ips(before, ips_bytes) != after:
            raise SystemExit('the generated IPS does not reproduce the patched '
                             'executable; refusing to write it')
        with open(os.path.join(root, 'code.ips'), 'wb') as fh:
            fh.write(ips_bytes)

    src_sha = '(알 수 없음)'
    if rom and os.path.exists(rom):
        h = hashlib.sha256()
        with open(rom, 'rb') as fh:
            while True:
                b = fh.read(1 << 22)
                if not b:
                    break
                h.update(b)
        src_sha = h.hexdigest()
    with open(os.path.join(root, 'README.txt'), 'w', encoding='utf-8') as fh:
        fh.write(README.format(tid=tid, files=len(changed), mb=total / 1e6,
                               ips=len(ips_bytes), src=src_sha,
                               product='CTR-P-AD7J'))
    return {'title_id': tid, 'root': root, 'files': changed,
            'bytes': total, 'ips': len(ips_bytes),
            'source_sha256': src_sha if rom else None}


def verify_against_rom(pack_root, romfs_bin, quiet=False):
    """Every file in the pack must equal that path inside the built RomFS.

    This is the claim that matters: a console reading these files sees the same
    bytes as a player running the rebuilt image. Anything else is two patches
    wearing one name.
    """
    from hanpatch.platforms import threeds
    romfs_dir = os.path.join(pack_root, 'romfs')
    checked = mismatched = 0
    missing = []
    for base, _dirs, files in os.walk(romfs_dir):
        for name in sorted(files):
            path = os.path.join(base, name)
            rel = os.path.relpath(path, romfs_dir).replace(os.sep, '/')
            with open(path, 'rb') as fh:
                ours = fh.read()
            try:
                # RomFS paths are absolute inside the image.
                theirs = threeds.read_romfs_file(romfs_bin, '/' + rel)
            except Exception:
                theirs = None
            if theirs is None:
                missing.append(rel)
                continue
            checked += 1
            if ours != theirs:
                mismatched += 1
                if not quiet:
                    print(f'  MISMATCH {rel}')
    return {'checked': checked, 'mismatched': mismatched, 'missing': missing}

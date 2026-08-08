"""IPS, and the fact that it will lie to you.

IPS is a list of `(offset, bytes)` writes with a `PATCH`/`EOF` bookend. It
carries no checksum of the ROM it was made for, no version, no name, nothing.
Hand it the wrong ROM and it will apply, report success, and hand back a file
that boots to a garbage screen - or worse, boots fine and corrupts an hour in.

Nearly two hundred of the patches in the local archive are IPS. Applying them
by filename match and believing the result is how a knowledge base fills up
with confident nonsense, so this module refuses to be the thing that believes.
It parses, it validates against a candidate ROM, and it says what it could not
rule out. `applies()` returns a verdict with reasons, never a bare boolean.

The checks are ordered cheapest first, because pairing 60 patches against
1,271 candidate ROMs means the expensive test must only ever see survivors.

    1. containment  - a record writing past the end of the ROM is decisive.
    2. header       - iNES PRG/CHR sizes must survive, or the ROM was not
                      what the patch expected.
    3. footprint    - a patch touching 0.02% of the file is not a translation.

None of that proves the pairing. It narrows it, and the diff is what decides.
"""
from __future__ import annotations

import struct
from typing import Dict, List, Optional, Sequence, Tuple

MAGIC = b'PATCH'
EOF = b'EOF'

# The format's own ceiling: offsets are 24-bit.
MAX_OFFSET = (1 << 24) - 1

INES_MAGIC = b'NES\x1a'
INES_HEADER = 16


class IPSError(ValueError):
    pass


def parse(data: bytes) -> List[Dict]:
    """Records, in file order, or an exception.

    RLE records are expanded here rather than carried as a special case,
    because every consumer downstream wants "these bytes at this offset" and a
    format quirk that leaks into three call sites is a format quirk that will
    be handled inconsistently in two of them.
    """
    if not data.startswith(MAGIC):
        raise IPSError('not an IPS file')

    out: List[Dict] = []
    at = len(MAGIC)
    while True:
        if at + 3 > len(data):
            raise IPSError('truncated before EOF marker')
        head = data[at:at + 3]
        if head == EOF:
            at += 3
            break
        offset = int.from_bytes(head, 'big')
        at += 3
        if at + 2 > len(data):
            raise IPSError('truncated record length')
        length = struct.unpack_from('>H', data, at)[0]
        at += 2
        if length == 0:
            if at + 3 > len(data):
                raise IPSError('truncated RLE record')
            run = struct.unpack_from('>H', data, at)[0]
            value = data[at + 2:at + 3]
            at += 3
            if not value:
                raise IPSError('truncated RLE value')
            out.append({'at': offset, 'size': run, 'rle': True,
                        'payload': value * run})
        else:
            if at + length > len(data):
                raise IPSError('truncated record payload')
            out.append({'at': offset, 'size': length, 'rle': False,
                        'payload': data[at:at + length]})
            at += length

    # A truncation extension is three trailing bytes after EOF. Recorded, not
    # applied - shrinking a ROM is not something a translation does, and a
    # patch that wants to is a patch we should look at by hand.
    if len(data) - at == 3:
        out.append({'at': int.from_bytes(data[at:at + 3], 'big'),
                    'size': 0, 'rle': False, 'payload': b'',
                    'truncate': True})
    return out


def span(records: Sequence[Dict]) -> Tuple[int, int]:
    """Lowest byte written, and one past the highest."""
    if not records:
        return (0, 0)
    lo = min(r['at'] for r in records)
    hi = max(r['at'] + r['size'] for r in records)
    return (lo, hi)


def ines_header(rom: bytes) -> Optional[Dict]:
    if not rom.startswith(INES_MAGIC) or len(rom) < INES_HEADER:
        return None
    prg, chr_ = rom[4], rom[5]
    return {
        'prg_banks': prg, 'chr_banks': chr_,
        'prg_bytes': prg * 16384, 'chr_bytes': chr_ * 8192,
        'expected_size': INES_HEADER + prg * 16384 + chr_ * 8192,
    }


def applies(rom: bytes, records: Sequence[Dict], *,
            min_footprint: float = 0.0005) -> Dict:
    """Could this patch have been made for this ROM.

    Deliberately named as a question. Every answer here is `not ruled out` at
    best - IPS gives us nothing to verify against, so the strongest honest
    verdict is that no cheap test caught it.
    """
    reasons: List[str] = []
    lo, hi = span(records)
    touched = sum(r['size'] for r in records)

    if any(r.get('truncate') for r in records):
        reasons.append('carries a truncation extension')

    # A patch may legitimately grow a ROM, but only off the end - a write that
    # starts past the end leaves an undefined hole, and no real patch does it.
    if lo > len(rom):
        reasons.append('first write starts %d bytes past the end' % (lo - len(rom)))
    grows = hi > len(rom)

    header = ines_header(rom)
    if header:
        if header['expected_size'] != len(rom):
            reasons.append('iNES header claims %d bytes, file has %d'
                           % (header['expected_size'], len(rom)))
        # The 16-byte header itself is almost never the target of a
        # translation. A patch rewriting it is usually a patch for a headered
        # dump being applied to a headerless one, or the reverse.
        if lo < INES_HEADER:
            reasons.append('writes inside the iNES header')

    footprint = touched / len(rom) if rom else 0.0
    if footprint < min_footprint:
        reasons.append('touches only %.4f%% of the ROM' % (footprint * 100))

    return {
        'plausible': not reasons,
        'reasons': reasons,
        'records': len(records),
        'bytes_touched': touched,
        'footprint': round(footprint, 6),
        'first_write': lo,
        'last_write': hi,
        'grows_rom': grows,
        'rom_size': len(rom),
        'ines': header,
    }


def apply(rom: bytes, records: Sequence[Dict]) -> bytes:
    """Write the records. No verification - `applies` is that, and it is the
    caller's job to have asked."""
    _, hi = span(records)
    out = bytearray(rom)
    if hi > len(out):
        out.extend(b'\x00' * (hi - len(out)))
    for r in records:
        if r.get('truncate'):
            continue
        out[r['at']:r['at'] + r['size']] = r['payload']
    return bytes(out)


def strip_header(rom: bytes) -> Tuple[bytes, bool]:
    """Some patches are cut against a headerless dump.

    Returning the flag matters: a pairing that only works after removing the
    header is a different pairing, and recording it as the same one would make
    two incompatible facts look like one confirmed fact.
    """
    if rom.startswith(INES_MAGIC):
        return rom[INES_HEADER:], True
    return rom, False

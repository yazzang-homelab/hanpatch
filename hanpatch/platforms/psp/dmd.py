"""The `.DMD` container: the same block layout as `SDT`, with gzip blocks.

`pspfs.py` records that a directory entry carries a decompressed size at 0x14
and refuses to replace such a member without it, but nothing in this package
could read one. Every `.DMD` member on the disc is compressed, so 96 members -
1,201,348 bytes decoded - were unreachable: `extract` copied the compressed
bytes out and no reader would parse them. This module is that reader.

    HEADER (little endian)

    0x00   4  block count
    0x04   4  kind; 0x00020000 on this disc, preserved rather than interpreted
    0x08   4  block offsets, one u32 each, then the blocks

That is byte for byte the header `sdt.py` documents. The difference is the block
body: `SDT` holds IMY containers, `DMD` holds gzip members (`1f 8b`, deflate,
FNAME set). The payload is the blocks' decoded contents concatenated in order -
the split is transport, not structure - so decode everything, then parse.

**That concatenation rule is carried from the SDT contract, not measured here.**
Every one of the 96 `.DMD` members on this disc declares exactly ONE block, so
`blocks()`'s multi-block path has never run against real data; it is covered by
synthetic fixtures only, and the tests say so. If a multi-block member ever turns
up, check two things before trusting the join: that a record really can straddle a
boundary (true of SDT, assumed here), and that the inter-block gap is zero padded.
The slice handed to gzip runs to the next offset, and Python's gzip tolerates
trailing zeros but rejects trailing non-zero bytes - so non-zero alignment padding
would surface as `does not inflate` rather than as a silent short read.

Measured on `Classic Dungeon X2 (Japan) (v1.02)`, `/PSP_GAME/USRDIR/DATA.DAT`:
all 96 `.DMD` members decode, and each decoded length equals the size PSPFS
records for that member - 1,201,348 bytes in total. That equality is the check
worth having, because PSPFS's number is written by the game's own packer and is
independent of anything here.

What this does NOT do: rebuild. gzip output depends on the encoder, so a rebuilt
block would not be byte-identical to the original, and nothing on this disc needs
one - none of the 96 members carries a player-visible Japanese string, so there is
no text in them to inject. Write the encoder when a member has to change, and
make `pspfs.build`'s `decompressed=` carry the new size when you do.
"""

import gzip
import struct

#: `0x00020000` on every DMD member on this disc. Carried, not interpreted -
#: the same value `sdt.py` sees, and no code here branches on it.
KIND = 0x00020000

HEADER = 8


class DmdError(Exception):
    """A DMD container that does not match the header above."""


def parse_header(blob):
    """`(count, kind, [offsets])`, with every offset checked against `blob`.

    Fails closed. An offset table that runs off the end, is not ascending, or
    overlaps its own table is a different format wearing the same first bytes,
    and guessing past that point produces garbage that looks like data.
    """
    if len(blob) < HEADER + 4:
        raise DmdError('too short for a header and one offset: %d bytes'
                       % len(blob))
    count, kind = struct.unpack_from('<II', blob, 0)
    if count < 1:
        raise DmdError('block count is zero')
    table_end = HEADER + count * 4
    if table_end > len(blob):
        raise DmdError('offset table for %d blocks runs past the end '
                       '(needs %d bytes, have %d)'
                       % (count, table_end, len(blob)))
    offsets = list(struct.unpack_from('<%dI' % count, blob, HEADER))
    previous = table_end - 1
    for index, offset in enumerate(offsets):
        if offset < table_end:
            raise DmdError('block %d starts at %d, inside the offset table '
                           'that ends at %d' % (index, offset, table_end))
        if offset >= len(blob):
            raise DmdError('block %d starts at %d, past the end (%d bytes)'
                           % (index, offset, len(blob)))
        if offset <= previous:
            raise DmdError('block %d starts at %d, not after block %d'
                           % (index, offset, index - 1))
        previous = offset
    return count, kind, offsets


def blocks(blob):
    """Decoded bytes of each block, in order."""
    count, _kind, offsets = parse_header(blob)
    out = []
    for index, offset in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < count else len(blob)
        chunk = blob[offset:end]
        if chunk[:2] != b'\x1f\x8b':
            raise DmdError('block %d at %d has no gzip magic (%s)'
                           % (index, offset, chunk[:2].hex() or 'empty'))
        try:
            out.append(gzip.decompress(bytes(chunk)))
        except Exception as exc:
            raise DmdError('block %d at %d does not inflate: %s'
                           % (index, offset, exc)) from None
    return out


def decode(blob, expect=None):
    """The container's payload: every block decoded and concatenated.

    `expect` is the size PSPFS recorded for the member. Pass it and a container
    that decodes to a different length is rejected rather than returned - the
    only independent check on this decoder, since that number comes from the
    game's own packer.
    """
    payload = b''.join(blocks(blob))
    if expect is not None and len(payload) != expect:
        raise DmdError('payload is %d bytes, PSPFS records %d'
                       % (len(payload), expect))
    return payload

"""SDT — one payload cut into IMY blocks, with a directory in front.

PROVENANCE: measured from the disc. The rebuild test parses the real file,
builds it again from its own payload, and demands the bytes come back identical.

    0x00   4  block count
    0x04   4  kind; 0x00020000 on this disc, preserved rather than interpreted
    0x08      block offsets, one u32 each, then the blocks

A block is an IMY container (see `imy.py`) and the payload is the blocks'
decoded contents concatenated in order — the split is transport, not structure.
A member of the archive inside can and does straddle a block boundary, so code
that reads a block on its own reads half a file. Decode everything, then parse.

The cut is uniform: every block but the last holds a full `stride * rows` of
payload, and the last one holds the remainder with its `height` reduced to fit.
Each block is then zero padded to a 4-byte boundary so the next one starts
aligned, which is the only reason a block's stored length can exceed what its
header accounts for.

Content boundary: this module knows the payload is bytes. That it happens to be
a DSARC archive on this disc is `dsarc.py`'s business.
"""
import struct

from . import imy

HEADER = 8


class SdtError(Exception):
    pass


class Sdt:
    """A parsed SDT: the directory, the block geometry, and the payload."""

    def __init__(self, data):
        if len(data) < HEADER:
            raise SdtError('too short to hold a directory')
        count, self.kind = struct.unpack_from('<II', data, 0)
        if not count:
            raise SdtError('directory declares no blocks')
        need = HEADER + count * 4
        if need > len(data):
            raise SdtError('directory of %d blocks needs %d bytes, have %d'
                           % (count, need, len(data)))
        self.offsets = list(struct.unpack_from('<%dI' % count, data, HEADER))
        if self.offsets[0] != need:
            raise SdtError('first block at %d, directory ends at %d'
                           % (self.offsets[0], need))
        payload = bytearray()
        self.headers = []
        prev = None
        for off in self.offsets:
            if prev is not None and off <= prev:
                raise SdtError('block offsets are not ascending: %d after %d'
                               % (off, prev))
            prev = off
            head, palette, pixels = imy.decode(data, off)
            if palette:
                raise SdtError('block at %d carries a palette; this container '
                               'holds data, not images' % off)
            self.headers.append(head)
            payload += pixels
        self.payload = bytes(payload)

    @property
    def stride(self):
        return self.headers[0].width

    @property
    def rows(self):
        return self.headers[0].height

    def __len__(self):
        return len(self.headers)

    def build(self, payload=None):
        """Re-emit the container, optionally around a different payload."""
        if payload is None:
            payload = self.payload
        first = self.headers[0]
        return build(payload, self.stride, self.rows, first.flags, first.depth,
                     self.kind)


def build(payload, stride, rows, flags=0x10, depth=8, kind=0x00020000):
    """Cut `payload` into IMY blocks and write the directory in front."""
    if stride <= 0 or rows <= 0:
        raise SdtError('block geometry must be positive, got %d x %d'
                       % (stride, rows))
    span = stride * rows
    pieces = [payload[i:i + span] for i in range(0, len(payload), span)] \
        or [b'']
    if len(pieces[-1]) % stride:
        raise SdtError('payload tail of %d bytes is not a whole number of '
                       'rows at stride %d' % (len(pieces[-1]), stride))
    blocks = []
    for piece in pieces:
        head = imy.Header(imy.HEADER + len(piece), stride, flags, depth,
                          len(piece) // stride, 0)
        blob = imy.encode(head, b'', piece)
        blocks.append(blob + b'\x00' * (-len(blob) % 4))
    out = bytearray(struct.pack('<II', len(blocks), kind))
    off = HEADER + len(blocks) * 4
    for blob in blocks:
        out += struct.pack('<I', off)
        off += len(blob)
    for blob in blocks:
        out += blob
    return bytes(out)

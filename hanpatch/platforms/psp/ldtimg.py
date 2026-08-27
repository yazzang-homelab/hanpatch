"""Image surfaces stored as gzip members inside an `.LDT` container.

`imy.py` reads the IMY-magic textures. It is not the only image carrier on this
disc: `.LDT` containers also hold plain gzip members whose decoded payload is a
swizzled paletted surface. Measured on `Classic Dungeon X2 (Japan) (v1.02)`:
2,602 such blocks across 212 `.LDT` members, against 686 IMY blocks - so a
reader that only knows IMY sees about a fifth of the images on the disc.

That gap had a concrete cost. The opening narration - the first text a player
reads - is not script text at all. It is a pre-rendered TEXT image in one of
these blocks (`OPENING.LDT`, the gzip member at `0xdc48`), which is why every
Shift-JIS search across the script, the database, the executable, the DMD
members and the whole image failed: there is no string to find.

    CONTAINER (little endian)

    0x00   4  block count
    0x04  12  zero on every member measured
    0x10   4  payload size, equal to `len(blob) - 32`
    0x14   4  a per-member value, carried and not interpreted
    0x20      payload: tables, then gzip members

`rebuild` recomputes `0x10` because re-encoding changes the member's length.

**Geometry is not guessable and must be measured.** A 32,768-byte block fits
both 8bpp 256x128 and 4bpp 512x128, and *both* render as readable glyphs - one of
them at half scale. Separate them by the on-screen pitch: for the narration block
the line height is 16 px under either reading, but the per-line ink width is
168 px at 8bpp against 259 px at 4bpp, and the shipped frame measures
16.25 px per character, so 4bpp (about 16.2 px) is the surface and 8bpp is
exactly half. Never conclude a geometry from "the render looks like text".

**Round trip.** `swizzle`/`unswizzle` and the 4bpp pack/unpack are exact
involutions on the real block. gzip is not: re-encoding the identical payload
produced 3,485 bytes against the shipped 3,513, because the format admits many
legal encodings of the same bytes. So the round-trip criterion here is decoded
equivalence, not byte equality - which is the rule the graphics-text strategy
already sets for formats with multiple legal representations. Verified on
`OPENING.LDT`: an unmodified rebuild parses, both blocks decode, and every pixel
of both surfaces is unchanged.
"""

import struct
import zlib

from hanpatch.platforms.psp import font

#: Header size, and the offset of the payload-size field inside it.
HEADER = 32
PAYLOAD_SIZE_AT = 0x10

#: Decoded members below this are tables or metadata, not surfaces.
SURFACE_FLOOR = 1024


class LdtImageError(Exception):
    """A container or surface that does not match the layout above."""


def payload_size(blob):
    """The size the header declares. Should equal `len(blob) - HEADER`."""
    if len(blob) < HEADER:
        raise LdtImageError('too short for a header: %d bytes' % len(blob))
    return struct.unpack_from('<I', blob, PAYLOAD_SIZE_AT)[0]


def header_consistent(blob):
    """True when the declared payload size matches the actual length."""
    try:
        return payload_size(blob) == len(blob) - HEADER
    except LdtImageError:
        return False


def blocks(blob, floor=SURFACE_FLOOR):
    """`[(start, end, decoded)]` for every gzip member, in file order.

    `end` is exclusive and is derived from what the inflater did not consume, so
    a caller can splice a member without guessing where it stopped.
    """
    out = []
    pos = 0
    while pos < len(blob):
        at = blob.find(b'\x1f\x8b', pos)
        if at < 0:
            break
        try:
            obj = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = obj.decompress(bytes(blob[at:]))
        except Exception:
            pos = at + 2
            continue
        if len(decoded) < floor:
            pos = at + 2
            continue
        end = len(blob) - len(obj.unused_data)
        out.append((at, end, decoded))
        pos = end
    return out


def unpack_4bpp(surface):
    """One byte per pixel, low nibble first - the order this disc stores."""
    out = bytearray(len(surface) * 2)
    out[0::2] = bytes(b & 0x0F for b in surface)
    out[1::2] = bytes(b >> 4 for b in surface)
    return bytes(out)


def pack_4bpp(pixels):
    """Inverse of `unpack_4bpp`. Rejects indices that do not fit 4 bits."""
    if len(pixels) % 2:
        raise LdtImageError('a 4bpp surface needs an even pixel count, got %d'
                            % len(pixels))
    if any(p > 0x0F for p in pixels):
        raise LdtImageError('pixel index above 15 cannot be packed at 4bpp')
    return bytes(((pixels[i + 1] << 4) | pixels[i])
                 for i in range(0, len(pixels), 2))


def decode_surface(decoded, stride, height, depth=4):
    """Linear pixels for a decoded block, one byte per pixel.

    `stride` is a BYTE stride, matching `imy.py`'s `width`. At 4bpp the pixel
    width is therefore `stride * 2`; reading it as a pixel count gets every
    buffer size wrong.
    """
    if depth not in (4, 8):
        raise LdtImageError('unsupported depth %r' % depth)
    need = stride * height
    if len(decoded) < need:
        raise LdtImageError('block holds %d bytes, a %dx%d surface needs %d'
                            % (len(decoded), stride, height, need))
    linear = font.unswizzle(decoded[:need], stride, height)
    return unpack_4bpp(linear) if depth == 4 else linear


def encode_surface(pixels, stride, height, depth=4):
    """Inverse of `decode_surface`: swizzled bytes ready to gzip."""
    packed = pack_4bpp(pixels) if depth == 4 else bytes(pixels)
    need = stride * height
    if len(packed) != need:
        raise LdtImageError('encoded surface is %d bytes, a %dx%d surface '
                            'needs %d' % (len(packed), stride, height, need))
    return font.swizzle(packed, stride, height)


def rebuild(blob, index, decoded, level=9):
    """Replace one gzip member and fix the header's payload size.

    The replacement is re-encoded, so the container length changes and the
    declared payload size must move with it. Everything outside the spliced
    member - earlier blocks, the trailing bytes after the last one - is copied
    verbatim.
    """
    found = blocks(blob)
    if not 0 <= index < len(found):
        raise LdtImageError('block %d does not exist (container holds %d)'
                            % (index, len(found)))
    start, end, _old = found[index]
    obj = zlib.compressobj(level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    stream = obj.compress(bytes(decoded)) + obj.flush()
    out = bytearray(blob[:start]) + stream + bytes(blob[end:])
    struct.pack_into('<I', out, PAYLOAD_SIZE_AT, len(out) - HEADER)
    return bytes(out)

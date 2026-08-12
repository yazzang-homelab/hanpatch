"""DMP textures - the raw image form Dragon Quest VII stores under /LAYOUTTEX.

A `.dmp` member is a 16-byte header followed by raw pixels:

    0x00   4  magic 'DMP' + version byte, 3 in all 45 textures measured
    0x04   4  pixel format, ASCII; '8888' (RGBA8888) in all 45
    0x08   2  u16 width
    0x0A   2  u16 height
    0x0C   2  u16 width again
    0x0E   2  u16 height again

The duplicated size is carried through rather than recomputed. It equals the
first pair in every texture this ROM ships, but a reader that cannot reproduce a
field it does not understand has no business rewriting the file, so both pairs
survive a round trip.

Pixels are NOT linear. The 3DS samples textures in 8x8 tiles whose 64 pixels are
in Morton (Z) order, and each pixel is stored ABGR - so a naive read produces an
image that looks like scrambled blocks with swapped channels. `decode` and
`encode` are exact inverses over every texture in the cartridge, which is the
property the tests assert; without it, replacing artwork would silently corrupt
the atlas around it.
"""
import struct

MAGIC = b'DMP'
HEADER = 0x10
RGBA8888 = '8888'
TILE = 8

# Morton order inside an 8x8 tile: bit 0 of the index is x0, bit 1 is y0, and so
# on, interleaved. Precomputed because it is the same 64 pairs for every tile.
_MORTON = [(((n & 1) | ((n >> 1) & 2) | ((n >> 2) & 4)),
            (((n >> 1) & 1) | ((n >> 2) & 2) | ((n >> 3) & 4)))
           for n in range(TILE * TILE)]


class TextureError(ValueError):
    pass


def parse(data, where='<texture>'):
    """(version, fmt, width, height, width2, height2) from the 16-byte header."""
    if len(data) < HEADER:
        raise TextureError(f'{where}: {len(data)} bytes is shorter than a DMP header')
    if data[:3] != MAGIC:
        raise TextureError(f'{where}: not a DMP texture (magic {data[:3]!r})')
    version = data[3]
    fmt = data[4:8].decode('latin1')
    width, height, width2, height2 = struct.unpack_from('<4H', data, 8)
    if fmt != RGBA8888:
        raise TextureError(
            f'{where}: pixel format {fmt!r} has no encoder here. Only {RGBA8888!r} '
            f'was measured in this cartridge (45 of 45 textures); guessing a '
            f'layout for another format would corrupt the atlas silently.')
    if width % TILE or height % TILE:
        raise TextureError(
            f'{where}: {width}x{height} is not a whole number of {TILE}x{TILE} '
            f'tiles, which is the only tiling this reader can reproduce')
    want = HEADER + width * height * 4
    if len(data) != want:
        raise TextureError(f'{where}: {width}x{height} needs {want} bytes, got {len(data)}')
    return version, fmt, width, height, width2, height2


def decode(data, where='<texture>'):
    """Return a PIL RGBA image, untiled and channel-corrected."""
    from PIL import Image
    _v, _f, width, height, _w2, _h2 = parse(data, where)
    out = Image.new('RGBA', (width, height))
    px = out.load()
    pos = HEADER
    for ty in range(0, height, TILE):
        for tx in range(0, width, TILE):
            for x, y in _MORTON:
                a, b, g, r = data[pos:pos + 4]
                pos += 4
                px[tx + x, ty + y] = (r, g, b, a)
    return out


def encode(image, template=None, where='<texture>'):
    """Return DMP bytes for `image`, reusing `template`'s header when given.

    A replacement has to keep the size the archive and the layout were built
    around: the atlas coordinates that reference this texture live in other
    files, so a differently sized image is refused rather than scaled silently.
    """
    if template is None:
        width, height = image.size
        version, fmt, width2, height2 = 3, RGBA8888, width, height
    else:
        version, fmt, width, height, width2, height2 = parse(template, where)
        if image.size != (width, height):
            raise TextureError(
                f'{where}: replacement is {image.size[0]}x{image.size[1]}, but this '
                f'texture is {width}x{height}. Resize it first - the layout that '
                f'references this texture is not in this file.')
    if width % TILE or height % TILE:
        raise TextureError(f'{where}: {width}x{height} is not a whole number of tiles')
    rgba = image.convert('RGBA')
    px = rgba.load()
    out = bytearray(MAGIC + bytes([version]) + fmt.encode('latin1'))
    out += struct.pack('<4H', width, height, width2, height2)
    for ty in range(0, height, TILE):
        for tx in range(0, width, TILE):
            for x, y in _MORTON:
                r, g, b, a = px[tx + x, ty + y]
                out += bytes((a, b, g, r))
    return bytes(out)

"""BLZ (backwards LZ) decompressor used for 3DS/NDS .code."""
import struct


def decompress(data: bytes) -> bytes:
    footer = data[-8:]
    extra_size, = struct.unpack('<I', footer[4:8])
    if extra_size == 0:
        return data
    hdr_size = data[-5]
    comp_end, = struct.unpack('<I', footer[0:4])
    comp_end &= 0xFFFFFF
    total = len(data)
    dec_size = total + extra_size
    out = bytearray(data)
    out.extend(b'\0' * extra_size)

    # region [total-comp_end, total-hdr_size) is compressed, read backwards
    src = total - hdr_size          # read pointer (moves down)
    dst = dec_size                  # write pointer (moves down)
    end = total - comp_end          # stop when src reaches this
    while src > end:
        src -= 1
        flags = out[src]
        for i in range(8):
            if flags & 0x80:
                src -= 2
                pos = (out[src] | (out[src + 1] << 8))
                length = 3 + (pos >> 12)
                pos = (pos & 0xFFF) + 3
                for _ in range(length):
                    b = out[dst + pos - 1]
                    dst -= 1
                    out[dst] = b
            else:
                src -= 1
                dst -= 1
                out[dst] = out[src]
            flags = (flags << 1) & 0xFF
            if src <= end:
                break
    return bytes(out[:dec_size])


if __name__ == '__main__':
    import sys
    open(sys.argv[2], 'wb').write(decompress(open(sys.argv[1], 'rb').read()))

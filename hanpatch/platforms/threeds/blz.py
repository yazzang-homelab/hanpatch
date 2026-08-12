"""BLZ (backwards LZ) decompressor used for 3DS/NDS .code."""
import struct


def decompress(data: bytes) -> bytes:
    if len(data) < 8:
        return data
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


def _forward_lz(data: bytes):
    """Greedily encode a forward byte stream for BLZ's reversed payload."""
    encoded = bytearray()
    current = 0
    trailing_data = 0
    trailing_encoded = 0
    best_saving = 0
    while current < len(data):
        flag_at = len(encoded)
        encoded.append(0)
        flags = 0
        trailing_encoded += 1
        for slot in range(8):
            if current >= len(data):
                break
            window_start = max(0, current - 0x1002)
            match_at = -1
            match_len = 0
            for length in range(min(18, len(data) - current), 2, -1):
                match_at = data.rfind(
                    data[current:current + length], window_start, current)
                if match_at >= 0:
                    match_len = length
                    break
            if match_len:
                flags |= 0x80 >> slot
                displacement = current - match_at - 3
                encoded.extend((
                    ((match_len - 3) << 4) | (displacement >> 8),
                    displacement & 0xFF,
                ))
                current += match_len
                trailing_data += match_len
                trailing_encoded += 2
            else:
                encoded.append(data[current])
                current += 1
                trailing_data += 1
                trailing_encoded += 1
            saving = current - len(encoded)
            if saving > best_saving:
                best_saving = saving
                trailing_data = 0
                trailing_encoded = 0
        encoded[flag_at] = flags
    return bytes(encoded), trailing_data, trailing_encoded


def compress(data: bytes) -> bytes:
    """Return a deterministic BLZ encoding that round-trips through ``decompress``."""
    if not data:
        return data

    forward, raw_prefix_size, encoded_prefix_size = _forward_lz(data[::-1])
    backward = bytearray(forward[::-1])
    if not backward:
        return data

    compressed_region_size = len(backward) - encoded_prefix_size
    backward = bytearray(data[:raw_prefix_size]) + backward[encoded_prefix_size:]
    extra_size = len(data) - len(backward)
    header_size = 8
    padding = (-len(backward)) % 4
    if extra_size <= header_size + padding:
        return data
    backward.extend(b'\xFF' * padding)
    header_size += padding
    footer_at = len(backward)
    backward.extend(b'\0' * 8)
    struct.pack_into(
        '<I', backward, footer_at, compressed_region_size + header_size)
    backward[footer_at + 3] = header_size
    struct.pack_into('<I', backward, footer_at + 4, extra_size - header_size)

    result = bytes(backward)
    if len(result) >= len(data):
        return data
    if decompress(result) != data:
        raise ValueError('BLZ compression round-trip mismatch')
    return result
if __name__ == '__main__':
    import sys
    open(sys.argv[2], 'wb').write(decompress(open(sys.argv[1], 'rb').read()))

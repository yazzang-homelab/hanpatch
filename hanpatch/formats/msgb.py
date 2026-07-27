"""msgb (.mbin) reader/writer for Crimson Shroud (3DS)."""
import struct


def align(x, a=4):
    return (x + a - 1) // a * a


class Msgb:
    def __init__(self, data):
        assert data[:4] == b'msgb', data[:4]
        self.entry_off, self.count, self.f0c, self.key_off = struct.unpack('<4I', data[4:0x14])
        self.tail = data[0x14:0x20]
        self.entries = []   # (flag_a, flag_b, text)
        for i in range(self.count):
            o = self.entry_off + i * 0x10
            a, b, so, sz = struct.unpack('<4I', data[o:o + 0x10])
            self.entries.append([a, b, data[so:so + sz].decode('utf-16-le')])
        self.keys = []
        for i in range(self.count):
            ko, = struct.unpack('<I', data[self.key_off + i * 4:self.key_off + i * 4 + 4])
            end = data.index(b'\0', ko)
            self.keys.append(data[ko:end].decode('ascii'))

    def items(self):
        return list(zip(self.keys, (e[2] for e in self.entries)))

    def build(self):
        n = self.count
        entry_off = 0x20
        str_start = align(entry_off + n * 0x10, 0x10)
        blobs = []
        pos = str_start
        ents = []
        for a, b, txt in self.entries:
            raw = txt.encode('utf-16-le')
            ents.append((a, b, pos, len(raw)))
            blobs.append((pos, raw + b'\0\0'))
            pos = align(pos + len(raw) + 2, 4)
        key_off = align(pos, 0x10)
        koffs = []
        kpos = key_off + n * 4
        kblob = bytearray()
        for k in self.keys:
            koffs.append(key_off + n * 4 + len(kblob))
            kblob += k.encode('ascii') + b'\0'
        total = align(key_off + n * 4 + len(kblob), 0x10)
        out = bytearray(total)
        out[:4] = b'msgb'
        struct.pack_into('<4I', out, 4, entry_off, n, self.f0c, key_off)
        out[0x14:0x20] = self.tail
        for i, (a, b, so, sz) in enumerate(ents):
            struct.pack_into('<4I', out, entry_off + i * 0x10, a, b, so, sz)
        for off, raw in blobs:
            out[off:off + len(raw)] = raw
        for i, ko in enumerate(koffs):
            struct.pack_into('<I', out, key_off + i * 4, ko)
        out[key_off + n * 4:key_off + n * 4 + len(kblob)] = kblob
        return bytes(out)


def load(path):
    return Msgb(open(path, 'rb').read())

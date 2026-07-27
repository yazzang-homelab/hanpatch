import struct


def align(x, a):
    return (x + a - 1) // a * a


class RomFS:
    def __init__(self, path):
        self.f = open(path, 'rb')
        h = self.f.read(0x60)
        assert h[:4] == b'IVFC', h[:4]
        mh_size, = struct.unpack('<I', h[0x08:0x0C])
        lvls = []
        for i in range(3):
            o = 0x0C + i * 0x18
            lo, sz, bs = struct.unpack('<QQI', h[o:o + 0x14])
            lvls.append((lo, sz, 1 << bs))
        # NCCH RomFS layout: IVFC header + master hash, then level3 (file data);
        # the level1/level2 hash blocks are appended after level3 data.
        self.lvl3 = align(0x60 + mh_size, lvls[2][2])
        off = self.lvl3
        self.f.seek(off)
        l3 = self.f.read(0x28)
        (hl, self.dh_off, self.dh_len, self.dm_off, self.dm_len,
         self.fh_off, self.fh_len, self.fm_off, self.fm_len,
         self.fd_off) = struct.unpack('<10I', l3)
        assert hl == 0x28, hl
        self.f.seek(off + self.dm_off)
        self.dirmeta = self.f.read(self.dm_len)
        self.f.seek(off + self.fm_off)
        self.filemeta = self.f.read(self.fm_len)

    def _dir(self, o):
        parent, sibling, child, file0, nexthash, namelen = struct.unpack('<6I', self.dirmeta[o:o + 0x18])
        name = self.dirmeta[o + 0x18:o + 0x18 + namelen].decode('utf-16-le')
        return parent, sibling, child, file0, name

    def _file(self, o):
        parent, sibling = struct.unpack('<2I', self.filemeta[o:o + 8])
        data_off, data_size = struct.unpack('<2Q', self.filemeta[o + 8:o + 0x18])
        nexthash, namelen = struct.unpack('<2I', self.filemeta[o + 0x18:o + 0x20])
        name = self.filemeta[o + 0x20:o + 0x20 + namelen].decode('utf-16-le')
        return parent, sibling, data_off, data_size, name

    def walk(self, diroff=0, prefix=''):
        """yields (path, data_offset_absolute, size)"""
        parent, sibling, child, file0, name = self._dir(diroff)
        fo = file0
        while fo != 0xFFFFFFFF:
            p, s, do, ds, fn = self._file(fo)
            yield (prefix + '/' + fn, self.lvl3 + self.fd_off + do, ds)
            fo = s
        co = child
        while co != 0xFFFFFFFF:
            p, s, c, f0, dn = self._dir(co)
            yield from self.walk(co, prefix + '/' + dn)
            co = s

    def read(self, off, size):
        self.f.seek(off)
        return self.f.read(size)


if __name__ == '__main__':
    import sys
    r = RomFS(sys.argv[1] if len(sys.argv) > 1 else 'extracted/romfs.bin')
    n = 0
    for path, off, size in r.walk():
        print(f'{size:>12} {off:#012x} {path}')
        n += 1
    print(n, 'files')

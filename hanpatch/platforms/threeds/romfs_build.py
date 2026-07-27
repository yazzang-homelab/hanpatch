"""RomFS (IVFC level3 + hash tree) builder/validator for 3DS NCCH."""
import hashlib
import os
import struct


def align(x, a):
    return (x + a - 1) // a * a


def hash_count(n):
    if n < 3:
        return 3
    if n < 19:
        return n | 1
    c = n
    while any(c % p == 0 for p in (2, 3, 5, 7, 11, 13, 17)):
        c += 1
    return c


def name_hash(parent_off, name):
    h = (parent_off ^ 123456789) & 0xFFFFFFFF
    for ch in name:
        h = ((h >> 5) | (h << 27)) & 0xFFFFFFFF
        h = (h ^ ord(ch)) & 0xFFFFFFFF
    return h


class Node:
    def __init__(self, name, parent=None):
        self.name = name
        self.parent = parent
        self.dirs = {}
        self.files = {}   # name -> (path or bytes)
        self.off = 0


def build_tree(root_dir):
    root = Node('')
    for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=True):
        rel = os.path.relpath(dirpath, root_dir)
        node = root
        if rel != '.':
            for part in rel.split(os.sep):
                node = node.dirs.setdefault(part, Node(part, node))
        for fn in filenames:
            node.files[fn] = os.path.join(dirpath, fn)
    return root


def build_level3(root):
    dirs = []      # ordered list of Node
    files = []     # ordered list of (Node parent, name, path)

    def walk(n):
        dirs.append(n)
        for k in sorted(n.dirs):
            walk(n.dirs[k])
    walk(root)
    for n in dirs:
        for k in sorted(n.files):
            files.append((n, k, n.files[k]))

    n_dir_buckets = hash_count(len(dirs))
    n_file_buckets = hash_count(len(files))

    def dir_entry_size(n):
        return 0x18 + align(len(n.name) * 2, 4)

    def file_entry_size(name):
        return 0x20 + align(len(name) * 2, 4)

    # assign metadata offsets
    off = 0
    for n in dirs:
        n.off = off
        off += dir_entry_size(n)
    dirmeta_size = off
    off = 0
    file_offs = {}
    for i, (p, name, path) in enumerate(files):
        file_offs[i] = off
        off += file_entry_size(name)
    filemeta_size = off

    dh_off = 0x28
    dh_len = n_dir_buckets * 4
    dm_off = dh_off + dh_len
    dm_len = dirmeta_size
    fh_off = dm_off + dm_len
    fh_len = n_file_buckets * 4
    fm_off = fh_off + fh_len
    fm_len = filemeta_size
    fd_off = align(fm_off + fm_len, 0x10)

    # file data offsets
    data_offs = []
    pos = 0
    sizes = []
    for (p, name, path) in files:
        sz = os.path.getsize(path)
        data_offs.append(pos)
        sizes.append(sz)
        pos = align(pos + sz, 0x10)
    data_total = pos

    # hash buckets
    dbuck = [0xFFFFFFFF] * n_dir_buckets
    dnext = {}
    for n in dirs:
        parent_off = n.parent.off if n.parent is not None else 0
        h = name_hash(parent_off, n.name) % n_dir_buckets
        dnext[n.off] = dbuck[h]
        dbuck[h] = n.off
    fbuck = [0xFFFFFFFF] * n_file_buckets
    fnext = {}
    for i, (p, name, path) in enumerate(files):
        h = name_hash(p.off, name) % n_file_buckets
        fnext[i] = fbuck[h]
        fbuck[h] = file_offs[i]

    # sibling / child links
    def dir_children(n):
        return [n.dirs[k] for k in sorted(n.dirs)]
    file_index_by_dir = {}
    for i, (p, name, path) in enumerate(files):
        file_index_by_dir.setdefault(id(p), []).append(i)

    out = bytearray(fd_off)
    struct.pack_into('<10I', out, 0, 0x28, dh_off, dh_len, dm_off, dm_len,
                     fh_off, fh_len, fm_off, fm_len, fd_off)
    for i, v in enumerate(dbuck):
        struct.pack_into('<I', out, dh_off + i * 4, v)
    for i, v in enumerate(fbuck):
        struct.pack_into('<I', out, fh_off + i * 4, v)

    for n in dirs:
        kids = dir_children(n)
        sibling = 0xFFFFFFFF
        if n.parent is not None:
            sibs = dir_children(n.parent)
            k = sibs.index(n)
            sibling = sibs[k + 1].off if k + 1 < len(sibs) else 0xFFFFFFFF
        child = kids[0].off if kids else 0xFFFFFFFF
        fl = file_index_by_dir.get(id(n))
        first_file = file_offs[fl[0]] if fl else 0xFFFFFFFF
        parent_off = n.parent.off if n.parent is not None else 0
        nb = n.name.encode('utf-16-le')
        struct.pack_into('<6I', out, dm_off + n.off, parent_off, sibling, child,
                         first_file, dnext.get(n.off, 0xFFFFFFFF), len(nb))
        out[dm_off + n.off + 0x18: dm_off + n.off + 0x18 + len(nb)] = nb

    for i, (p, name, path) in enumerate(files):
        sibs = file_index_by_dir[id(p)]
        k = sibs.index(i)
        sibling = file_offs[sibs[k + 1]] if k + 1 < len(sibs) else 0xFFFFFFFF
        nb = name.encode('utf-16-le')
        o = fm_off + file_offs[i]
        struct.pack_into('<II', out, o, p.off, sibling)
        struct.pack_into('<QQ', out, o + 8, data_offs[i], sizes[i])
        struct.pack_into('<II', out, o + 0x18, fnext[i], len(nb))
        out[o + 0x20:o + 0x20 + len(nb)] = nb

    return bytes(out), files, data_offs, sizes, data_total


def write_romfs(root_dir, out_path):
    root = build_tree(root_dir)
    meta, files, data_offs, sizes, data_total = build_level3(root)
    level3_size = len(meta) + data_total
    lvl3_phys = 0x1000

    with open(out_path, 'w+b') as f:
        f.seek(lvl3_phys)
        f.write(meta)
        for (p, name, path), do, sz in zip(files, data_offs, sizes):
            f.seek(lvl3_phys + len(meta) + do)
            with open(path, 'rb') as src:
                while True:
                    b = src.read(1 << 22)
                    if not b:
                        break
                    f.write(b)
        # pad level3 to 0x1000
        end = lvl3_phys + align(level3_size, 0x1000)
        f.truncate(end)

        # level2 = hashes of level3 blocks
        f.seek(lvl3_phys)
        lvl2 = bytearray()
        remaining = level3_size
        nblocks = align(level3_size, 0x1000) // 0x1000
        for i in range(nblocks):
            blk = f.read(0x1000)
            blk = blk + b'\0' * (0x1000 - len(blk))
            lvl2 += hashlib.sha256(blk).digest()
        lvl2_size = len(lvl2)
        lvl1 = bytearray()
        pad2 = bytes(lvl2) + b'\0' * ((-lvl2_size) % 0x1000)
        for i in range(0, len(pad2), 0x1000):
            lvl1 += hashlib.sha256(pad2[i:i + 0x1000]).digest()
        lvl1_size = len(lvl1)
        master = bytearray()
        pad1 = bytes(lvl1) + b'\0' * ((-lvl1_size) % 0x1000)
        for i in range(0, len(pad1), 0x1000):
            master += hashlib.sha256(pad1[i:i + 0x1000]).digest()

        lvl1_phys = end
        lvl2_phys = lvl1_phys + align(lvl1_size, 0x1000)
        f.seek(lvl1_phys)
        f.write(pad1)
        f.seek(lvl2_phys)
        f.write(pad2)
        total = lvl2_phys + len(pad2)

        # IVFC header
        hdr = bytearray(0x60)
        hdr[0:4] = b'IVFC'
        struct.pack_into('<I', hdr, 4, 0x10000)
        struct.pack_into('<I', hdr, 8, len(master))
        struct.pack_into('<QQI', hdr, 0x0C, 0, lvl1_size, 12)
        struct.pack_into('<QQI', hdr, 0x24, align(lvl1_size, 0x1000), lvl2_size, 12)
        struct.pack_into('<QQI', hdr, 0x3C, align(lvl1_size, 0x1000) + align(lvl2_size, 0x1000),
                         level3_size, 12)
        struct.pack_into('<I', hdr, 0x54, 0x5C)
        f.seek(0)
        f.write(bytes(hdr))
        f.write(bytes(master))
        f.truncate(total)
    return total

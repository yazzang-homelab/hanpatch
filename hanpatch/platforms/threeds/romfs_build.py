"""RomFS (IVFC level3 + hash tree) builder/validator for 3DS NCCH."""
import hashlib
import os
import struct

from hanpatch.platforms.threeds.copyx import copy_exact


#: level3 always starts on this grain, and the IVFC header plus the master hash
#: table must fit inside the first one.
LVL3_GRAIN = 0x1000


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


def sibling_order(image):
    """Entry order read out of an EXISTING RomFS image.

    The order is CAPTURED rather than derived from a rule, because no simple rule
    explains this cartridge. Measured over its 14117-file, 56-directory RomFS:
    of the 47 file
    sibling lists holding more than one entry, 39 are casefold-sorted, 36 are
    ASCII-sorted and 7 are neither; and the 56 directory metadata records are not
    laid out purely breadth-first either (the depth sequence contains descents and
    an inversion). Any rule guessed from a sample would therefore move entries in
    the files it did not explain, and moving one entry shifts every data offset
    after it.

    `children` - the per-directory sibling order, read from the sibling chains.

    `dir_layout` - the order the directory METADATA itself is laid out in, read
    from the dirmeta offset sequence. Metadata offsets are what the
    parent/sibling/hash-bucket fields all point at, so getting this wrong changes
    thousands of pointers even when every name and every byte of file data is
    already in the right place.

    Returns {'children': {dir_path: ([dir names], [file names])}, 'dir_layout':
    [dir_path, ...]} with '' for the root.
    """
    from hanpatch.platforms.threeds import romfs as romfs_read
    r = romfs_read.RomFS(image)

    # Sequential parse: metadata offset order IS the layout order.
    seq = []
    o = 0
    while o + 0x18 <= len(r.dirmeta):
        parent, _sib, _child, _f0, _nx, namelen = struct.unpack_from(
            '<6I', r.dirmeta, o)
        name = (r.dirmeta[o + 0x18:o + 0x18 + namelen].decode('utf-16-le')
                if namelen else '')
        seq.append((o, parent, name))
        o += 0x18 + align(namelen, 4)

    # Resolve by walking the parent chain, not in one forward pass. A forward pass
    # silently treats a record whose parent it has not reached yet as top-level,
    # files its captured sibling list under a wrong key, and then falls through to
    # the default sort for that directory - the exact behaviour the capture exists
    # to override, and invisible afterwards. This layout is not monotone (its depth
    # sequence contains descents and an inversion), so that case is reachable.
    by_off = {off: (parent, name) for off, parent, name in seq}
    path_by_off = {}

    def resolve(off, seen=None):
        if off in path_by_off:
            return path_by_off[off]
        if off not in by_off:
            raise SystemExit(
                f'{image}: a directory record names parent offset {off}, which is '
                f'not a directory record in this image; refusing to guess where it '
                f'belongs rather than silently treating it as top-level')
        seen = seen or set()
        if off in seen:
            raise SystemExit(f'{image}: directory parent chain loops at offset {off}')
        seen.add(off)
        parent, name = by_off[off]
        if not name:
            path_by_off[off] = ''
        else:
            base = resolve(parent, seen)
            path_by_off[off] = f'{base}/{name}' if base else name
        return path_by_off[off]

    for off, _parent, _name in seq:
        resolve(off)

    children = {}
    for off, _parent, _name in seq:
        _p, _s, child, file0, _n = r._dir(off)
        dnames, fnames = [], []
        fo = file0
        while fo != 0xFFFFFFFF:
            _pp, sib, _do, _ds, fn = r._file(fo)
            fnames.append(fn)
            fo = sib
        co = child
        while co != 0xFFFFFFFF:
            _pp, sib, _c, _f, dn = r._dir(co)
            dnames.append(dn)
            co = sib
        children[path_by_off[off]] = (dnames, fnames)

    # The FILE table has its own layout order and it does not follow the
    # directory one: grouping files by the directory layout order put thousands of
    # entries - and every data offset after them - in the wrong place on this
    # cartridge. Captured from the filemeta sequence, not inferred.
    fseq = []
    o = 0
    while o + 0x20 <= len(r.filemeta):
        parent = struct.unpack_from('<I', r.filemeta, o)[0]
        namelen = struct.unpack_from('<I', r.filemeta, o + 0x1C)[0]
        name = (r.filemeta[o + 0x20:o + 0x20 + namelen].decode('utf-16-le')
                if namelen else '')
        if parent not in path_by_off:
            raise SystemExit(
                f'{image}: file {name!r} names parent offset {parent}, which is not '
                f'a directory record in this image')
        fseq.append((path_by_off[parent], name))
        o += 0x20 + align(namelen, 4)

    return {'children': children,
            'dir_layout': [path_by_off[off] for off, _p, _n in seq],
            'file_layout': fseq}


def build_level3(root, order=None):
    dirs = []      # ordered list of Node
    files = []     # ordered list of (Node parent, name, path)

    children = (order or {}).get('children', {})
    layout = (order or {}).get('dir_layout')

    def path_of(n):
        parts = []
        while n is not None and n.name:
            parts.append(n.name)
            n = n.parent
        return '/'.join(reversed(parts))

    def ordered(names, want):
        """`want` first, in its order; anything new after it, sorted."""
        if not want:
            return sorted(names)
        known = [n for n in want if n in names]
        return known + sorted(set(names) - set(known))

    # ONE ordering authority. Sorting again anywhere else silently overrides this
    # and leaves the order honoured in the traversal but not in the sibling
    # chain, which is exactly how a rebuild ends up almost byte-identical.
    def child_dirs(n):
        want = children.get(path_of(n), ([], []))[0]
        return [n.dirs[k] for k in ordered(n.dirs, want)]

    def child_files(n):
        want = children.get(path_of(n), ([], []))[1]
        return ordered(n.files, want)

    def walk(n):
        dirs.append(n)
        for kid in child_dirs(n):
            walk(kid)
    walk(root)
    if layout and len(layout) != len(dirs):
        raise SystemExit(
            f'the captured order describes {len(layout)} directories but the staged '
            f'tree has {len(dirs)}; a count mismatch guarantees the rebuild cannot '
            f'reproduce the source, so refusing rather than producing an almost '
            f'identical image (a directory that exists but is EMPTY is the usual '
            f'cause - see unpack_romfs)')
    if layout:
        # Directory metadata follows the ORIGINAL image's own layout order, which
        # is neither the default depth-first traversal nor a clean breadth-first
        # one. Captured, not inferred, so a title whose tool laid it out any other
        # way is reproduced too.
        rank = {p: i for i, p in enumerate(layout)}
        dirs.sort(key=lambda n: (rank.get(path_of(n), len(rank)), path_of(n)))
    for n in dirs:
        for k in child_files(n):
            files.append((n, k, n.files[k]))
    # The per-directory chain order is a SEPARATE captured fact from the file
    # metadata layout order, and they used to be conflated silently. The contract
    # now is: the layout order assigns offsets AND the sibling chain, and the
    # agreement check below refuses any capture where the captured chain disagrees
    # with it. So the chain is honoured because it is proven equal, not because a
    # second sort re-imposes it.
    flayout = (order or {}).get('file_layout')
    if flayout:
        frank = {pair: i for i, pair in enumerate(flayout)}
        files.sort(key=lambda f: (frank.get((path_of(f[0]), f[1]), len(frank)),
                                  path_of(f[0]), f[1]))
        per_dir = {}
        for n, k, _p in files:
            per_dir.setdefault(path_of(n), []).append(k)
        for dpath, got in per_dir.items():
            want = [k for k in children.get(dpath, ([], []))[1] if k in set(got)]
            if want and got[:len(want)] != want:
                raise SystemExit(
                    f'the captured file layout order and the captured sibling chain '
                    f'disagree for directory {dpath!r}: the chain says '
                    f'{want[:4]} but the layout puts {got[:4]} first. Both were read '
                    f'from the same image, so one of them was misparsed - refusing '
                    f'rather than picking a winner')

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
    dir_children = child_dirs
    file_index_by_dir = {}
    for i, (p, name, path) in enumerate(files):
        file_index_by_dir.setdefault(id(p), []).append(i)
    # THE CONTRACT, stated because the two mechanisms cannot both be load-bearing:
    # the agreement check above is the single authority. It refuses any capture whose
    # sibling-chain order and metadata layout order disagree inside a directory, so
    # for every capture that survives, chain order and layout order are equal and a
    # second re-sort here would be a provable no-op - dead machinery that no test can
    # discriminate. If a title ever needs the divergence, the check is what will
    # surface it, naming the directory and both orders, and THEN this becomes real
    # code with a fixture behind it.

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


def write_romfs(root_dir, out_path, order_from=None, order=None):
    """Write a RomFS image from a staged tree.

    `order_from` is an existing RomFS image whose entry order should be
    reproduced; `order` is an already-captured mapping in the shape
    `sibling_order` returns, which is what makes an order that is NOT this
    writer's default reachable - both for a title whose tool differed and for a
    test that has to prove the capture is honoured rather than coincidental. Without it the entries are sorted by a plain ASCII rule, which is
    what every image this pipeline has built so far used; passing the title's own
    image is what makes an untouched rebuild BYTE-IDENTICAL for a title whose
    authoring tool ordered them some other way. It is opt-in precisely because the
    default must not move for a project whose artifacts are already pinned.
    """
    root = build_tree(root_dir)
    if order is not None and order_from is not None:
        raise SystemExit('pass either order_from (an image to capture) or order (an '
                         'already-captured mapping), not both')
    if order is None and order_from is not None:
        order = sibling_order(order_from)
    meta, files, data_offs, sizes, data_total = build_level3(root, order)
    level3_size = len(meta) + data_total
    # The master hash is not known until every level3 block has been written, so
    # the writer RESERVES the first 0x1000 for the IVFC header plus that table
    # rather than computing where level3 starts from it. verify_tree then checks the
    # reservation was actually big enough, which is the direction that can fail.
    lvl3_phys = LVL3_GRAIN

    with open(out_path, 'w+b') as f:
        f.seek(lvl3_phys)
        f.write(meta)
        for (p, name, path), do, sz in zip(files, data_offs, sizes):
            f.seek(lvl3_phys + len(meta) + do)
            # The size was measured once, in build_level3, and every later data
            # offset plus the filemeta size field was derived from it. Copying
            # without counting meant a file that shrank between the two passes left
            # a hole reading back as zeros while filemeta still declared the full
            # size - and verify_tree cannot see it, because it hashes whatever
            # landed. A file that GREW would overrun its neighbour's slot.
            with open(path, 'rb') as src:
                copy_exact(src, f, sz, path)
                if src.read(1):
                    raise SystemExit(
                        f'{path} grew past the {sz} bytes measured for it while the '
                        f'image was being written; its neighbours already own the '
                        f'space that follows, so refusing rather than overrunning '
                        f'them')
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
        f.flush()
        os.fsync(f.fileno())

    verify_tree(out_path)
    return total


def lvl3_phys_expected(master_size, grain=LVL3_GRAIN):
    """Where level3 must physically start, given the master hash size."""
    return align(0x60 + master_size, grain)


def verify_tree(path):
    """Re-derive the whole IVFC hash tree from the FINISHED file and refuse a lie.

    Why this is not paranoia: an untouched rebuild of a 1.4 GB image once produced
    two level2 leaves that were each a SINGLE BIT away from the correct digest
    over data that was itself byte-identical. The level1 and master hashes were
    then computed over the corrupted leaves, so the image was internally
    consistent and every superblock check passed. It did not reproduce on two
    further runs, so it was a transient memory or I/O fault - exactly the failure
    a release pipeline must refuse rather than sign.

    An earlier version of this check compared the level3 blocks against the
    IN-MEMORY leaf table and ran BEFORE level1, level2 and the master hash were
    written, so a fault in any of those later writes still returned success. This
    reads the finished file back and recomputes every level, which is the only
    version of the check that covers what actually shipped.
    """
    with open(path, 'rb') as f:
        hdr = f.read(0x60)
        if hdr[:4] != b'IVFC':
            raise SystemExit(f'ROMFS WRITE FAILED: {path} does not start with IVFC '
                             f'(found {hdr[:4]!r})')
        master_size = struct.unpack_from('<I', hdr, 8)[0]
        levels = []
        for i in range(3):
            o = 0x0C + i * 0x18
            lo, sz, bs = struct.unpack_from('<QQI', hdr, o)
            levels.append((lo, sz, bs))
        lvl1_size, lvl2_size, lvl3_size = (lv[1] for lv in levels)
        stored_master = f.read(master_size)

        # The header fields are what LOCATE the tree, and nothing validated them:
        # a wrong block-size exponent or a wrong level offset passes every hash
        # check here while breaking the walk on hardware. They are written by
        # write_romfs, so they have exactly one correct value each.
        want = [(0, lvl1_size, 12),
                (align(lvl1_size, 0x1000), lvl2_size, 12),
                (align(lvl1_size, 0x1000) + align(lvl2_size, 0x1000), lvl3_size, 12)]
        for i, (got, exp) in enumerate(zip(levels, want), start=1):
            if got != exp:
                raise SystemExit(
                    f'ROMFS WRITE FAILED: {path}: level{i} descriptor is '
                    f'(offset {got[0]}, size {got[1]}, block shift {got[2]}) but the '
                    f'layout requires (offset {exp[0]}, size {exp[1]}, block shift '
                    f'{exp[2]}); the tree cannot be walked from these fields')
        # The writer RESERVES one grain for the IVFC header plus the master hash
        # table, before it knows how big that table will be. This is the check that
        # the reservation held. It is not a tautology: comparing against
        # align(0x60 + master_size, grain) was, because align(x, g) >= x always.
        lvl3_phys = LVL3_GRAIN
        if 0x60 + master_size > lvl3_phys:
            raise SystemExit(f'ROMFS WRITE FAILED: {path}: the IVFC header plus a '
                             f'{master_size}-byte master hash need '
                             f'{0x60 + master_size} bytes, but level3 starts at '
                             f'{lvl3_phys} and would be overwritten')
        lvl1_phys = lvl3_phys + align(lvl3_size, 0x1000)
        lvl2_phys = lvl1_phys + align(lvl1_size, 0x1000)

        def blocks(off, size):
            # ALIGNED span, not the logical one. Every region is physically padded
            # to 0x1000 and hardware hashes whole blocks including that pad, so
            # reading only the logical length left up to three partial blocks -
            # about 12 KB - never checked, which is where a post-write fault could
            # hide while every level still "verified".
            f.seek(off)
            left = align(size, 0x1000)
            while left > 0:
                b = f.read(min(0x1000, left))
                if not b:
                    raise SystemExit(f'ROMFS WRITE FAILED: {path} ends inside the '
                                     f'region at {off} ({size} bytes expected)')
                left -= len(b)
                yield b + b'\0' * (0x1000 - len(b))

        def recompute(off, size):
            return b''.join(hashlib.sha256(b).digest() for b in blocks(off, size))

        for label, want_off, want_size, computed_over in (
                ('level2', lvl2_phys, lvl2_size, (lvl3_phys, lvl3_size)),
                ('level1', lvl1_phys, lvl1_size, (lvl2_phys, lvl2_size))):
            expect = recompute(*computed_over)
            f.seek(want_off)
            stored = f.read(want_size)
            if len(stored) != want_size:
                raise SystemExit(
                    f'ROMFS WRITE FAILED: {path}: the {label} table is {len(stored)} '
                    f'bytes but the header declares {want_size}; the file is '
                    f'truncated inside the hash tree')
            if stored != expect:
                bad = [i for i in range(0, min(len(stored), len(expect)), 32)
                       if stored[i:i + 32] != expect[i:i + 32]]
                raise SystemExit(
                    f'ROMFS WRITE FAILED: {path}: {len(bad)} of {want_size // 32} '
                    f'{label} hashes disagree with the data they cover '
                    f'(first bad index {bad[0] if bad else "n/a"}, stored '
                    f'{stored[bad[0]:bad[0] + 32].hex() if bad else "n/a"}, expected '
                    f'{expect[bad[0]:bad[0] + 32].hex() if bad else "n/a"}). The '
                    f'data and the hash tree disagree, so this image would ship a '
                    f'corrupt patch that still passes every superblock check. This '
                    f'is a transient memory or I/O fault, not a translation '
                    f'problem: rebuild, and if it repeats in the same place, stop '
                    f'and check the machine.')

        expect_master = recompute(lvl1_phys, lvl1_size)
        if stored_master != expect_master:
            raise SystemExit(
                f'ROMFS WRITE FAILED: {path}: the master hash in the IVFC header '
                f'does not cover the level1 table on disk (stored '
                f'{stored_master[:32].hex()}, expected {expect_master[:32].hex()})')

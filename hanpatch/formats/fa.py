"""Level-5 style flat archive (.fa) used by Crimson Shroud (ap4/bcs.fa)."""
import os
import struct

ENTRY = 0x50


def read_index(path):
    f = open(path, 'rb')
    cnt, total = struct.unpack('<II', f.read(8))
    f.seek(0x10)
    tbl = f.read(cnt * ENTRY)
    ents = []
    for i in range(cnt):
        e = tbl[i * ENTRY:(i + 1) * ENTRY]
        off, size = struct.unpack('<II', e[:8])
        mid = e[8:0x10]
        name = e[0x10:ENTRY].split(b'\0')[0].decode('latin1')
        ents.append({'off': off, 'size': size, 'mid': mid, 'name': name})
    return f, ents, total


def extract(path, outdir):
    f, ents, total = read_index(path)
    for e in ents:
        p = os.path.join(outdir, e['name'])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        f.seek(e['off'])
        open(p, 'wb').write(f.read(e['size']))
    return ents


def unpack(path, outdir):
    """Alias of extract() matching the adapter vocabulary."""
    return extract(path, outdir)


def read(blob):
    """Parse an in-memory archive into {name: bytes} (used by verification)."""
    cnt, total = struct.unpack('<II', blob[:8])
    out = {}
    for i in range(cnt):
        e = blob[0x10 + i * ENTRY:0x10 + (i + 1) * ENTRY]
        off, size = struct.unpack('<II', e[:8])
        name = e[0x10:ENTRY].split(b'\0')[0].decode('latin1')
        out[name] = blob[off:off + size]
    return out


def build(src_path, srcdir, out_path):
    """Rebuild the archive preserving entry order/names from src_path,
    taking file contents from srcdir."""
    f, ents, total = read_index(src_path)
    n = len(ents)
    data_off = 0x10 + n * ENTRY
    assert data_off % 0x10 == 0
    with open(out_path, 'wb') as o:
        o.seek(data_off)
        pos = data_off
        for e in ents:
            p = os.path.join(srcdir, e['name'])
            b = open(p, 'rb').read()
            pad = (-len(b)) % 0x10
            o.write(b + b'\0' * pad)
            e['new_off'] = pos
            e['new_size'] = len(b) + pad
            pos += len(b) + pad
        total_new = pos
        o.seek(0)
        o.write(struct.pack('<IIII', n, total_new, 0, 0))
        for e in ents:
            o.write(struct.pack('<II', e['new_off'], e['new_size']))
            o.write(e['mid'])
            nb = e['name'].encode('latin1')
            o.write(nb + b'\0' * (0x40 - len(nb)))
    return total_new

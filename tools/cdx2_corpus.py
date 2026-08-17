#!/usr/bin/env python3
"""Extract the title's script corpus into the normalised source document.

    python3 tools/cdx2_corpus.py <extract dir> <work dir>

`extract dir` holds the files unpacked from USRDIR/DATA.DAT. The work directory
receives `text_src.json` in the shape the translation core reads:

    {family: [{"key": ..., "en": <source text>, "jp": ""}, ...]}

Family is the source file a chunk was compiled from, so a translator sees a
scene at a time rather than a flat list. The key is the chunk's id and the
record's offset within it - the id is the table's own identifier and does not
move when chunks are re-laid-out, which the index would.

The corpus is the game's text and does not belong in the repository. This writes
it to the work directory the caller names and prints only counts.
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hanpatch.platforms.psp import dsarc  # noqa: E402
from hanpatch.platforms.psp import dsf  # noqa: E402
from hanpatch.platforms.psp import sdt  # noqa: E402

SCRIPT = 'SCRIPT.SDT'
PAIRS = (('SCRIPT.TBL', 'SCRIPT.DAT'), ('AISCRIPT.TBL', 'AISCRIPT.DAT'))


def archive(path):
    with open(path, 'rb') as fh:
        return dsarc.Dsarc(sdt.Sdt(fh.read()).payload)


def collect(arc):
    """Every text record, grouped by the source file its chunk came from."""
    src = {}
    stats = []
    for tname, dname in PAIRS:
        script = dsf.Script(arc.read(tname), arc.read(dname))
        rebuilt = script.build()
        if rebuilt != (arc.read(tname), arc.read(dname)):
            raise SystemExit('%s does not rebuild to itself; refusing to '
                             'extract from a pair we cannot put back' % dname)
        records = script.records()
        counts = {}
        for r in records:
            chunk = script.chunks[r.chunk]
            family = chunk.name.replace('\\', '__')
            n = counts.get(r.chunk, 0)
            counts[r.chunk] = n + 1
            # ordinal, not byte offset: a rewritten line moves every offset
            # after it, so an offset key stops resolving in the patched file
            src.setdefault(family, []).append({
                'key': '%d:%d' % (chunk.id, n),
                'en': r.text.decode('shift_jis'),
                'jp': '',
            })
        stats.append((dname, len(script), len(records)))
    return src, stats


def main(argv):
    if len(argv) != 3:
        print(__doc__.strip())
        return 2
    extract, work = argv[1], argv[2]
    path = os.path.join(extract, SCRIPT)
    if not os.path.isfile(path):
        print('no %s in %s' % (SCRIPT, extract))
        return 1
    src, stats = collect(archive(path))
    os.makedirs(work, exist_ok=True)
    out = os.path.join(work, 'text_src.json')
    with open(out, 'w') as fh:
        json.dump(src, fh, ensure_ascii=False, indent=1)
    rows = sum(len(v) for v in src.values())
    chars = sum(len(e['en']) for v in src.values() for e in v)
    for dname, chunks, records in stats:
        print('%-14s %5d chunks  %5d records' % (dname, chunks, records))
    print('families %d, rows %d, characters %d' % (len(src), rows, chars))
    with open(os.path.join(work, 'extract.json'), 'w') as fh:
        json.dump({'source': SCRIPT,
                   'pairs': [{'data': d, 'chunks': c, 'records': r}
                             for d, c, r in stats],
                   'families': len(src), 'rows': rows, 'characters': chars},
                  fh, indent=1)
    print('wrote %s' % out)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

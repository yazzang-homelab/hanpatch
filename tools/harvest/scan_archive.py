"""Turn a local patch archive into ledger findings.

    python3 tools/harvest/scan_archive.py ARCHIVE_ROOT ledger.json
    python3 tools/harvest/scan_archive.py ARCHIVE_ROOT ledger.json --hash
    python3 tools/harvest/scan_archive.py ARCHIVE_ROOT ledger.json --platform GBA

The archive is laid out the way a person collects, not the way a program would
design: `PLATFORM/제목/파일`. That is enough. Platform and title come from the
path, the patch format from the extension, and a scraped `_page.html` beside a
patch is recorded as the source page it came from - the thing that will one day
say which dump the patch expects.

This is the discovery half of the loop and it is deliberately dumb. It hashes
and counts; it does not apply, probe, or conclude. A scanner that could
promote its own findings would be a scanner that decides what is true, so
everything it emits enters the ledger at `discovered` and climbs from there
under `ledger.promote`.

`--hash` lifts entries to `fetched`, because for a file already on our own
disk "the bytes exist and hash to something stable" is a claim we can settle
immediately. Nothing beyond that rung is this script's business.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Dict, Iterator, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tools.harvest import ledger  # noqa: E402

# Patches we can act on directly. A container may hold one of these, but
# opening it is a separate step with its own failure modes, so a container is
# recorded as a container and not guessed at.
PATCH_EXT = {'.ips': 'ips', '.xdelta': 'xdelta', '.bps': 'bps',
             '.ups': 'ups', '.vcdiff': 'xdelta', '.patch': 'unknown'}
CONTAINER_EXT = {'.zip', '.7z', '.rar', '.tar', '.gz'}

# Synology's thumbnail droppings and the like. Not evidence of anything.
SKIP_DIRS = {'@eaDir', '#recycle', '.git', '__pycache__', 'node_modules'}


def sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def walk(root: str, platform: Optional[str] = None) -> Iterator[str]:
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(files):
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root)
            if platform and rel.split(os.sep)[0].lower() != platform.lower():
                continue
            yield path


def describe(root: str, path: str) -> Optional[Dict]:
    """One file, as a finding - or None if it is not one.

    A file sitting directly under a platform with no title directory is kept
    with an empty title rather than dropped. Somebody put it there on purpose
    and a scanner that silently discards what it cannot file is a scanner
    whose totals cannot be trusted.
    """
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    ext = os.path.splitext(path)[1].lower()

    kind = PATCH_EXT.get(ext)
    if kind is None and ext in CONTAINER_EXT:
        kind = 'container'
    if kind is None:
        return None

    platform = parts[0]
    title = parts[1] if len(parts) > 2 else ''
    page = None
    for sibling in ('_page.html', 'page.html', 'index.html'):
        candidate = os.path.join(os.path.dirname(path), sibling)
        if os.path.exists(candidate):
            page = candidate
            break

    return {
        'target': '%s/%s' % (platform, title or os.path.basename(path)),
        'url': 'file://' + path,
        'host': 'local-archive',
        'platform': platform,
        'title_hint': title,
        'producer': '',
        'licence': None,
        'source_available': False,
        # Carried through `normalise` only as far as the fields it keeps; the
        # rest is printed in the report so a human can see the shape of the
        # population without the ledger growing a scraper's private state.
        '_kind': kind,
        '_bytes': os.path.getsize(path),
        '_page': page,
        '_path': path,
    }


def scan(root: str, platform: Optional[str] = None) -> List[Dict]:
    out = []
    for path in walk(root, platform):
        found = describe(root, path)
        if found:
            out.append(found)
    return out


def report(findings: List[Dict]) -> Dict:
    by_kind: Dict[str, int] = {}
    by_platform: Dict[str, Dict[str, int]] = {}
    pages = 0
    for f in findings:
        by_kind[f['_kind']] = by_kind.get(f['_kind'], 0) + 1
        bucket = by_platform.setdefault(f['platform'], {'patches': 0,
                                                        'containers': 0,
                                                        'bytes': 0})
        bucket['containers' if f['_kind'] == 'container' else 'patches'] += 1
        bucket['bytes'] += f['_bytes']
        if f['_page']:
            pages += 1
    direct = sum(v for k, v in by_kind.items() if k != 'container')
    return {
        'files': len(findings),
        'directly_applicable': direct,
        'containers': by_kind.get('container', 0),
        'with_source_page': pages,
        'by_kind': dict(sorted(by_kind.items())),
        'by_platform': dict(sorted(by_platform.items(),
                                   key=lambda kv: -kv[1]['patches'])),
    }


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('root')
    ap.add_argument('out')
    ap.add_argument('--platform', default=None)
    ap.add_argument('--hash', action='store_true',
                    help='hash direct patches and lift them to fetched')
    ap.add_argument('--now', default=None)
    args = ap.parse_args(argv)

    now = args.now or __import__('datetime').date.today().isoformat()

    findings = scan(args.root, args.platform)
    stats = report(findings)

    known = {}
    if os.path.exists(args.out):
        with open(args.out) as f:
            known = json.load(f)

    led, receipt = ledger.merge(known, findings, now=now)

    hashed = 0
    if args.hash:
        for f in findings:
            if f['_kind'] == 'container':
                continue
            key = ledger.entry_id(f['target'], f['url'])
            if led['entries'][key]['status'] != 'discovered':
                continue
            ledger.promote(led, key, now=now, evidence={
                'patch_sha256': sha256(f['_path']),
                'bytes': f['_bytes'],
                'format': f['_kind'],
            })
            hashed += 1
        receipt['by_status'] = ledger.counts(led)

    receipt['archive'] = stats
    receipt['hashed'] = hashed

    with open(args.out, 'w') as f:
        f.write(ledger.dumps(led))

    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

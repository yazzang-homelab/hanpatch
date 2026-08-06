"""Measure a set of GitHub repositories and write the result down.

The project already had these numbers once - how many of the harvested
repositories carry real source, which licences allow vendoring, how many
distinct people are actually behind them - and they were lost because they
lived in a conversation instead of in a file. A number nobody can recompute is
an opinion. This module recomputes them.

The output is deterministic: keys sorted, no timestamps, no wall clock, no
ordering that depends on how fast the network answered. Two runs against an
unchanged upstream produce byte-identical files, which is the bar the project
set for itself before anything is allowed to call itself a pipeline.

Nothing here touches retrodb. The first-tier source is the GitHub API and only
the GitHub API, so this runs today without waiting on anyone's permission.

    python3 tools/harvest/inventory.py seeds.json resources/inventory.json
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Dict, Iterable, List, Optional, Sequence

API = 'https://api.github.com'

# Source, not release notes. A repository whose only content is a .zip and a
# README has published a patch, not a method, and the project has no use for it.
SOURCE_SUFFIXES = (
    '.py', '.c', '.cc', '.cpp', '.h', '.hpp', '.cs', '.java', '.js', '.ts',
    '.lua', '.rb', '.go', '.rs', '.pl', '.sh', '.bat', '.ps1', '.asm', '.s',
    '.tbl', '.json', '.xml', '.yml', '.yaml',
)
# Anything the project could legally port or vendor.
VENDOR_OK = ('MIT', 'Apache-2.0', 'BSD-2-Clause', 'BSD-3-Clause', 'MPL-2.0',
             'Unlicense', 'CC0-1.0', 'ISC', 'Zlib')
# Readable and citable, but its expression cannot enter the project.
REFERENCE_ONLY = ('GPL-2.0', 'GPL-3.0', 'LGPL-2.1', 'LGPL-3.0', 'AGPL-3.0')

MIN_SOURCE_FILES = 3


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def get(self, path: str) -> Optional[dict]:
        url = path if path.startswith('http') else API + path
        req = urllib.request.Request(url)
        req.add_header('Authorization', 'Bearer %s' % self.token)
        req.add_header('Accept', 'application/vnd.github+json')
        req.add_header('X-GitHub-Api-Version', '2022-11-28')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 409, 451):
                return None          # gone, empty, blocked, or rate-limited
            raise


def licence_tier(spdx: Optional[str]) -> str:
    """Three outcomes, and the unknown one is not 'probably fine'."""
    if spdx in VENDOR_OK:
        return 'vendor_ok'
    if spdx in REFERENCE_ONLY:
        return 'reference_ok'
    return 'quarantine'


def classify_tree(paths: Sequence[str]) -> Dict[str, object]:
    source = [p for p in paths if p.lower().endswith(SOURCE_SUFFIXES)]
    return {
        'files': len(paths),
        'source_files': len(source),
        'has_source': len(source) >= MIN_SOURCE_FILES,
        'release_only': len(paths) <= 2,
    }


def measure(api: GitHub, full_name: str) -> Dict[str, object]:
    """One repository, reduced to facts that survive a rerun."""
    meta = api.get('/repos/%s' % full_name)
    if meta is None:
        return {'id': full_name, 'reachable': False}

    branch = meta.get('default_branch') or 'main'
    tree = api.get('/repos/%s/git/trees/%s?recursive=1' % (full_name, branch)) or {}
    paths = [node['path'] for node in tree.get('tree', []) if node.get('type') == 'blob']
    spdx = ((meta.get('license') or {}).get('spdx_id') or None)
    if spdx in ('NOASSERTION', 'NONE'):
        spdx = None

    record = {
        'id': meta.get('full_name', full_name),
        'reachable': True,
        'owner': (meta.get('owner') or {}).get('login', ''),
        'fork': bool(meta.get('fork')),
        'archived': bool(meta.get('archived')),
        'default_branch': branch,
        'license': spdx,
        'license_tier': licence_tier(spdx),
        'truncated_tree': bool(tree.get('truncated')),
    }
    record.update(classify_tree(paths))
    return record


def clusters(records: Iterable[dict]) -> List[dict]:
    """One cluster, one vote.

    Thirteen repositories by one person are one opinion repeated thirteen
    times. Counting them as thirteen is how a project convinces itself that a
    convention is corroborated when it is merely repeated.
    """
    by_owner: Dict[str, List[str]] = {}
    for record in records:
        if not record.get('reachable'):
            continue
        by_owner.setdefault(record.get('owner', ''), []).append(record['id'])
    return [{'owner': owner, 'members': sorted(members), 'weight': 1}
            for owner, members in sorted(by_owner.items())]


def summarise(records: Sequence[dict], groups: Sequence[dict]) -> Dict[str, object]:
    live = [r for r in records if r.get('reachable')]
    with_source = [r for r in live if r.get('has_source')]
    usable = [r for r in with_source if r['license_tier'] == 'vendor_ok']
    tiers: Dict[str, int] = {}
    for record in live:
        tiers[record['license_tier']] = tiers.get(record['license_tier'], 0) + 1
    return {
        'seeds': len(records),
        'reachable': len(live),
        'unreachable': len(records) - len(live),
        'with_source': len(with_source),
        'release_only': sum(1 for r in live if r.get('release_only')),
        'license_tiers': dict(sorted(tiers.items())),
        'vendorable_with_source': len(usable),
        'clusters': len(groups),
        'largest_cluster': max((len(g['members']) for g in groups), default=0),
    }


def normalise_seed(seed: str) -> Optional[str]:
    """`owner/name`, or a GitHub Pages host that stands for one."""
    seed = seed.strip().strip('/')
    if not seed:
        return None
    if '.github.io/' in seed:
        host, _, name = seed.partition('/')
        return '%s/%s' % (host.split('.github.io')[0], name)
    parts = seed.split('/')
    return '%s/%s' % (parts[0], parts[1]) if len(parts) >= 2 else None


def load_seeds(path: str) -> List[str]:
    with open(path, encoding='utf-8') as fh:
        raw = json.load(fh)
    if isinstance(raw, dict):
        pool = list(raw.get('repos', [])) + list(raw.get('pages', []))
    else:
        pool = list(raw)
    seen: Dict[str, None] = {}
    for seed in pool:
        name = normalise_seed(seed)
        if name:
            seen.setdefault(name, None)
    return sorted(seen)


def build(api: GitHub, seeds: Sequence[str]) -> Dict[str, object]:
    records = sorted((measure(api, name) for name in seeds), key=lambda r: r['id'])
    groups = clusters(records)
    return {
        'schema': 'hanpatch.inventory.v1',
        'summary': summarise(records, groups),
        'clusters': groups,
        'resources': records,
    }


def main(argv: Sequence[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    token = os.environ.get('GITHUB_TOKEN', '')
    if not token:
        print('GITHUB_TOKEN is required')
        return 2

    seeds = load_seeds(argv[1])
    print('measuring %d repositories' % len(seeds))
    report = build(GitHub(token), seeds)

    os.makedirs(os.path.dirname(os.path.abspath(argv[2])), exist_ok=True)
    with open(argv[2], 'w', encoding='utf-8') as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write('\n')
    for key, value in report['summary'].items():
        print('  %-24s %s' % (key, value))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))

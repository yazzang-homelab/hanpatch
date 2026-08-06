"""Adversarial regression tests for the harvest inventory.

Run:  python3 tests/test_inventory.py

The inventory exists because a number nobody can recompute is an opinion.
These cases are the ways it could quietly go back to being one.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.harvest.inventory import (  # noqa: E402
    MIN_SOURCE_FILES, build, classify_tree, clusters, licence_tier,
    load_seeds, normalise_seed, summarise,
)

PASS = []
FAIL = []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def sec(title):
    print()
    print(title)


sec('a licence decides what may be carried, and unknown is not yes')

case('MIT may be vendored', licence_tier('MIT') == 'vendor_ok')
case('Apache-2.0 may be vendored', licence_tier('Apache-2.0') == 'vendor_ok')
case('GPL may be cited but not carried', licence_tier('GPL-3.0') == 'reference_ok')
case('LGPL may be cited but not carried', licence_tier('LGPL-2.1') == 'reference_ok')
case('no licence is quarantined, not assumed permissive',
     licence_tier(None) == 'quarantine')
case('an unrecognised licence is quarantined',
     licence_tier('WTFPL') == 'quarantine')


sec('source, not release notes')

case('three source files is source',
     classify_tree(['a.py', 'b.py', 'c.py'])['has_source'] is True)
case('two is not', classify_tree(['a.py', 'b.py'])['has_source'] is False)
case('the threshold is the declared one', MIN_SOURCE_FILES == 3)
case('a zip and a readme is a release, not a method',
     classify_tree(['patch.zip', 'README.md'])['release_only'] is True)
case('binaries do not count as source',
     classify_tree(['a.zip', 'b.bin', 'c.7z', 'd.exe'])['source_files'] == 0)
case('a table file counts, because that is the method',
     classify_tree(['x.tbl', 'y.tbl', 'z.tbl'])['has_source'] is True)
case('an empty repository is not source',
     classify_tree([])['has_source'] is False)


sec('one cluster, one vote')

RECORDS = [
    {'id': 'snake/a', 'owner': 'snake', 'reachable': True, 'has_source': True,
     'license_tier': 'vendor_ok', 'release_only': False},
    {'id': 'snake/b', 'owner': 'snake', 'reachable': True, 'has_source': True,
     'license_tier': 'vendor_ok', 'release_only': False},
    {'id': 'snake/c', 'owner': 'snake', 'reachable': True, 'has_source': True,
     'license_tier': 'vendor_ok', 'release_only': False},
    {'id': 'other/d', 'owner': 'other', 'reachable': True, 'has_source': False,
     'license_tier': 'quarantine', 'release_only': True},
    {'id': 'gone/e', 'owner': '', 'reachable': False},
]
GROUPS = clusters(RECORDS)

case('three repositories by one person are one cluster', len(GROUPS) == 2)
case('and that cluster still gets exactly one vote',
     all(g['weight'] == 1 for g in GROUPS))
case('a cluster names its members so the merge can be reviewed',
     sorted(g['members'] for g in GROUPS)[1] == ['snake/a', 'snake/b', 'snake/c'])
case('an unreachable repository joins no cluster',
     all('gone/e' not in g['members'] for g in GROUPS))
case('clusters come out in a stable order',
     [g['owner'] for g in GROUPS] == sorted(g['owner'] for g in GROUPS))

SUMMARY = summarise(RECORDS, GROUPS)
case('the summary counts what is reachable, not what was asked for',
     SUMMARY['reachable'] == 4 and SUMMARY['seeds'] == 5)
case('it does not lose the unreachable ones', SUMMARY['unreachable'] == 1)
case('vendorable means source AND licence, not either',
     SUMMARY['vendorable_with_source'] == 3)
case('licence tiers are counted, not averaged',
     SUMMARY['license_tiers'] == {'quarantine': 1, 'vendor_ok': 3})


sec('seeds')

case('a plain repository name passes through',
     normalise_seed('navamog/ZoE-GBA') == 'navamog/ZoE-GBA')
case('a pages host stands for the repository behind it',
     normalise_seed('jiseo79.github.io/ys4pce-kr') == 'jiseo79/ys4pce-kr')
case('a bare owner is not a repository', normalise_seed('navamog') is None)
case('an empty line is not a repository', normalise_seed('  ') is None)
case('a deep url keeps only owner and repository',
     normalise_seed('owner/repo/tree/main/src') == 'owner/repo')


class FakeGitHub:
    """Answers the two routes the inventory uses, in a deliberately awkward order."""

    def __init__(self):
        self.seen = []

    def get(self, path):
        self.seen.append(path)
        if path == '/repos/a/one':
            return {'full_name': 'a/one', 'default_branch': 'trunk', 'fork': False,
                    'archived': False, 'owner': {'login': 'a'},
                    'license': {'spdx_id': 'MIT'}}
        if path == '/repos/a/two':
            return {'full_name': 'a/two', 'default_branch': 'main', 'fork': True,
                    'archived': False, 'owner': {'login': 'a'},
                    'license': {'spdx_id': 'NOASSERTION'}}
        if path.startswith('/repos/a/one/git/trees/trunk'):
            return {'tree': [{'path': 'src/x.py', 'type': 'blob'},
                             {'path': 'src/y.py', 'type': 'blob'},
                             {'path': 'src/z.py', 'type': 'blob'},
                             {'path': 'src', 'type': 'tree'}]}
        if path.startswith('/repos/a/two/git/trees/main'):
            return {'tree': [{'path': 'patch.zip', 'type': 'blob'}]}
        return None


PROBE = FakeGitHub()
REPORT = build(PROBE, ['a/one', 'a/two', 'a/missing'])

case('a repository that is gone is recorded, not dropped',
     len(REPORT['resources']) == 3
     and any(r['id'] == 'a/missing' and not r['reachable'] for r in REPORT['resources']))
case('the default branch is read, not assumed to be main',
     any(p.startswith('/repos/a/one/git/trees/trunk') for p in PROBE.seen)
     and not any('/repos/a/one/git/trees/main' in p for p in PROBE.seen))
case('NOASSERTION is not a licence',
     [r for r in REPORT['resources'] if r['id'] == 'a/two'][0]['license'] is None)
case('and it lands in quarantine',
     [r for r in REPORT['resources'] if r['id'] == 'a/two'][0]['license_tier']
     == 'quarantine')
case('a directory entry is not a file',
     [r for r in REPORT['resources'] if r['id'] == 'a/one'][0]['files'] == 3)
case('records come out sorted by id, whatever order the network answered in',
     [r['id'] for r in REPORT['resources']] == sorted(r['id'] for r in REPORT['resources']))
case('the report declares its schema', REPORT['schema'] == 'hanpatch.inventory.v1')
case('two runs of the same facts serialise identically',
     json.dumps(REPORT, sort_keys=True)
     == json.dumps(build(FakeGitHub(), ['a/missing', 'a/two', 'a/one']), sort_keys=True))


sec('the inventory that ships')

SHIPPED = os.path.join(ROOT, 'resources', 'inventory.json')
if os.path.exists(SHIPPED):
    with open(SHIPPED, encoding='utf-8') as fh:
        live = json.load(fh)
    case('it declares the schema it was built with',
         live.get('schema') == 'hanpatch.inventory.v1')
    case('it carries a resource for every seed',
         len(live['resources']) == live['summary']['seeds'])
    case('it records the unreachable rather than hiding them',
         live['summary']['unreachable']
         == sum(1 for r in live['resources'] if not r.get('reachable')))
    case('no repository counts as vendorable without source',
         live['summary']['vendorable_with_source']
         == sum(1 for r in live['resources']
                if r.get('has_source') and r.get('license_tier') == 'vendor_ok'))
    case('the largest cluster is a real cluster',
         live['summary']['largest_cluster']
         == max(len(g['members']) for g in live['clusters']))
    case('there are fewer clusters than repositories, which is the whole point',
         live['summary']['clusters'] < live['summary']['reachable'])
else:
    print('  skip  resources/inventory.json is not built')

print()
print(f'{len(PASS)} passed, {len(FAIL)} failed')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
sys.exit(1 if FAIL else 0)

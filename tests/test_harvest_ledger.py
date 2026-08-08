"""A ledger that can only be climbed one rung at a time.

Run:  python3 tests/test_harvest_ledger.py

Every case here is an attempt to get a finding treated as knowledge without
doing the work: promoting two rungs at once, confirming a hypothesis with the
tool that produced it, resurrecting a withdrawn source by finding it again,
letting one prolific translator outvote a platform. The ledger is only worth
having if it says no to all of them.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.harvest import ledger  # noqa: E402

PASS = []
FAIL = []


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def sec(title):
    print()
    print(title)


def raises(fn):
    try:
        fn()
    except Exception:
        return True
    return False


FIND_A = {'target': 'threeds/square-enix/dragon-quest-vii',
          'url': 'https://cafe.example.invalid/thread/1',
          'host': 'cafe.example.invalid', 'producer': 'snake7594',
          'platform': 'threeds'}
FIND_B = {'target': 'nds/nintendo/fire-emblem',
          'url': 'https://blog.example.invalid/post/9',
          'host': 'blog.example.invalid', 'producer': 'navamog',
          'platform': 'nds'}


def climb(led, key, *, to, now='2026-08-07'):
    steps = {
        'fetched': {'patch_sha256': 'a' * 64, 'bytes': 4096},
        'applied': {'rom_sha256': 'b' * 64, 'patched_sha256': 'c' * 64},
        'probed': {'hypothesis_sha256': 'd' * 64, 'regions': 3,
                   'unexplained_bytes': 128},
        'confirmed': {'measured_by': 'fingerprint', 'recipe_id': 'r1',
                      'agreed_fields': {'width': 4, 'endian': 'little'}},
    }
    while True:
        nxt = ledger.TRANSITIONS.get(led['entries'][key]['status'])
        if nxt is None:
            return
        ledger.promote(led, key, evidence=steps[nxt], now=now)
        if nxt == to:
            return


sec('a sweep proposes; it does not overwrite')
led, r1 = ledger.merge({}, [FIND_A, FIND_B], now='2026-08-01')
case('both findings land', r1['added'] == 2)
case('everything starts on the bottom rung',
     r1['by_status']['discovered'] == 2)
case('nothing arrives already verified',
     all(e['status'] == 'discovered' for e in led['entries'].values()))

sec('running it again is not an event')
led2, r2 = ledger.merge(led, [FIND_A, FIND_B], now='2026-08-02')
case('a repeat sweep adds nothing', r2['added'] == 0)
case('a repeat sweep is still counted as a sweep', r2['swept'] == 2)
case('the second sight is recorded',
     all(e['last_seen'] == '2026-08-02' for e in led2['entries'].values()))
case('the receipt of a quiet sweep is all zeroes',
     (r2['added'], r2['returned_from_withdrawn']) == (0, 0))

sec('the same patch found twice is one entry')
alias = dict(FIND_A)
alias['host'] = 'mirror.example.invalid'
alias['url'] = 'https://mirror.example.invalid/thread/1'
led3, r3 = ledger.merge(led2, [alias], now='2026-08-03')
case('a different URL for the same title is a different entry',
     r3['added'] == 1)
same = dict(FIND_A)
led4, r4 = ledger.merge(led3, [same], now='2026-08-04')
case('the same URL for the same title is not', r4['added'] == 0)

sec('the ladder cannot be skipped')
key = ledger.entry_id(FIND_A['target'], FIND_A['url'])
case('confirming a freshly discovered entry is refused',
     raises(lambda: ledger.promote(
         led4, key, evidence={'measured_by': 'fingerprint', 'recipe_id': 'r1',
                              'agreed_fields': {}}, now='2026-08-05')))
case('a rung without its receipts is refused',
     raises(lambda: ledger.promote(led4, key, evidence={}, now='2026-08-05')))
ledger.promote(led4, key, evidence={'patch_sha256': 'a' * 64, 'bytes': 10},
               now='2026-08-05')
case('a rung with its receipts is granted',
     led4['entries'][key]['status'] == 'fetched')
case('the receipt is kept, not just the rung',
     led4['entries'][key]['evidence']['fetched']['bytes'] == 10)
case('the climb is recorded in order',
     [h['to'] for h in led4['entries'][key]['history']]
     == ['discovered', 'fetched'])

sec('the probe may not confirm its own hypothesis')
climb(led4, key, to='probed')
case('reaching probed is fine', led4['entries'][key]['status'] == 'probed')
case('patchprobe confirming itself is refused',
     raises(lambda: ledger.promote(
         led4, key,
         evidence={'measured_by': 'patchprobe', 'recipe_id': 'r1',
                   'agreed_fields': {'width': 4}}, now='2026-08-06')))
ledger.promote(led4, key,
               evidence={'measured_by': 'fingerprint', 'recipe_id': 'r1',
                         'agreed_fields': {'width': 4, 'endian': 'little'}},
               now='2026-08-06')
case('an independent measurement confirms it',
     led4['entries'][key]['status'] == 'confirmed')
case('a confirmed entry cannot climb further',
     raises(lambda: ledger.promote(led4, key, evidence={}, now='2026-08-07')))

sec('a source that vanishes leaves a mark')
other = ledger.entry_id(FIND_B['target'], FIND_B['url'])
ledger.withdraw(led4, other, reason='host offline', now='2026-08-06')
case('it is tombstoned, not deleted', other in led4['entries'])
case('a withdrawn entry cannot be promoted',
     raises(lambda: ledger.promote(led4, other,
                                   evidence={'patch_sha256': 'a' * 64,
                                             'bytes': 1}, now='2026-08-07')))
led5, r5 = ledger.merge(led4, [FIND_B], now='2026-08-07')
case('finding it again does not resurrect it',
     led5['entries'][other]['status'] == ledger.TOMBSTONE)
case('but the receipt says a human should look',
     r5['returned_from_withdrawn'] == 1)

sec('one producer is one vote')
prolific = ledger.merge({}, [
    {'target': 'gba/x/title-%d' % i, 'url': 'https://h.invalid/%d' % i,
     'host': 'h.invalid', 'producer': 'snake7594', 'platform': 'gba'}
    for i in range(13)], now='2026-08-01')[0]
for entry_key in list(prolific['entries']):
    climb(prolific, entry_key, to='confirmed')
p = ledger.priors(prolific)
case('thirteen titles by one person are one group', len(p) == 1)
case('and one vote', p[0]['votes'] == 1)
case('the title count is still visible', p[0]['titles'] == 13)
case('a consistent producer yields a convention',
     p[0]['status'] == 'producer-convention'
     and p[0]['fields']['width'] == '4')

sec('a producer who did it two ways has no convention')
mixed = ledger.merge({}, [
    {'target': 'gba/y/a', 'url': 'https://h.invalid/a', 'host': 'h.invalid',
     'producer': 'mixed', 'platform': 'gba'},
    {'target': 'gba/y/b', 'url': 'https://h.invalid/b', 'host': 'h.invalid',
     'producer': 'mixed', 'platform': 'gba'}], now='2026-08-01')[0]
keys = sorted(mixed['entries'])
for i, entry_key in enumerate(keys):
    for rung in ledger.RUNGS[1:4]:
        ledger.promote(mixed, entry_key, evidence={
            'fetched': {'patch_sha256': 'a' * 64, 'bytes': 1},
            'applied': {'rom_sha256': 'b' * 64, 'patched_sha256': 'c' * 64},
            'probed': {'hypothesis_sha256': 'd' * 64, 'regions': 1,
                       'unexplained_bytes': 0},
        }[rung], now='2026-08-02')
    ledger.promote(mixed, entry_key, evidence={
        'measured_by': 'fingerprint', 'recipe_id': 'r',
        'agreed_fields': {'endian': 'little' if i == 0 else 'big'}},
        now='2026-08-03')
case('disagreement is reported as contested, not averaged',
     ledger.priors(mixed)[0]['status'] == 'contested')
case('and no field is settled', ledger.priors(mixed)[0]['fields'] == {})

sec('licences are recorded, never used as a filter')
nolicence = ledger.merge({}, [dict(FIND_A, licence=None,
                                   source_available=False)],
                         now='2026-08-01')[0]
only = list(nolicence['entries'].values())[0]
case('a patch with no licence and no source is still admitted',
     only['status'] == 'discovered')
case('the absence is written down', only['licence'] is None
     and only['source_available'] is False)

sec('growth alone is not progress')
case('no sweeps means no claim', ledger.evolution([])['compounding'] is False)
flat = [{'by_status': {'confirmed': 0}, 'added': 400},
        {'by_status': {'confirmed': 0}, 'added': 400}]
case('a thousand new links and nothing confirmed is not compounding',
     ledger.evolution(flat)['compounding'] is False)
unmeasured = [{'by_status': {'confirmed': 0}, 'added': 10},
              {'by_status': {'confirmed': 9}, 'added': 10}]
case('confirmations without a measured search gain make no speed claim',
     ledger.evolution(unmeasured)['compounding'] is False)
case('and say so out loud',
     'not made' in ledger.evolution(unmeasured)['note'])
real = [{'by_status': {'confirmed': 0}, 'added': 10},
        {'by_status': {'confirmed': 9}, 'added': 10,
         'candidates_before': 100, 'candidates_after': 55}]
case('confirmations plus a measured 45% cut is compounding',
     ledger.evolution(real)['compounding'] is True)
case('the reduction is reported as a number',
     ledger.evolution(real)['candidate_reduction'] == 0.45)
weak = [{'by_status': {'confirmed': 0}, 'added': 10},
        {'by_status': {'confirmed': 9}, 'added': 10,
         'candidates_before': 100, 'candidates_after': 80}]
case('a 20% cut does not clear the bar the plan set',
     ledger.evolution(weak)['compounding'] is False)

sec('a sweep that only re-finds the same links is visible')
case('entries stuck on the bottom rung are listable',
     ledger.stale(led5, before='2026-08-09') != [])
case('a climbed entry is not stale',
     key not in ledger.stale(led5, before='2026-08-09'))

sec('the ledger is a document, not a process')
case('it round-trips through JSON',
     ledger.dumps(led5) == ledger.dumps(
         __import__('json').loads(ledger.dumps(led5))))
case('it holds no bytes from anybody',
     'b64' not in ledger.dumps(led5).lower())


print()
print('%d passed, %d failed' % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)

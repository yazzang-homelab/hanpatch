"""The patch inventory as a ledger that grows, not a number that was true once.

`1,149 patches` is a measurement from one afternoon. Treated as a total it
starts rotting the moment it is written: cafes post, blogs die, a translator
finishes a title nobody had covered. Treated as the opening balance of a
ledger it is useful, because the interesting quantity was never the count -
it is the derivative. How many new patches per sweep, how many survive
verification, how many of those teach us something we did not already know.

So a sweep does not overwrite. It proposes.

    known = load(path)
    found = crawl(...)                      # somebody else's problem
    merged, receipt = merge(known, found, now=...)

Everything discovered enters at `discovered` and climbs one rung at a time.
Nothing is promoted by the thing that found it, because a crawler that could
promote its own findings is a crawler that decides what is true.

    discovered -> fetched    the bytes exist and hash to something stable
    fetched    -> applied    it applies to a ROM we hold, cleanly
    applied    -> probed     patchprobe produced a hypothesis from the diff
    probed     -> confirmed  our own measurement reproduced that structure

`confirmed` is the only rung that means knowledge, and the only one this
module refuses to grant on the strength of a source's own claim - it requires
a measurement receipt from code that never read the source.

The recursion is the point. A confirmed structure is a prior for the next
title by the same producer, which makes the next probe cheaper, which makes
more patches worth probing. That loop is a claim about speed, so it is only
allowed to count once `evolution()` can show the candidate count actually
falling. A loop that cannot show its own gain is a loop that is spending
compute to feel productive.

Contains no patch bytes and no ROM bytes. Hashes, offsets, hosts and dates.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = {'major': 1, 'minor': 0}

RUNGS = ('discovered', 'fetched', 'applied', 'probed', 'confirmed')

# One step at a time, forwards only. Skipping a rung is how an unverified
# entry ends up quoted as a fact three months later with nobody able to say
# which step was the one nobody did.
TRANSITIONS = {a: b for a, b in zip(RUNGS, RUNGS[1:])}

# Withdrawn, not deleted. A source that disappears may have to be answered for
# later, and an entry that was silently dropped cannot be answered for at all.
TOMBSTONE = 'withdrawn'

# What each promotion has to bring with it. The rung is the claim; these are
# the receipts that make the claim checkable by somebody who was not there.
REQUIRED_EVIDENCE = {
    'fetched': ('patch_sha256', 'bytes'),
    'applied': ('rom_sha256', 'patched_sha256'),
    'probed': ('hypothesis_sha256', 'regions', 'unexplained_bytes'),
    'confirmed': ('measured_by', 'recipe_id', 'agreed_fields'),
}


def entry_id(target: str, url: str) -> str:
    """Stable across sweeps, so the same patch found twice is one entry.

    Keyed on the target title as well as the URL, because the same archive
    host will happily serve the same filename for two different games, and a
    collision there would merge two titles' structures into one lie.
    """
    digest = hashlib.sha256(('%s\n%s' % (target, url)).encode()).hexdigest()
    return '%s:%s' % (target, digest[:16])


def normalise(record: Dict, *, now: str) -> Dict:
    """A crawler's finding, reduced to the fields a ledger can carry.

    Unknown keys are dropped rather than stored. A ledger that accepts
    arbitrary fields from whatever scraped them becomes a place where a
    scraper's private state is quietly load-bearing.
    """
    required = ('target', 'url', 'host')
    missing = [k for k in required if not record.get(k)]
    if missing:
        raise ValueError('finding is missing %s' % ', '.join(missing))

    return {
        'id': entry_id(record['target'], record['url']),
        'target': record['target'],
        'url': record['url'],
        'host': record['host'],
        'title_hint': record.get('title_hint', ''),
        'producer': record.get('producer', ''),
        'platform': record.get('platform', ''),
        # Recorded, never acted on. Absence of a licence is the normal case
        # for this population and is not a reason to skip an entry, because
        # we are not redistributing anything - we are measuring our own ROM.
        'licence': record.get('licence'),
        'source_available': bool(record.get('source_available')),
        'status': 'discovered',
        'first_seen': now,
        'last_seen': now,
        'history': [{'at': now, 'to': 'discovered', 'evidence': {}}],
        'evidence': {},
    }


def merge(ledger: Dict, findings: Iterable[Dict], *, now: str) -> Tuple[Dict, Dict]:
    """Fold a sweep into what we already had, and say exactly what changed.

    Idempotent: the same sweep applied twice moves `last_seen` and nothing
    else. That property is what lets a scheduled job run without a human
    reading its output every time - a receipt of all zeroes is a job that
    correctly did nothing.

    An entry already at a higher rung is never demoted by rediscovery. Finding
    a patch again does not unlearn the measurement we made from it.
    """
    out = copy.deepcopy(ledger) if ledger else {
        'schema_version': dict(SCHEMA_VERSION), 'entries': {}}
    entries = out.setdefault('entries', {})

    added, seen_again, conflicting = [], [], []
    for raw in findings:
        record = normalise(raw, now=now)
        known = entries.get(record['id'])
        if known is None:
            entries[record['id']] = record
            added.append(record['id'])
            continue

        seen_again.append(record['id'])
        known['last_seen'] = now
        if known['status'] == TOMBSTONE:
            # It came back. That is a fact about the source, not permission to
            # resurrect the entry, so the rung stays where it was and a human
            # reads the receipt.
            conflicting.append(record['id'])
            continue
        for field in ('producer', 'platform', 'title_hint'):
            if not known.get(field) and record.get(field):
                known[field] = record[field]

    receipt = {
        'at': now,
        'swept': len(added) + len(seen_again),
        'added': len(added),
        'seen_again': len(seen_again),
        'returned_from_withdrawn': len(conflicting),
        'total': len(entries),
        'by_status': counts(out),
        'new_ids': sorted(added),
    }
    out.setdefault('receipts', []).append(receipt)
    return out, receipt


def counts(ledger: Dict) -> Dict[str, int]:
    tally = {rung: 0 for rung in RUNGS}
    tally[TOMBSTONE] = 0
    for entry in ledger.get('entries', {}).values():
        tally[entry['status']] = tally.get(entry['status'], 0) + 1
    return tally


def promote(ledger: Dict, entry_key: str, *, evidence: Dict, now: str) -> Dict:
    """One rung up, with receipts, or an exception.

    There is deliberately no `to` argument. The next rung is a function of the
    current one, so a caller cannot promote an entry to `confirmed` by asking
    nicely; it has to walk every step and produce the evidence each step
    demands.
    """
    entry = ledger.get('entries', {}).get(entry_key)
    if entry is None:
        raise KeyError(entry_key)
    if entry['status'] == TOMBSTONE:
        raise ValueError('%s is withdrawn; reinstate it deliberately' % entry_key)

    nxt = TRANSITIONS.get(entry['status'])
    if nxt is None:
        raise ValueError('%s is already %s' % (entry_key, entry['status']))

    missing = [k for k in REQUIRED_EVIDENCE[nxt] if k not in evidence]
    if missing:
        raise ValueError('%s -> %s needs %s' % (entry['status'], nxt,
                                                ', '.join(missing)))
    if nxt == 'confirmed' and evidence.get('measured_by') == 'patchprobe':
        # The probe reads their patch. Letting it confirm its own hypothesis
        # would make the whole ladder a formality with one rung.
        raise ValueError('confirmation must come from a measurement that did '
                         'not read the patch')

    entry['status'] = nxt
    entry['evidence'] = dict(entry.get('evidence', {}))
    entry['evidence'][nxt] = dict(evidence)
    entry['history'].append({'at': now, 'to': nxt,
                             'evidence': sorted(evidence)})
    return entry


def withdraw(ledger: Dict, entry_key: str, *, reason: str, now: str) -> Dict:
    entry = ledger['entries'][entry_key]
    entry['status'] = TOMBSTONE
    entry['history'].append({'at': now, 'to': TOMBSTONE, 'reason': reason})
    return entry


def stale(ledger: Dict, *, before: str, rung: str = 'discovered') -> List[str]:
    """Entries that have sat on a rung since before a date.

    A sweep that keeps finding the same thousand links and never lifts one off
    `discovered` is a sweep that is only proving the crawler still runs.
    """
    return sorted(k for k, e in ledger.get('entries', {}).items()
                  if e['status'] == rung and e['last_seen'] < before)


def priors(ledger: Dict) -> List[Dict]:
    """What confirmed entries say about the next unseen title.

    Grouped by producer and platform, because that is the axis on which a
    convention actually holds - one person reuses their own toolchain across
    their own releases, and that regularity is the thing worth betting search
    order on.

    One producer is one vote no matter how many titles they shipped. Thirteen
    releases by one person are one opinion repeated thirteen times, and
    counting them thirteen times is how a single toolchain's quirk gets
    promoted to a law of the platform.
    """
    groups: Dict[Tuple[str, str], Dict] = {}
    for entry in ledger.get('entries', {}).values():
        if entry['status'] != 'confirmed':
            continue
        key = (entry.get('platform', ''), entry.get('producer', ''))
        agreed = entry['evidence']['confirmed'].get('agreed_fields', {})
        bucket = groups.setdefault(key, {'platform': key[0], 'producer': key[1],
                                         'titles': 0, 'fields': {}})
        bucket['titles'] += 1
        for field, value in sorted(agreed.items()):
            tally = bucket['fields'].setdefault(field, {})
            tally[str(value)] = tally.get(str(value), 0) + 1

    out = []
    for bucket in groups.values():
        settled = {f: max(t, key=lambda v: (t[v], v))
                   for f, t in bucket['fields'].items()
                   # A producer who did it two different ways has no
                   # convention, only a history.
                   if len(t) == 1}
        out.append({'platform': bucket['platform'],
                    'producer': bucket['producer'],
                    'titles': bucket['titles'],
                    'votes': 1,
                    'fields': settled,
                    'status': 'producer-convention' if settled else 'contested'})
    out.sort(key=lambda p: (p['platform'], p['producer']))
    return out


def evolution(receipts: Sequence[Dict]) -> Dict:
    """Is the loop actually compounding, or just running.

    Reports the two numbers that decide it: how many entries reached
    `confirmed` per sweep, and whether the search got cheaper. If a sweep adds
    findings but confirms nothing, the derivative is zero and the honest
    reading is that we built a link collector.
    """
    if not receipts:
        return {'sweeps': 0, 'compounding': False,
                'note': 'no sweeps recorded'}

    first, last = receipts[0], receipts[-1]
    confirmed_first = first.get('by_status', {}).get('confirmed', 0)
    confirmed_last = last.get('by_status', {}).get('confirmed', 0)
    added = sum(r.get('added', 0) for r in receipts)

    before = last.get('candidates_before')
    after = last.get('candidates_after')
    reduction = None
    if before:
        reduction = round(1 - (after or 0) / before, 4)

    return {
        'sweeps': len(receipts),
        'added_total': added,
        'confirmed_delta': confirmed_last - confirmed_first,
        'candidate_reduction': reduction,
        # Both halves required. Growth without confirmation is hoarding;
        # confirmation without a measured search gain is a claim.
        'compounding': (confirmed_last > confirmed_first
                        and reduction is not None and reduction >= 0.30),
        'note': ('reduction is unmeasured; the speed claim is not made'
                 if reduction is None else ''),
    }


def dumps(ledger: Dict) -> str:
    return json.dumps(ledger, ensure_ascii=False, sort_keys=True, indent=2)

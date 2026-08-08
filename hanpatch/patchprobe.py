"""Structure from somebody else's patch, measured against our own ROM.

The harvest spike counted 74 GitHub repositories and found seven that carry
both source and a licence we could act on. That number answers "whose code may
we copy", and we are not copying code.

We hold the ROM. So for any released patch - a cafe attachment, a blog mirror,
an xdelta on a dead forum, a bare IPS whose author left no address - we can do
this instead:

    ours   = dump of our own cartridge
    theirs = ours + their patch, applied locally, on our disk
    diff(ours, theirs) = the exact bytes a translator had to touch

A translator touches pointer tables because the text moved. Touches text banks
because the text changed language. Touches the font because Hangul was not in
the ROM. Those three edits are forced by the format, so the diff is a map of
the format drawn by somebody who already solved it. That map is a set of
offsets, strides and widths - facts about *our* bytes.

Hence the one hard rule, enforced in `_no_payload`: the document this emits
never contains ROM bytes or patch bytes, from either side. Offsets and
measurements leave; content does not. We redistribute nothing, and what we
keep we re-derived on hardware we own.

The output is a hypothesis, never a recipe. Status is pinned to `asserted`:
excluded from metrics, never returned to a `min_status` query, useful for one
thing - reordering the fingerprinter's candidate list so the expensive search
tries the right `(width, endian, base)` first. A prior reorders the search;
measurement decides. A wrong hypothesis costs search time and nothing else,
and that property is what makes a grey-zone source safe to read.

No network. No model calls. Two runs over the same pair are byte identical.
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = {'major': 1, 'minor': 0}

# Pinned. A patch-differential observation is one person's assertion about a
# ROM until we confirm it ourselves, however confident the arithmetic looks.
STATUS = 'asserted'

LABELS = ('pointer_table', 'text_bank', 'font', 'unknown')

# Enough entries that ascent is evidence rather than three numbers happening
# to go up.
MIN_TABLE_ENTRIES = 8

# A translated line differs in length from its source, so pointers after it
# shift. If almost nothing shifted, this is not a table a translation moved.
MIN_SHIFTED_FRACTION = 0.30

# How many candidate base constants to score per reading. They are generated
# from (pointer, region-start) pairs, so the true one is almost always
# proposed by whichever pointer addresses the first string in a block; the cap
# keeps a ROM with fifty changed regions from turning the search quadratic.
BASE_CANDIDATES = 24

# A run is cut when its trailing window stops producing new values. Padding
# reads as a constant that never decreases and would otherwise run forever.
RUN_WINDOW = 8
MIN_DISTINCT_FRACTION = 0.5

# Three is not a typo. The SNES addresses ROM with 24-bit long pointers and
# stores them in three bytes; leaving the width out meant the detector was
# structurally blind to an entire console. Widths are tried widest-run-first,
# not in this order, so listing 3 costs a pass and buys a platform.
TABLE_WIDTHS = (2, 3, 4)

JP_RANGES = ((0x3040, 0x30FF), (0x4E00, 0x9FFF), (0xFF01, 0xFF60))
KO_RANGES = ((0xAC00, 0xD7A3), (0x3131, 0x318E))


# ------------------------------------------------------------------ segmenting

def segment(old: bytes, new: bytes, gap: int = 16,
            min_length: int = 4) -> List[Dict]:
    """Runs of changed bytes, merged across short unchanged gaps.

    `gap` exists because a table whose entries partly kept their values shows
    up as changed words separated by unchanged ones. Those are one structure,
    not forty.

    Merging has a cost the classifier has to pay back: a pointer table sits
    immediately before the text it points at, so a real image usually hands us
    the table and the bank fused into a single region. `classify` splits them
    again. The segmenter deliberately does not guess where a structure ends -
    it only reports what changed.

    Only same-size images are index-aligned. A rebuild that changes the file
    size is refused rather than diffed at shifted offsets, because a one-byte
    insertion at the front would mark the whole ROM as changed and every
    number downstream would be a lie.
    """
    if len(old) != len(new):
        raise ValueError('segment() needs same-size images; see probe()')

    spans: List[List[int]] = []
    start = None
    for i, (a, b) in enumerate(zip(old, new)):
        if a != b:
            if start is None:
                start = i
        elif start is not None:
            spans.append([start, i])
            start = None
    if start is not None:
        spans.append([start, len(old)])

    merged: List[List[int]] = []
    for span in spans:
        if merged and span[0] - merged[-1][1] <= gap:
            merged[-1][1] = span[1]
        else:
            merged.append(span)

    return [{'start': s, 'end': e, 'length': e - s}
            for s, e in merged if e - s >= min_length]


# --------------------------------------------------------------------- shared

def _fraction(hits: int, total: int) -> float:
    return round(hits / total, 4) if total else 0.0


def _word(buf: bytes, at: int, width: int, endian: str) -> int:
    return int.from_bytes(buf[at:at + width],
                          'little' if endian == 'little' else 'big')


def _in_any(value: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(lo <= value < hi for lo, hi in spans)


# ------------------------------------------------------------- pointer tables

def _ascending_run(old: bytes, new: bytes, start: int, end: int,
                   width: int, endian: str, size: int) -> Tuple[int, List[int], List[int]]:
    """How far a table-shaped reading survives before it stops being one.

    This is the part that undoes the segmenter's merge. Walking forward from
    `start`, entries are accepted while they ascend in both images and still
    point inside the ROM. Text does not satisfy that for long - the first word
    of a Shift-JIS line is as likely to fall below its predecessor as above -
    so the run ends at the table's real boundary rather than the region's.

    Non-decreasing, not increasing: a real table may address the same string
    twice. But that tolerance is also a hole, and running this against 348
    cartridges found it - a stretch of `FF FF FF FF ...` padding reads as one
    constant value repeating, never decreases, and so ran for thousands of
    "entries". It matched a Kunio-kun patch to Super Mario Bros 2.

    So the run is cut when it stops being informative: `_distinct_enough`
    requires the window to keep producing new values. A table of pointers
    does; a field of padding does not.

    Returns `(entries, before, after)`.
    """
    before: List[int] = []
    after: List[int] = []
    at = start
    while at + width <= end:
        b = _word(old, at, width, endian)
        a = _word(new, at, width, endian)
        if not (0 < b < size and 0 < a < size):
            break
        if before and (b < before[-1] or a < after[-1]):
            break
        before.append(b)
        after.append(a)
        if not _distinct_enough(before):
            before.pop()
            after.pop()
            break
        at += width
    return len(before), before, after


def _distinct_enough(values: Sequence[int]) -> bool:
    """Is the tail of this run still saying anything.

    Judged on a trailing window rather than the whole run, so a genuine table
    that happens to end in padding is truncated at the padding instead of
    being thrown away entirely.
    """
    if len(values) < RUN_WINDOW:
        return True
    window = values[-RUN_WINDOW:]
    return len(set(window)) / RUN_WINDOW >= MIN_DISTINCT_FRACTION


def _extend_back(old: bytes, new: bytes, start: int, first: int, width: int,
                 endian: str, size: int,
                 changed: Sequence[Tuple[int, int]], base_delta: int = 0) -> int:
    """Walk backwards to the entry the patch had no reason to move.

    The first pointer in a table addresses the first string, and the first
    string does not move no matter how the translation changes its length. So
    the diff never contains entry zero, and a run that starts at the diff's
    first changed byte starts one entry late - sometimes several, if the
    opening lines happened to keep their length.

    Recovering those entries means reading bytes the patch did not touch.
    That is our own dump, freely readable, and the walk stays disciplined: an
    entry is only reclaimed if it is identical in both images, still ascends
    into the entry that follows it, and points into bytes this patch rewrote.
    A table's unmoved head satisfies all three; the record that happens to sit
    in front of the table almost never does.
    """
    at = start - width
    while at >= 0:
        b = _word(old, at, width, endian)
        if _word(new, at, width, endian) != b:
            break
        if not (0 < b < size) or b > first:
            break
        if not _in_any(b + base_delta, changed):
            break
        first = b
        start = at
        at -= width
    return start


def _best_base(values: Sequence[int],
               changed: Sequence[Tuple[int, int]]) -> Tuple[int, float]:
    """The constant that turns stored numbers into file offsets.

    A pointer is rarely a file offset. On the NES it is a CPU address in the
    $8000-$FFFF window that a mapper points at some bank; on the GBA it is
    `0x08000000 + offset`; inside a container it is measured from the member's
    start. In every case the stored value differs from the offset we can check
    by one constant per table.

    So instead of demanding that the raw value land in changed bytes, we look
    for the constant that makes it land. Every (value, changed region) pair
    proposes `region_start - value`; the constant proposed most often wins,
    and then we measure how much of the table it actually explains. Zero is in
    the running like any other candidate, which is why a plain file-offset
    table still comes out with `base_delta = 0`.

    This is the `base` field of the recipe schema, discovered rather than
    assumed - and it is the difference between reading a 3DS container and
    reading a cartridge with a mapper.
    """
    if not values or not changed:
        return (0, 0.0)

    proposals: Dict[int, int] = {}
    for v in values:
        for lo, _hi in changed:
            proposals[lo - v] = proposals.get(lo - v, 0) + 1

    # A wide region admits many constants that all land every pointer inside
    # it, so ties are the normal case, not the exception. Scoring the first
    # candidate that clears a threshold picked an arbitrary member of that tie
    # and reported a mapper where the table was plain file offsets.
    #
    # So: score a bounded candidate set, then choose by hit first and by
    # smallest magnitude second. Zero is always in the set - the answer "these
    # are file offsets" must never be unreachable because no pointer happened
    # to equal a region start.
    ranked = sorted(proposals, key=lambda d: (-proposals[d], abs(d)))[:BASE_CANDIDATES]
    if 0 not in ranked:
        ranked.append(0)

    scored = [(_fraction(sum(1 for v in values if _in_any(v + d, changed)),
                         len(values)), abs(d), d) for d in ranked]
    hit, _mag, delta = max(scored, key=lambda s: (s[0], -s[1]))
    return (delta, hit)


def pointer_table_evidence(old: bytes, new: bytes, region: Dict, size: int,
                           changed: Sequence[Tuple[int, int]]) -> Optional[Dict]:
    """The strongest signal a translation patch leaves behind.

    Four conditions have to hold at once. The reading ascends before the
    patch and still ascends after; the values moved; and - the one that
    actually separates a table from a coincidence - the values point *into
    bytes this same patch rewrote*. A translator moves pointers because the
    text they address was replaced, so a table whose targets land in
    untouched bytes is not the table this patch was written for.

    The earlier draft scored "shifts accumulate down the table" instead. That
    was wrong: the shift is a cumulative sum of per-line length changes, and
    Korean is sometimes shorter than Japanese, so the sequence is a random
    walk. It failed the fixture, which is the fixture doing its job.
    """
    best = None
    for width in TABLE_WIDTHS:
        for align in range(width):
            start = region['start'] + align
            for endian in ('little', 'big'):
                count, before, after = _ascending_run(
                    old, new, start, region['end'], width, endian, size)
                if count < MIN_TABLE_ENTRIES:
                    continue

                deltas = [b - a for a, b in zip(before, after)]
                shifted = _fraction(sum(1 for d in deltas if d), len(deltas))
                if shifted < MIN_SHIFTED_FRACTION:
                    continue

                base_delta, into_changed = _best_base(before, changed)
                if into_changed < 0.5:
                    continue

                head = _extend_back(old, new, start, before[0], width,
                                    endian, size, changed, base_delta)
                reclaimed = (start - head) // width

                asc = 1.0  # by construction of the run
                score = round((asc + shifted + into_changed) / 3, 4)
                evidence = {
                    'width': width,
                    'endian': endian,
                    'entries': count + reclaimed,
                    'moved_entries': count,
                    'unmoved_head_entries': reclaimed,
                    'table_end': start + count * width,
                    'shifted_fraction': shifted,
                    'base_delta': base_delta,
                    'targets_in_changed_fraction': into_changed,
                    'first_target': _word(old, head, width, endian),
                    'last_target': before[-1],
                    'net_shift': deltas[-1],
                }
                if best is None or evidence['entries'] > best[1]['entries'] or (
                        evidence['entries'] == best[1]['entries'] and score > best[0]):
                    best = (score, evidence, head)

    if best is None:
        return None
    return {'confidence': best[0], 'evidence': best[1], 'start': best[2]}


# ----------------------------------------------------------------- text banks

def _script_rate(data: bytes, encoding: str,
                 ranges: Sequence[Tuple[int, int]]) -> float:
    """How much of this block is script in this encoding.

    Decodability proves nothing - euc-kr decodes compressed garbage happily.
    So we count characters landing in ranges a human language uses, over the
    characters that decoded at all.
    """
    try:
        text = data.decode(encoding, errors='replace')
    except (LookupError, UnicodeError):
        return 0.0
    if not text:
        return 0.0
    hits = sum(1 for ch in text
               if any(lo <= ord(ch) <= hi for lo, hi in ranges))
    return _fraction(hits, len(text))


def text_bank_evidence(old: bytes, new: bytes, region: Dict) -> Optional[Dict]:
    """A block that stopped being Japanese and started being Korean.

    The flip is the evidence. Korean on both sides was already Korean and
    tells us nothing; script on neither side is a font, code, or compressed
    data.
    """
    a = old[region['start']:region['end']]
    b = new[region['start']:region['end']]

    jp_before = max(_script_rate(a, 'shift_jis', JP_RANGES),
                    _script_rate(a, 'utf-16-le', JP_RANGES))
    ko_after = max(_script_rate(b, 'euc-kr', KO_RANGES),
                   _script_rate(b, 'utf-8', KO_RANGES),
                   _script_rate(b, 'utf-16-le', KO_RANGES))
    ko_before = max(_script_rate(a, 'euc-kr', KO_RANGES),
                    _script_rate(a, 'utf-8', KO_RANGES))

    if jp_before < 0.25 or ko_after < 0.25 or ko_before >= jp_before:
        return None

    return {
        'confidence': round(min(jp_before, ko_after), 4),
        'evidence': {
            'source_script_rate': jp_before,
            'target_script_rate': ko_after,
            'target_script_rate_before': ko_before,
        },
    }


# ---------------------------------------------------------------------- fonts

def _bit_density(data: bytes) -> float:
    if not data:
        return 0.0
    return _fraction(sum(bin(b).count('1') for b in data), len(data) * 8)


def font_evidence(old: bytes, new: bytes, region: Dict) -> Optional[Dict]:
    """Glyphs added to a bitmap font.

    Weak on purpose. A font block is dense and gets *more* set bits when
    Hangul arrives, because a syllable covers more of its cell than a kana
    does. That is worth ordering a search by and nothing more, so it never
    outranks a table.

    Candidate strides are offered without requiring the region length to
    divide by them. Region bounds come from the diff, not from the structure -
    the first and last bytes of a font often survive a patch unchanged - so
    exact divisibility would be a test of luck.
    """
    a = old[region['start']:region['end']]
    b = new[region['start']:region['end']]
    if len(a) < 256:
        return None

    before = _bit_density(a)
    after = _bit_density(b)
    if after <= before or after < 0.15:
        return None

    strides = [s for s in (8, 16, 24, 32, 64, 128) if len(b) >= s * 16]
    if not strides:
        return None

    return {
        'confidence': round(min(after - before, 0.5) * 2, 4),
        'evidence': {
            'bit_density_before': before,
            'bit_density_after': after,
            'candidate_strides': strides,
            'cells_at_smallest_stride': len(b) // strides[0],
        },
    }


# --------------------------------------------------------------------- driver

def _labelled(region: Dict, label: str, found: Optional[Dict] = None) -> Dict:
    out = {'start': region['start'], 'end': region['end'],
           'length': region['end'] - region['start'],
           'label': label, 'status': STATUS,
           'confidence': 0.0, 'evidence': {}}
    if found:
        out['confidence'] = found['confidence']
        out['evidence'] = found['evidence']
    return out


def classify(old: bytes, new: bytes, region: Dict, size: int,
             changed: Sequence[Tuple[int, int]]) -> List[Dict]:
    """One region in, one or more labelled regions out.

    A region may legitimately hold two structures, because the segmenter
    merges what the patch touched and a patch touches a table and its text
    together. So a found table splits the region: the head is the table, and
    the tail goes back through the remaining classifiers. The split happens
    once - a tail is not re-searched for further tables, because at that point
    the ascending run has already ended and anything found beyond it would be
    the classifier arguing with its own boundary.
    """
    table = pointer_table_evidence(old, new, region, size, changed)
    out: List[Dict] = []
    tail = region

    if table:
        head = {'start': table['start'], 'end': table['evidence']['table_end']}
        if table['start'] > region['start']:
            out.extend(_rest(old, new,
                             {'start': region['start'], 'end': table['start']}))
        out.append(_labelled(head, 'pointer_table', table))
        tail = {'start': head['end'], 'end': region['end']}
        if tail['end'] - tail['start'] <= 0:
            return out

    out.extend(_rest(old, new, tail))
    return out


def _rest(old: bytes, new: bytes, region: Dict) -> List[Dict]:
    if region['end'] - region['start'] <= 0:
        return []
    for label, fn in (('text_bank', text_bank_evidence),
                      ('font', font_evidence)):
        found = fn(old, new, region)
        if found:
            return [_labelled(region, label, found)]
    return [_labelled(region, 'unknown')]


def targets(old: bytes, tables: Sequence[Dict]) -> List[int]:
    """Every file offset a found table addresses.

    Read from the pre-patch image, through each table's own `base_delta`, so
    a mapped address space and a plain file offset produce the same kind of
    answer: places in our ROM.
    """
    out: List[int] = []
    for t in tables:
        ev = t['evidence']
        width, endian = ev['width'], ev['endian']
        for i in range(ev['entries']):
            at = t['start'] + i * width
            if at + width > len(old):
                break
            out.append(_word(old, at, width, endian) + ev['base_delta'])
    return out


def pointed_to_evidence(region: Dict, aimed: Sequence[int]) -> Optional[Dict]:
    """Text we cannot read, identified by what addresses it.

    A cartridge stores its script in a private encoding - a byte per tile,
    numbered however the artist felt. No decoder we could write would
    recognise it, so `text_bank_evidence` stays silent on exactly the systems
    where the script matters most.

    But we do not have to read it. If a table we just proved is a table spends
    most of its entries pointing into this block, the block is what the table
    indexes, and a table whose entries all moved indexes something the
    translator rewrote. That is text, established by structure rather than by
    encoding, and it is a stronger argument than a decode rate because it
    cannot be produced by a lucky byte distribution.
    """
    hits = [v for v in aimed if region['start'] <= v < region['end']]
    if len(hits) < MIN_TABLE_ENTRIES:
        return None
    return {
        'confidence': round(min(len(hits) / MIN_TABLE_ENTRIES, 1.0), 4),
        'evidence': {
            'method': 'pointed_to',
            'pointers_landing_here': len(hits),
            'first': min(hits),
            'last': max(hits),
        },
    }


def _no_payload(node, pointer: str = '') -> List[str]:
    """Refuse to emit their bytes, or ours.

    This is the guarantee that makes reading a grey-zone patch defensible, so
    it is checked mechanically on the way out rather than left to review.
    """
    bad: List[str] = []
    if isinstance(node, (bytes, bytearray, memoryview)):
        bad.append(pointer or '/')
    elif isinstance(node, dict):
        for key, value in node.items():
            bad.extend(_no_payload(value, '%s/%s' % (pointer, key)))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            bad.extend(_no_payload(value, '%s/%d' % (pointer, index)))
    return bad


def probe(old: bytes, new: bytes, *, rom_id: str, source: Dict,
          gap: int = 16, min_length: int = 4) -> Dict:
    """Compare our ROM against our ROM with their patch applied.

    `source` says where the patch came from - host, retrieval date, whether an
    author or licence was found at all. Recorded because a hypothesis with no
    traceable origin may have to be withdrawn, and withdrawal needs to know
    what to look for.
    """
    if len(old) != len(new):
        return {
            'schema_version': dict(SCHEMA_VERSION),
            'rom_id': rom_id,
            'status': STATUS,
            'alignment': 'size_changed',
            'regions': [],
            'summary': {'note': 'a rebuild changes the image size; an index '
                                'diff would be meaningless. Needs a '
                                'container-aware comparison per member.',
                        'size_before': len(old), 'size_after': len(new)},
            'source': dict(source),
        }

    regions = segment(old, new, gap=gap, min_length=min_length)
    changed = [(r['start'], r['end']) for r in regions]

    # Two passes, because the second one needs the first one's answer. Tables
    # are found first; then every block still unexplained is offered the
    # question "does a table we just proved point in here?". Doing it in one
    # pass would mean asking that question with only the tables found so far,
    # which would make a region's label depend on the order the segmenter
    # happened to emit regions in.
    classified: List[Dict] = []
    for region in regions:
        classified.extend(classify(old, new, region, len(old), changed))

    tables = [r for r in classified if r['label'] == 'pointer_table']
    if tables:
        aimed = targets(old, tables)
        for i, region in enumerate(classified):
            if region['label'] != 'unknown':
                continue
            found = pointed_to_evidence(region, aimed)
            if found:
                classified[i] = _labelled(region, 'text_bank', found)

    counts = {label: sum(1 for r in classified if r['label'] == label)
              for label in LABELS}

    doc = {
        'schema_version': dict(SCHEMA_VERSION),
        'rom_id': rom_id,
        'status': STATUS,
        'alignment': 'same_size',
        'regions': classified,
        'summary': {
            'image_size': len(old),
            'regions': len(classified),
            'bytes_changed': sum(r['length'] for r in classified),
            'fraction_changed': _fraction(
                sum(r['length'] for r in classified), len(old)),
            'labels': counts,
            # Visible on purpose. A probe explaining 5% of what the patch
            # touched has told us very little, and a summary that hid the
            # unknown count would let it look like it told us a lot.
            'unexplained_bytes': sum(r['length'] for r in classified
                                     if r['label'] == 'unknown'),
        },
        'source': dict(source),
        'provenance': {
            'method': 'patch-differential',
            'independent_measurement': False,
            'derived_from_our_dump': True,
            'contains_third_party_bytes': False,
            'old_sha256': hashlib.sha256(old).hexdigest(),
            'new_sha256': hashlib.sha256(new).hexdigest(),
        },
    }

    leaks = _no_payload(doc)
    if leaks:
        raise AssertionError('payload bytes in hypothesis: %s' % ', '.join(leaks))
    return doc


def candidates(doc: Dict) -> List[Dict]:
    """Reorderings for the fingerprinter, best evidence first.

    Not recipes. A recipe needs a count, a base and an address space, and this
    knows none of those. It knows where to look and in what shape, which is
    the part that costs a full search to find.
    """
    out = []
    for region in doc.get('regions', []):
        if region['label'] != 'pointer_table':
            continue
        ev = region['evidence']
        out.append({
            'at': region['start'],
            'width': ev['width'],
            'endian': ev['endian'],
            'stride': ev['width'],
            'entries': ev['entries'],
            'confidence': region['confidence'],
            'status': STATUS,
        })
    out.sort(key=lambda c: (-c['confidence'], c['at']))
    return out

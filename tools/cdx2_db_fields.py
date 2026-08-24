"""Re-derive DATABASE.DAT text fields from the bytes, and check the derivation.

Why this exists: the shipped Korean build left 821 database strings Japanese or
garbled, and every one of them was invisible to the extractor rather than
mistranslated. Three separate causes, all measured on the Japanese image:

  1. The old walk scanned a member LINEARLY and jumped to the byte after the
     next NUL. A numeric field with no zero byte therefore swallows the text
     field behind it. `MONSTER.DAT` record 0 holds `01 00 b8 0b` at rel 0x68 and
     the monster name at rel 0x6c, so the walk read `b8 0b 83 68 …`, failed to
     decode it, and skipped the name - all 295 monster names, gone.
  2. Reading with Python's `shift_jis` codec. This disc uses the NEC row
     (circled digits are all over the equipment descriptions: `弱点②④⑥`), which
     that codec cannot decode and `cp932` can. 267 strings dropped.
  3. A ratio test that demanded Japanese characters outnumber the rest 3:1,
     which drops `SP+30マナ15増` and `CRT+5 弱点②`. 75 strings dropped.

The replacement derives fields by OFFSET with cross-record support: a relative
offset is a text field when the same offset holds a decodable Japanese string in
many records. That cannot be swallowed by a neighbouring field, because nothing
is read sequentially.

    python3 tools/cdx2_db_fields.py <iso>            report per member
    python3 tools/cdx2_db_fields.py <iso> --json OUT write the slot table

Checks it runs on itself, so a derivation that looks plausible and is wrong
fails here rather than in a build: slots may not overlap, the new set must be a
superset of what the old walk found, and the offsets must cover a supplied gap
list (`--gap work/db_gap.json`).
"""
import argparse
import collections
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch.platforms.psp import dsarc, dbtbl, pspfs, sdt  # noqa: E402

DATA_OFFSET = 87568 * 2048
MEMBER = 'DATABASE.DAT'
HEADER = 16
MAX_STRING = 512
#: A field only has to appear TWICE to be a field. A share-of-records threshold
#: looks safer and is not: skill and effect names are filled in a minority of
#: their records, and 0.2 dropped `JOBSKILL.DAT` rel 28 and `MAHO-ZIN.DAT`
#: rel 72 - 450 strings - because most records leave them empty. False starts are
#: rejected per instance instead, which is exact rather than statistical.
MIN_RECORDS = 2


def text(raw):
    """`raw` as a line the game could draw, or None."""
    if len(raw) < 2 or len(raw) > MAX_STRING:
        return None
    try:
        s = raw.decode('cp932')
    except UnicodeDecodeError:
        return None
    if any(ord(c) < 0x20 and c != '\n' for c in s):
        return None
    # cp932 maps 0xFF41.. into the private use area, so a numeric field holding
    # 0xFF reads as a legal character and a string appears to start one byte
    # early - which is how the same line was counted at 17 by one reader and at
    # 18 by another. Nothing the game draws lives in the PUA.
    if any('\ue000' <= c <= '\uf8ff' for c in s):
        return None
    return s


def japanese(s):
    """One Japanese character is enough. The ratio test is what dropped rows.

    Halfwidth katakana (U+FF61..U+FF9F) does NOT count. It is one byte per
    character, so a stat field of small integers decodes as a run of it, and
    counting it would turn binary into text - which is how a numeric field came
    to swallow the monster names in the first place.
    """
    return any('\u3040' <= c <= '\u30ff' or '\u4e00' <= c <= '\u9fff'
               or '\uff01' <= c <= '\uff5e' or c == '\u3000'
               or '\u2460' <= c <= '\u2473' for c in s)


#: Header sizes this file uses in front of its record array. The count is always
#: a u32 at 0, but the array does not always start at 16: `CHARNAMEMAN.DAT` puts
#: it at 4 and `MAHO-ZIN.DAT` at 8, and assuming 16 for those two hid 689 names
#: and spell descriptions - the member simply failed to parse and was skipped.
HEADERS = (4, 8, 16, 32)


def shapes(blob):
    """Every (header, count, stride) the length and the count make possible."""
    out = []
    if len(blob) < 8:
        return out
    count, = struct.unpack_from('<I', blob, 0)
    if not 0 < count <= 65535:
        return out
    for head in HEADERS:
        rest = len(blob) - head
        if rest > 0 and rest % count == 0 and rest // count >= 4:
            out.append((head, count, rest // count))
    return out


def _string_at(rec, r):
    end = rec.find(b'\x00', r)
    if end < 0:
        end = len(rec)
    return text(rec[r:end])


def fields(blob, sh=None):
    """Relative offsets that hold text, most-supported first.

    Support - how many records hold a decodable Japanese string at that offset -
    is what separates a field from an offset INSIDE a field. `DIFFICULTY.DAT`
    keeps a signed id in the first two bytes and `DEFが99` at rel 2: reading from
    rel 0 decodes in 67 of 199 records (the id happens to be a valid lead byte
    that often), reading from rel 2 decodes in all 199. Taking the earliest start
    picks the id and writes a translation over a stat; taking the best-supported
    start picks the text. Ties go to the lower offset, which is the start of the
    string rather than a position inside it.
    """
    if sh is None:
        cands = shapes(blob)
        if not cands:
            return None, []
        sh = cands[0]
    head, count, stride = sh
    support = collections.Counter()
    spans = collections.defaultdict(dict)
    for i in range(count):
        rec = blob[head + i * stride:head + (i + 1) * stride]
        for r in range(stride - 1):
            if rec[r] == 0:
                continue
            s = _string_at(rec, r)
            if s is not None and japanese(s):
                support[r] += 1
                spans[r][i] = len(s.encode('cp932'))
    accepted = []
    for r in sorted(support, key=lambda x: (-support[x], x)):
        # Overlap is symmetric and that matters: rel 0 of `DIFFICULTY.DAT` reads
        # as a 9-byte string that COVERS the real field at rel 2, so testing only
        # "is r inside something accepted" would keep both and the earlier one
        # would swallow the text again. A candidate has to be free of every
        # accepted field, in front of it or behind it, in at least MIN_RECORDS of
        # the records where it appears at all.
        free = 0
        for i, length in spans[r].items():
            clash = False
            for a in accepted:
                al = spans[a].get(i)
                if al and r < a + al and a < r + length:
                    clash = True
                    break
            if clash:
                continue
            free += 1
            if free >= MIN_RECORDS:
                break
        if free >= MIN_RECORDS:
            accepted.append(r)
    return sh, sorted(accepted)


def pool_slots(blob):
    """Strings in a member that is a pool, not a record array.

    `MAHO-ZIN.DAT` divides evenly by no header this file uses and `DEFAULTTALK.DAT`
    divides evenly by one that does not explain its text - a grid derived for it
    hands back slots whose budget is smaller than the string already sitting in
    them, which is proof the grid is wrong. Both are pools: NUL-separated strings
    with padding, so a start is a byte after a NUL and nothing has to be guessed.
    """
    out = []
    i = 0
    n = len(blob)
    while i < n:
        if blob[i] == 0:
            i += 1
            continue
        end = blob.find(b'\x00', i)
        if end < 0:
            break
        s = text(blob[i:end])
        if s is None or not japanese(s):
            # Advance ONE byte, not past the terminator. Skipping the whole run
            # is what lost the text behind every numeric field: `DIFFICULTY.DAT`
            # keeps `9d ff` in front of `DEFが99`, and a walk that gave up on the
            # pair and jumped to the NUL jumped over the string as well.
            i += 1
            continue
        room = end
        while room < n and blob[room] == 0:
            room += 1
        out.append((i, s, room - i - 1))
        i = end + 1
    return out


def _grid_is_sane(found):
    """A grid that cannot hold what is already stored in it is not the layout."""
    return all(b >= len(s.encode('cp932')) for _o, s, b in found)


def slots(blob):
    """[(offset, text, budget)] for one member, and how it was read."""
    tab = dbtbl.table(blob)
    if tab:
        # An offset table: the strings live in a pool the table points into, so
        # the pool is read through the table rather than walked. Same codec and
        # same one-Japanese-character rule as the record path, which is what the
        # old reader lacked - 47 of `STRTBL.DAT`'s lines were invisible to it.
        base, offs = tab
        live = sorted({o for o in offs if o and o < len(blob)})
        out = []
        for j, at in enumerate(live):      # table offsets are absolute
            end = blob.find(b'\x00', at)
            if end < 0:
                end = len(blob)
            txt = text(blob[at:end])
            if txt is None or not japanese(txt):
                continue
            nxt = live[j + 1] if j + 1 < len(live) else len(blob)
            room = end
            while room < nxt and blob[room] == 0:
                room += 1
            out.append((at, txt, min(room, nxt) - at - 1))
        return out, 'table'
    # Several headers can divide the member evenly; the right one is the one
    # that actually explains the text, so each candidate is tried and the one
    # yielding the most strings wins. Ties go to the smaller header.
    best = ([], 'opaque')
    for cand in shapes(blob):
        got = _record_slots(blob, cand)
        if _grid_is_sane(got) and len(got) > len(best[0]):
            best = (got, 'records')
    pool = pool_slots(blob)
    if not best[0]:
        return pool, 'pool'
    # A grid explains the FIXED fields and gives them a field-bounded budget; it
    # does not explain strings packed at variable positions - a monster record
    # carries extra sentences right behind its description, and `DUNGEON.DAT`
    # keeps 76 lines the grid has no column for. Both are real text, so the two
    # readings are merged: grid first, then any pooled string that does not sit
    # inside a slot the grid already owns.
    merged = list(best[0])
    taken = [(o, o + b) for o, _s, b in merged]
    for o, txt, bud in pool:
        if any(a <= o < z for a, z in taken):
            continue
        merged.append((o, txt, bud))
    merged.sort()
    kind = 'records' if len(merged) == len(best[0]) else 'records+pool'
    return merged, kind


def _record_slots(blob, sh):
    sh, flds = fields(blob, sh)
    if not sh:
        return []
    head, count, stride = sh
    out = []
    for i in range(count):
        base = head + i * stride
        rec = blob[base:base + stride]
        # Which fields this particular record actually fills. A field grid is
        # where a string may START; how long it is varies per record, so the
        # room is measured on the instance and stopped at the next filled
        # field - a string that grew into its neighbour would corrupt a stat.
        live = []
        for r in flds:
            if rec[r] == 0:
                continue
            if live:
                prev_r, prev_s = live[-1]
                if r < prev_r + len(prev_s.encode('cp932')):
                    continue        # inside the previous string, not a field
            s = _string_at(rec, r)
            if s is not None and japanese(s):
                live.append((r, s))
        for j, (r, s) in enumerate(live):
            raw = s.encode('cp932')
            room = r + len(raw)
            while room < stride and rec[room] == 0:
                room += 1
            nxt = live[j + 1][0] if j + 1 < len(live) else stride
            out.append((base + r, s, min(room, nxt) - r - 1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('iso')
    ap.add_argument('--json')
    ap.add_argument('--gap')
    args = ap.parse_args()
    with open(args.iso, 'rb') as fh:
        fh.seek(DATA_OFFSET)
        fs = pspfs.Pspfs(fh.read())
    inner = dsarc.Dsarc(sdt.Sdt(fs.read(MEMBER)).payload)
    gap = collections.defaultdict(set)
    if args.gap:
        for row in json.load(open(args.gap)):
            gap[row['member']].add(row['offset'])
    problems = []
    table = {}
    print('%-20s %6s %6s %6s %5s %7s' % ('member', 'old', 'new', 'fields', 'kind', 'gap'))
    told = tnew = twant = thit = 0
    for name in inner.names():
        blob = inner.read(name)
        old = {s.offset for s in dbtbl.record_slots(blob)}
        found, kind = slots(blob)
        offs = [o for o, _s, _b in found]
        if len(set(offs)) != len(offs):
            problems.append('%s: duplicate slot offsets' % name)
        for (o, s, b) in found:
            if b < len(s.encode('cp932')):
                problems.append('%s: slot %#x has budget %d for %d bytes'
                                % (name, o, b, len(s.encode('cp932'))))
        lost = old - set(offs)
        if lost:
            problems.append('%s: %d slots the old walk found are missing (%s)'
                            % (name, len(lost), sorted(lost)[:4]))
        want = gap.get(name, set())
        hit = len(want & set(offs))
        if want and hit < len(want):
            problems.append('%s: %d of %d known-bad strings still not seen'
                            % (name, len(want) - hit, len(want)))
        told += len(old); tnew += len(found); twant += len(want); thit += hit
        _sh, flds = fields(blob)
        print('%-20s %6d %6d %6d %5s %4d/%d'
              % (name, len(old), len(found), len(flds), kind[:5], hit, len(want)))
        table[name] = [{'offset': o, 'jp': s, 'budget': b} for o, s, b in found]
    print('%-20s %6d %6d %6s %5s %4d/%d' % ('TOTAL', told, tnew, '', '', thit, twant))
    if args.json:
        json.dump(table, open(args.json, 'w'), ensure_ascii=False, indent=1)
        print('slot table -> %s' % args.json)
    if problems:
        print('\nPROBLEMS')
        for p in problems[:40]:
            print('  ' + p)
        return 1
    print('\nno problems: no overlap, no regression, every known-bad string seen')
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""DATABASE.DAT — the title's second text domain.

`SCRIPT.SDT` holds the story. This file holds everything the interface says:
weapon and armour names, monster names and their hints, job and magic
descriptions, dungeon names, the two name-entry pools, and the system string
table (`%sの わな にのった！`). None of it travels through the script, so a patch
that rewrites only `SCRIPT.SDT` ships a game whose menus are still Japanese —
which is exactly what shipped once.

Two layouts live inside the same DSARC and they differ in what a translation is
allowed to cost.

**A string table** (`STRTBL.DAT`) is an array of u32 offsets followed by the
bytes they point at. The engine reaches a string through the table, so the bytes
may move: the member is rebuilt, every offset recomputed, and a translation may
be longer than the Japanese it replaces.

**A record table** (everything else) stores its strings INSIDE fixed-stride
records at fixed offsets. Such a string can neither move nor grow past the field
it occupies, and a zero byte after a name is NOT proof of free space: a numeric
field whose value happens to be zero looks exactly the same. So the budget is
the run of zeros that follows the string, capped by the next field the records
themselves prove — and where that geometry is not proved, the budget is the
Japanese byte length and not one byte more. Fail-closed, because the failure it
prevents is a corrupted stat that no gate downstream would notice.

Halfwidth katakana is deliberately not evidence of text. Binary fields decode
cleanly to it (`ﾍﾌﾌ=c`, `ﾄ"ｲ`), and counting those as strings offered 200-odd
"translatable" slots inside drop tables and level curves.
"""
import collections
import struct

#: Record-count headers seen in this file: the count is a u32 at 0 and the rows
#: begin after one of these.
HEADERS = (4, 8, 16, 32)

#: How many records must show a string at an offset before it counts as a field.
#: Two. A share-of-records threshold looks safer and is not: skill and effect
#: names are filled in a minority of their records, and demanding 20% dropped
#: `JOBSKILL.DAT` rel 28 and `MAHO-ZIN.DAT` rel 72 - 450 strings - because most
#: records leave them empty. False starts are rejected per record instead, which
#: is exact rather than statistical.
MIN_RECORDS = 2

MAX_STRING = 512

#: ASCII punctuation that does not occur in a single line this game draws.
NON_TEXT = frozenset('"><\\^|`{}~@#;')

Entry = collections.namedtuple('Entry', 'key jp budget')
Slot = collections.namedtuple('Slot', 'offset text budget')


class DbError(Exception):
    pass


def _text(raw):
    """`raw` as text if it could be a line the game shows, else None.

    Read with `cp932`, not `shift_jis`. The disc uses the NEC row - circled
    digits are all over the equipment descriptions (`弱点②④⑥`) - and Python's
    `shift_jis` cannot decode it. Reading with the narrower codec silently
    dropped 267 strings: they were not mistranslated, they were never offered.

    The private use area is rejected. cp932 maps 0xFF41.. into it, so a numeric
    field holding 0xFF reads as a legal character and a string appears to start
    one byte earlier than it does - which is how the same line got counted at
    offset 17 by one reader and 18 by another. Nothing this game draws is in the
    PUA.
    """
    if not raw or len(raw) > MAX_STRING:
        return None
    try:
        s = raw.decode('cp932')
    except UnicodeDecodeError:
        return None
    if any(ord(c) < 0x20 and c != '\n' for c in s):
        return None
    # C1 and the ASCII punctuation this game never draws. Measured on the 11279
    # strings the project has already translated: `" > < \\ ^ | ` { } ~ @ # ;`
    # occur ZERO times between them, so a candidate carrying one is a numeric
    # field that happened to decode - `劔>"` out of a level curve, `\x80迄` out of
    # an effect table. Offering those for translation writes a stat over with
    # prose, which is worse than leaving text Japanese.
    if any('\x7f' <= c <= '\x9f' for c in s):
        return None
    if any(c in NON_TEXT for c in s):
        return None
    if any('\ue000' <= c <= '\uf8ff' for c in s):
        return None
    return s


def _japanese(s):
    """Whether `s` holds Japanese at all.

    ONE character is enough. The old rule demanded two Japanese characters and a
    3:1 majority, which reads as prudence and cost 75 strings the player sees:
    `SP+30マナ15増`, `CRT+5 弱点②`, `DEFが99`. A short mixed line is still a
    line.

    Halfwidth katakana (U+FF61..U+FF9F) still does not count. It is one byte per
    character, so a stat field of small integers decodes as a run of it - and
    counting it as text is how a numeric field came to swallow the string behind
    it.
    """
    return any('\u3040' <= c <= '\u30ff' or '\u4e00' <= c <= '\u9fff'
               or '\uff01' <= c <= '\uff5e' or c == '\u3000'
               or '\u2460' <= c <= '\u2473' for c in s)


def _field(record, off):
    end = record.find(b'\x00', off)
    if end < 0 or end == off:
        return None
    return _text(record[off:end])


def shapes(blob):
    """Every (header, count, stride) the length and the record count allow.

    The count is a u32 at 0 but the record array does not always start at 16:
    `CHARNAMEMAN.DAT` starts at 4 and assuming 16 made the member fail to parse
    at all, hiding 249 names. Usually only one header divides the member evenly,
    and where several do the caller picks the one that explains the most text.
    """
    out = []
    if len(blob) < 8:
        return out
    count, = struct.unpack_from('<I', blob, 0)
    if not 0 < count <= 65535:
        return out
    for head in HEADERS:
        body = len(blob) - head
        if body > 0 and body % count == 0 and body // count >= 4:
            out.append((head, count, body // count))
    return out


def _string_at(record, off):
    end = record.find(b'\x00', off)
    if end < 0:
        end = len(record)
    return _text(record[off:end])


def fields(blob, shape):
    """Relative offsets that hold text, proven by cross-record support.

    A text field is an OFFSET that holds a decodable Japanese string in many
    records. Reading records sequentially cannot find them: a numeric field with
    no zero byte runs into the string behind it, the pair fails to decode, and a
    walk that skips to the next NUL skips the string too. That is what hid all
    295 monster names (`01 00 b8 0b` sits in front of every one of them) and
    every `DEFが99` in `DIFFICULTY.DAT` behind a signed id.

    Support is also what tells a field from a position INSIDE a field. In
    `DIFFICULTY.DAT` reading from rel 0 decodes in 67 of 199 records - the id is
    a valid lead byte that often - and reading from rel 2 decodes in all 199. The
    best-supported start wins, ties go to the lower offset, and a candidate that
    OVERLAPS an accepted field in most records is dropped. Overlap has to be
    tested both ways: rel 0 covers rel 2, so testing only "is it inside" would
    keep both and the earlier one would swallow the text again.
    """
    head, count, stride = shape
    support = collections.Counter()
    spans = collections.defaultdict(dict)
    for i in range(count):
        record = blob[head + i * stride:head + (i + 1) * stride]
        for off in range(stride - 1):
            if record[off] == 0:
                continue
            s = _string_at(record, off)
            if s is not None and _japanese(s):
                support[off] += 1
                spans[off][i] = len(s.encode('cp932'))
    accepted = []
    for off in sorted(support, key=lambda x: (-support[x], x)):
        free = 0
        for i, length in spans[off].items():
            if any(spans[a].get(i) and off < a + spans[a][i] and a < off + length
                   for a in accepted):
                continue
            free += 1
            if free >= MIN_RECORDS:
                break
        if free >= MIN_RECORDS:
            accepted.append(off)
    return sorted(accepted)


def _grid_slots(blob, shape):
    """Slots for one candidate shape, with field-bounded budgets."""
    head, count, stride = shape
    flds = fields(blob, shape)
    out = []
    for i in range(count):
        base = head + i * stride
        record = blob[base:base + stride]
        live = []
        for off in flds:
            if record[off] == 0:
                continue
            if live:
                prev, text = live[-1]
                if off < prev + len(text.encode('cp932')):
                    continue        # inside the previous string, not a field
            s = _string_at(record, off)
            if s is not None and _japanese(s):
                live.append((off, s))
        for j, (off, s) in enumerate(live):
            room = off + len(s.encode('cp932'))
            while room < stride and record[room] == 0:
                room += 1
            nxt = live[j + 1][0] if j + 1 < len(live) else stride
            out.append(Slot(base + off, s, min(room, nxt) - off - 1))
    return out


def _pool_slots(blob):
    """Strings in a member that is not a uniform record array.

    `MAHO-ZIN.DAT` divides evenly by no header this file uses; it is a pool of
    NUL-separated strings with padding. The one rule that matters here is to
    advance ONE byte when a run fails to decode instead of jumping past its
    terminator - jumping is what lost the text behind every numeric field.
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
        s = _text(blob[i:end])
        if s is None or not _japanese(s):
            i += 1
            continue
        room = _zeros_end(blob, end)
        out.append(Slot(i, s, room - i - 1))
        i = end + 1
    return out


def _zeros_end(blob, at):
    """First index at or after `at` that is not a zero byte."""
    z = at
    while z < len(blob) and blob[z] == 0:
        z += 1
    return z


def _anchored(blob, slots):
    """How many of these slots start right after a zero byte.

    A string a writer emitted starts at a field boundary, and a field that
    follows text is preceded by the previous string's terminator. A shape that is
    misaligned reads from the middle of a character instead, so its slots are
    preceded by a trailing byte. This is the measurement that separates the two
    when both explain the same NUMBER of strings.
    """
    return sum(1 for s in slots if s.offset == 0 or blob[s.offset - 1] == 0)


def _shape_rank(blob, shape, slots):
    """Sort key: aligned stride first, then anchored slots, then slot count.

    `CHAR.DAT` is why this exists. Header 4 with stride 159 explains eight
    strings and so does header 16 with stride 156 - but the first reads three
    bytes into every one of them (`サンプルン` came out as `[トン`, `見本語の説明文。`
    as `p本語の説明文。`), and picking by count alone took it, then the pool added the
    correct readings on top: 13 slots for 8 strings, five of them shifted
    garbage that no translator can act on.

    Stride alignment is the primary signal because these structures are
    4-aligned; anchoring settles the rest. Where a record keeps text behind a
    numeric field - `DIFFICULTY.DAT` has a signed id in front of `DEFが99` -
    nothing is anchored and the count decides, which is what it did before.
    """
    return (shape[2] % 4 == 0, _anchored(blob, slots), len(slots))


def _is_numeric_pair(slot):
    """A two-byte CJK reading with no room to be anything else.

    `DUNGEON.DAT` keeps `8d 90 00` where a record's flags live and cp932 reads it
    as `告`; `EQUIP-EFFECT.DAT` produces `迄` and `湫` the same way. The tell is
    the field: two bytes of CJK with a budget of three has no room for a word, and
    the value changes with the record rather than naming anything.

    Measured on the corpus: of the strings this project has already translated,
    NONE is a two-byte CJK-only line in a three-byte field, while 35 rows the
    models refused to translate are exactly that. Kana is excluded from the rule -
    a two-byte kana word is a real word - and so is anything longer.
    """
    if slot.budget > 3 or len(slot.text) > 1:
        return False
    return all('\u4e00' <= c <= '\u9fff' for c in slot.text)


def _prune_overlaps(blob, slots):
    """Drop slots that overlap a better reading of the same bytes.

    Two slots covering the same bytes cannot both be strings. The one to keep is
    the anchored one, then the longer one - a shifted read is always shorter than
    the string it starts inside.
    """
    order = sorted(slots, key=lambda s: (
        -(s.offset == 0 or blob[s.offset - 1] == 0),
        -len(s.text.encode('cp932')), s.offset))
    kept = []
    for slot in order:
        end = slot.offset + len(slot.text.encode('cp932'))
        if any(slot.offset < k.offset + len(k.text.encode('cp932'))
               and k.offset < end for k in kept):
            continue
        kept.append(slot)
    kept = [s for s in kept if not _is_numeric_pair(s)]
    kept.sort(key=lambda s: s.offset)
    return kept


def record_slots(blob):
    """Every string a record member holds, with what may be written there.

    Grid first: it explains the fixed fields and bounds each budget by the next
    field, because a name that grows into its neighbour corrupts a stat. A grid
    that hands back a slot too small for the string ALREADY stored in it is not
    the layout, so it is rejected outright, and among the survivors the
    best-aligned one wins (`_shape_rank`). Then the pool: strings packed at
    variable positions - the extra sentences behind a monster description, the 76
    lines `DUNGEON.DAT` has no column for - are text too. Finally overlaps are
    pruned, because a grid and a pool reading of the same bytes must not both
    become translatable rows.
    """
    best = []
    best_rank = None
    for shape in shapes(blob):
        got = _grid_slots(blob, shape)
        if not got or not all(s.budget >= len(s.text.encode('cp932')) for s in got):
            continue
        rank = _shape_rank(blob, shape, got)
        if best_rank is None or rank > best_rank:
            best, best_rank = got, rank
    pool = _pool_slots(blob)
    if not best:
        return _prune_overlaps(blob, pool)
    owned = [(s.offset, s.offset + s.budget) for s in best]
    merged = list(best)
    for slot in pool:
        if any(a <= slot.offset < z for a, z in owned):
            continue
        merged.append(slot)
    return _prune_overlaps(blob, merged)


def geometry(blob):
    """(header, stride, field offsets) for the shape that explains the text."""
    best = None
    for shape in shapes(blob):
        flds = fields(blob, shape)
        if flds and (best is None or len(flds) > len(best[2])):
            best = (shape[0], shape[2], flds)
    return best


def _table_shape(blob):
    """(base, offsets) when the bytes are SHAPED like an offset table.

    Structure only, with no test of what language the strings are in, because
    this is also how a member that has ALREADY been translated is read back.
    The language test belongs to `table()`, which classifies the untouched file.

    The first u32 is both the first string's offset and the table's length in
    bytes, so the entry count follows from it. Trailing zero entries are
    padding the engine never dereferences and are preserved as zeros.
    """
    if len(blob) < 8:
        return None
    base, = struct.unpack_from('<I', blob, 0)
    if base < 8 or base % 4 or base >= len(blob):
        return None
    entries = base // 4
    offs = list(struct.unpack_from('<%dI' % entries, blob, 0))
    live = [o for o in offs if o]
    if len(live) < 8 or any(o >= len(blob) for o in live):
        return None
    if any(blob.find(b'\x00', off) < 0 for off in live):
        return None
    return base, offs


def table(blob):
    """(base, offsets) when the member is a Japanese offset table, else None.

    Shape alone is not enough to classify: `DROP.DAT` passes every structural
    test and its "strings" are drop-rate bytes that happen to decode. So a real
    table has to be mostly Japanese as well.
    """
    shaped = _table_shape(blob)
    if not shaped:
        return None
    base, offs = shaped
    live = [o for o in offs if o]
    good = 0
    for off in live:
        s = _text(blob[off:blob.find(b'\x00', off)])
        if s is not None and _japanese(s):
            good += 1
    if good < 20 or good * 5 < len(live) * 2:
        return None
    return base, offs


def stored(blob, keys):
    """{key: stored bytes} for `keys`, read with no assumption about language.

    Verification reads a member the patch has already rewritten, so the scan
    that located the Japanese cannot be reused - it tests FOR Japanese and would
    not find the Korean that replaced it, reporting every translated slot as
    missing. A key carries its own address instead: a record field's offset does
    not move, and a table entry's index survives the repack.
    """
    shaped = _table_shape(blob)
    offs = shaped[1] if shaped else None
    out = {}
    for key in keys:
        if key.startswith('s') and offs is not None:
            i = int(key[1:])
            at = offs[i] if i < len(offs) else 0
        elif key.startswith('off'):
            at = int(key[3:])
        else:
            continue
        if not at or at >= len(blob):
            continue
        end = blob.find(b'\x00', at)
        if end >= 0:
            out[key] = blob[at:end]
    return out


def strings(blob):
    """[Entry] for one DSARC member; empty when it carries no text.

    A key is stable across a rebuild, which is what lets a translation be
    matched back to its slot after the bytes have moved: `s<index>` names a
    table entry, whose index does not move when the body is repacked, and
    `off<offset>` names a record field, which does not move at all.

    `budget` is None for a table entry, because a relocatable string has no
    length limit.
    """
    tab = table(blob)
    if tab:
        _base, offs = tab
        out = []
        seen = set()
        for i, off in enumerate(offs):
            if not off or off in seen:
                continue
            end = blob.find(b'\x00', off)
            s = _text(blob[off:end])
            if s is None or not _japanese(s):
                continue
            seen.add(off)
            out.append(Entry('s%d' % i, s, None))
        return out
    return [Entry('off%d' % s.offset, s.text, s.budget)
            for s in record_slots(blob)]


def build(blob, edits):
    """`blob` with `edits` ({key: encoded bytes}) applied.

    Applying no edits returns the input unchanged. That identity is the test
    this module has to pass before it may be trusted to rewrite a member of a
    file the game cannot reload if it is wrong.
    """
    tab = table(blob)
    if tab:
        return _build_table(blob, tab, edits)
    return _build_records(blob, edits)


def _build_records(blob, edits):
    out = bytearray(blob)
    for slot in record_slots(blob):
        new = edits.get('off%d' % slot.offset)
        if new is None:
            continue
        if len(new) > slot.budget:
            raise DbError('%d bytes at %#x exceed the %d the field holds'
                          % (len(new), slot.offset, slot.budget))
        end = blob.find(b'\x00', slot.offset)
        # Never clear past the budget. The zero run after a string can reach into
        # the next field, and blanking it there would wipe a neighbouring name -
        # the budget is the field boundary, so it bounds the write as well.
        room = min(_zeros_end(blob, end), slot.offset + slot.budget + 1)
        out[slot.offset:room] = new + b'\x00' * (room - slot.offset - len(new))
    return bytes(out)


def _build_table(blob, tab, edits):
    base, offs = tab
    body = {}
    for off in offs:
        if not off:
            continue
        end = blob.find(b'\x00', off)
        body.setdefault(off, blob[off:end])
    for i, off in enumerate(offs):
        new = edits.get('s%d' % i)
        if new is not None and off:
            body[off] = new
    packed = bytearray()
    at = {}
    for off in sorted(body):
        at[off] = base + len(packed)
        packed += body[off] + b'\x00'
    head = bytearray()
    for off in offs:
        head += struct.pack('<I', at[off] if off else 0)
    if len(head) != base:
        raise DbError('table head is %d bytes but the base says %d'
                      % (len(head), base))
    return bytes(head) + bytes(packed)

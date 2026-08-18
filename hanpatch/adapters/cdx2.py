"""Classic Dungeon X2 (PSP) — disc adapter.

The container stack, outermost first, each layer proved by rebuilding the real
thing byte for byte:

    ISO 9660 image                  iso9660.py
      PSP_GAME/USRDIR/DATA.DAT      pspfs.py    547 files, 371 MB
        SCRIPT.SDT                  sdt.py      9 IMY blocks -> one payload
          DSARC FL archive          dsarc.py    4 members
            SCRIPT.TBL / .DAT       dsf.py      1663 chunks, 4587 records
        FONT1.ARC / FONT2.ARC       font.py     2725 glyphs in 3072 cells

Two facts drive everything this adapter does differently from a 3DS one.

**Korean travels as Shift-JIS.** The font maps a code to a cell by the code's
position in FONT.BIN, so a Hangul glyph lives in a cell that used to hold a
kanji and keeps that kanji's code. Encoding a translated line therefore means
substituting each Hangul syllable for the code of the cell it was baked into.
`work/font_map.json` is that mapping and this adapter refuses to inject without
it, because a line encoded against a font that was never built renders as the
Japanese the cells still hold - which looks like a translation that silently did
not apply.

**A grown payload must be declared.** PSPFS records a decompressed size per file
and the loader allocates from it, so rewriting SCRIPT.SDT means recomputing that
number. `pspfs.build` refuses to take a compressed file without one rather than
carrying the stale value forward.
"""
import json
import os
import struct

from hanpatch import adapter
from hanpatch import config
from hanpatch.platforms.psp import dsarc
from hanpatch.platforms.psp import dsf
from hanpatch.platforms.psp import font as fontmod
from hanpatch.platforms.psp import iso9660
from hanpatch.platforms.psp import pspfs
from hanpatch.platforms.psp import sdt

DATA = '/PSP_GAME/USRDIR/DATA.DAT'
SCRIPT = 'SCRIPT.SDT'
PAIRS = (('SCRIPT.TBL', 'SCRIPT.DAT'), ('AISCRIPT.TBL', 'AISCRIPT.DAT'))
FONT_ARCHIVES = ('FONT1.ARC', 'FONT2.ARC')
FONT_MAP = 'font_map.json'


class EncodeError(Exception):
    pass


def _sjis_ok(ch):
    try:
        ch.encode('shift_jis')
        return True
    except UnicodeEncodeError:
        return False


def load_font_map(work=None):
    path = os.path.join(work or config.work(), FONT_MAP)
    if not os.path.isfile(path):
        raise EncodeError(
            'no %s; run tools/cdx2_font.py first. Injecting without it would '
            'encode Korean against cells that still hold Japanese glyphs, '
            'which renders as untranslated text rather than as an error'
            % path)
    with open(path) as fh:
        doc = json.load(fh)
    return {ch: int(code) for ch, code in doc['map'].items()}


#: Characters a translator reaches for that this font does not hold, mapped to
#: the mark it DOES hold. Not a convenience: the middle dot was swept out of the
#: corpus once by hand and the next retranslation put it straight back, because
#: nothing enforced it. An equivalence belongs where the bytes are written, so it
#: cannot be undone by the next pass.
#:
#: Each entry is the same mark in a different codepoint, verified present in the
#: shipped font - never a substitution that changes what the reader sees.
EQUIVALENT = {
    '\u00b7': '\u30fb',      # MIDDLE DOT -> KATAKANA MIDDLE DOT (advance 5)
    '\u2022': '\u30fb',      # BULLET
    '\uff65': '\u30fb',      # HALFWIDTH KATAKANA MIDDLE DOT
    '\u2013': '-',           # EN DASH -> HYPHEN-MINUS
    '\u2014': '-',           # EM DASH
    '\u2026': '\u2026',      # HORIZONTAL ELLIPSIS, kept explicit for the audit
}


def normalise(text):
    """Replace marks the font lacks with the identical mark it carries."""
    return ''.join(EQUIVALENT.get(c, c) for c in text)


def encode_line(text, hangul):
    """Shift-JIS bytes for a translated line, Hangul via retargeted cells."""
    out = bytearray()
    for ch in normalise(text):
        code = hangul.get(ch)
        if code is not None:
            out += bytes([(code >> 8) & 0xFF, code & 0xFF]) if code > 0xFF \
                else bytes([code])
            continue
        try:
            raw = ch.encode('shift_jis')
        except UnicodeEncodeError:
            raise EncodeError('%r is neither in the font map nor Shift-JIS'
                              % ch)
        if b'\x00' in raw:
            raise EncodeError('%r encodes to a NUL' % ch)
        out += raw
    return bytes(out)


def decode_line(raw, hangul):
    """The inverse, for verification: read a stored line back as text."""
    back = {code: ch for ch, code in hangul.items()}
    out = []
    i = 0
    while i < len(raw):
        b = raw[i]
        if (0x81 <= b <= 0x9F or 0xE0 <= b <= 0xEF) and i + 1 < len(raw):
            code = (b << 8) | raw[i + 1]
            if code in back:
                out.append(back[code])
            else:
                out.append(raw[i:i + 2].decode('shift_jis', 'replace'))
            i += 2
            continue
        if b in back:
            out.append(back[b])
        else:
            out.append(bytes([b]).decode('shift_jis', 'replace'))
        i += 1
    return ''.join(out)


@adapter.register('cdx2')
class ClassicDungeonX2(adapter.Adapter):
    platform = 'psp'

    # -- helpers ---------------------------------------------------------

    def _archive(self, rom):
        """(pspfs, mmap, file handle) over the disc's DATA.DAT."""
        import mmap
        with iso9660.Iso.from_path(rom) as iso:
            entry = iso.find(DATA)
            if entry is None:
                raise SystemExit('%s holds no %s' % (rom, DATA))
            base, size = entry.offset, entry.size
        fh = open(rom, 'rb')
        buf = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        return pspfs.Pspfs(buf[base:base + size]), buf, fh

    def _scripts(self, blob):
        """{data member: Script} for every table/data pair in SCRIPT.SDT."""
        box = sdt.Sdt(blob)
        arc = dsarc.Dsarc(box.payload)
        pairs = {}
        for table, data in PAIRS:
            pairs[data] = dsf.Script(arc.read(table), arc.read(data))
        return box, arc, pairs

    @staticmethod
    def _record_keys(script):
        """{(chunk index, record start): 'chunkid:ordinal'}.

        A record's BYTE OFFSET is not a stable identity: rewriting a line to a
        different length moves every record after it, so a key built from the
        offset stops resolving in the very file the patch produced. Verify then
        reports "no record in the built ROM" for text that is present and
        correct - measured here as 4192 false problems out of 4587 entries.

        The ordinal within the chunk does not move, so that is the key.
        """
        out = {}
        counts = {}
        for record in script.records():
            chunk = script.chunks[record.chunk]
            n = counts.get(record.chunk, 0)
            counts[record.chunk] = n + 1
            out[(record.chunk, record.start)] = '%d:%d' % (chunk.id, n)
        return out

    @staticmethod
    def _family_of(chunk):
        """A family name is ONE filename component, never a path.

        The core shards its state as work/<lang>/<kind>_<family>.json and merges
        the shards with a NON-recursive glob. A family carrying a separator
        therefore writes into a subdirectory that the merge never reads: 40
        families' translations were invisible to `tm.load()`, so every pass
        retranslated them and reported progress that did not move.
        """
        return chunk.name.replace('\\', '__')

    # -- extract ---------------------------------------------------------

    def extract(self, rom):
        adapter.require(rom, 'ROM')
        out = config.extracted()
        os.makedirs(out, exist_ok=True)
        fs, buf, fh = self._archive(rom)
        try:
            for entry in fs:
                with open(os.path.join(out, entry.name), 'wb') as sink:
                    sink.write(fs.read(entry.name))
            blob = fs.read(SCRIPT)
        finally:
            buf.close()
            fh.close()

        _box, _arc, pairs = self._scripts(blob)
        src = {}
        for script in pairs.values():
            keys = self._record_keys(script)
            for record in script.records():
                chunk = script.chunks[record.chunk]
                src.setdefault(self._family_of(chunk), []).append({
                    'key': keys[(record.chunk, record.start)],
                    'en': record.text.decode('shift_jis'),
                    'jp': '',
                })
        os.makedirs(config.work(), exist_ok=True)
        with open(config.src_path(), 'w') as sink:
            json.dump(src, sink, ensure_ascii=False, indent=1)
        rows = sum(len(v) for v in src.values())
        with open(config.work('extract.json'), 'w') as sink:
            json.dump({'files': len(fs), 'families': len(src), 'records': rows},
                      sink, indent=1)
        print('extract: %d files, %d families, %d records'
              % (len(fs), len(src), rows))
        return rows

    # -- inject ----------------------------------------------------------

    def _rewrite_script(self, blob, entries, hangul):
        box, arc, pairs = self._scripts(blob)
        applied = missing = 0
        rebuilt = {}
        for data, script in pairs.items():
            keys = self._record_keys(script)
            edits = {}
            for record in script.records():
                chunk = script.chunks[record.chunk]
                key = '%s/%s' % (self._family_of(chunk),
                                 keys[(record.chunk, record.start)])
                text = entries.get(key)
                if not text:
                    missing += 1
                    continue
                edits[record.key] = encode_line(text, hangul)
                applied += 1
            table, body = script.build(edits)
            for name, value in zip(PAIRS[list(pairs).index(data)],
                                   (table, body)):
                rebuilt[name] = value
        members = [(m.name, rebuilt.get(m.name, arc.read(m.name)))
                   for m in arc]
        payload = dsarc.build(members, arc.reserved)
        return box.build(payload), len(payload), applied, missing

    def inject(self, entries, rom, out):
        adapter.require(rom, 'ROM')
        hangul = load_font_map()
        fs, buf, fh = self._archive(rom)
        try:
            blob = fs.read(SCRIPT)
            new, payload, applied, missing = self._rewrite_script(
                blob, entries, hangul)
            replace = {SCRIPT: new}
            for name in FONT_ARCHIVES:
                built = config.work(name)
                if os.path.isfile(built):
                    with open(built, 'rb') as src:
                        replace[name] = src.read()
            data = fs.build(replace, {SCRIPT: payload})
        finally:
            buf.close()
            fh.close()
        iso9660.write(rom, out, {DATA: data})
        print('inject: %d strings applied, %d left as source, fonts %s'
              % (applied, missing,
                 ', '.join(n for n in FONT_ARCHIVES if n in replace) or 'none'))
        return out

    # -- verify ----------------------------------------------------------

    def verify(self, rom, entries):
        adapter.require(rom, 'built ROM')
        hangul = load_font_map()
        problems = []
        fs, buf, fh = self._archive(rom)
        try:
            blob = fs.read(SCRIPT)
            fonts = {name: fontmod.Font(fs.read(name))
                     for name in FONT_ARCHIVES if name in fs.names()}
        finally:
            buf.close()
            fh.close()

        _box, _arc, pairs = self._scripts(blob)
        found = {}
        for script in pairs.values():
            keys = self._record_keys(script)
            for record in script.records():
                chunk = script.chunks[record.chunk]
                key = '%s/%s' % (self._family_of(chunk),
                                 keys[(record.chunk, record.start)])
                found[key] = record.text
        # A syllable with no cell cannot be encoded at all, so it must be
        # reported as a problem rather than raised: with the glyph authority at
        # build time this is the check that carries the whole weight, and a
        # traceback out of verify would stop at the FIRST bad row instead of
        # listing every one that needs rewording.
        unmappable = set()
        for key, text in entries.items():
            stored = found.get(key)
            if stored is None:
                problems.append('%s: no record in the built ROM' % key)
                continue
            try:
                wanted = encode_line(text, hangul)
            except EncodeError as exc:
                bad = {c for c in text
                       if c not in hangul and not _sjis_ok(c)}
                unmappable |= bad
                problems.append('%s: %s' % (key, exc))
                continue
            if stored != wanted:
                problems.append('%s: stored bytes differ from the sealed text'
                                % key)
        if unmappable:
            problems.append('%d syllable(s) have no cell in the built font: %s'
                            % (len(unmappable),
                               ''.join(sorted(unmappable))[:40]))

        # a glyph is renderable because the built font holds it, not because it
        # is in a Unicode range
        needed = {c for text in entries.values() for c in text
                  if c in hangul}
        blank = bytes(fontmod.CELL * fontmod.CELL)
        for name, face in fonts.items():
            by_code = {g.code: g for g in face.glyphs}
            for ch in sorted(needed):
                glyph = by_code.get(hangul[ch])
                if glyph is None:
                    problems.append('%s: no cell for %r' % (name, ch))
                elif face.read(glyph) == blank:
                    problems.append('%s: cell for %r is blank' % (name, ch))
        print('verify: %d strings, %d syllables against %d fonts, %d problems'
              % (len(entries), len(needed), len(fonts), len(problems)))
        return problems

    def font_paths(self):
        return (['extract/FONT1.ARC', 'extract/FONT2.ARC'],
                ['work/FONT1.ARC', 'work/FONT2.ARC'])

    def font_metrics(self, blob):
        """Measure against the shipped font, including retargeted cells.

        A retargeted cell keeps the advance of the glyph it replaced, so the
        map has to be folded in or every Hangul syllable measures as unknown
        and the layout gate falls back to a default width it never verified.
        """
        try:
            hangul = load_font_map()
        except EncodeError:
            hangul = None
        return fontmod.Metrics(blob, hangul)

    def font_coverage(self, paths):
        """Every character the built fonts render, read back off the files.

        A syllable is renderable because a cell holds it, so the answer is the
        intersection across the built archives: a glyph present in one font and
        not the other would render in some screens and not others. Retargeted
        cells are matched by code through the font map, since their stored code
        is still the kanji's.
        """
        hangul = load_font_map()
        blank = bytes(fontmod.CELL * fontmod.CELL)
        sets = []
        for path in paths:
            with open(path, 'rb') as fh:
                face = fontmod.Font(fh.read())
            by_code = {g.code: g for g in face.glyphs}
            cov = {g.char for g in face.glyphs
                   if g.char is not None and face.read(g) != blank}
            cov |= {ch for ch, code in hangul.items()
                    if code in by_code and face.read(by_code[code]) != blank}
            sets.append(cov)
        return set.intersection(*sets) if sets else set()

    def recipe_facts(self):
        return {
            'platform': 'psp',
            'container': 'iso9660/pspfs/sdt/dsarc/dsf',
            'text': 'inline length-prefixed records in script bytecode',
            'encoding': 'shift_jis with Hangul on retargeted font cells',
        }

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
import re
import struct

from hanpatch import adapter
from hanpatch import config
from hanpatch.platforms.psp import dbtbl
from hanpatch.platforms.psp import dsarc
from hanpatch.platforms.psp import dsf
from hanpatch.platforms.psp import eboot as ebootmod
from hanpatch.platforms.psp import font as fontmod
from hanpatch.platforms.psp import iso9660
from hanpatch.platforms.psp import mpb
from hanpatch.platforms.psp import pspfs
from hanpatch.platforms.psp import sdt

DATA = '/PSP_GAME/USRDIR/DATA.DAT'
SCRIPT = 'SCRIPT.SDT'
DATABASE = 'DATABASE.DAT'
PAIRS = (('SCRIPT.TBL', 'SCRIPT.DAT'), ('AISCRIPT.TBL', 'AISCRIPT.DAT'))
FONT_ARCHIVES = ('FONT1.ARC', 'FONT2.ARC')

#: The title-screen logo. It is pixel art, not font-rendered text, so no amount
#: of text translation touches it - it is replaced as an image or it ships
#: Japanese. `LOGO_PNG` in the work directory is the hand-drawn replacement; when
#: it is absent the original is passed through untouched.
LOGO_MEMBER = 'TITLE_LOGO.MPB'
LOGO_PNG = 'TITLE_LOGO_ko.png'
FONT_MAP = 'font_map.json'

#: Files the INJECTOR reads, which means they are read on the recipient's machine
#: too. A path that only exists where the build ran makes a bundle that cannot be
#: applied - the lesson `release.PAYLOAD_KEYS` already records for DQ7 assets -
#: so each of these is declared in the profile under `build_inputs`, travels in
#: the bundle, and is resolved through the profile rather than through a fixed
#: work path. `build_input` falls back to the work directory so a local build,
#: which is where these files are produced, keeps working unchanged.
BUILD_INPUTS = 'build_inputs'


def build_input(name):
    """Path to an injector input, as the profile resolves it, or None."""
    declared = (config.prof(BUILD_INPUTS) or {}).get(name)
    path = config.p(declared) if declared else config.work(name)
    return path if os.path.isfile(path) else None

#: Family-name prefix for `DATABASE.DAT` members, so one glance at a key says
#: which domain it belongs to and the two can never collide.
DB_PREFIX = 'db__'

#: A text record whose source NAMES A FILE ON THE DISC is not a line: it is an
#: operand the loader resolves by name. `story\demo1001_00.dsf` starts the
#: prologue by passing `OPENING.LDT` to it, and `dsf.scan` cannot tell that
#: record from dialogue - both are a length-prefixed Shift-JIS string sitting
#: inline in the bytecode.
#:
#: Translating one is fatal, not cosmetic. The 2026-08-20 build had `OPENING.LDT`
#: come back as Hangul, the loader was asked for a member that does not exist,
#: and the prologue wrote through the null it got: New Game died a few seconds
#: after the BGM prompt. 29 records were hit - `OPENING.LDT`, `ANMPARTY.LDT` and
#: `ANMVITER.LDT` across nine story chunks.
#:
#: The test is membership of the archive's own name list, not the shape of the
#: string. A shape rule cannot be made safe here: every ASCII-only spelling it
#: would have to catch is also legal dialogue on this disc, and the corpus holds
#: `3`, `HP/MP` and `...` as records the player reads. Measured on the Japanese
#: image: 66 of 4587 script records name an archive member and none of them is
#: prose.
#:
#: Applied on both sides, always against the SOURCE: `extract` does not offer
#: these records, and `inject` refuses them even when the translation memory
#: already holds one, because a corpus sealed before this rule existed still
#: carries all 29.
def is_operand(text, assets):
    """True when a record's source names a file the disc carries."""
    return text in assets


#: Kana, kanji, or a Latin letter in either width. A source with none of those
#: holds nothing a translator can act on: `④＋５、⑤－１` is an equipment effect
#: written entirely in the game's own symbols, and `？？？` is a placeholder. They
#: are not defects and they are not work - offering them makes the coverage gate
#: demand a translation that would only make the line worse, and the two
#: "translations" the corpus did have for them just narrowed fullwidth marks the
#: font draws properly (`？？？` -> `???`).
#:
#: A letter IS enough to make a row work: `ＳＤＥＦ－１０` becomes `마법방어-10`
#: and `ＤＪ` becomes `디제이`, so the test is letters rather than "no kana".
LETTERS = re.compile(r'[\u3040-\u30ff\u4e00-\u9fffA-Za-z\uff21-\uff3a\uff41-\uff5a]')


def is_translatable(text):
    """False when a source line holds no letters in any script."""
    return bool(LETTERS.search(text))

#: Player-visible text compiled into the executable: the fourth title-menu item,
#: the install and memory-stick messages, and the one-line help under most
#: options. The disc ships `EBOOT.BIN` under the `~PSP` encryption header, so
#: none of it is reachable by scanning the image - measured on this disc,
#: `インストール` returns 0 hits across the whole ISO in three encodings and 0
#: across all 547 archive members, while being on screen the whole time.
#:
#: `EBOOT_ELF` is the decrypted executable, a declared build input rather than
#: something derived from the ROM: this build cannot decrypt `~PSP`. When it is
#: absent the executable is passed through untouched and its text ships Japanese,
#: the same rule the logo follows. When it is present the patched ELF is written
#: into the image AS `EBOOT.BIN` - a patched ISO is already unsigned, so a
#: plain-ELF boot image costs nothing this patch has not already spent, and both
#: CFW and the emulators load it. Proven on the device: the fourth menu item
#: renders `설지`.
EBOOT_MEMBER = '/PSP_GAME/SYSDIR/EBOOT.BIN'
EBOOT_ELF = 'EBOOT.elf'
EBOOT_FAMILY = 'eboot.elf'

#: {family/key: bytes writable there} for the database, written by `extract` and
#: read by the layout gate. A record-table field cannot grow, so the budget is
#: the only thing standing between a translation and a corrupted stat - and it is
#: a property of the ROM, not of the translation, so it is derived once here
#: rather than recomputed by every caller that validates a line.
DB_BUDGET = 'db_budget.json'


class EncodeError(Exception):
    pass


#: Bytes for one source character, or None when this font cannot hold it.
#:
#: `shift_jis` is tried first so every byte this project has already shipped comes
#: out identical. `cp932` is the fallback for exactly one reason: the NEC row. The
#: disc writes its icon glyphs there - `弱点⑥` is stored as `8e e3 93 5f 87 45` -
#: and Python's `shift_jis` codec cannot encode ①-⑳, Ⅰ-Ⅹ, ㍻ or №, while `cp932`
#: encodes them to the same bytes the ROM already holds (verified: `⑥` -> 87 45,
#: which is what BODY.DAT contains). Reading was fixed first and writing was not,
#: so the build died on `'⑥' is neither in the font map nor Shift-JIS` with a green
#: gate behind it - the corpus could hold a line the injector could not store.
def _encode_char(ch):
    for codec in ('shift_jis', 'cp932'):
        try:
            return ch.encode(codec)
        except UnicodeEncodeError:
            continue
    return None


def _sjis_ok(ch):
    return _encode_char(ch) is not None


def load_font_map(work=None):
    path = (os.path.join(work, FONT_MAP) if work
            else build_input(FONT_MAP) or config.work(FONT_MAP))
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
    # NOT the katakana middle dot, though the font holds it and it is the same
    # mark: it is kana, `audit` rejects kana left in Korean output, and this
    # mapping runs AFTER sealing - so it would put kana in the ROM at the one
    # point no gate is looking. A comma separates coordinate nouns in Korean and
    # is in the font at advance 4.
    '\u00b7': ',',            # MIDDLE DOT
    '\u2022': ',',            # BULLET
    '\uff65': ',',            # HALFWIDTH KATAKANA MIDDLE DOT
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
        raw = _encode_char(ch)
        if raw is None:
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
    def _database(blob):
        """(sdt box, dsarc) over DATABASE.DAT - the same stack as the script."""
        box = sdt.Sdt(blob)
        return box, dsarc.Dsarc(box.payload)

    @classmethod
    def _db_rows(cls, blob):
        """{family: [(key, japanese, budget)]} for every member that has text."""
        _box, arc = cls._database(blob)
        out = {}
        for member in arc:
            rows = dbtbl.strings(arc.read(member.name))
            if rows:
                out[DB_PREFIX + member.name] = rows
        return out

    @classmethod
    def _db_stored(cls, blob, entries):
        """{family: {key: stored bytes}} for the DB keys `entries` mentions.

        Keyed off the sealed entries rather than off a fresh scan, because the
        member being read has already been translated: a scan that tests for
        Japanese would miss every Korean line it is supposed to confirm.
        """
        wanted = {}
        for key in entries:
            family, _, leaf = key.partition('/')
            if family.startswith(DB_PREFIX) and leaf:
                wanted.setdefault(family, []).append(leaf)
        if not wanted:
            return {}
        _box, arc = cls._database(blob)
        out = {}
        for member in arc:
            family = DB_PREFIX + member.name
            keys = wanted.get(family)
            if keys:
                out[family] = dbtbl.stored(arc.read(member.name), keys)
        return out

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
            assets = set(fs.names())
        finally:
            buf.close()
            fh.close()

        _box, _arc, pairs = self._scripts(blob)
        src = {}
        operands = 0
        symbols = 0
        for script in pairs.values():
            keys = self._record_keys(script)
            for record in script.records():
                chunk = script.chunks[record.chunk]
                text = record.text.decode('shift_jis')
                if is_operand(text, assets):
                    operands += 1
                    continue
                if not is_translatable(text):
                    symbols += 1
                    continue
                src.setdefault(self._family_of(chunk), []).append({
                    'key': keys[(record.chunk, record.start)],
                    'en': text,
                    'jp': '',
                })
        # The interface is a second text domain and it is NOT optional: a patch
        # that extracts only the script ships a game whose menus, item names and
        # name-entry pool are untouched Japanese. Its byte budgets are written
        # beside the corpus because a record-table string cannot grow.
        budgets = {}
        for family, rows in self._db_rows(fs.read(DATABASE)).items():
            for row in rows:
                if not is_translatable(row.jp):
                    symbols += 1
                    continue
                src.setdefault(family, []).append({
                    'key': row.key, 'en': row.jp, 'jp': '',
                })
                if row.budget is None:
                    continue
                # Keyed by SOURCE, not by slot, because that is how a
                # translation is stored: one Korean line serves every slot
                # holding the same Japanese. The binding limit is therefore the
                # SMALLEST of those slots - honouring the average would write a
                # name that fits the roomiest field and overruns the tightest.
                seen = budgets.setdefault(family, {})
                prior = seen.get(row.jp)
                seen[row.jp] = (row.budget if prior is None
                                else min(prior, row.budget))
        # The executable's own prose is a third domain, and it belongs in the
        # corpus for the same reason the interface does: without it the fourth
        # title-menu item and the option help ship Japanese. It is keyed by file
        # offset because one Japanese string occupies several slots, which is the
        # key `inject` and `verify` already resolve. A project with no decrypted
        # ELF declared simply has no such rows - and until this was written here,
        # a re-extract silently dropped the 465 that a local pass had put in the
        # corpus by hand.
        eboot = build_input(EBOOT_ELF)
        if eboot:
            with open(eboot, 'rb') as fh:
                elf = fh.read()
            for off, _raw, text in ebootmod.strings(elf):
                src.setdefault(EBOOT_FAMILY, []).append({
                    'key': 'off%x' % off, 'en': text, 'jp': '',
                })
        os.makedirs(config.work(), exist_ok=True)
        with open(config.src_path(), 'w') as sink:
            json.dump(src, sink, ensure_ascii=False, indent=1)
        with open(config.work(DB_BUDGET), 'w') as sink:
            json.dump(budgets, sink, indent=1)
        rows = sum(len(v) for v in src.values())
        with open(config.work('extract.json'), 'w') as sink:
            json.dump({'files': len(fs), 'families': len(src), 'records': rows},
                      sink, indent=1)
        budgeted = sum(len(v) for v in budgets.values())
        print('extract: %d files, %d families, %d records (%d byte-budgeted, '
              '%d engine operands and %d symbol-only lines held back)'
              % (len(fs), len(src), rows, budgeted, operands, symbols))
        return rows

    # -- inject ----------------------------------------------------------

    def _rewrite_script(self, blob, entries, hangul, assets):
        box, arc, pairs = self._scripts(blob)
        applied = missing = 0
        rebuilt = {}
        for data, script in pairs.items():
            keys = self._record_keys(script)
            edits = {}
            for record in script.records():
                chunk = script.chunks[record.chunk]
                if is_operand(record.text.decode('shift_jis'), assets):
                    continue
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

    def _rewrite_db(self, blob, entries, hangul):
        """DATABASE.DAT with the interface translated, and its payload length.

        A record-table member is rewritten in place and a string table is
        repacked, both by `dbtbl.build`. An over-budget line raises rather than
        silently truncating: a clipped item name is a bug the player sees, and a
        field that overran its neighbour is a corrupted stat nothing downstream
        would catch.
        """
        box = sdt.Sdt(blob)
        arc = dsarc.Dsarc(box.payload)
        applied = missing = 0
        members = []
        for member in arc:
            raw = arc.read(member.name)
            family = '%s%s' % (DB_PREFIX, member.name)
            edits = {}
            for row in dbtbl.strings(raw):
                text = entries.get('%s/%s' % (family, row.key))
                if not text:
                    missing += 1
                    continue
                edits[row.key] = encode_line(text, hangul)
                applied += 1
            members.append((member.name,
                            dbtbl.build(raw, edits) if edits else raw))
        payload = dsarc.build(members, arc.reserved)
        return box.build(payload), len(payload), applied, missing

    @staticmethod
    def _rewrite_eboot(entries, hangul):
        """The decrypted ELF with its Korean written in, or None when absent.

        The corpus keys these rows by SLOT OFFSET, because the same Japanese
        string occupies several slots and a key must name a place in the file.
        One Korean string still serves every slot sharing a source - that is
        already what the byte budget assumes, since it is the minimum over
        those slots - so the lookup collapses back to source text before the
        rewrite.

        Returns a 3-tuple even when the ELF is absent: a project without the
        decrypted executable declared still builds, it just ships the
        executable's own prose in Japanese.
        """
        path = build_input(EBOOT_ELF)
        if not path:
            return None, 0, 0
        with open(path, 'rb') as fh:
            blob = fh.read()
        by_source = {}
        missing = 0
        for off, _raw, text in ebootmod.strings(blob):
            ko = entries.get('%s/off%x' % (EBOOT_FAMILY, off))
            if ko:
                by_source[text] = ko
            else:
                missing += 1
        new, applied = ebootmod.build(blob, by_source,
                                      lambda s: encode_line(s, hangul))
        return new, applied, missing

    @staticmethod
    def _eboot_stored(rom):
        """{key: stored bytes} for every executable slot in the BUILT image.

        The offsets come from the SOURCE ELF and the bytes from the built one,
        which is the only order that works: a patched slot holds the retargeted
        Shift-JIS codes, and those decode as kanji rather than kana, so the
        recogniser that found the Japanese would skip every Korean slot and
        verify would report the whole domain missing from a correct build.
        """
        path = build_input(EBOOT_ELF)
        if not path:
            return {}
        with open(path, 'rb') as fh:
            src = fh.read()
        with iso9660.Iso.from_path(rom) as iso:
            entry = iso.find(EBOOT_MEMBER)
            if entry is None:
                return {}
            built = iso.read(entry)
        out = {}
        for off, raw, _text in ebootmod.strings(src):
            slot = built[off:off + len(raw)]
            out['%s/off%x' % (EBOOT_FAMILY, off)] = slot.split(b'\x00', 1)[0]
        return out

    @staticmethod
    def _rewrite_logo(blob, png):
        """`blob` with `png` painted into its atlas.

        The title logo is pixel art, not font-rendered text, so no amount of
        translating strings touches it: a patch that leaves it alone ships a
        Korean game whose first screen is Japanese. The replacement is authored
        by hand and dropped in as a PNG.

        The size is checked here rather than trusted, because the engine reads
        the picture's bounds from the MPB header: a replacement of the wrong size
        is drawn clipped or stretched, and nothing downstream would report it.
        """
        from PIL import Image
        with Image.open(png) as im:
            im = im.convert('RGBA')
            rows = [[im.getpixel((x, y)) for x in range(im.width)]
                    for y in range(im.height)]
        return mpb.encode(blob, rows)

    def inject(self, entries, rom, out):
        adapter.require(rom, 'ROM')
        hangul = load_font_map()
        fs, buf, fh = self._archive(rom)
        try:
            blob = fs.read(SCRIPT)
            new, payload, applied, missing = self._rewrite_script(
                blob, entries, hangul, set(fs.names()))
            replace = {SCRIPT: new}
            sizes = {SCRIPT: payload}
            db_new, db_payload, db_applied, db_missing = self._rewrite_db(
                fs.read(DATABASE), entries, hangul)
            replace[DATABASE] = db_new
            sizes[DATABASE] = db_payload
            applied += db_applied
            missing += db_missing
            # The built fonts are profile-declared output (`font_out`), and
            # `release.apply` rewrites those paths to the copies inside the
            # bundle. Reading them through the profile is what makes a bundle
            # applicable on a machine that never ran `hanpatch fonts`.
            for name, declared in zip(FONT_ARCHIVES,
                                      config.prof('font_out') or ()):
                built = config.p(declared)
                if not os.path.isfile(built):
                    built = build_input(name)
                if built:
                    with open(built, 'rb') as src:
                        replace[name] = src.read()
            # The title logo is pixel art, not text, so it cannot be translated
            # through the corpus: it is replaced only when a hand-drawn PNG is
            # present, and its absence leaves the Japanese logo untouched.
            logo = build_input(LOGO_PNG)
            if logo:
                replace[LOGO_MEMBER] = self._rewrite_logo(
                    fs.read(LOGO_MEMBER), logo)
            data = fs.build(replace, sizes)
        finally:
            buf.close()
            fh.close()
        image = {DATA: data}
        # The executable carries player-visible prose of its own - install and
        # save messages, option help, and the fourth title-menu item - and the
        # disc ships it encrypted, so it is reachable only through the decrypted
        # ELF declared as a build input. The patched ELF is written AS
        # `EBOOT.BIN`: a patched ISO is unsigned already, and both CFW and the
        # emulators boot a plain ELF. Proven on the device.
        eboot_new, eboot_applied, eboot_missing = self._rewrite_eboot(
            entries, hangul)
        if eboot_new is not None:
            image[EBOOT_MEMBER] = eboot_new
            applied += eboot_applied
            missing += eboot_missing
        iso9660.write(rom, out, image)
        print('inject: %d strings applied (%d interface, %d executable), '
              '%d left as source, fonts %s'
              % (applied, db_applied, eboot_applied, missing,
                 ', '.join(n for n in FONT_ARCHIVES if n in replace) or 'none'))
        return {
            'translated': applied,
            'total': applied + missing,
            'skipped': missing,
            'size': os.path.getsize(out),
        }

    # -- verify ----------------------------------------------------------

    def verify(self, rom, entries):
        adapter.require(rom, 'built ROM')
        hangul = load_font_map()
        problems = []
        fs, buf, fh = self._archive(rom)
        try:
            blob = fs.read(SCRIPT)
            db = self._db_stored(fs.read(DATABASE), entries)
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
        for family, rows in db.items():
            for key, raw in rows.items():
                found['%s/%s' % (family, key)] = raw
        found.update(self._eboot_stored(rom))
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

    def encoded_length(self, text):
        """Bytes `text` occupies once stored, or None before the font exists.

        A record-table field is a fixed number of BYTES, so the limit has to be
        measured in the encoding that actually lands in the ROM - Shift-JIS with
        Hangul on retargeted cells. Counting characters instead would let a
        two-byte syllable pass a budget it overruns by half.
        """
        try:
            hangul = load_font_map()
        except EncodeError:
            return None
        try:
            return len(encode_line(text, hangul))
        except EncodeError:
            return None

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

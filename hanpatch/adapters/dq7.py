"""Dragon Quest VII (3DS) — cartridge adapter.

The message surface is 343 FPT0 archives under /MESS holding 66208 UTF-8 records
(see `formats/fpt0.py` and `formats/fpttxt.py`, whose docstrings carry the
measured populations). One source row is one RECORD: `en` is the record's display
lines joined with '\\n', which is the shape the layout gate measures, and the head
line ('#<digits>' or 'TALKER=<digits>') is machinery that never leaves the
container. Rebuilding takes the head from the source record rather than routing
it through the manifest, so no wording layer can invent one.

CONTAINER IDENTITY IS A REAL BRANCH, not a formality. The reference title is an
eShop CIA; this one is a CCI cartridge image (NCSD at +0x100, title id
0004000000065E00), so:
  * the rebuild goes through the container-agnostic `threeds.rebuild`, never
    `rebuild_cia`;
  * `content_hashes()` is CIA-only and returns [] for any other container. An
    empty list is NEVER accepted as clean here: on a cartridge `verify()` demands
    the NCCH superblock hashes, which are what actually cover a CCI, and refuses
    only when neither source of evidence exists. A verifier that reports clean
    because it had nothing to compare is worse than no verifier.

Artifacts: extraction writes the DECODED, normalised rows to `work/text_src.json`
and nothing else derived from the text. It also leaves the raw rebuild inputs
`extracted/romfs.bin` and the unpacked `extracted/romfs/` tree, whose
`MESS/*.fpt` members obviously still contain the source records - they are the
cartridge's own bytes and inject/verify rebuild from them. So "text lives only in
text_src.json" is true of decoded artifacts, not of the extraction directory.

Layering: this module must not import the wording layer (translate, glossary,
josa, providers, wrap). The test suite fails the build if it does.
"""
import json
import os
import shutil

from hanpatch import adapter
from hanpatch import config
from hanpatch import tm
from hanpatch.formats import darc
from hanpatch.formats import fpt0
from hanpatch.formats import fpttxt
from hanpatch.platforms import threeds
from hanpatch.platforms.threeds import keys as keysmod
from hanpatch.platforms.threeds.bcfnt import Bcfnt

MESS_DIR = 'MESS'
LAYOUT_DIR = 'LAYOUT'
ARC_EXT = '.arc'
FONT_EXT = '.bcfnt'
FPT_EXT = '.fpt'
TEXT_EXT = '.txt'


@adapter.register('dq7')
class DragonQuest7(adapter.Adapter):
    platform = 'threeds'

    # -- paths --------------------------------------------------------------

    @property
    def romfs_bin(self):
        return config.extracted('romfs.bin')

    @property
    def romfs_dir(self):
        return config.extracted('romfs')

    def mess_dir(self):
        return os.path.join(self.romfs_dir, MESS_DIR)

    def _keystore(self):
        """A cartridge NCCH is encrypted; the CIA reference path never needed this."""
        return keysmod.store()

    def archives(self):
        """Every DARC archive under /LAYOUT, sorted."""
        d = os.path.join(self.romfs_dir, LAYOUT_DIR)
        adapter.require(d, f'extracted {LAYOUT_DIR} directory (run extract first)')
        return sorted(f for f in os.listdir(d) if f.endswith(ARC_EXT))

    def build_fonts(self):
        """Generate the Korean fonts. Without this the CLI silently did nothing.

        `adapter.Adapter.build_fonts` returns [] meaning "this title has no fonts to
        build", and DQ7 inherited it, so `hanpatch fonts` printed nothing and exited 0
        while the title in fact has seven fonts across twelve archive slots. The fonts
        only ever got built because a module call was made by hand. A CLI that reports
        success for work it never did is worse than one that refuses.
        """
        from hanpatch.platforms.threeds import fontbuild
        return fontbuild.build_all()

    def font_paths(self):
        """(source fonts, built fonts) for width measurement.

        Both sides come from the profile rather than from a directory scan, because the
        built set has to correspond to the declared sources: a font measured against the
        wrong source reports the wrong advance widths, and the layout budget is derived
        from those widths.
        """
        src = [config.p(x) for x in (config.prof('font_src') or [])]
        out = [config.p(x) for x in (config.prof('font_out') or [])]
        return (src, out)

    def font_slots(self):
        """{font name: [(archive file, member path), ...]} for every shipped font.

        A font is mirrored: `tbud_maru_b12` lives in both the bundled
        system_font.arc and its own system_font12.arc, so a Korean font has to be
        written into every slot that holds it or the engine draws the old one from
        whichever archive it happens to load.
        """
        slots = {}
        for fname in self.archives():
            path = os.path.join(self.romfs_dir, LAYOUT_DIR, fname)
            with open(path, 'rb') as fh:
                blob = fh.read()
            if blob[:4] != darc.MAGIC:
                continue
            for m in darc.parse(blob, path)[1]:
                if m.is_dir or not m.path.endswith(FONT_EXT):
                    continue
                name = os.path.splitext(os.path.basename(m.path))[0]
                slots.setdefault(name, []).append((fname, m.path))
        return slots

    def containers(self):
        d = self.mess_dir()
        adapter.require(d, f'extracted {MESS_DIR} directory (run extract first)')
        return sorted(f for f in os.listdir(d) if f.endswith(FPT_EXT))

    # -- extract ------------------------------------------------------------

    def extract(self, rom):
        adapter.require(rom, 'ROM')
        out = config.extracted()
        os.makedirs(out, exist_ok=True)
        kind = threeds.detect(rom)
        threeds.dump(rom, out, keystore=self._keystore())
        threeds.unpack_romfs(self.romfs_bin, self.romfs_dir)

        src = {}
        for fname in self.containers():
            family = fname[:-len(FPT_EXT)]
            path = os.path.join(self.mess_dir(), fname)
            with open(path, 'rb') as fh:
                _hdr, entries = fpt0.parse(fh.read(), path)
            rows = []
            for e in entries:
                if not e.name.endswith(TEXT_EXT):
                    continue
                rec = fpttxt.parse(e.data, f'{fname}/{e.name}')
                rows.append({'key': e.name, 'en': rec.text, 'jp': ''})
            if rows:
                src[family] = rows

        fonts = {}
        for name, slots in sorted(self.font_slots().items()):
            arc, member = slots[0]
            with open(os.path.join(self.romfs_dir, LAYOUT_DIR, arc), 'rb') as fh:
                blob = fh.read()
            for m in darc.parse(blob, arc)[1]:
                if m.path == member:
                    out_dir = config.extracted('fonts')
                    os.makedirs(out_dir, exist_ok=True)
                    with open(os.path.join(out_dir, name + FONT_EXT), 'wb') as fh:
                        fh.write(m.data)
                    fonts[name] = {'bytes': len(m.data),
                                   'slots': [f'{a}:{p}' for a, p in slots]}
                    break
        print(f'fonts: {len(fonts)} in {len(self.archives())} archives, '
              f'{sum(len(v["slots"]) for v in fonts.values())} slots')

        os.makedirs(config.work(), exist_ok=True)
        json.dump(src, open(config.src_path(), 'w'), ensure_ascii=False, indent=1)
        records = sum(len(v) for v in src.values())
        # Recorded AND printed: the container kind decides which rebuild path runs
        # later, so an operator reading the extract log should see which one this
        # cartridge selected rather than having to infer it from a JSON nobody
        # opens.
        json.dump({'container': kind, 'containers': len(src), 'records': records,
                   'fonts': fonts},
                  open(config.work('extract.json'), 'w'), indent=1)
        print(f'container {kind}: {len(src)} message archives, {records} records')
        return records

    # -- inject -------------------------------------------------------------

    def _stage_romfs(self):
        """Mirror the extracted tree, linking everything the patch never touches.

        The RomFS is 1.4 GB and only /MESS changes, so copying the whole tree per
        build would spend minutes moving bytes that cannot differ. Symlinks are
        resolved by the RomFS writer, and /MESS itself is a real copy so the
        extracted source stays pristine.
        """
        stage = config.p('build', 'romfs')
        if os.path.exists(stage):
            shutil.rmtree(stage)
        os.makedirs(stage)
        for name in sorted(os.listdir(self.romfs_dir)):
            src = os.path.join(self.romfs_dir, name)
            dst = os.path.join(stage, name)
            if name == MESS_DIR:
                shutil.copytree(src, dst, symlinks=False)
            else:
                os.symlink(os.path.abspath(src), dst)
        return stage

    def inject(self, entries, rom, out):
        adapter.require(self.romfs_dir, 'extracted RomFS (run extract first)')
        stage = self._stage_romfs()

        left = dict(entries)
        stats = {'translated': 0, 'total': 0, 'skipped': 0}
        missing = []
        for fname in self.containers():
            family = fname[:-len(FPT_EXT)]
            path = os.path.join(self.mess_dir(), fname)
            with open(path, 'rb') as fh:
                hdr, src_entries = fpt0.parse(fh.read(), path)
            rebuilt = []
            for e in src_entries:
                if not e.name.endswith(TEXT_EXT):
                    rebuilt.append(fpt0.Entry(e.name, e.data))
                    continue
                where = f'{fname}/{e.name}'
                rec = fpttxt.parse(e.data, where)
                stats['total'] += 1
                text = rec.text
                t = left.pop(f'{family}/{e.name}', None)
                if t is not None:
                    # Edit the PARSED record in place: a fresh Record would carry
                    # no source line count and would opt itself out of the
                    # per-record geometry check.
                    lines = t.split('\n')
                    if len(lines) != len(rec.lines):
                        raise SystemExit(
                            f'INJECT BLOCKED: {where} has {len(rec.lines)} display '
                            f'lines and the translation has {len(lines)}; a window '
                            f'proven to draw {len(rec.lines)} is not evidence that '
                            f'it draws {len(lines)}')
                    rec.lines = lines
                    stats['translated'] += 1
                elif tm.is_skip(text, e.name) or not text.strip():
                    stats['skipped'] += 1
                else:
                    missing.append(f'{family}/{e.name}')
                rebuilt.append(fpt0.Entry(e.name, fpttxt.build(rec, where)))
            data = fpt0.build(hdr, rebuilt, path)
            with open(os.path.join(stage, MESS_DIR, fname), 'wb') as fh:
                fh.write(data)

        if missing:
            raise SystemExit(f'INJECT BLOCKED: {len(missing)} shippable records '
                             f'absent from the manifest, e.g. {missing[:5]}')
        if left:
            raise SystemExit(f'INJECT BLOCKED: {len(left)} manifest keys were not '
                             f'consumed, e.g. {list(left)[:5]}')

        self._apply_fonts(stage)

        image = config.p('build', 'romfs.bin')
        # Reproduce the cartridge's own entry order, captured from its own image.
        # No sort rule reproduces it - see romfs_build.sibling_order for the
        # measured breakdown - and the writer's default ASCII rule moves entries,
        # which shifts every data offset after them, so an untouched rebuild would
        # differ from the source image byte for byte.
        threeds.build_romfs(stage, image, order_from=self.romfs_bin)
        # Container-agnostic: CIA in, CIA out; CCI in, CCI out. rebuild_cia is
        # CIA-only and misparses a cartridge (repack.Cia raises struct.error on
        # this image), so the agnostic entry point is the correct call rather than
        # a stylistic preference.
        threeds.rebuild(rom, image, out, keystore=self._keystore())
        stats['size'] = os.path.getsize(out)
        stats['container'] = threeds.detect(out)
        return stats

    def _chars_absent_from_source(self, entries):
        """Characters the manifest uses that the extracted source never did.

        Counted, never printed: the caller reports how many, not which, so a
        diagnostic cannot leak prose.
        """
        src_path = config.src_path()
        if not os.path.exists(src_path):
            if entries:
                raise SystemExit(
                    f'VERIFY REFUSED: {src_path} is missing, so there is nothing to '
                    f'compare the manifest against and no way to tell which glyphs '
                    f'this build introduces. Run `hanpatch extract` first. Returning '
                    f'"no new glyphs" here would hand a Korean build a green verify '
                    f'precisely because the evidence was absent.')
            return set()
        src = config.load_object(src_path, 'the extracted source')
        have = set()
        for rows in src.values():
            for row in rows:
                have.update(row.get('en') or '')
        used = set()
        for v in entries.values():
            used.update(v)
        return {c for c in used - have if not c.isascii()}

    def _apply_fonts(self, stage):
        """Write every built font into EVERY archive slot that holds it.

        A font is mirrored across archives - `tbud_maru_b12` sits in both the
        bundled system_font.arc and its own system_font12.arc - so writing one slot
        leaves the engine free to draw the Japanese glyphs from whichever archive it
        loads. Measured on this cartridge: 7 fonts across 12 slots in 10 archives.
        """
        built = {}
        for path in config.prof('font_out'):
            p = config.p(path)
            name = os.path.splitext(os.path.basename(p))[0]
            adapter.require(p, f'built font {name}')
            with open(p, 'rb') as fh:
                built[name] = fh.read()
        if not built:
            raise SystemExit(
                'INJECT BLOCKED: the profile declares no "font_out", so this build '
                'would ship the source font and render every Korean glyph as a hole. '
                'Build the fonts first (hanpatch fonts).')

        slots = self.font_slots()
        missing = sorted(set(built) - set(slots))
        if missing:
            raise SystemExit(
                f'INJECT BLOCKED: built fonts {missing} are in no archive on this '
                f'cartridge; the profile names a font the ROM does not ship')
        written = 0
        for name, data in sorted(built.items()):
            for arc, member in slots[name]:
                dst = os.path.join(stage, LAYOUT_DIR, arc)
                with open(dst, 'rb') as fh:
                    blob = fh.read()
                with open(dst, 'wb') as fh:
                    fh.write(darc.replace(blob, member, data, where=dst))
                written += 1
        untouched = sorted(set(slots) - set(built))
        if untouched:
            print(f'fonts: {written} slots written; {len(untouched)} shipped fonts '
                  f'left untouched ({untouched})')
        else:
            print(f'fonts: {written} slots written across {len(built)} fonts')
        return written

    # -- verify -------------------------------------------------------------

    def verify(self, rom, entries):
        problems = []
        # Fonts are M4 work and the hooks are deliberately at their defaults. What
        # is NOT acceptable is letting that be silent in the one function whose
        # whole argument is that silence is worse than no verifier: with no target
        # font built, a Korean build can pass every check here and still render
        # every translated string as a missing glyph. The refusal retires itself
        # the moment the profile names a built font.
        # The test is NOT "the manifest has non-ASCII text" - the Japanese source is
        # non-ASCII and the shipped font obviously renders it. What needs a font is
        # a character the SOURCE never used, which is exactly what a translation
        # introduces.
        new_chars = self._chars_absent_from_source(entries)
        if new_chars and not config.prof('font_out'):
            raise SystemExit(
                f'VERIFY REFUSED: {rom}: the manifest introduces {len(new_chars)} '
                f'characters the source never used, and the profile declares no '
                f'"font_out", so nothing here can prove the shipped font contains '
                f'them. The DQ7 text font lives inside a darc archive that has no '
                f'reader yet (M4); until that lands, a build that adds glyphs cannot '
                f'be verified as renderable. (An identity or ASCII-only rebuild is '
                f'verifiable today, because it adds no glyph.)')
        kind = threeds.detect(rom)
        chunks = threeds.content_hashes(rom)
        if kind == 'cia':
            for c in chunks:
                if not c['ok']:
                    problems.append(f"content {c['idx']} TMD hash mismatch")
            # The TMD hashes are recomputed by the rebuild from the bytes it just
            # wrote, so they detect later damage rather than a header the build
            # wrote wrong - which is exactly the class the superblock hashes cover.
            for label, ok in threeds.superblock_hashes(
                    rom, keystore=self._keystore()).items():
                if not ok:
                    problems.append(f'ncch {label} superblock hash mismatch')
        elif chunks:
            problems.append(f'content_hashes() returned {len(chunks)} chunks for a '
                            f'{kind} container, which has no TMD to compare against')
        else:
            # THE FAIL-CLOSED OWNER. content_hashes() is CIA-only and reports []
            # for a cartridge, so accepting that silently would mean the strongest
            # integrity check in the pipeline reports clean precisely because it
            # never ran. The superblock hashes below are what actually covers a
            # CCI, so demand them explicitly instead.
            # `superblock_hashes` is the fail-closed owner of "verified nothing"
            # since it refuses an empty result itself; here we only have to make
            # sure it is CALLED, because an empty content_hashes() on a cartridge
            # means the TMD comparison never ran.
            blocks = threeds.superblock_hashes(rom, keystore=self._keystore())
            for label, ok in blocks.items():
                if not ok:
                    problems.append(f'ncch {label} superblock hash mismatch')

        work = config.work('verify')
        os.makedirs(work, exist_ok=True)
        image = threeds.dump_romfs(rom, os.path.join(work, 'romfs.bin'),
                                   keystore=self._keystore())

        by_family = {}
        for key in entries:
            fam, _, k = key.partition('/')
            by_family.setdefault(fam, {})[k] = entries[key]

        checked = 0
        for family, wanted in sorted(by_family.items()):
            member = f'/{MESS_DIR}/{family}{FPT_EXT}'
            try:
                blob = threeds.read_romfs_file(image, member)
            except KeyError:
                problems.append(f'{member} missing from the rebuilt RomFS')
                continue
            _hdr, built = fpt0.parse(blob, member)
            got = {}
            for e in built:
                if e.name.endswith(TEXT_EXT):
                    got[e.name] = fpttxt.parse(e.data, f'{member}/{e.name}').text
            for k, want in wanted.items():
                checked += 1
                if k not in got:
                    problems.append(f'{family}/{k}: record vanished')
                elif got[k] != want:
                    problems.append(f'{family}/{k}: text differs after round-trip')
        # Glyph authority, with two properties the first version lacked. (1) It
        # counts what it examined: reporting clean after checking ZERO slots is the
        # same "verified nothing therefore fine" shape the superblock check exists to
        # refuse, and it was reachable through a basename mismatch or a dropped
        # member. (2) It separates a REGRESSION from a pre-existing absence: the
        # manifest legitimately carries untranslated source rows during the whole
        # project, and a small font like iwamaru_p15 never shipped their kanji, so
        # demanding every non-ASCII character of every row would make verify red for
        # every honest intermediate state and green only after the last row lands.
        # What is a build problem: a glyph the SOURCE font had and the shipped one
        # lost, or a glyph this build INTRODUCES that is missing.
        introduced = self._chars_absent_from_source(entries)
        if config.prof('font_out'):
            names = {os.path.splitext(os.path.basename(p))[0]
                     for p in config.prof('font_out')}
            expected = {n: len(v) for n, v in self.font_slots().items() if n in names}
            examined = {n: 0 for n in expected}
            manifest_chars = {ch for v in entries.values() for ch in v
                              if ord(ch) > 0x7F}
            for arc in self.archives():
                try:
                    blob = threeds.read_romfs_file(image, f'/{LAYOUT_DIR}/{arc}')
                except KeyError:
                    problems.append(f'/{LAYOUT_DIR}/{arc} missing from the rebuilt '
                                    f'RomFS')
                    continue
                for m in darc.parse(blob, arc)[1]:
                    if m.is_dir or not m.path.endswith(FONT_EXT):
                        continue
                    name = os.path.splitext(os.path.basename(m.path))[0]
                    if name not in names:
                        continue
                    examined[name] = examined.get(name, 0) + 1
                    font = Bcfnt(m.data)
                    base_path = config.extracted('fonts', name + FONT_EXT)
                    base = (Bcfnt(open(base_path, 'rb').read())
                            if os.path.exists(base_path) else None)
                    lost = [c for c in sorted(manifest_chars)
                            if font.char_to_index(c) is None and base is not None
                            and base.char_to_index(c) is not None]
                    missing_new = [c for c in sorted(introduced)
                                   if font.char_to_index(c) is None]
                    if lost:
                        problems.append(
                            f'{arc}:{m.path}: {len(lost)} glyphs the SOURCE font had '
                            f'are absent from the shipped one')
                    if missing_new:
                        problems.append(
                            f'{arc}:{m.path}: {len(missing_new)} glyphs this build '
                            f'introduces are absent from the shipped font')
                    pre = [c for c in sorted(manifest_chars)
                           if font.char_to_index(c) is None and c not in introduced
                           and (base is None or base.char_to_index(c) is None)]
                    if pre:
                        print(f'note {arc}:{m.path}: {len(pre)} characters the '
                              f'manifest carries were never in this font either; '
                              f'pre-existing, not a regression')
            if expected and not any(examined.values()):
                raise SystemExit(
                    f'VERIFY REFUSED: {rom}: the profile declares fonts '
                    f'{sorted(names)} but not one of them was found in the rebuilt '
                    f'RomFS, so the glyph check examined nothing. Refusing to report '
                    f'a clean verify from an absence of evidence.')
            for name, want in sorted(expected.items()):
                if examined.get(name, 0) != want:
                    problems.append(
                        f'{name}: found in {examined.get(name, 0)} of {want} archive '
                        f'slots in the rebuilt RomFS')
        self.checked = checked
        return problems

    def recipe_facts(self):
        """What `FPT0` actually looks like, measured over 345 containers and
        66253 entries by the reader in `formats/fpt0.py`.

        Each entry's data offset is relative to the start of the payload
        region, and that region begins after the entry table and a 64-byte tag
        block - so the anchor is computed from the count, not from the member
        start. No union of {member_start, const, mapper} can say that.
        """
        return {
            'id': 'threeds/square-enix/dragon-quest-vii',
            'platform': 'threeds',
            'title': 'Dragon Quest VII',
            'address_spaces': [
                {'id': 'rom', 'kind': 'file'},
                {'id': 'romfs', 'kind': 'member', 'params': {'parent': 'rom'}},
                {'id': 'fpt', 'kind': 'member', 'params': {'parent': 'romfs'}},
            ],
            'tables': [
                {
                    'id': 'entries', 'space': 'fpt', 'format': 'fpt0',
                    'kind': 'offset_size', 'stride': fpt0.ENTRY, 'endian': 'little',
                    'alignment': 1, 'applies_to': [MESS_DIR, LAYOUT_DIR],
                    'at': {'kind': 'const', 'space': 'fpt', 'value': fpt0.HEADER},
                    'count': {'space': 'fpt', 'at': 8, 'width': 4, 'endian': 'little'},
                    'base': {'kind': 'after_table', 'padding': fpt0.TAG},
                    'payload': 'opaque',
                    'name_source': 'inline',
                },
            ],
            'measured': [
                "magic 'FPT0' at 0x00",
                'u32 at 0x04 is 0 in all 345 containers; refused otherwise',
                'u32 entry count at 0x08',
                'u32 version at 0x0C is 1 in all 345 containers; refused otherwise',
                'entry row 32 bytes: 16 byte ASCII name, name key, data offset, '
                'length, reserved u32 that is 0 in all 66253 entries',
                'name key = (len(name) << 24) | (poly13(name) & 0xFFFFFF), validated '
                'against all 66253 names and all 345 tag strings',
                'a 64 byte tag block sits between the table and the payloads',
                'entry data offsets are relative to the payload region, not the member',
                'residual ambiguity: every name is 10 or 11 characters, so a length '
                'byte cannot be told from a constant by observation alone',
            ],
        }

"""Crimson Shroud (3DS, eShop CIA) — reference adapter.

Text lives in `romfs:/ap4/bcs.fa`, a flat archive of MSGB message binaries plus
BCFNT fonts.  The English and Japanese builds ship the same archive with
`message/en` and `message/jp` subtrees, so extraction reads both and injection
writes the translation into both (the title picks a tree from the system
language and we want one behaviour either way).

This adapter is deliberately small: everything hard lives in the platform layer
(`platforms/threeds`) and the format readers (`formats/msgb`, `formats/fa`).
Use it as the template when adding a title.
"""
import json
import os
import shutil

from hanpatch import adapter, config, tm
from hanpatch.formats import fa
from hanpatch.formats.msgb import Msgb
from hanpatch.platforms import threeds
from hanpatch.platforms.threeds.bcfnt import Bcfnt

ARCHIVE = '/ap4/bcs.fa'
MSG_FILES = ['arms', 'arms_help', 'battle', 'common', 'dialogue', 'ending',
             'item', 'magic', 'region', 'stage1', 'system', 'title', 'unit']
FONTS = ['font_text', 'font_system']
# every place inside the archive a font is mirrored to
FONT_SLOTS = ['font/en/{name}.bcfnt', 'font/jp/{name}.bcfnt', 'font/{name}.bcfnt']


@adapter.register('crimson_shroud')
class CrimsonShroud(adapter.Adapter):
    platform = 'threeds'

    # -- paths --------------------------------------------------------------

    @property
    def fa_dir(self):
        return config.extracted('fa')

    @property
    def romfs_bin(self):
        return config.extracted('romfs.bin')

    def stage_dir(self):
        return config.p('build', 'fa')

    # -- extract ------------------------------------------------------------

    def extract(self, rom):
        adapter.require(rom, 'ROM')
        out = config.extracted()
        os.makedirs(out, exist_ok=True)
        threeds.dump(rom, out)
        threeds.unpack_romfs(self.romfs_bin, config.extracted('romfs'))
        fa.unpack(config.extracted('romfs' + ARCHIVE), self.fa_dir)

        src = {}
        for name in MSG_FILES:
            en = Msgb(open(f'{self.fa_dir}/message/en/{name}.mbin', 'rb').read())
            jp_path = f'{self.fa_dir}/message/jp/{name}.mbin'
            jp = Msgb(open(jp_path, 'rb').read()) if os.path.exists(jp_path) else None
            jp_by_key = {}
            if jp:
                jp_by_key = {k: jp.entries[i][2] for i, k in enumerate(jp.keys)}
            rows = []
            for i, key in enumerate(en.keys):
                row = {'key': key, 'en': en.entries[i][2],
                       'jp': jp_by_key.get(key, '')}
                rows.append(row)
            src[name] = rows
        os.makedirs(config.work(), exist_ok=True)
        json.dump(src, open(config.src_path(), 'w'), ensure_ascii=False, indent=1)
        return sum(len(v) for v in src.values())

    # -- inject -------------------------------------------------------------

    def inject(self, entries, rom, out):
        adapter.require(self.fa_dir, 'extracted archive (run extract first)')
        stage = self.stage_dir()
        if os.path.exists(stage):
            shutil.rmtree(stage)
        shutil.copytree(self.fa_dir, stage, symlinks=False)

        left = dict(entries)
        stats = {'translated': 0, 'total': 0, 'skipped': 0}
        missing = []
        for name in MSG_FILES:
            m = Msgb(open(f'{self.fa_dir}/message/en/{name}.mbin', 'rb').read())
            for i, key in enumerate(m.keys):
                stats['total'] += 1
                en = m.entries[i][2]
                t = left.pop(f'{name}/{key}', None)
                if t is not None:
                    m.entries[i][2] = t
                    stats['translated'] += 1
                elif tm.is_skip(en, key) or not en.strip():
                    stats['skipped'] += 1
                else:
                    missing.append(f'{name}/{key}')
            data = m.build()
            for lang in ('en', 'jp'):
                p = f'{stage}/message/{lang}/{name}.mbin'
                if os.path.exists(os.path.dirname(p)):
                    open(p, 'wb').write(data)
        if missing:
            raise SystemExit(f'INJECT BLOCKED: {len(missing)} shippable keys '
                             f'absent from the manifest, e.g. {missing[:5]}')
        if left:
            raise SystemExit(f'INJECT BLOCKED: {len(left)} manifest keys were '
                             f'not consumed, e.g. {list(left)[:5]}')

        self._apply_fonts(stage)

        romfs_stage = config.p('build', 'romfs')
        os.makedirs(f'{romfs_stage}/ap4', exist_ok=True)
        fa.build(config.extracted('romfs' + ARCHIVE), stage,
                 f'{romfs_stage}/ap4/bcs.fa')
        # the untouched siblings are large; link instead of copying
        for sub in ('movie', 'sound'):
            dst = f'{romfs_stage}/ap4/{sub}'
            src = config.extracted(f'romfs/ap4/{sub}')
            if os.path.exists(src) and not os.path.exists(dst):
                os.symlink(os.path.abspath(src), dst)

        image = config.p('build', 'romfs.bin')
        threeds.build_romfs(romfs_stage, image)
        threeds.rebuild_cia(rom, image, out)
        stats['size'] = os.path.getsize(out)
        return stats

    def _apply_fonts(self, stage):
        for name, path in zip(FONTS, [config.p(x) for x in config.prof('font_out')]):
            adapter.require(path, f'built font {name}')
            data = open(path, 'rb').read()
            for slot in FONT_SLOTS:
                dst = f'{stage}/{slot.format(name=name)}'
                if os.path.exists(os.path.dirname(dst)):
                    open(dst, 'wb').write(data)

    # -- verify -------------------------------------------------------------

    def verify(self, rom, entries):
        problems = []
        for c in threeds.content_hashes(rom):
            if not c['ok']:
                problems.append(f"content {c['idx']} TMD hash mismatch")
        for label, ok in threeds.superblock_hashes(rom).items():
            if not ok:
                problems.append(f'ncch {label} superblock hash mismatch')

        work = config.work('verify')
        os.makedirs(work, exist_ok=True)
        image = threeds.dump_romfs(rom, f'{work}/romfs.bin')

        try:
            arch = threeds.read_romfs_file(image, ARCHIVE)
        except KeyError:
            return problems + [f'{ARCHIVE} missing from the rebuilt RomFS']
        members = fa.read(arch)

        checked = 0
        for name in MSG_FILES:
            blob = members.get(f'message/en/{name}.mbin')
            if blob is None:
                problems.append(f'message/en/{name}.mbin missing')
                continue
            m = Msgb(blob)
            got = {k: m.entries[i][2] for i, k in enumerate(m.keys)}
            for key, want in entries.items():
                fam, _, k = key.partition('/')
                if fam != name:
                    continue
                checked += 1
                if k not in got:
                    problems.append(f'{key}: key vanished')
                elif got[k] != want:
                    problems.append(f'{key}: text differs after round-trip')

        # every glyph the translation uses must exist in the shipped font
        need = set()
        for v in entries.values():
            need.update(ch for ch in v if ord(ch) > 0x7F)
        for slot in FONT_SLOTS:
            for name in FONTS:
                blob = members.get(slot.format(name=name))
                if blob is None:
                    continue
                f = Bcfnt(blob)
                miss = [c for c in sorted(need) if f.char_to_index(c) is None]
                if miss:
                    problems.append(f'{slot.format(name=name)}: '
                                    f'{len(miss)} glyphs missing, e.g. {miss[:8]}')
        self.checked = checked
        return problems

    # -- fonts --------------------------------------------------------------

    def font_paths(self):
        return ([config.p(x) for x in config.prof('font_src')],
                [config.p(x) for x in config.prof('font_out')])

    def build_fonts(self):
        from hanpatch.platforms.threeds import fontbuild
        return fontbuild.build_all()

    def recipe_facts(self):
        """What `msgb` actually looks like, measured by the reader above.

        The entry table is not at a fixed offset - the header says where it is,
        and where the key table is too. That is why a recipe's table location
        is a locator rather than a number.
        """
        return {
            'id': 'threeds/level-5/crimson-shroud',
            'platform': 'threeds',
            'title': 'Crimson Shroud',
            'address_spaces': [
                {'id': 'rom', 'kind': 'file'},
                {'id': 'romfs', 'kind': 'member', 'params': {'parent': 'rom'}},
                {'id': 'mbin', 'kind': 'member', 'params': {'parent': 'romfs'}},
            ],
            'tables': [
                {
                    'id': 'messages', 'space': 'mbin', 'format': 'msgb',
                    'kind': 'offset_size', 'stride': 0x10, 'endian': 'little',
                    'alignment': 4, 'applies_to': list(MSG_FILES),
                    'at': {'kind': 'read', 'space': 'mbin', 'at': 4,
                           'width': 4, 'endian': 'little'},
                    'count': {'space': 'mbin', 'at': 8, 'width': 4, 'endian': 'little'},
                    'base': {'kind': 'member_start'},
                    'payload': 'text', 'encoding': 'utf-16-le',
                    'name_source': 'offset_table',
                },
                {
                    'id': 'keys', 'space': 'mbin', 'format': 'msgb',
                    'kind': 'offset_only', 'stride': 4, 'endian': 'little',
                    'alignment': 0x10,
                    'at': {'kind': 'read', 'space': 'mbin', 'at': 0x10,
                           'width': 4, 'endian': 'little'},
                    'count': {'space': 'mbin', 'at': 8, 'width': 4, 'endian': 'little'},
                    'base': {'kind': 'member_start'},
                    'payload': 'text', 'encoding': 'ascii',
                    'name_source': 'none',
                },
            ],
            'measured': [
                "magic 'msgb' at 0x00",
                'header: entry table offset, entry count, unknown u32, key table offset '
                'as four little-endian u32 at 0x04',
                'bytes 0x14..0x20 carried opaque; the reader cannot reproduce them',
                'entry row 0x10 bytes: two flag u32, string offset, byte length',
                'strings UTF-16-LE, NUL terminated, each padded to 4',
                'key table: one u32 offset per entry, pointing at an ASCII NUL '
                'terminated name',
            ],
        }

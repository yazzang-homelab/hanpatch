"""Release bundles — the distributable form of a translation.

A binary delta is the wrong shape for an encrypted container. The 3DS encrypts
RomFS in CTR mode, where the keystream depends on the byte offset, so shifting
file data by one byte changes every ciphertext byte after it. Measured on the
reference title, both a block delta and xdelta3 come out at ~82% of the full ROM:
useless as a patch, and it *is* the game.

What actually changes is the text and the fonts. A bundle carries exactly that —
the sealed manifest, the built fonts, and the title profile — and the recipient
rebuilds against their own copy. Because the pipeline is deterministic, the
result is byte-identical to the author's build, which the bundle asserts by
recording both hashes.

    hanpatch release --out mypatch.hpk
    hanpatch apply mypatch.hpk --rom /path/to/their.cia

Bundle contents:

    bundle.json        title, adapter, target, expected input/output sha256
    manifest.json      the sealed text and its digest
    profile.json       markup grammar, terms, budgets, register
    fonts/*.bcfnt      the built target-language fonts
    README.txt         what this is and how to apply it
"""
import hashlib
import json
import os
import shutil
import tempfile
import zipfile

from hanpatch import config, manifest

FORMAT = 1


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(1 << 22)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


README = """{title} — {target} translation patch
{rule}

This bundle contains the translation, not the game. Applying it rebuilds your
own copy of the ROM with the translated text and fonts.

    pip install hanpatch
    hanpatch apply "{bundle}" --rom /path/to/your/rom{ext}

Expected input   sha256 {src}
Resulting output sha256 {dst}

If your ROM's hash differs, it is a different dump, region or revision. The
patch will refuse to claim success; use --force to attempt it anyway and check
the result yourself.

Contents: {entries} translated strings, manifest digest {digest}.

You are responsible for owning the game you patch and for complying with the
law where you live.
"""


#: Profile keys whose values are PROJECT FILES the injector reads, not text. The
#: first two hold language-bearing artwork - DQ7 draws its title subtitle from a
#: texture atlas and its home-menu banner from an ExeFS member - and neither was
#: ever put in the bundle.
#:
#: `build_inputs` is the same failure one step earlier: a file the injector needs
#: in order to encode at all. Classic Dungeon X2 encodes Korean into retargeted
#: Shift-JIS cells, so its injector reads `font_map.json`, the decrypted
#: `EBOOT.elf` and the redrawn logo. None of them travelled, and `hanpatch apply`
#: on the shipped CDX2 bundle died at `no .../work/font_map.json` - the bundle was
#: the only artifact this project is allowed to distribute, and nobody could apply
#: it.
PAYLOAD_KEYS = ('assets', 'exefs_replace', 'build_inputs')


def _declared_payload():
    """key -> {declared name: (path inside the bundle, path on this machine)}.

    Refuses rather than skips a missing file. A bundle that omits one is not a smaller
    bundle, it is a broken one: the recipient's `apply` dies at the injector with a path
    that only exists on the machine that built it. Measured on the shipped DQ7 r3 bundle -
    `hanpatch apply` failed for every reader with `missing rebuilt asset
    CHARACTER/p0450.bcmdl.lz`, and the release had been signed and published.
    """
    payload = {}
    for key in PAYLOAD_KEYS:
        declared = config.prof(key) or {}
        if not declared:
            continue
        mapping = {}
        for name, src in sorted(declared.items()):
            path = config.p(src)
            if not os.path.exists(path):
                raise SystemExit(
                    f'refusing to release: the profile declares {key}[{name!r}] = {src!r} '
                    f'and that file is missing ({path}). The injector reads it on the '
                    f'RECIPIENT\'s machine, so a bundle without it cannot be applied.')
            where = f'payload/{key}/{name.replace("/", "_")}'
            if any(where == w for w, _ in mapping.values()):
                raise SystemExit(
                    f'refusing to release: two {key} entries flatten to the same bundle '
                    f'path {where!r}; rename one of them in the profile.')
            mapping[name] = (where, path)
        payload[key] = mapping
    return payload


def create(out=None, rom=None, built=None, notes=None):
    """Write a release bundle from the current project state."""
    cfg = config.cfg()
    doc = manifest.load()
    rom = rom or config.p(cfg.get('rom', 'game.cia'))
    built = built or config.dist(config.built_name())
    out = out or config.dist(f"{cfg['title']} ({config.target()}).hpk")

    approved = config.out('manifest.approved')
    if not os.path.exists(approved):
        raise SystemExit('refusing to release: the manifest was never approved '
                         'by the QA gate. Run `hanpatch gates` first.')
    approval = config.load_object(approved, 'the approval token')
    if approval.get('digest') != doc['digest']:
        raise SystemExit('refusing to release: the manifest changed after the '
                         'QA gate approved it.')

    info = {
        'format': FORMAT,
        'title': cfg['title'],
        'platform': cfg.get('platform'),
        'adapter': cfg.get('adapter'),
        'target': config.target(),
        'entries': len(doc['entries']),
        'digest': doc['digest'],
        'source_sha256': _sha(rom) if os.path.exists(rom) else None,
        'output_sha256': _sha(built) if os.path.exists(built) else None,
        'notes': notes or '',
    }

    fonts = [config.p(x) for x in config.prof('font_out')]
    payload = _declared_payload()
    profile = dict(config.profile())
    for key, mapping in payload.items():
        profile[key] = {name: where for name, (where, _src) in mapping.items()}
    ext = os.path.splitext(rom)[1] or '.cia'
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr('bundle.json', json.dumps(info, indent=1, ensure_ascii=False))
        z.writestr('manifest.json', json.dumps(doc, ensure_ascii=False))
        z.writestr('profile.json', json.dumps(profile, indent=1,
                                              ensure_ascii=False))
        for f in fonts:
            if os.path.exists(f):
                z.write(f, f'fonts/{os.path.basename(f)}')
        for mapping in payload.values():
            for where, src in mapping.values():
                z.write(src, where)
        z.writestr('README.txt', README.format(
            title=cfg['title'], target=config.lang_name(),
            rule='=' * 60, bundle=os.path.basename(out), ext=ext,
            src=info['source_sha256'] or '(unknown)',
            dst=info['output_sha256'] or '(unknown)',
            entries=info['entries'], digest=info['digest'][:16]))
    info['bundle'] = out
    info['size'] = os.path.getsize(out)
    _record_package(info)
    return info


def _record_package(info):
    """Report the packaging step to the staged ledger, if one is active.

    The ledger observes; `create` above remains the only authority on whether a
    bundle exists. A token nobody writes is a claim nobody checked, which is why
    this exists rather than the ledger asserting PATCH_PACKAGE on its own.
    """
    try:
        from hanpatch import stage_ledger
        if not stage_ledger.enabled():
            return
        stage_ledger.record(
            'PATCH_PACKAGE', stage_ledger.PASS,
            evidence=os.path.basename(info.get('bundle') or ''),
            checked=info.get('entries'),
            reason='release.create wrote the bundle; the ledger records it')
    except Exception as err:  # pragma: no cover - defensive
        # A refusal is not a malfunction. The guard declining to pass PATCH_PACKAGE
        # over an earlier failure is a fact the operator should read as one.
        from hanpatch import stage_ledger as _sl
        kind = ('refused' if isinstance(err, _sl.LedgerError) else 'could not record')
        print('stage ledger: %s PATCH_PACKAGE: %s' % (kind, err), flush=True)


def inspect(bundle):
    with zipfile.ZipFile(bundle) as z:
        return json.loads(z.read('bundle.json'))


def _open_for(bundle, rom, force, workdir, quiet, what):
    """Unpack a bundle into a throwaway project and extract the ROM into it.

    Shared by `apply` and `luma`: both start from the same sealed manifest and
    the same extracted cartridge, and the only difference is what they do with
    the staged result. Two copies of this setup would drift on the day one of
    them learns something about the profile that the other does not.
    """
    from hanpatch import adapter
    info = inspect(bundle)
    if info.get('format') != FORMAT:
        raise SystemExit(f'unsupported bundle format {info.get("format")}')

    got = _sha(rom)
    if info.get('source_sha256') and got != info['source_sha256']:
        msg = (f'input mismatch\n  bundle expects {info["source_sha256"]}\n'
               f'  your file is   {got}')
        if not force:
            raise SystemExit(msg + '\nDifferent dump, region or revision. '
                                   'Pass --force to try anyway.')
        if not quiet:
            print(msg + '\n--force given; continuing, verify the result yourself')

    tmp = workdir or tempfile.mkdtemp(prefix=f'hanpatch-{what}-')
    os.makedirs(tmp, exist_ok=True)
    with zipfile.ZipFile(bundle) as z:
        z.extractall(tmp)
    profile_path = os.path.join(tmp, 'profile.json')

    # a throwaway project rooted at the temp dir, using the bundled profile
    proj = {'title': info['title'], 'platform': info['platform'],
            'adapter': info['adapter'], 'target': info['target'],
            'profile': 'profile.json', 'rom': os.path.abspath(rom)}
    json.dump(proj, open(os.path.join(tmp, config.PROJECT_FILE), 'w'), indent=1)
    config.set_root(tmp)

    prof = config.load_object(profile_path, 'the bundled title profile')
    # point the profile's font paths at the bundled fonts
    prof['font_out'] = [f'fonts/{os.path.basename(p)}'
                        for p in prof.get('font_out', [])]
    json.dump(prof, open(profile_path, 'w'), ensure_ascii=False, indent=1)
    config.set_root(tmp)

    from hanpatch import wrap
    wrap.reset()

    ad = adapter.project_adapter()
    if not quiet:
        print(f'extracting {os.path.basename(rom)} …', flush=True)
    ad.extract(rom)
    doc = config.load_object(os.path.join(tmp, 'manifest.json'),
                             'the sealed manifest')
    return info, doc, tmp, ad


def luma(bundle, rom, out=None, force=False, workdir=None, quiet=False):
    """Write a Luma3DS LayeredFS pack instead of a rebuilt image.

    For a cartridge this is the only shape that reaches real hardware: the cart
    cannot be rewritten and a rebuilt NCSD no longer matches its own signature,
    so a retail console refuses it. Luma reads the changed files off the SD card
    instead, which is also a few hundred megabytes rather than two gigabytes.
    """
    from hanpatch.platforms.threeds import luma as luma_mod
    info, doc, tmp, ad = _open_for(bundle, rom, force, workdir, quiet, 'luma')
    out = out or os.path.join(os.path.dirname(os.path.abspath(rom)),
                              f"{info['title']} ({info['target']}) LayeredFS")
    os.makedirs(out, exist_ok=True)
    if not quiet:
        print(f'staging {len(doc["entries"])} strings '
              f'(digest {doc["digest"][:16]}) …', flush=True)
    rep = luma_mod.pack(ad, doc['entries'], out, rom=rom, quiet=quiet)
    rep['out'] = out
    rep['title'] = info['title']
    rep['target'] = info['target']
    if not quiet:
        print(f"{rep['root']}\n  RomFS {len(rep['files'])} files, "
              f"{rep['bytes'] / 1e6:.1f} MB\n  code.ips {rep['ips']} bytes\n"
              f"  title id {rep['title_id']}")
    if workdir is None:
        shutil.rmtree(tmp, ignore_errors=True)
    return rep


def apply(bundle, rom, out=None, force=False, workdir=None, quiet=False):
    """Rebuild `rom` with the bundle's translation. Returns a report.

    The recipient needs the game and this tool; the bundle supplies the text.
    Nothing here trusts the bundle blindly: the input hash is checked before
    work starts and the output hash after it finishes.
    """
    info, doc, tmp, ad = _open_for(bundle, rom, force, workdir, quiet, 'apply')
    if not quiet:
        print(f'injecting {len(doc["entries"])} strings '
              f'(digest {doc["digest"][:16]}) …', flush=True)
    out = out or os.path.join(os.path.dirname(os.path.abspath(rom)),
                              f"{info['title']} ({info['target']})"
                              f"{os.path.splitext(rom)[1]}")
    ad.inject(doc['entries'], rom, out)

    result = _sha(out)
    ok = (not info.get('output_sha256')) or result == info['output_sha256']
    if not quiet:
        print(f'{out}\n  sha256 {result}')
        if info.get('output_sha256'):
            print('  matches the author\'s build' if ok else
                  '  DIFFERS from the author\'s build — inspect before using')
    if workdir is None:
        shutil.rmtree(tmp, ignore_errors=True)
    return {'out': out, 'sha256': result, 'reproduced': ok}

"""hanpatch command line.

    hanpatch init    --title T --adapter A --profile P --rom R
    hanpatch info
    hanpatch extract
    hanpatch translate --family dialogue [--workers 4]
    hanpatch a6-translate ...                 isolated DQ7 A6 pilot (dedicated parser)
    hanpatch fonts
    hanpatch gates
    hanpatch qa      [--judges 2] [--workers 4]
    hanpatch build
    hanpatch verify
    hanpatch book    [--out DIR]
    hanpatch all

    hanpatch feedback index              index the book for whole-book search
    hanpatch feedback serve              reader feedback API + loopback triage
    hanpatch feedback orders             work orders and their approval state
    hanpatch feedback show ORD           print one work order
    hanpatch feedback approve ORD --by N approve an order (a human act)
    hanpatch feedback apply ORD --fixes F  write approved fixes into the override

    hanpatch keys                        show loaded key material
    hanpatch release --out patch.hpk     bundle the translation for distribution
    hanpatch apply patch.hpk --rom ROM   rebuild someone else's copy
    hanpatch delta --old A --new B --out P   raw binary delta (any file pair)
"""
import argparse
import json
import os
import sys
import time

from hanpatch import config
from hanpatch import star


def _p(*a, **kw):
    print(*a, flush=True, **kw)


def cmd_init(args):
    root = os.path.abspath(args.dir or '.')
    os.makedirs(root, exist_ok=True)
    doc = {'title': args.title, 'platform': args.platform,
           'adapter': args.adapter, 'profile': args.profile,
           'target': args.target, 'rom': args.rom}
    path = os.path.join(root, config.PROJECT_FILE)
    if os.path.exists(path) and not args.force:
        raise SystemExit(f'{path} exists; pass --force to overwrite')
    json.dump(doc, open(path, 'w'), ensure_ascii=False, indent=1)
    for d in ('work', 'dist', 'extracted'):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    _p(f'wrote {path}')
    return 0


def cmd_info(args):
    _p(config.describe())
    from hanpatch import adapter
    _p(f'adapters {", ".join(adapter.available())}')
    rom = config.p(config.cfg().get('rom', 'game.cia'))
    if os.path.exists(rom):
        try:
            from hanpatch.platforms import threeds
            kind = threeds.detect(rom)
            _p(f'rom      {os.path.basename(rom)} [{kind}]')
            n = threeds.open_ncch(rom)
            _p(f'         {n.describe()}')
        except Exception as e:
            _p(f'rom      {os.path.basename(rom)} — {e}')
    src = config.src_path()
    if os.path.exists(src):
        d = config.load_object(src, 'the extracted source')
        _p(f'source   {sum(len(v) for v in d.values())} entries, '
           f'{len(d)} families')
    man = config.out('manifest.json')
    if os.path.exists(man):
        m = config.load_object(man, 'the sealed manifest')
        _p(f'manifest {len(m["entries"])} entries, digest {m["digest"][:16]}')
    return 0


def cmd_extract(args):
    from hanpatch import adapter
    ad = adapter.project_adapter()
    rom = args.rom or config.p(config.cfg().get('rom', 'game.cia'))
    n = ad.extract(rom)
    _p(f'{n} source entries -> {config.src_path()}')
    return 0


def cmd_translate(args):
    from hanpatch import run
    argv = ['--family', args.family]
    if args.models:
        argv += ['--models', args.models]
    if args.limit:
        argv += ['--limit', str(args.limit)]
    if args.workers:
        argv += ['--workers', str(args.workers)]
    if args.batch:
        # `--batch` means STRINGS per call, the same as it does on `hanpatch qa`.
        # It used to be forwarded verbatim, and the runner has no such option, so
        # argparse abbreviation resolved it to `--batch-chars`: a window of eight
        # SOURCE CHARACTERS. Every call then carried one string and repaid the full
        # ~768-token prompt prefix. The cost ledger shows 57,621 of 57,621 flash
        # calls like that. Forward the flag the runner actually has.
        argv += ['--max-items', str(args.batch)]
    if args.refail:
        argv.append('--refail')
    if args.qafail:
        argv.append('--qafail')
    if args.qa_list:
        argv += ['--qa-list', args.qa_list]
    return run.main(argv) or 0


def cmd_fonts(args):
    from hanpatch import adapter
    res = adapter.project_adapter().build_fonts()
    for r in res:
        _p(f"{r['font']}: {r['glyphs']} glyphs (+{r['added']}), {r['bytes']} bytes")
    return 0


def cmd_gates(args):
    from hanpatch import pipeline
    try:
        rep = pipeline.gates(quiet=args.quiet)
    except pipeline.GateFailed as e:
        _p(f'GATE FAILED — {e}')
        return 1
    _p(f"gates passed: {rep['entries']} entries, digest {rep['manifest']}")
    return 0


def cmd_qa(args):
    from hanpatch import qa
    argv = []
    if args.workers:
        argv += ['--workers', str(args.workers)]
    if args.batch:
        argv += ['--batch', str(args.batch)]
    if args.judges:
        argv += ['--judges', str(args.judges)]
    return qa.main(argv) or 0


def cmd_build(args):
    from hanpatch import pipeline
    try:
        rep = pipeline.build(rom=args.rom, out=args.out, quiet=args.quiet)
    except pipeline.GateFailed as e:
        _p(f'BUILD BLOCKED — {e}')
        return 1
    st = rep['inject']
    _p(f"{st['translated']}/{st['total']} strings replaced, "
       f"{st['skipped']} skipped, {st['size']} bytes")
    _p(rep['rom'])
    return 0


def cmd_verify(args):
    from hanpatch import pipeline
    try:
        pipeline.verify(rom=args.rom)
    except pipeline.GateFailed as e:
        _p(f'VERIFY FAILED — {e}')
        return 1
    _p('ALL CHECKS PASSED')
    return 0


def cmd_book(args):
    from hanpatch import scriptbook
    return scriptbook.main(args.out) or 0


def _fbstore(args):
    from hanpatch import feedback
    return feedback.Store(args.db or config.p('work', 'feedback.db'))


def _fborders(args):
    return args.orders or config.p('work', 'orders')


def cmd_feedback(args):
    from hanpatch import feedback
    store = _fbstore(args)
    op = args.op
    if op == 'index':
        n = feedback.index_from_book(store)
        _p(f'indexed {n} rows -> {store.path}')
        return 0
    if op == 'serve':
        if not store.stat()['lines']:
            _p('the search index is empty; run `hanpatch feedback index` first')
            return 1
        token = ''
        if args.token_file and os.path.exists(args.token_file):
            token = open(args.token_file, encoding='utf-8').read().strip()
        elif args.token_file:
            _p(f'no such token file: {args.token_file}')
            return 1
        srv, admin = feedback.serve(store, args.port, args.admin_port, args.host,
                                   token, _fborders(args))
        _p(f'public  http://{args.host}:{args.port}/api/')
        if admin:
            _p(f'admin   http://{args.host}:{args.admin_port}/'
               + ('  (token required)' if token else '  (no token set)'))
        _p(f'orders  {_fborders(args)}')
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            srv.shutdown()
            if admin:
                admin.shutdown()
        return 0
    if op == 'orders':
        for o in store.orders():
            _p(f'{o["id"]}  {o["status"]:<8} {o["approved_by"] or "-":<12} '
               f'{o["title"]}')
        return 0
    if op == 'show':
        row = store.order(args.id)
        if row is None:
            _p(f'no such order: {args.id}')
            return 1
        _p(feedback.order_md(json.loads(row['payload'])))
        return 0
    if op == 'approve':
        try:
            p = store.approve_order(args.id, args.by, _fborders(args))
        except ValueError as e:
            _p(str(e))
            return 1
        _p(f'{p["id"]} approved by {p["approved_by"]} '
           f'({len(p["rows"])} rows) -> {_fborders(args)}')
        return 0
    if op == 'apply':
        if not args.fixes:
            _p('--fixes is required: a JSON object of {"family/key": "new text"}')
            return 1
        fixes = config.load_object(args.fixes, 'the fix set')
        bad = [k for k, v in fixes.items() if not isinstance(v, str)]
        if bad:
            _p(f'--fixes holds non-text values for: {", ".join(sorted(bad))}')
            return 1
        res = feedback.apply_order(store, args.id, fixes, _fborders(args))
        if not res['ok']:
            _p(f'REFUSED: {res["error"]}')
            for pb in res.get('problems', [])[:25]:
                _p(f'  {pb}')
            return 1
        _p(f'applied {res["applied"]} overrides -> {res["override"]}')
        _p('run `hanpatch gates` to re-seal the manifest, then rebuild the book.')
        return 0
    _p(f'unknown feedback op: {op}')
    return 2


def cmd_keys(args):
    from hanpatch.platforms.threeds import keys as keysmod
    ks = keysmod.KeyStore(project=config.root())
    _p('key material')
    _p(ks.describe())
    if not ks.sources:
        _p('')
        _p('hanpatch ships no keys. Put boot9.bin, keys.txt or seeddb.bin in')
        _p(f'  {os.path.join(config.root(), "keys")}/  or  ~/.hanpatch/keys/')
        _p('or set HANPATCH_KEYS to a path. Titles using only crypto method 0')
        _p('need nothing; encrypted retail dumps need slot 0x25/0x18/0x1B, and')
        _p('title-key encrypted CIAs need slot 0x3D plus a common key.')
    return 0


def cmd_release(args):
    from hanpatch import release
    info = release.create(out=args.out, rom=args.rom, built=args.built,
                          notes=args.notes)
    _p(f"{info['bundle']}  {info['size']} bytes")
    _p(f"  {info['entries']} strings, digest {info['digest'][:16]}")
    _p(f"  input  {info['source_sha256']}")
    _p(f"  output {info['output_sha256']}")
    return 0


def cmd_apply(args):
    from hanpatch import release
    if args.info:
        import json as _j
        _p(_j.dumps(release.inspect(args.bundle), indent=1, ensure_ascii=False))
        return 0
    if not args.rom:
        _p('--rom is required')
        return 2
    r = release.apply(args.bundle, args.rom, out=args.out, force=args.force)
    return 0 if r['reproduced'] or args.force else 1


def cmd_luma(args):
    from hanpatch import release
    r = release.luma(args.bundle, args.rom, out=args.out, force=args.force,
                     workdir=args.workdir)
    if args.verify_rom:
        from hanpatch.platforms.threeds import luma as luma_mod
        from hanpatch.platforms import threeds
        import tempfile
        tmp = os.path.join(tempfile.mkdtemp(prefix='hanpatch-luma-verify-'),
                           'romfs.bin')
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        _p(f'reading RomFS out of {args.verify_rom} …')
        threeds.dump_romfs(args.verify_rom, tmp)
        v = luma_mod.verify_against_rom(r['root'], tmp)
        _p(f"  compared {v['checked']} files against the built ROM: "
           f"{v['mismatched']} differ, {len(v['missing'])} not found")
        return 0 if not v['mismatched'] and not v['missing'] else 1
    return 0


def cmd_publish(args):
    from hanpatch import channel
    rec = channel.publish(args.bundle, args.root, url_base=args.url_base,
                          version=args.version, notes=args.notes)
    verb = 'already published' if rec.pop('republished') else 'published'
    _p(f"{verb}: {rec['id']} {rec['version']}  {rec['size']} bytes")
    _p(f"  {os.path.join(args.root, rec['file'])}")
    _p(f"  sha256 {rec['sha256']}")
    base = (args.url_base or '').rstrip('/')
    if base:
        _p(f"  {base}/{rec['file']}")
        _p(f"  index {base}/index.json   client {base}/hpk-update.py")
    return 0


def channel_default():
    from hanpatch import channel
    return channel.DEFAULT_CHANNEL


def cmd_update(args):
    from hanpatch import channel
    dest = args.dir or channel.default_dest()
    rep = channel.update(args.channel, dest, ids=args.id or None,
                         check_only=args.check, quiet=args.quiet)
    if args.check and rep['pending']:
        return 10
    return 0


def cmd_delta(args):
    from hanpatch import delta
    if args.apply:
        r = delta.apply(args.old, args.patch, args.out)
        _p(f"{args.out}  {r['size']} bytes")
        return 0
    r = delta.create(args.old, args.new, args.out, backend=args.backend)
    _p(f"{args.out}  {r['size']} bytes  ({r['ratio'] * 100:.1f}% of the target, "
       f"backend {r['backend']})")
    if r['ratio'] > 0.5:
        _p('note: this delta is most of the file. Encrypted containers defeat '
           'binary diffing — use `hanpatch release` instead.')
    if args.applier:
        _p(delta.write_applier(args.applier))
    return 0


def cmd_all(args):
    for fn, a in ((cmd_fonts, args), (cmd_gates, args), (cmd_build, args),
                  (cmd_verify, args)):
        rc = fn(a)
        if rc:
            return rc
    return 0


def main(argv=None):
    # The isolated A6 lane owns a stricter parser, output namespace and budget.
    # Dispatch before constructing the ordinary CLI so this path cannot build the
    # normal provider pool or accidentally inherit model/retry switches.
    command = list(sys.argv[1:] if argv is None else argv)
    if command and command[0] == 'a6-translate':
        from hanpatch import a6isolated_run
        return a6isolated_run.main(command[1:])
    ap = argparse.ArgumentParser(prog='hanpatch', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                allow_abbrev=False)
    ap.add_argument('--project', help='project directory (default: nearest '
                                      'parent with hanpatch.json)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('init', help='create hanpatch.json')
    s.add_argument('dir', nargs='?')
    s.add_argument('--title', required=True)
    s.add_argument('--adapter', required=True)
    s.add_argument('--profile', required=True)
    s.add_argument('--platform', default='threeds')
    s.add_argument('--target', default='ko')
    s.add_argument('--rom', default='game.cia')
    s.add_argument('--force', action='store_true')
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser('info', help='show project/adapter state')
    s.set_defaults(fn=cmd_info)

    s = sub.add_parser('extract', help='ROM -> work/text_src.json')
    s.add_argument('--rom')
    s.set_defaults(fn=cmd_extract)

    s = sub.add_parser('translate', help='machine-translate one family')
    s.add_argument('--family', required=True)
    s.add_argument('--models', default='',
                   help='explicit comma-separated pool; beats the profile and the '
                        'registry, for narrowing to endpoints with budget left')
    s.add_argument('--limit', type=int, default=0,
                   help='translate at most N strings, for a bounded trial')
    s.add_argument('--workers', type=int)
    s.add_argument('--batch', type=int)
    s.add_argument('--refail', action='store_true')
    s.add_argument('--qafail', action='store_true')
    s.add_argument('--qa-list', default='',
                   help='precomputed actionable map for --qafail, so one process per '
                        'family does not re-read the whole verdict file each time')
    s.set_defaults(fn=cmd_translate)

    s = sub.add_parser('fonts', help='build target-language fonts')
    s.set_defaults(fn=cmd_fonts)

    s = sub.add_parser('gates', help='run every gate, seal a manifest')
    s.add_argument('--quiet', action='store_true')
    s.set_defaults(fn=cmd_gates)

    s = sub.add_parser('qa', help='top up the independent judge panel')
    s.add_argument('--workers', type=int)
    s.add_argument('--batch', type=int)
    s.add_argument('--judges', type=int)
    s.set_defaults(fn=cmd_qa)

    s = sub.add_parser('build', help='gates + inject -> patched ROM')
    s.add_argument('--rom')
    s.add_argument('--out')
    s.add_argument('--quiet', action='store_true')
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser('verify', help='re-read the built ROM')
    s.add_argument('--rom')
    s.set_defaults(fn=cmd_verify)

    s = sub.add_parser('book', help='render the bilingual script book')
    s.add_argument('--out')
    s.set_defaults(fn=cmd_book)

    s = sub.add_parser('feedback', help='reader feedback, search index, work orders')
    s.add_argument('op', choices=['index', 'serve', 'orders', 'show', 'approve',
                                 'apply'])
    s.add_argument('id', nargs='?', help='work order id')
    s.add_argument('--db', help='default: work/feedback.db')
    s.add_argument('--orders', help='where approved orders are written '
                                    '(default: work/orders)')
    s.add_argument('--host', default='127.0.0.1')
    s.add_argument('--port', type=int, default=8120)
    s.add_argument('--admin-port', type=int,
                   help='serve the loopback triage surface on this port too')
    s.add_argument('--token-file',
                   help='shared secret the triage surface requires')
    s.add_argument('--by', help='who approves this order; recorded in it')
    s.add_argument('--fixes', help='JSON object of {"family/key": "new text"}')
    s.set_defaults(fn=cmd_feedback)

    s = sub.add_parser('keys', help='show loaded key material')
    s.set_defaults(fn=cmd_keys)

    s = sub.add_parser('release', help='bundle the translation for distribution')
    s.add_argument('--out')
    s.add_argument('--rom')
    s.add_argument('--built', help='the patched ROM whose hash to record')
    s.add_argument('--notes', default='')
    s.set_defaults(fn=cmd_release)

    s = sub.add_parser('apply', help='apply a release bundle to your own ROM')
    s.add_argument('bundle')
    s.add_argument('--rom')
    s.add_argument('--out')
    s.add_argument('--force', action='store_true',
                   help='continue when the input hash does not match')
    s.add_argument('--info', action='store_true', help='print bundle metadata')
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser('luma', help='LayeredFS pack for Luma3DS on real hardware')
    s.add_argument('bundle')
    s.add_argument('--rom', required=True)
    s.add_argument('--out', help='SD card root (default: beside the ROM)')
    s.add_argument('--force', action='store_true',
                   help='continue when the input hash does not match')
    s.add_argument('--workdir', help='keep the working tree here')
    s.add_argument('--verify-rom',
                   help='a rebuilt ROM to check every packed file against')
    s.set_defaults(fn=cmd_luma)

    s = sub.add_parser('publish', help='put a bundle in the update channel')
    s.add_argument('bundle')
    s.add_argument('--root', required=True,
                   help='the directory the channel is served from')
    s.add_argument('--url-base', default=channel_default(),
                   help='public URL of that directory, recorded in the index')
    s.add_argument('--version', help='default: today, deduplicated')
    s.add_argument('--notes', help='override the note carried in the bundle')
    s.set_defaults(fn=cmd_publish)

    s = sub.add_parser('update', help='fetch published bundles, verified')
    s.add_argument('id', nargs='*', help='patch ids (default: every patch)')
    s.add_argument('--channel', default=channel_default())
    s.add_argument('--dir', help='where to keep bundles (default: dist/)')
    s.add_argument('--check', action='store_true',
                   help='report only; exit 10 when an update is waiting')
    s.add_argument('--quiet', action='store_true')
    s.set_defaults(fn=cmd_update)

    s = sub.add_parser('delta', help='raw binary delta between two files')
    s.add_argument('--old', required=True)
    s.add_argument('--new')
    s.add_argument('--patch')
    s.add_argument('--out', required=True)
    s.add_argument('--backend', default='auto', choices=['auto', 'xdelta', 'hpd'])
    s.add_argument('--apply', action='store_true')
    s.add_argument('--applier', help='also write a standalone applier script')
    s.set_defaults(fn=cmd_delta)

    s = sub.add_parser('all', help='fonts + gates + build + verify')
    s.add_argument('--rom')
    s.add_argument('--out')
    s.add_argument('--quiet', action='store_true')
    s.set_defaults(fn=cmd_all)

    args = ap.parse_args(argv)
    if args.project:
        config.set_root(args.project)
    rc = args.fn(args)
    # AFTER the command, and never able to change its exit code: the ask is worth
    # a couple of seconds of a human's attention, not one byte of a build's
    # result. `star.nudge` swallows its own failures for the same reason.
    star.nudge(argv=command)
    return rc


if __name__ == '__main__':
    sys.exit(main())

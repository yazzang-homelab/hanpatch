"""hanpatch command line.

    hanpatch init    --title T --adapter A --profile P --rom R
    hanpatch info
    hanpatch extract
    hanpatch translate --family dialogue [--workers 4]
    hanpatch fonts
    hanpatch gates
    hanpatch qa      [--judges 2] [--workers 4]
    hanpatch build
    hanpatch verify
    hanpatch book    [--out DIR]
    hanpatch all
"""
import argparse
import json
import os
import sys

from hanpatch import config


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
    src = config.src_path()
    if os.path.exists(src):
        d = json.load(open(src))
        _p(f'source   {sum(len(v) for v in d.values())} entries, '
           f'{len(d)} families')
    man = config.out('manifest.json')
    if os.path.exists(man):
        m = json.load(open(man))
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
    if args.workers:
        argv += ['--workers', str(args.workers)]
    if args.batch:
        argv += ['--batch', str(args.batch)]
    if args.refail:
        argv.append('--refail')
    if args.qafail:
        argv.append('--qafail')
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


def cmd_all(args):
    for fn, a in ((cmd_fonts, args), (cmd_gates, args), (cmd_build, args),
                  (cmd_verify, args)):
        rc = fn(a)
        if rc:
            return rc
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='hanpatch', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
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
    s.add_argument('--workers', type=int)
    s.add_argument('--batch', type=int)
    s.add_argument('--refail', action='store_true')
    s.add_argument('--qafail', action='store_true')
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

    s = sub.add_parser('all', help='fonts + gates + build + verify')
    s.add_argument('--rom')
    s.add_argument('--out')
    s.add_argument('--quiet', action='store_true')
    s.set_defaults(fn=cmd_all)

    args = ap.parse_args(argv)
    if args.project:
        config.set_root(args.project)
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())

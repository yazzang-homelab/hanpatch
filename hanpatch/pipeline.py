"""Fail-closed gate runner.

The gate order is the whole safety argument, so it lives in one place:

    glossary     rebuild the authoritative term table from the name tables
    capacity     re-derive proven text-box capacities from the source text
    materialize  expand rule-derived entries (numbered variants and the like)
    audit        structural checks: coverage, tags, register, drift, dedup
    manifest     seal every shippable string into one digest
    qagate       independent multi-judge semantic verdicts, bound to that digest

Each stage runs in-process and raises `GateFailed` on the first problem.  There
is no skip switch: a caller that wants to build must make the gates pass.  The
packer re-runs `qagate.validate()` itself at pack time, so tampering with the
approval token between gate and build does not get a ROM out.
"""
import io
import sys

from hanpatch import audit, capacity, glossary, manifest, qagate


class GateFailed(RuntimeError):
    def __init__(self, stage, detail):
        super().__init__(f'{stage}: {detail}')
        self.stage = stage
        self.detail = detail


def _run(stage, fn, quiet):
    buf = io.StringIO()
    old = sys.stdout
    if quiet:
        sys.stdout = buf
    try:
        return fn(), buf.getvalue()
    finally:
        sys.stdout = old


def gates(quiet=False, on_stage=None):
    """Run every gate in order. Returns a report dict; raises GateFailed."""
    report = {}

    def note(name, value):
        report[name] = value
        if on_stage:
            on_stage(name, value)

    if on_stage is None and not quiet:
        def on_stage(name, value):  # noqa: F811
            print(f'gate {name}: ok', flush=True)

    g, _ = _run('glossary', glossary.build, quiet)
    note('glossary', len(g))

    c, _ = _run('capacity', capacity.build, quiet)
    note('capacity', len(c))

    from hanpatch import materialize
    rc, text = _run('materialize', materialize.main, quiet)
    if rc:
        raise GateFailed('materialize', f'invalid derived entries\n{text}')
    note('materialize', 'clean')

    rc, text = _run('audit', audit.main, quiet)
    if rc:
        raise GateFailed('audit', f'{rc} hard failure(s)\n{text}')
    note('audit', 'clean')

    doc, _ = _run('manifest', manifest.build, quiet)
    note('manifest', doc['digest'][:16])
    note('entries', len(doc['entries']))

    blocked, bad, stale = qagate.validate(doc['entries'])
    if blocked or bad or stale:
        raise GateFailed('qagate',
                         f'{len(blocked)} blocked, {len(bad)} invalid waivers, '
                         f'{len(stale)} stale waivers; e.g. '
                         f'{(blocked + bad + stale)[:3]}')
    qagate.approve(doc['digest'], len(doc['entries']))
    note('qagate', 'approved')
    return report


def build(rom=None, out=None, quiet=False):
    """Run the gates, then hand the sealed manifest to the title adapter."""
    from hanpatch import adapter, config
    report = gates(quiet=quiet)
    ad = adapter.project_adapter()
    cfg = config.cfg()
    rom = rom or config.p(cfg.get('rom', 'game.cia'))
    out = out or config.dist(f"{cfg['title']} ({config.target()}).cia")
    doc = manifest.load()
    stats = ad.inject(doc['entries'], rom, out)
    report['inject'] = stats
    report['rom'] = out
    return report


def verify(rom=None, quiet=False):
    """Re-read a built ROM and prove the sealed text survived the round trip."""
    from hanpatch import adapter, config
    ad = adapter.project_adapter()
    cfg = config.cfg()
    rom = rom or config.dist(f"{cfg['title']} ({config.target()}).cia")
    doc = manifest.load()
    problems = ad.verify(rom, doc['entries'])
    if not quiet:
        print(f'verified {getattr(ad, "checked", 0)} entries in {rom}')
        for p in problems:
            print('  FAIL', p)
    if problems:
        raise GateFailed('verify', f'{len(problems)} problem(s): {problems[:5]}')
    return {'rom': rom, 'checked': getattr(ad, 'checked', 0)}

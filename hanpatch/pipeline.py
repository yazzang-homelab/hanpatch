"""Fail-closed gate runner.

The gate order is the whole safety argument, so it lives in one place:

    glossary     rebuild the authoritative term table from the name tables
    capacity     re-derive proven text-box capacities from the source text
    materialize  expand rule-derived entries (numbered variants and the like)
    audit        structural checks: coverage, tags, register, drift, dedup
    manifest     seal every shippable string into one digest
    qagate       independent multi-judge semantic verdicts, bound to that digest

Each stage runs in-process and raises `GateFailed` on the first problem.  There
is no skip switch: a caller that wants to build must make the gates pass.

The approval token IS the authority.  `release.create()` authorises on the token
plus a digest match and does NOT re-run the gates, which is why every failure
path here revokes it: a gate verdict, a floor, an unsealed manifest, a failed
injection, a failed verification, or an interrupt.  Tampering is caught from the
other side instead: `manifest.load()` re-derives the digest from the sealed
values, so an edited manifest cannot ride an old token.
"""
import io
import sys

from hanpatch import audit, capacity, config, glossary, manifest, qagate


class GateFailed(RuntimeError):
    def __init__(self, stage, detail):
        super().__init__(f'{stage}: {detail}')
        self.stage = stage
        self.detail = detail


_INPUT_GATES = ('glossary', 'capacity', 'materialize', 'audit', 'manifest',
                'qagate')


def _validated_thresholds():
    # Validate at gate-run time, not in config.profile(): the gate names and
    # examined-input contract live here, so schema validation cannot drift from
    # the gates that actually honour floors.
    thresholds = config.prof('gate_thresholds')
    names = ', '.join(_INPUT_GATES)
    if not isinstance(thresholds, dict):
        raise GateFailed('gate_thresholds',
                         f'gate_thresholds must be a mapping; accepted gate names: '
                         f'{names}')
    for name, value in thresholds.items():
        if name not in _INPUT_GATES:
            raise GateFailed('gate_thresholds',
                             f'gate_thresholds[{name!r}] = {value!r}: unknown gate; '
                             f'accepted gate names: {names}')
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise GateFailed('gate_thresholds',
                             f'gate_thresholds[{name!r}] = {value!r}: must be a '
                             f'positive integer; accepted gate names: {names}')
    return thresholds



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
    """Run every gate in order. Returns a report dict; raises GateFailed.

    Any failure revokes the approval token. `release.create` authorises on that
    token plus a digest match and never re-runs the gates, so a token left over
    from an earlier passing run would make a failed run releasable.
    """
    try:
        return _gates(quiet=quiet, on_stage=on_stage)
    except BaseException:
        # ANY failure revokes, not just GateFailed: a bare TypeError from an
        # unsealed manifest or an interrupt mid-run would otherwise leave the
        # previous run's approval standing and the failed run releasable.
        qagate.revoke()
        raise


def _gates(quiet=False, on_stage=None):
    report = {'inputs': {}}
    thresholds = _validated_thresholds()
    show_stage_output = on_stage is None and not quiet

    def note(name, value, inputs=None):
        if inputs is not None:
            report['inputs'][name] = inputs
            # A profile opts into a floor only for gates that must cover this title;
            # absent keys retain the legacy behaviour for existing releases.
            if name in thresholds and inputs < thresholds[name]:
                raise GateFailed(name,
                                 f'{name} examined {inputs} inputs; required minimum '
                                 f'is {thresholds[name]}')
        report[name] = value
        if on_stage:
            on_stage(name, value)
        if inputs is not None and show_stage_output:
            print(f'gate {name}: {inputs} inputs examined', flush=True)

    if show_stage_output:
        def on_stage(name, value):  # noqa: F811
            print(f'gate {name}: ok', flush=True)

    # Gates publish LAST_EXAMINED so established return contracts remain intact
    # while floors use the eligibility predicate that did the examining.
    g, _ = _run('glossary', glossary.build, quiet)
    note('glossary', len(g), glossary.LAST_EXAMINED)

    # Counted here rather than inside capacity.build, but no longer a proxy: this
    # applies the SAME predicate the derivation applies, so the reported input
    # count cannot drift from the rows actually examined.
    src = config.load_object(config.src_path(), 'the extracted source')
    capacity_inputs = sum(1
                          for family, items in src.items()
                          for it in items
                          if it['en'].strip()
                          and not capacity.wrap.engine_lays_out(it['en']))
    c, _ = _run('capacity', capacity.build, quiet)
    note('capacity', len(c), capacity_inputs)

    from hanpatch import materialize
    rc, text = _run('materialize', materialize.main, quiet)
    if rc:
        raise GateFailed('materialize', f'invalid derived entries\n{text}')
    note('materialize', 'clean', materialize.LAST_EXAMINED)

    rc, text = _run('audit', audit.main, quiet)
    if rc:
        raise GateFailed('audit', f'{rc} hard failure(s)\n{text}')
    note('audit', 'clean', audit.LAST_EXAMINED)

    doc, _ = _run('manifest', manifest.build, quiet)
    if not doc or 'digest' not in doc or 'entries' not in doc:
        # manifest.build returns None when its own validation fails. Subscripting
        # that would raise a bare TypeError, which is not a gate failure and so
        # would escape the revocation boundary below with an approval standing.
        raise GateFailed('manifest', 'the manifest was not sealed')
    note('manifest', doc['digest'][:16], manifest.LAST_EXAMINED)
    note('entries', len(doc['entries']))

    blocked, bad, stale = qagate.validate(doc['entries'])
    if blocked or bad or stale:
        raise GateFailed('qagate',
                         f'{len(blocked)} blocked, {len(bad)} invalid waivers, '
                         f'{len(stale)} stale waivers; e.g. '
                         f'{(blocked + bad + stale)[:3]}')
    # The floor is checked BEFORE the approval token is written. `release.create`
    # authorises on the token plus a digest match and never re-runs the gates, so
    # approving first and failing the floor afterwards would leave a failed run
    # holding a release-ready approval.
    note('qagate', 'validated', qagate.LAST_EXAMINED)
    qagate.approve(doc['digest'], len(doc['entries']))
    report['qagate'] = 'approved'
    return report


def build(rom=None, out=None, quiet=False):
    """Run the gates, then hand the sealed manifest to the title adapter.

    Injection failures revoke the approval token for the same reason gate
    failures do: `release.create` authorises on the token plus a digest match and
    never re-runs the gates, so a token left standing by a half-written build
    would make that build releasable.
    """
    from hanpatch import adapter, config
    report = gates(quiet=quiet)
    try:
        ad = adapter.project_adapter()
        cfg = config.cfg()
        rom = rom or config.p(cfg.get('rom', 'game.cia'))
        out = out or config.dist(config.built_name())
        doc = manifest.load()
        stats = ad.inject(doc['entries'], rom, out)
    except BaseException:
        qagate.revoke()
        raise
    report['inject'] = stats
    report['rom'] = out
    return report


def verify(rom=None, quiet=False):
    """Re-read a built ROM and prove the sealed text survived the round trip.

    A failed verification revokes the approval token. `hanpatch all` chains build
    into verify, and `release.create` authorises on the token plus a digest match
    without re-running anything, so a build whose ROM failed its own round trip
    would otherwise stay releasable.
    """
    from hanpatch import adapter, config
    try:
        ad = adapter.project_adapter()
        cfg = config.cfg()
        rom = rom or config.dist(config.built_name())
        doc = manifest.load()
        problems = ad.verify(rom, doc['entries'])
    except BaseException:
        qagate.revoke()
        raise
    if problems:
        # Revoke BEFORE reporting. Printing is not part of the safety contract and
        # can itself fail - a closed stdout raises BrokenPipeError - which would
        # exit past a later revoke and leave a failed round trip releasable.
        qagate.revoke()
    if not quiet:
        print(f'verified {getattr(ad, "checked", 0)} entries in {rom}')
        for p in problems:
            print('  FAIL', p)
    if problems:
        raise GateFailed('verify', f'{len(problems)} problem(s): {problems[:5]}')
    return {'rom': rom, 'checked': getattr(ad, 'checked', 0)}

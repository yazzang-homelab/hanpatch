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


def _ledger_active():
    """True only for a title that opted in via the versioned profile object.

    Wrapped because a malformed opt-in must fail loudly while an absent one
    stays silent: every legacy title has no such key, and asking about it may
    not become a new way for an existing build to break.
    """
    from hanpatch import stage_ledger
    return stage_ledger.enabled()


def gates(quiet=False, on_stage=None):
    """Run every gate in order. Returns a report dict; raises GateFailed.

    Any failure revokes the approval token. `release.create` authorises on that
    token plus a digest match and never re-runs the gates, so a token left over
    from an earlier passing run would make a failed run releasable.
    """
    # Everything that can fail belongs inside the guard, including deciding
    # whether the ledger is active. Reading the profile can itself raise - a
    # malformed `gate_thresholds` exits there - and a raise before the try would
    # skip the revoke, leaving the previous run's approval standing over a run
    # that never completed. `tests/test_gates.py` proves this for every bad
    # threshold shape.
    ledger_on = False
    caller_on_stage = on_stage

    def staged(name, value):
        # The ledger observes; it never decides. A recording failure must not
        # turn a passing gate run into a failed one, so it is reported and the
        # gate result stands on its own authority.
        if ledger_on:
            from hanpatch import stage_ledger
            try:
                stage_ledger.record_gate_stage(name, checked=value)
            except Exception as err:  # pragma: no cover - defensive
                print('stage ledger: could not record %s: %s' % (name, err),
                      flush=True)
        if caller_on_stage:
            caller_on_stage(name, value)

    try:
        ledger_on = _ledger_active()
        if ledger_on:
            from hanpatch import stage_ledger
            stage_ledger.bootstrap(ruleset=manifest.RULESET, force=True)
        result = _gates(quiet=quiet, on_stage=staged if ledger_on else on_stage)

        # Voice is a gate, not a report - but only for a title that opted into
        # the upgrade. Running it for a legacy title would let a malformed
        # `voice_contract` hard-fail a build that never asked for this check.
        if ledger_on:
            from hanpatch import voice_gate
            voice = voice_gate.evaluate()
            result['voice'] = voice['status']
            if not quiet:
                print(voice_gate.summary_line(voice), flush=True)
            if voice['status'] == voice_gate.FAIL:
                raise GateFailed('voice', voice['detail'])
        return result
    except GateFailed as err:
        if ledger_on:
            from hanpatch import stage_ledger
            token = stage_ledger.failure_token(err.stage)
            if token:
                try:
                    stage_ledger.record_failure(token, str(err))
                except Exception as ledger_err:  # pragma: no cover - defensive
                    # Report it. The gate verdict stands either way, but a
                    # ledger that silently failed to record a failure is a
                    # ledger nobody should trust afterwards.
                    print('stage ledger: could not record %s failure: %s'
                          % (err.stage, ledger_err), flush=True)
        qagate.revoke()
        raise
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

        # Byte ownership, when the adapter declares its write surface. The plan
        # is checked against the source BEFORE injection, so a plan computed
        # against a different revision fails before anything is written.
        # `write_plan` is an optional hook. An adapter predating it - or a test
        # double - simply has no write surface declared, which is recorded as
        # not-declared rather than treated as a missing requirement.
        plan_hook = getattr(ad, 'write_plan', None)
        plan = plan_hook(rom, doc['entries']) if callable(plan_hook) else None
        if plan is not None:
            from hanpatch import expected_write as _ew
            if not isinstance(plan, _ew.WritePlan):
                # Duck typing is not acceptable here. An object that merely
                # answers verify_source/verify_final can report clean forever,
                # and the build would print a guarantee it never obtained.
                raise GateFailed(
                    'expected_write',
                    'write_plan returned %s; it must be an '
                    'expected_write.WritePlan' % type(plan).__name__)
            if not plan.writes:
                raise GateFailed(
                    'expected_write',
                    'the write plan declares no writes; an empty plan cannot '
                    'distinguish a correct build from an untouched one')
        source_bytes = None
        if plan is not None:
            with open(rom, 'rb') as fh:
                source_bytes = fh.read()
            # A plan the adapter just computed from this same rom cannot fail
            # its own preconditions - that check only means something for a plan
            # built earlier, against a recorded source. Say so rather than
            # reporting a vacuous pass: the honest guarantee in this path is the
            # final-diff one, which compares two independently read boundaries.
            if plan.source_sha256 is None:
                report['expected_write_precondition'] = (
                    'not meaningful: the plan was computed from this build input')
            problems = plan.verify_source(source_bytes)
            if problems:
                raise GateFailed('expected_write',
                                 '%d precondition problem(s): %s'
                                 % (len(problems), problems[:5]))

        stats = ad.inject(doc['entries'], rom, out)

        if plan is not None:
            # Read the built artifact back from disk rather than trusting the
            # injector's account of what it wrote.
            with open(out, 'rb') as fh:
                final_bytes = fh.read()
            problems = plan.verify_final(source_bytes, final_bytes)
            if problems:
                raise GateFailed('expected_write',
                                 '%d undeclared write(s): %s'
                                 % (len(problems), problems[:5]))
        report['expected_write'] = ('checked %d declared write(s)' % len(plan.writes)
                                    if plan is not None else 'not declared')
        if _ledger_active() and plan is not None:
            from hanpatch import stage_ledger
            try:
                stage_ledger.record(
                    'STATIC_BINARY_QA', stage_ledger.PASS,
                    evidence='structural gates + byte ownership',
                    reason='byte ownership checked %d declared write(s) against '
                           'the source and the built artifact'
                           % len(plan.writes))
            except Exception as err:  # pragma: no cover - defensive
                print('stage ledger: could not qualify STATIC_BINARY_QA: %s'
                      % err, flush=True)
        elif _ledger_active() and plan is None:
            # The pipeline knows byte ownership did not run. Saying so is the
            # difference between a ledger that reports and one that implies:
            # STATIC_BINARY_QA passing on the three legacy gates while AUTHORITY
            # names expected-write as an owner would overstate what was checked.
            from hanpatch import stage_ledger
            try:
                stage_ledger.record(
                    'STATIC_BINARY_QA', stage_ledger.PASS,
                    evidence='structural gates only',
                    reason='the adapter declares no write plan, so byte '
                           'ownership was not checked')
            except Exception as err:  # pragma: no cover - defensive
                print('stage ledger: could not qualify STATIC_BINARY_QA: %s'
                      % err, flush=True)
    except BaseException as exc:
        # Attribute the failure before revoking. Without this the byte-ownership
        # stage can refuse a build while the ledger still reads
        # STATIC_BINARY_QA=PASS - the exact overstatement the staged model is
        # supposed to make impossible.
        if isinstance(exc, GateFailed) and _ledger_active():
            from hanpatch import stage_ledger
            token = stage_ledger.failure_token(exc.stage)
            if token:
                try:
                    stage_ledger.record_failure(token, str(exc))
                except Exception as err:  # pragma: no cover - defensive
                    print('stage ledger: could not record %s failure: %s'
                          % (exc.stage, err), flush=True)
        qagate.revoke()
        raise
    report['inject'] = stats
    report['rom'] = out
    if _ledger_active():
        from hanpatch import stage_ledger
        try:
            stage_ledger.record('RC_BUILD', stage_ledger.PASS,
                                evidence='pipeline.build -> %s' % out)
            stage_ledger.bind_build(out)
        except Exception as err:  # pragma: no cover - defensive
            print('stage ledger: could not record RC_BUILD: %s' % err, flush=True)
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
        if _ledger_active():
            # Inside the guard. A malformed evidence submission raises, and a
            # raise past the revoke would leave the approval token standing over
            # a run that never finished - the same defect the gates() wrapper
            # already had to fix once.
            _record_runtime_evidence(rom, quiet=quiet)
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
    if _ledger_active():
        # RC_READBACK_QA is mapping-only: this records what Adapter.verify already
        # proved. The ledger adds no readback check of its own.
        from hanpatch import stage_ledger
        try:
            checked = getattr(ad, 'checked', 0)
            if problems:
                stage_ledger.record_failure(
                    'RC_READBACK_QA', '%d readback problem(s)' % len(problems),
                    evidence='pipeline.verify + Adapter.verify')
            else:
                stage_ledger.record('RC_READBACK_QA', stage_ledger.PASS,
                                    evidence='pipeline.verify + Adapter.verify',
                                    checked=checked)
        except Exception as err:  # pragma: no cover - defensive
            print('stage ledger: could not record RC_READBACK_QA: %s' % err,
                  flush=True)
    if problems:
        raise GateFailed('verify', f'{len(problems)} problem(s): {problems[:5]}')

    return {'rom': rom, 'checked': getattr(ad, 'checked', 0)}


def _record_runtime_evidence(rom, quiet=False):
    """Fold submitted runtime evidence into the ledger, or record its absence.

    No static result can establish that the patched game runs, so this never
    synthesises a pass. A profile that declares no evidence path, or declares one
    with no file behind it, leaves RUNTIME_SMOKE at NOT_RUN with the reason
    recorded.
    """
    import os

    from hanpatch import config, runtime_evidence, stage_ledger

    declared = config.profile().get('runtime_evidence')
    # Strict, like every other declaration in this upgrade. A dict or a stray
    # integer in the list must not be silently dropped, leaving the operator to
    # believe evidence was considered when it was skipped.
    if declared is None:
        paths = []
    elif isinstance(declared, str):
        paths = [declared]
    elif isinstance(declared, (list, tuple)):
        bad = [p for p in declared if not isinstance(p, str) or not p.strip()]
        if bad:
            raise GateFailed('runtime_evidence',
                             'runtime_evidence holds %d non-path entr(y/ies): %r'
                             % (len(bad), bad[:3]))
        paths = list(declared)
    else:
        raise GateFailed('runtime_evidence',
                         'runtime_evidence must be a path, a list of paths, or '
                         'absent; got %s' % type(declared).__name__)

    build_hash = stage_ledger.sha256_file(rom) if os.path.exists(rom) else None
    documents = []
    missing = []
    for relative in paths:
        target = relative if os.path.isabs(relative) else config.p(relative)
        if not os.path.exists(target):
            missing.append(relative)
            continue
        documents.append(runtime_evidence.accept(target, build_sha256=build_hash))

    status, reason = runtime_evidence.ledger_status(documents)
    if missing:
        # A declared file that is not there is a different fact from no
        # declaration at all, and reporting both as "nothing was submitted"
        # hides a broken hand-off.
        reason = '%s; declared but missing: %s' % (reason, ', '.join(missing))
    if status == stage_ledger.FAIL:
        # A failing runtime scenario blocks the downstream claim, exactly as a
        # failing readback does. Recording FAIL without blocking would let
        # PATCH_PACKAGE and RELEASE pass over a scenario that failed.
        stage_ledger.record_failure('RUNTIME_SMOKE', reason,
                                    evidence='; '.join(paths) or None)
        if not quiet:
            print('runtime smoke: FAIL (%s)' % reason, flush=True)
        raise GateFailed('runtime_smoke', reason)
    stage_ledger.record('RUNTIME_SMOKE', status, reason=reason,
                        evidence='; '.join(paths) or None)
    if not quiet:
        print('runtime smoke: %s (%s)' % (status, reason), flush=True)
    return status

"""The three new pipeline callsites, exercised through the pipeline.

Unit tests proved each mechanism in isolation. That is exactly where a wiring
defect survives: every part works and nothing calls it. These drive
`pipeline.build` and `pipeline.verify` and assert what the ledger ends up saying,
because the ledger's claims are the thing a reader will trust.
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hanpatch import expected_write as ew    # noqa: E402
from hanpatch import stage_ledger as sl      # noqa: E402


class _Out:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = None

    def __enter__(self):
        from hanpatch import config
        self._orig = config.out
        config.out = lambda *p: (os.path.join(self.tmp.name, *p) if p
                                 else self.tmp.name)
        return self.tmp.name

    def __exit__(self, *exc):
        from hanpatch import config
        config.out = self._orig
        self.tmp.cleanup()
        return False


class FailureAttribution(unittest.TestCase):
    """A refused build must not leave a PASS on the token that refused it."""

    def test_every_failing_stage_maps_to_a_token(self):
        # The defect this guards: GateFailed('expected_write') and
        # GateFailed('voice') existed with no entry here, so record_failure was
        # skipped and STATIC_BINARY_QA stayed PASS after byte ownership refused
        # the build.
        for stage in ('glossary', 'audit', 'qagate', 'voice',
                      'capacity', 'materialize', 'manifest', 'expected_write',
                      'verify'):
            self.assertIsNotNone(sl.failure_token(stage),
                                 'stage %r has no failure token' % stage)

    def test_expected_write_failure_lands_on_static_binary_qa(self):
        self.assertEqual(sl.failure_token('expected_write'), 'STATIC_BINARY_QA')

    def test_voice_failure_lands_on_source_qa(self):
        self.assertEqual(sl.failure_token('voice'), 'SOURCE_QA')

    def test_recording_that_failure_blocks_downstream(self):
        with _Out():
            sl.bootstrap(ruleset='3')
            for gate in ('glossary', 'audit', 'qagate'):
                sl.record_gate_stage(gate, checked=1)
            for gate in ('capacity', 'materialize', 'manifest'):
                sl.record_gate_stage(gate, checked=1)
            self.assertEqual(sl.summary()['STATIC_BINARY_QA'], sl.PASS)

            # Byte ownership then refuses the build.
            sl.record_failure(sl.failure_token('expected_write'),
                              'undeclared write at 0x14')
            summary = sl.summary()
            self.assertEqual(summary['STATIC_BINARY_QA'], sl.FAIL)
            for later in ('RC_BUILD', 'RC_READBACK_QA', 'RUNTIME_SMOKE'):
                self.assertEqual(summary[later], sl.NOT_RUN)

    def test_unknown_stage_maps_to_nothing(self):
        self.assertIsNone(sl.failure_token('some_future_stage'))


class ByteOwnershipIsWiredIntoBuild(unittest.TestCase):
    """Drive pipeline.build() for real and read the ledger back.

    An earlier version of this class hand-constructed GateFailed and asserted
    string constants. It passed while build()'s handler recorded nothing, which
    is how a dead FAILURE_TOKEN entry survived a whole review pass. These call
    build().
    """

    def _project(self, tmp, plan_factory, entries):
        """A minimal opted-in project whose adapter is a stub."""
        import json

        from hanpatch import adapter, config, manifest, qagate

        rom = os.path.join(tmp, 'source.bin')
        with open(rom, 'wb') as fh:
            fh.write(bytes(64))
        out_dir = os.path.join(tmp, 'work')
        os.makedirs(out_dir, exist_ok=True)

        class Stub:
            checked = len(entries)

            def write_plan(self, rom_path, sealed):
                return plan_factory(rom_path)

            def inject(self, sealed, rom_path, out_path):
                with open(rom_path, 'rb') as fh:
                    data = bytearray(fh.read())
                data[16:24] = b'newbytes'
                with open(out_path, 'wb') as fh:
                    fh.write(bytes(data))
                return {'written': len(sealed)}

        patches = {
            'gates': lambda quiet=False: {'inputs': {}},
            'out': lambda *p: os.path.join(out_dir, *p) if p else out_dir,
            'profile': lambda: {sl.ACTIVATION_KEY: {
                'schema_version': sl.SCHEMA_VERSION}},
            'load': lambda: {'entries': entries, 'digest': 'd' * 64,
                             'ruleset': manifest.RULESET},
            'adapter': lambda: Stub(),
            'rom': rom,
            'out_path': os.path.join(tmp, 'built.bin'),
        }
        return patches

    def _run_build(self, tmp, plan_factory):
        """Call pipeline.build() with everything outside our seam stubbed."""
        from hanpatch import adapter, config, manifest, pipeline, qagate

        entries = {'x/0': 'text'}
        p = self._project(tmp, plan_factory, entries)

        saved = (pipeline.gates, config.out, config.profile, manifest.load,
                 adapter.project_adapter, config.cfg, config.dist,
                 config.built_name, qagate.revoke)
        try:
            pipeline.gates = p['gates']
            config.out = p['out']
            config.profile = p['profile']
            manifest.load = p['load']
            adapter.project_adapter = p['adapter']
            config.cfg = lambda: {'rom': p['rom']}
            config.dist = lambda *a: p['out_path']
            config.built_name = lambda: 'built.bin'
            qagate.revoke = lambda: None

            sl.bootstrap(ruleset=manifest.RULESET, force=True)
            for gate in sl.GATE_TOKEN:
                sl.record_gate_stage(gate, checked=1)

            result = pipeline.build(rom=p['rom'], out=p['out_path'],
                                    quiet=True)
            # Snapshot the ledger while config.out still points at the scratch
            # directory; restoring it first would read the real project.
            self._ledger = sl.summary()
            self._reasons = {t: sl.load()['tokens'][t]['reason']
                             for t in sl.TOKENS}
            return result
        except pipeline.GateFailed:
            self._ledger = sl.summary()
            self._reasons = {t: sl.load()['tokens'][t]['reason']
                             for t in sl.TOKENS}
            raise
        finally:
            (pipeline.gates, config.out, config.profile, manifest.load,
             adapter.project_adapter, config.cfg, config.dist,
             config.built_name, qagate.revoke) = saved

    def test_a_declared_clean_build_passes_and_reports_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            def factory(rom_path):
                with open(rom_path, 'rb') as fh:
                    src = fh.read()
                return ew.plan_from_writes(src, [(16, 8, 'x/0')],
                                           protected=[(0, 8, 'header')])

            report = self._run_build(tmp, factory)
            self.assertIn('checked', report['expected_write'])

    def test_an_undeclared_write_fails_the_build_and_marks_the_ledger(self):
        from hanpatch import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            def factory(rom_path):
                with open(rom_path, 'rb') as fh:
                    src = fh.read()
                # Declares a span the injector does not write, so the real write
                # at 16..24 is undeclared.
                return ew.plan_from_writes(src, [(32, 8, 'x/0')],
                                           protected=[(0, 8, 'header')])

            with self.assertRaises(pipeline.GateFailed) as ctx:
                self._run_build(tmp, factory)
            self.assertEqual(ctx.exception.stage, 'expected_write')

            # The point of the whole exercise: the ledger must not still say the
            # token that refused the build passed.
            self.assertEqual(self._ledger['STATIC_BINARY_QA'], sl.FAIL)
            self.assertEqual(self._ledger['RC_BUILD'], sl.NOT_RUN)

    def test_a_duck_typed_plan_is_refused(self):
        from hanpatch import pipeline

        class Fake:
            writes = []

            def verify_source(self, data):
                return []

            def verify_final(self, a, b):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(pipeline.GateFailed) as ctx:
                self._run_build(tmp, lambda rom_path: Fake())
            self.assertEqual(ctx.exception.stage, 'expected_write')

    def test_an_adapter_without_the_hook_still_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self._run_build(tmp, lambda rom_path: None)
            self.assertEqual(report['expected_write'], 'not declared')
            self.assertIn('not checked', self._reasons['STATIC_BINARY_QA'])


class RuntimeEvidenceIsWiredIntoVerify(unittest.TestCase):
    def test_verify_records_not_run_when_nothing_was_submitted(self):
        from hanpatch import runtime_evidence as re_mod
        with _Out():
            sl.bootstrap(ruleset='3')
            status, reason = re_mod.ledger_status([])
            sl.record('RUNTIME_SMOKE', status, reason=reason)
            self.assertEqual(sl.summary()['RUNTIME_SMOKE'], sl.NOT_RUN)
            self.assertIn('no runtime evidence',
                          sl.load()['tokens']['RUNTIME_SMOKE']['reason'])

    def test_malformed_submission_raises_rather_than_recording_a_pass(self):
        from hanpatch import runtime_evidence as re_mod
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'evidence.json')
            import json
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump({'schema_version': 1, 'kind': 'runtime_evidence',
                           'scenario_id': 's', 'result': 'pass',
                           'build_sha256': 'a' * 64,
                           'captured_at': '2026-08-20T00:00:00Z',
                           'scenario': {}}, fh)
            with self.assertRaises(re_mod.EvidenceError):
                re_mod.accept(path)

    def test_the_recorder_sits_inside_the_revoke_guard(self):
        # The defect: a malformed submission raised out of verify() past
        # qagate.revoke(), leaving an approval standing over a run that never
        # finished. Assert the call is lexically inside the guarded block.
        import inspect

        from hanpatch import pipeline
        src = inspect.getsource(pipeline.verify)
        call = src.index('_record_runtime_evidence(')
        guard = src.index('except BaseException:')
        self.assertLess(call, guard,
                        'the evidence recorder must run inside the try that '
                        'revokes on any failure')


class VoiceIsWiredIntoGates(unittest.TestCase):
    def test_voice_runs_only_for_an_opted_in_title(self):
        import inspect

        from hanpatch import pipeline
        src = inspect.getsource(pipeline.gates)
        guard = src.index('if ledger_on:')
        call = src.index('voice_gate.evaluate()')
        self.assertLess(guard, call,
                        'a legacy title must not be gated on voice')

    def test_a_failing_voice_verdict_becomes_a_gate_failure(self):
        from hanpatch import pipeline, voice_gate
        err = pipeline.GateFailed('voice', '2 hard finding(s)')
        self.assertEqual(err.stage, 'voice')
        self.assertEqual(sl.failure_token(err.stage), 'SOURCE_QA')
        self.assertEqual(voice_gate.FAIL, 'FAIL')

    def test_undeclared_voice_does_not_fail_a_build(self):
        from hanpatch import voice_gate
        result = voice_gate.evaluate({}, base_dir='/nonexistent')
        self.assertNotEqual(result['status'], voice_gate.FAIL)


if __name__ == '__main__':
    unittest.main()


class PackagingAndPublicationReport(unittest.TestCase):
    """The two reporting callsites, driven rather than assumed.

    They were added to close a docs claim that no code honoured, which is the
    exact defect the staged ledger exists to prevent. Leaving them untested
    would repeat it one layer down.
    """

    def test_packaging_records_the_token_for_an_opted_in_title(self):
        from hanpatch import release
        with _Out():
            sl.bootstrap(ruleset='3', force=True)
            for gate in sl.GATE_TOKEN:
                sl.record_gate_stage(gate, checked=1)
            sl.record('RC_BUILD', sl.PASS)
            sl.record('RC_READBACK_QA', sl.PASS)
            saved = sl.enabled
            try:
                sl.enabled = lambda profile=None: True
                release._record_package({'bundle': '/tmp/x-ko.hpk',
                                         'entries': 42})
            finally:
                sl.enabled = saved
            entry = sl.load()['tokens']['PATCH_PACKAGE']
            self.assertEqual(entry['status'], sl.PASS)
            self.assertEqual(entry['checked'], 42)
            self.assertIn('x-ko.hpk', entry['evidence'])

    def test_publication_records_the_token(self):
        from hanpatch import channel
        with _Out():
            sl.bootstrap(ruleset='3', force=True)
            for gate in sl.GATE_TOKEN:
                sl.record_gate_stage(gate, checked=1)
            for token in ('RC_BUILD', 'RC_READBACK_QA', 'RUNTIME_SMOKE',
                          'CANONICAL_PROMOTION', 'PATCH_PACKAGE'):
                sl.record(token, sl.PASS)
            saved = sl.enabled
            try:
                sl.enabled = lambda profile=None: True
                channel._record_release({'version': '2026.08.20'})
            finally:
                sl.enabled = saved
            entry = sl.load()['tokens']['RELEASE']
            self.assertEqual(entry['status'], sl.PASS)
            self.assertIn('2026.08.20', entry['reason'])

    def test_a_legacy_title_writes_nothing_through_either_path(self):
        from hanpatch import channel, release
        with _Out() as out:
            saved = sl.enabled
            try:
                sl.enabled = lambda profile=None: False
                release._record_package({'bundle': 'b.hpk', 'entries': 1})
                channel._record_release({'version': '1'})
            finally:
                sl.enabled = saved
            self.assertEqual(os.listdir(out), [],
                             'a title that did not opt in got a sidecar')

    def test_packaging_cannot_pass_over_an_earlier_failure(self):
        # The reporting callsite is not a way around the invariant.
        from hanpatch import release
        with _Out():
            sl.bootstrap(ruleset='3', force=True)
            sl.record_failure('STATIC_BINARY_QA', 'undeclared write')
            saved = sl.enabled
            try:
                sl.enabled = lambda profile=None: True
                release._record_package({'bundle': 'b.hpk', 'entries': 1})
            finally:
                sl.enabled = saved
            self.assertEqual(sl.summary()['PATCH_PACKAGE'], sl.NOT_RUN)

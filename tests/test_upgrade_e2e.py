"""End to end, without a ROM: every new mechanism connected, on both families.

Unit tests prove each piece in isolation, which is exactly where an integration
defect hides. This walks one flow per fixture family - extract, translate, plan
the write, build, read back, record the staged tokens, submit or withhold runtime
evidence, judge voice, export for interop - and asserts the result at each seam.

Two properties matter more than the happy path:

**Absence stays absent.** With no runtime evidence submitted, RUNTIME_SMOKE ends
NOT_RUN and CANONICAL_PROMOTION never moves, no matter how green everything
static is.

**Tampering stops at the right stage.** Each mutation is checked for *where* it
is caught, not merely that something failed. A defect caught by the wrong stage
means the stage that should have caught it is not doing its job.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hanpatch import expected_write as ew          # noqa: E402
from hanpatch import interop                        # noqa: E402
from hanpatch import runtime_evidence as re_mod     # noqa: E402
from hanpatch import stage_ledger as sl             # noqa: E402
from hanpatch import voice_gate as vg               # noqa: E402
from tests.fixtures.upgrade import fxr1, iar1       # noqa: E402

SOURCE_TEXT = {1: 'alpha', 2: 'beta'}
TARGET_TEXT = {1: '알파', 2: '베타'}


class _Out:
    """Point config.out() at a scratch directory."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = None

    def __enter__(self):
        from hanpatch import config
        self._orig = config.out
        config.out = lambda *parts: (
            os.path.join(self.tmp.name, *parts) if parts else self.tmp.name)
        return self.tmp.name

    def __exit__(self, *exc):
        from hanpatch import config
        config.out = self._orig
        self.tmp.cleanup()
        return False


def sha256_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


class Flow:
    """One pass through the pipeline for a single fixture family."""

    def __init__(self, family):
        self.family = family
        if family == 'fxr1':
            self.source = fxr1.build(list(SOURCE_TEXT.items()))
        else:
            self.source = iar1.build(list(SOURCE_TEXT.items()))

    def extract(self):
        module = fxr1 if self.family == 'fxr1' else iar1
        return module.entries(self.source)

    def sealed(self):
        prefix = self.family
        return {'%s/%d' % (prefix, k): v for k, v in TARGET_TEXT.items()}

    def plan(self):
        if self.family == 'fxr1':
            writes = []
            for index in range(len(SOURCE_TEXT)):
                writes.extend(fxr1.writable_spans(index))
            return ew.plan_from_writes(
                self.source, writes,
                protected=fxr1.protected_spans(len(SOURCE_TEXT)))
        index_off, index_len = iar1.index_span(len(SOURCE_TEXT))
        arena_off, arena_len = iar1.arena_span()
        return ew.plan_from_writes(
            self.source,
            [(index_off, index_len, 'iar1:index'),
             (arena_off, arena_len, 'iar1:arena')],
            protected=iar1.protected_spans(len(SOURCE_TEXT)))

    def build(self):
        if self.family == 'fxr1':
            out = self.source
            for index, stable_id in enumerate(sorted(TARGET_TEXT)):
                out = fxr1.write_text(out, index, TARGET_TEXT[stable_id])
            return out
        out = self.source
        for index, stable_id in enumerate(sorted(TARGET_TEXT)):
            out = iar1.rebuild_with(out, index, TARGET_TEXT[stable_id])
        return out

    def readback(self, built):
        module = fxr1 if self.family == 'fxr1' else iar1
        return module.entries(built)


class BothFamiliesRunEndToEnd(unittest.TestCase):
    def _run(self, family):
        flow = Flow(family)
        profile = {sl.ACTIVATION_KEY: {'schema_version': sl.SCHEMA_VERSION}}

        with _Out():
            self.assertTrue(sl.enabled(profile))
            sl.bootstrap(manifest_digest='seal-%s' % family, ruleset='3')

            # source QA + static binary QA, projected from the gate names
            for gate in ('glossary', 'audit', 'qagate'):
                sl.record_gate_stage(gate, checked=len(SOURCE_TEXT))
            for gate in ('capacity', 'materialize', 'manifest'):
                sl.record_gate_stage(gate, checked=len(SOURCE_TEXT))

            source_entries = flow.extract()
            self.assertEqual(len(source_entries), len(SOURCE_TEXT))

            plan = flow.plan()
            self.assertEqual(plan.verify_source(flow.source), [])

            built = flow.build()
            self.assertEqual(plan.verify_final(flow.source, built), [])
            sl.record('RC_BUILD', sl.PASS, evidence='fixture build')

            entries = flow.readback(built)
            self.assertEqual(set(entries.values()), set(TARGET_TEXT.values()))
            sl.record('RC_READBACK_QA', sl.PASS, checked=len(entries))

            build_hash = sha256_bytes(built)
            summary = sl.summary()
            return flow, built, build_hash, summary

    def test_fxr1_end_to_end(self):
        flow, built, build_hash, summary = self._run('fxr1')
        self.assertEqual(summary['SOURCE_QA'], sl.PASS)
        self.assertEqual(summary['STATIC_BINARY_QA'], sl.PASS)
        self.assertEqual(summary['RC_BUILD'], sl.PASS)
        self.assertEqual(summary['RC_READBACK_QA'], sl.PASS)

    def test_iar1_end_to_end(self):
        flow, built, build_hash, summary = self._run('iar1')
        self.assertEqual(summary['SOURCE_QA'], sl.PASS)
        self.assertEqual(summary['RC_READBACK_QA'], sl.PASS)


class AbsenceStaysAbsent(unittest.TestCase):
    def test_a_fully_green_static_run_leaves_runtime_not_run(self):
        with _Out():
            sl.bootstrap(ruleset='3')
            for gate in sl.GATE_TOKEN:
                sl.record_gate_stage(gate, checked=2)
            sl.record('RC_BUILD', sl.PASS)
            sl.record('RC_READBACK_QA', sl.PASS)

            status, reason = re_mod.ledger_status([])
            sl.record('RUNTIME_SMOKE', status, reason=reason)

            summary = sl.summary()
            self.assertEqual(summary['STATIC_BINARY_QA'], sl.PASS)
            self.assertEqual(summary['RC_READBACK_QA'], sl.PASS)
            self.assertEqual(summary['RUNTIME_SMOKE'], sl.NOT_RUN)
            self.assertEqual(summary['CANONICAL_PROMOTION'], sl.NOT_RUN)
            self.assertIn('no runtime evidence',
                          sl.load()['tokens']['RUNTIME_SMOKE']['reason'])

    def test_submitted_evidence_bound_to_the_build_passes(self):
        flow = Flow('fxr1')
        built = flow.build()
        build_hash = sha256_bytes(built)
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.bind_build.__name__          # touch, binding needs a real file
            document = {
                'schema_version': 1, 'kind': 'runtime_evidence',
                'scenario_id': 'whatever-the-profile-declared',
                'result': 'PASS', 'build_sha256': build_hash,
                'captured_at': '2026-08-20T00:00:00Z',
                'scenario': {'entirely': 'up to the title'},
                'evidence': [], 'interventions': [], 'collector': 'manual',
            }
            self.assertEqual(re_mod.validate(document, build_sha256=build_hash), [])
            status, reason = re_mod.ledger_status([document])
            sl.record('RUNTIME_SMOKE', status, reason=reason)
            self.assertEqual(sl.summary()['RUNTIME_SMOKE'], sl.PASS)

    def test_evidence_for_another_build_does_not_pass(self):
        flow = Flow('fxr1')
        build_hash = sha256_bytes(flow.build())
        document = {
            'schema_version': 1, 'kind': 'runtime_evidence',
            'scenario_id': 's', 'result': 'PASS',
            'build_sha256': 'f' * 64,
            'captured_at': '2026-08-20T00:00:00Z',
            'scenario': {}, 'evidence': [], 'interventions': [],
        }
        self.assertTrue(re_mod.validate(document, build_sha256=build_hash))


class TamperingStopsAtTheRightStage(unittest.TestCase):
    def test_reserved_byte_clobber_is_caught_by_expected_write(self):
        flow = Flow('fxr1')
        built = bytearray(flow.build())
        built[0x14] = 0x01
        findings = flow.plan().verify_final(flow.source, bytes(built))
        self.assertEqual([f.reason for f in findings], [ew.UNREGISTERED_DIFF])
        # And readback alone would not have noticed.
        self.assertEqual(set(fxr1.entries(bytes(built)).values()),
                         set(TARGET_TEXT.values()))

    def test_stale_crc_is_caught_by_readback_not_by_byte_ownership(self):
        flow = Flow('iar1')
        built = bytearray(flow.build())
        base = iar1.index_offset(0)
        built[base + 16:base + 20] = (0).to_bytes(4, 'little')
        self.assertEqual(flow.plan().verify_final(flow.source, bytes(built)), [])
        with self.assertRaises(iar1.Iar1Error):
            flow.readback(bytes(built))

    def test_a_failed_stage_blocks_every_later_token(self):
        with _Out():
            sl.bootstrap(ruleset='3')
            for gate in ('glossary', 'audit', 'qagate'):
                sl.record_gate_stage(gate, checked=2)
            sl.record_failure('STATIC_BINARY_QA', 'capacity overflow')
            summary = sl.summary()
            self.assertEqual(summary['SOURCE_QA'], sl.PASS)
            self.assertEqual(summary['STATIC_BINARY_QA'], sl.FAIL)
            for later in ('RC_BUILD', 'RC_READBACK_QA', 'RUNTIME_SMOKE',
                          'CANONICAL_PROMOTION', 'PATCH_PACKAGE', 'RELEASE'):
                self.assertEqual(summary[later], sl.NOT_RUN)


class VoiceAndInteropConnect(unittest.TestCase):
    def test_undeclared_voice_passes_and_is_stated(self):
        result = vg.evaluate({}, base_dir='/nonexistent')
        self.assertEqual(result['status'], vg.NOT_DECLARED)
        self.assertIn('NOT_DECLARED', vg.summary_line(result))

    def test_export_carries_the_axes_and_the_seal(self):
        flow = Flow('fxr1')
        sealed = flow.sealed()
        source_entries = {k: v for k, v in
                          zip(sorted(sealed), sorted(flow.extract().values()))}
        languages = interop.LanguageMap(evidence='ja', target='ko')
        doc = interop.export_host_rows(sealed, languages,
                                       source_entries=source_entries,
                                       manifest_digest='seal', manifest_ruleset='3')
        self.assertEqual(doc['direction'], 'ja->ko')
        self.assertEqual(doc['manifestRuleset'], '3')
        self.assertEqual(sorted(doc['families']), ['fxr1'])


class NoRomOrEmulatorInTheFlow(unittest.TestCase):
    def test_no_rom_file_is_required(self):
        # The whole flow above ran on bytes built in memory. Assert the fixtures
        # expose no path-taking entry point that would need one.
        for module in (fxr1, iar1):
            for name in dir(module):
                if name.startswith('_'):
                    continue
                attribute = getattr(module, name)
                if callable(attribute) and hasattr(attribute, '__code__'):
                    args = attribute.__code__.co_varnames[
                        :attribute.__code__.co_argcount]
                    self.assertNotIn('path', args,
                                     '%s.%s takes a path' % (module.__name__, name))

    def test_no_emulator_module_is_imported_anywhere_in_the_flow(self):
        leaked = [m for m in sys.modules if 'emucap' in m]
        self.assertEqual(leaked, [])


class NewContainersNeedAFixtureNotAGateBranch(unittest.TestCase):
    """A third family must arrive as a fixture, never as a branch in the gate."""

    def test_the_generic_checker_has_no_family_vocabulary(self):
        with open(os.path.join(ROOT, 'hanpatch', 'expected_write.py'),
                  encoding='utf-8') as fh:
            src = fh.read()
        for token in ('fxr', 'iar', 'family ==', 'container_type'):
            self.assertNotIn(token, src.lower())

    def test_a_third_family_would_reuse_the_same_api(self):
        # A synthetic third shape verifies through the identical entry points,
        # which is what "add a fixture, not a branch" means in practice.
        source = bytes(range(64))
        plan = ew.plan_from_writes(source, [(16, 8, 'third/0')],
                                   protected=[(0, 8, 'header'),
                                              (56, 8, 'tail')])
        final = bytearray(source)
        final[16:24] = b'newbytes'
        self.assertEqual(plan.verify(source, bytes(final)), [])
        final[0] = 0xFF
        self.assertEqual(
            [f.reason for f in plan.verify(source, bytes(final))],
            [ew.UNREGISTERED_DIFF])


if __name__ == '__main__':
    unittest.main()

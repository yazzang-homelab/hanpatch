"""Runtime evidence: arbitrary scenarios pass, malformed envelopes do not.

Two failure modes are equally bad. A checker that enforces scene semantics turns
a generic pipeline into a single-genre one. A checker that accepts anything is
decoration. These tests pin both edges: any scenario content at all is fine, and
every structural field is checked.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hanpatch import runtime_evidence as re_mod  # noqa: E402

BUILD = 'a' * 64
SOURCE = 'b' * 64


def envelope(**overrides):
    doc = {
        'schema_version': 1,
        'kind': 'runtime_evidence',
        'scenario_id': 'boot-and-first-line',
        'result': 'PASS',
        'build_sha256': BUILD,
        'source_sha256': SOURCE,
        'captured_at': '2026-08-20T00:00:00Z',
        'scenario': {'anything': 'the profile decides'},
        'expected': None,
        'observed': None,
        'evidence': [],
        'interventions': [],
        'collector': 'whatever the operator used',
    }
    doc.update(overrides)
    return doc


class ScenarioContentIsNotConstrained(unittest.TestCase):
    """The profile owns what a smoke test means."""

    def test_arbitrary_scenario_shapes_all_pass(self):
        for scenario in (
            {'scene': 'title screen'},
            {'steps': ['boot', 'press start', 'read line 1']},
            ['a', 'list', 'is', 'fine'],
            'a bare string is fine',
            42,
            {'deeply': {'nested': {'but': {'bounded': True}}}},
            None,
        ):
            self.assertEqual(
                re_mod.validate(envelope(scenario=scenario)), [],
                'scenario %r must be accepted; content is the profile''s call'
                % (scenario,))

    def test_expected_and_observed_are_opaque(self):
        doc = envelope(expected={'frames': [1, 2, 3]},
                       observed='the operator wrote prose here')
        self.assertEqual(re_mod.validate(doc), [])

    def test_no_scene_vocabulary_is_hardcoded(self):
        with open(os.path.join(ROOT, 'hanpatch', 'runtime_evidence.py'),
                  encoding='utf-8') as fh:
            src = fh.read()
        for word in ('boot', 'menu', 'boss', 'title_screen', 'save_load',
                     'first_line'):
            self.assertNotIn("'%s'" % word, src)
            self.assertNotIn('"%s"' % word, src)

    def test_module_knows_nothing_about_emucap(self):
        with open(os.path.join(ROOT, 'hanpatch', 'runtime_evidence.py'),
                  encoding='utf-8') as fh:
            src = fh.read()
        import ast
        modules = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                modules.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        self.assertFalse([m for m in modules if 'emucap' in m])


class StructureIsChecked(unittest.TestCase):
    def test_a_well_formed_envelope_passes(self):
        self.assertEqual(re_mod.validate(envelope()), [])

    def test_non_object_is_refused(self):
        self.assertTrue(re_mod.validate([]))
        self.assertTrue(re_mod.validate('nope'))

    def test_wrong_schema_version_and_kind(self):
        self.assertTrue(re_mod.validate(envelope(schema_version=99)))
        self.assertTrue(re_mod.validate(envelope(kind='something-else')))

    def test_scenario_id_must_be_present_and_meaningful(self):
        for bad in ('', '   ', None, 42):
            self.assertTrue(re_mod.validate(envelope(scenario_id=bad)),
                            'scenario_id %r must be refused' % (bad,))

    def test_result_is_closed_and_not_run_is_not_claimable(self):
        self.assertEqual(re_mod.validate(envelope(result='PASS')), [])
        self.assertEqual(re_mod.validate(envelope(result='FAIL')), [])
        for bad in ('NOT_RUN', 'pass', 'ok', None, True):
            problems = re_mod.validate(envelope(result=bad))
            self.assertTrue(problems, 'result %r must be refused' % (bad,))
        self.assertIn('NOT_RUN',
                      ' '.join(re_mod.validate(envelope(result='NOT_RUN'))))

    def test_hashes_must_be_hashes(self):
        for bad in ('A' * 64, 'abc', 'g' * 64, 123, None):
            self.assertTrue(re_mod.validate(envelope(build_sha256=bad)))

    def test_timestamp_must_be_rfc3339(self):
        for bad in ('yesterday', '2026-08-20', '', None):
            self.assertTrue(re_mod.validate(envelope(captured_at=bad)))
        self.assertEqual(
            re_mod.validate(envelope(captured_at='2026-08-20T00:00:00+09:00')), [])

    def test_scenario_key_is_required_even_though_opaque(self):
        doc = envelope()
        del doc['scenario']
        self.assertTrue(re_mod.validate(doc))

    def test_evidence_items_are_structurally_checked(self):
        good = envelope(evidence=[{'id': 'shot-1', 'sha256': 'c' * 64,
                                   'size': 1024, 'kind': 'screenshot'}])
        self.assertEqual(re_mod.validate(good), [])
        for bad_item in ({'id': '', 'sha256': 'c' * 64, 'size': 1},
                         {'id': 'x', 'sha256': 'nope', 'size': 1},
                         {'id': 'x', 'sha256': 'c' * 64, 'size': -1},
                         {'id': 'x', 'sha256': 'c' * 64, 'size': True},
                         'not an object'):
            self.assertTrue(re_mod.validate(envelope(evidence=[bad_item])),
                            'evidence item %r must be refused' % (bad_item,))

    def test_evidence_kind_is_free_text(self):
        for kind in ('screenshot', 'savestate', 'a thing I made up', None):
            doc = envelope(evidence=[{'id': 'e', 'sha256': 'c' * 64,
                                      'size': 1, 'kind': kind}])
            self.assertEqual(re_mod.validate(doc), [])

    def test_oversize_and_overdeep_are_refused(self):
        deep = {'a': None}
        cursor = deep
        for _ in range(re_mod.MAX_DEPTH + 3):
            cursor['a'] = {'a': None}
            cursor = cursor['a']
        self.assertTrue(re_mod.validate(envelope(scenario=deep)))

        huge = envelope(scenario={'blob': 'x' * (re_mod.MAX_BYTES + 10)})
        self.assertTrue(re_mod.validate(huge))

        self.assertTrue(re_mod.validate(
            envelope(evidence=[{'id': 'e', 'sha256': 'c' * 64, 'size': 1}]
                     * (re_mod.MAX_EVIDENCE_ITEMS + 1))))

    def test_all_problems_are_reported_together(self):
        problems = re_mod.validate(envelope(schema_version=9, result='nope',
                                            build_sha256='short'))
        self.assertGreaterEqual(len(problems), 3)


class BuildBinding(unittest.TestCase):
    def test_matching_build_hash_is_accepted(self):
        self.assertEqual(re_mod.validate(envelope(), build_sha256=BUILD), [])

    def test_evidence_for_another_build_is_refused(self):
        problems = re_mod.validate(envelope(), build_sha256='d' * 64)
        self.assertTrue(any('describes build' in p for p in problems))

    def test_source_binding_is_checked_when_declared(self):
        problems = re_mod.validate(envelope(), source_sha256='e' * 64)
        self.assertTrue(any('describes source' in p for p in problems))


class AbsenceIsNeverAPass(unittest.TestCase):
    def test_no_documents_yields_not_run_with_a_reason(self):
        from hanpatch import stage_ledger
        status, reason = re_mod.ledger_status([])
        self.assertEqual(status, stage_ledger.NOT_RUN)
        self.assertIn('no runtime evidence', reason)

    def test_one_failure_fails_the_token(self):
        from hanpatch import stage_ledger
        status, reason = re_mod.ledger_status(
            [envelope(scenario_id='a'), envelope(scenario_id='b', result='FAIL')])
        self.assertEqual(status, stage_ledger.FAIL)
        self.assertIn('b', reason)

    def test_all_passing_yields_pass_naming_the_scenarios(self):
        from hanpatch import stage_ledger
        status, reason = re_mod.ledger_status(
            [envelope(scenario_id='a'), envelope(scenario_id='b')])
        self.assertEqual(status, stage_ledger.PASS)
        self.assertIn('a', reason)
        self.assertIn('b', reason)

    def test_no_api_manufactures_an_envelope(self):
        for name in ('default_evidence', 'synthesise', 'synthesize',
                     'make_pass', 'assume_pass', 'blank'):
            self.assertFalse(hasattr(re_mod, name),
                             'runtime_evidence must not expose %r' % name)

    def test_accept_raises_on_a_missing_file_rather_than_defaulting(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises((OSError, SystemExit)):
                re_mod.accept(os.path.join(tmp, 'nope.json'))


class Loading(unittest.TestCase):
    def test_accept_validates_and_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'evidence.json')
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(envelope(), fh)
            self.assertEqual(re_mod.accept(path, build_sha256=BUILD)['result'],
                             'PASS')

    def test_accept_raises_on_a_malformed_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'evidence.json')
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(envelope(result='NOT_RUN'), fh)
            with self.assertRaises(re_mod.EvidenceError):
                re_mod.accept(path)

    def test_list_shaped_document_is_named_by_the_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'evidence.json')
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump([], fh)
            with self.assertRaises(SystemExit):
                re_mod.accept(path)


if __name__ == '__main__':
    unittest.main()

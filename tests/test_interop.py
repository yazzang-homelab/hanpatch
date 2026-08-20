"""Interop export: the axes must be named, and the join must be strict.

The defect this guards against is invisible to both sides. If the exporter
guesses which language fills `evidence`, it still emits three populated columns
and the reader still reads three populated columns; only the meaning is
transposed, and no downstream check can tell. Hence: no inference, ever.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hanpatch import interop  # noqa: E402

SEALED = {'dialogue/1': '용사가 왔다', 'dialogue/2': '문이 열렸다',
          'system/save': '저장했다'}
SOURCE = {'dialogue/1': '勇者が来た', 'dialogue/2': '扉が開いた',
          'system/save': 'セーブした'}
PIVOT = {'dialogue/1': 'The hero arrived', 'dialogue/2': 'The door opened',
         'system/save': 'Saved'}

#: The directions this project declares support for. Tests below only assert
#: swapped-axis behaviour for these; demanding unsupported permutations would
#: test a claim nobody made.
SUPPORTED_DIRECTIONS = (('ja', 'ko'), ('en', 'ko'))


class LanguageMapIsRequired(unittest.TestCase):
    def test_a_plain_dict_is_refused(self):
        with self.assertRaisesRegex(interop.InteropError, 'LanguageMap'):
            interop.export_host_rows(SEALED, {'evidence': 'ja', 'target': 'ko'},
                                     source_entries=SOURCE)

    def test_missing_language_names_are_refused(self):
        for kwargs in ({'evidence': '', 'target': 'ko'},
                       {'evidence': 'ja', 'target': ''},
                       {'evidence': None, 'target': 'ko'}):
            with self.assertRaises(interop.InteropError):
                interop.LanguageMap(**kwargs)

    def test_identical_evidence_and_target_are_refused(self):
        with self.assertRaisesRegex(interop.InteropError, 'measures nothing'):
            interop.LanguageMap(evidence='ko', target='ko')

    def test_each_supported_direction_round_trips(self):
        for evidence, target in SUPPORTED_DIRECTIONS:
            languages = interop.LanguageMap(evidence=evidence, target=target)
            doc = interop.export_host_rows(SEALED, languages,
                                           source_entries=SOURCE)
            self.assertEqual(doc['languages']['evidence'], evidence)
            self.assertEqual(doc['languages']['target'], target)
            self.assertEqual(doc['direction'], '%s->%s' % (evidence, target))


class SwappedAxisIsVisible(unittest.TestCase):
    """A transposed map must produce a visibly different document."""

    def test_swapping_the_declared_axes_changes_the_document(self):
        for evidence, target in SUPPORTED_DIRECTIONS:
            straight = interop.export_host_rows(
                SEALED, interop.LanguageMap(evidence=evidence, target=target),
                source_entries=SOURCE)
            swapped = interop.export_host_rows(
                SEALED, interop.LanguageMap(evidence=target, target=evidence),
                source_entries=SOURCE)
            self.assertNotEqual(straight['languages'], swapped['languages'])
            self.assertNotEqual(straight['direction'], swapped['direction'])

    def test_the_direction_is_recorded_not_derived_from_content(self):
        # Content cannot disambiguate: the same rows are valid under either
        # reading. Only the declaration distinguishes them.
        languages = interop.LanguageMap(evidence='ja', target='ko')
        doc = interop.export_host_rows(SEALED, languages, source_entries=SOURCE)
        rows = doc['families']['dialogue']
        self.assertEqual(rows[0]['evidence'], SOURCE['dialogue/1'])
        self.assertEqual(rows[0]['target'], SEALED['dialogue/1'])
        self.assertEqual(doc['direction'], 'ja->ko')


class MissingColumnsAreRefused(unittest.TestCase):
    def test_absent_evidence_column_is_refused(self):
        languages = interop.LanguageMap(evidence='ja', target='ko')
        with self.assertRaisesRegex(interop.InteropError, 'evidence column'):
            interop.export_host_rows(SEALED, languages, source_entries=None)

    def test_declared_pivot_without_rows_is_refused(self):
        languages = interop.LanguageMap(evidence='ja', pivot='en', target='ko')
        with self.assertRaisesRegex(interop.InteropError, 'pivot'):
            interop.export_host_rows(SEALED, languages, source_entries=SOURCE)

    def test_pivot_rows_without_a_declared_language_are_refused(self):
        languages = interop.LanguageMap(evidence='ja', target='ko')
        with self.assertRaisesRegex(interop.InteropError, 'no axis'):
            interop.export_host_rows(SEALED, languages, source_entries=SOURCE,
                                     pivot_entries=PIVOT)

    def test_an_entry_without_evidence_text_is_refused(self):
        languages = interop.LanguageMap(evidence='ja', target='ko')
        partial = dict(SOURCE)
        del partial['system/save']
        with self.assertRaisesRegex(interop.InteropError, 'no evidence-side'):
            interop.export_host_rows(SEALED, languages, source_entries=partial)

    def test_pivot_export_populates_all_three_roles(self):
        languages = interop.LanguageMap(evidence='ja', pivot='en', target='ko')
        doc = interop.export_host_rows(SEALED, languages, source_entries=SOURCE,
                                       pivot_entries=PIVOT)
        row = doc['families']['dialogue'][0]
        self.assertEqual(row['evidence'], SOURCE['dialogue/1'])
        self.assertEqual(row['pivot'], PIVOT['dialogue/1'])
        self.assertEqual(row['target'], SEALED['dialogue/1'])


class KeyShape(unittest.TestCase):
    def test_a_key_without_a_family_is_refused(self):
        languages = interop.LanguageMap(evidence='ja', target='ko')
        for bad in ({'nofamily': 'x'}, {'/empty': 'x'}, {'family/': 'x'}):
            with self.assertRaises(interop.InteropError):
                interop.export_host_rows(bad, languages,
                                         source_entries={list(bad)[0]: 'y'})

    def test_families_group_by_prefix(self):
        languages = interop.LanguageMap(evidence='ja', target='ko')
        doc = interop.export_host_rows(SEALED, languages, source_entries=SOURCE)
        self.assertEqual(sorted(doc['families']), ['dialogue', 'system'])
        self.assertEqual(len(doc['families']['dialogue']), 2)

    def test_non_string_target_is_refused(self):
        languages = interop.LanguageMap(evidence='ja', target='ko')
        with self.assertRaises(interop.InteropError):
            interop.export_host_rows({'a/b': 42}, languages,
                                     source_entries={'a/b': 'x'})


class Persistence(unittest.TestCase):
    def test_write_is_atomic_and_round_trips(self):
        languages = interop.LanguageMap(evidence='ja', target='ko')
        doc = interop.export_host_rows(SEALED, languages, source_entries=SOURCE,
                                       manifest_digest='d' * 64,
                                       manifest_ruleset='3')
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'rows.json')
            interop.write(doc, path)
            leftovers = [f for f in os.listdir(tmp) if f.startswith('.host-rows-')]
            self.assertEqual(leftovers, [])
            with open(path, encoding='utf-8') as fh:
                self.assertEqual(json.load(fh), doc)

    def test_the_export_carries_the_seal_it_describes(self):
        languages = interop.LanguageMap(evidence='ja', target='ko')
        doc = interop.export_host_rows(SEALED, languages, source_entries=SOURCE,
                                       manifest_digest='d' * 64,
                                       manifest_ruleset='3')
        self.assertEqual(doc['manifestDigest'], 'd' * 64)
        self.assertEqual(doc['manifestRuleset'], '3')


if __name__ == '__main__':
    unittest.main()

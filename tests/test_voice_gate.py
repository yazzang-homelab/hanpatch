"""Voice gate: opt-in, provenance binding, and the no-duplication boundary.

The gate must be usable by a project that has no speech contract at all - which
is most of them - so "undeclared" passes and says so. Everything else is a
provenance check on a document another repository produced.
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hanpatch import voice_gate as vg  # noqa: E402


def canonical_sha256(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(',', ':'),
                   ensure_ascii=False).encode('utf-8')).hexdigest()


CONTRACT = {'character_id': 'hatori', 'source_lang': 'ja',
            'catalogue_version': '1', 'axes': {}}


def _file_sha256(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def write_pair(base, contract=None, verdict_overrides=None, file_hash=True):
    contract = contract or CONTRACT
    contract_path = os.path.join(base, 'contract.json')
    with open(contract_path, 'w', encoding='utf-8') as fh:
        json.dump(contract, fh)

    document = {
        'schemaVersion': 1,
        'kind': vg.DOCUMENT_KIND,
        'targetLang': 'ko',
        'contractSha256': canonical_sha256(contract),
        'contractFileSha256': (_file_sha256(contract_path) if file_hash else None),
        'verdict': 'pass',
        'hardFindingCount': 0,
        'findings': [],
        'gateStatuses': {},
    }
    document.update(verdict_overrides or {})
    verdict_path = os.path.join(base, 'verdict.json')
    with open(verdict_path, 'w', encoding='utf-8') as fh:
        json.dump(document, fh)
    return contract_path, verdict_path


def profile_for(contract=None):
    return {
        'voice_authority': 'translator_declared',
        'voice_contract': {
            'schema_version': 1,
            'verdict_path': 'verdict.json',
            'contract_path': 'contract.json',
            'contract_sha256': canonical_sha256(contract or CONTRACT),
        },
    }


class Undeclared(unittest.TestCase):
    def test_absent_key_is_not_declared_and_passes(self):
        result = vg.evaluate({}, base_dir='/nonexistent')
        self.assertEqual(result['status'], vg.NOT_DECLARED)
        self.assertFalse(result['declared'])
        self.assertIsNone(result['authority'])

    def test_explicit_null_is_not_declared_and_passes(self):
        result = vg.evaluate({'voice_contract': None}, base_dir='/nonexistent')
        self.assertEqual(result['status'], vg.NOT_DECLARED)

    def test_summary_states_the_absence_rather_than_implying_coverage(self):
        line = vg.summary_line(vg.evaluate({}, base_dir='/nonexistent'))
        self.assertIn('NOT_DECLARED', line)
        self.assertNotIn('PASS', line)

    def test_shipped_profiles_are_undeclared(self):
        for name in ('dq7.json', 'crimson_shroud.json', 'classic_dungeon_x2.json'):
            path = os.path.join(ROOT, 'profiles', name)
            if not os.path.exists(path):
                continue
            with open(path, encoding='utf-8') as fh:
                profile = json.load(fh)
            self.assertFalse(vg.declared(profile), '%s must stay undeclared' % name)


class PointerShape(unittest.TestCase):
    def test_non_object_pointer_is_refused(self):
        with self.assertRaises(vg.VoiceGateError):
            vg.pointer({'voice_contract': 'contract.json'})

    def test_missing_fields_are_refused(self):
        with self.assertRaises(vg.VoiceGateError) as ctx:
            vg.pointer({'voice_authority': 'translator_declared',
                        'voice_contract': {'schema_version': 1}})
        self.assertIn('missing', str(ctx.exception))

    def test_authority_is_required_and_closed(self):
        base = profile_for()
        for bad in (None, 'invented_authority', ''):
            profile = dict(base, voice_authority=bad)
            with self.assertRaises(vg.VoiceGateError):
                vg.pointer(profile)

    def test_both_authorities_are_accepted_but_distinct(self):
        for authority in vg.AUTHORITIES:
            profile = dict(profile_for(), voice_authority=authority)
            self.assertEqual(vg.pointer(profile)['voice_authority'], authority)


class ProvenanceBinding(unittest.TestCase):
    def test_matching_verdict_passes_and_records_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_pair(tmp)
            result = vg.evaluate(profile_for(), base_dir=tmp)
            self.assertEqual(result['status'], vg.PASS)
            self.assertEqual(result['authority'], 'translator_declared')

    def test_hard_findings_fail_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_pair(tmp, verdict_overrides={
                'verdict': 'fail', 'hardFindingCount': 2,
                'findings': [{'code': 'marker_loss'}, {'code': 'marker_invention'}]})
            result = vg.evaluate(profile_for(), base_dir=tmp)
            self.assertEqual(result['status'], vg.FAIL)
            self.assertEqual(len(result['findings']), 2)

    def test_contract_swapped_after_sealing_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = dict(CONTRACT, character_id='someone-else')
            write_pair(tmp, contract=other)
            with self.assertRaisesRegex(vg.VoiceGateError, 'pins contract'):
                vg.evaluate(profile_for(), base_dir=tmp)

    def test_verdict_for_a_different_contract_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_pair(tmp, verdict_overrides={'contractSha256': 'f' * 64})
            with self.assertRaisesRegex(vg.VoiceGateError, 'judged contract'):
                vg.evaluate(profile_for(), base_dir=tmp)

    def test_reformatted_contract_still_matches_the_semantic_seal(self):
        # Whitespace is not a contract change. A file-hash comparison here would
        # reject a reformat that changed nothing.
        with tempfile.TemporaryDirectory() as tmp:
            contract_path, _ = write_pair(tmp, file_hash=False)
            with open(contract_path, 'w', encoding='utf-8') as fh:
                json.dump(CONTRACT, fh, indent=4)
            result = vg.evaluate(profile_for(), base_dir=tmp)
            self.assertEqual(result['status'], vg.PASS)

    def test_recorded_file_hash_is_checked_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            contract_path, _ = write_pair(tmp, file_hash=True)
            with open(contract_path, 'w', encoding='utf-8') as fh:
                json.dump(CONTRACT, fh, indent=4)
            with self.assertRaisesRegex(vg.VoiceGateError, 'contract bytes'):
                vg.evaluate(profile_for(), base_dir=tmp)

    def test_missing_files_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(vg.VoiceGateError, 'missing'):
                vg.evaluate(profile_for(), base_dir=tmp)

    def test_wrong_document_kind_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_pair(tmp, verdict_overrides={'kind': 'something-else'})
            with self.assertRaisesRegex(vg.VoiceGateError, 'kind'):
                vg.evaluate(profile_for(), base_dir=tmp)

    def test_unknown_verdict_value_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_pair(tmp, verdict_overrides={'verdict': 'probably-fine'})
            with self.assertRaisesRegex(vg.VoiceGateError, 'neither pass nor fail'):
                vg.evaluate(profile_for(), base_dir=tmp)

    def test_absent_verdict_cannot_be_forged_by_omission(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_pair(tmp, verdict_overrides={'verdict': None})
            with self.assertRaises(vg.VoiceGateError):
                vg.evaluate(profile_for(), base_dir=tmp)


class NoVoiceLogicHere(unittest.TestCase):
    """The consumer must stay a provenance checker."""

    def _source(self):
        with open(os.path.join(ROOT, 'hanpatch', 'voice_gate.py'),
                  encoding='utf-8') as fh:
            return fh.read()

    def _code_identifiers(self):
        """Names the module actually uses, ignoring prose.

        Scanning raw text would flag the docstring that explains *why* marker,
        density and corpus logic lives in hancharacter - punishing the
        explanation instead of the duplication. The rule is about code.
        """
        import ast
        names = set()
        for node in ast.walk(ast.parse(self._source())):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # String constants are code too: a regex or a marker table would
                # hide there. Docstrings are excluded separately below.
                names.add(node.value)
        docstrings = set()
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        return names - docstrings

    def test_no_marker_density_or_corpus_logic(self):
        identifiers = self._code_identifiers()
        banned = ('marker_set', 'density', 'corpus', 'ending_transform',
                  'speech_level')
        for name in identifiers:
            lowered = name.lower()
            for token in banned:
                self.assertNotIn(
                    token, lowered,
                    'voice judgement belongs to hancharacter, not here: %r' % name)

    def test_no_regex_engine_is_used(self):
        # A regex here would be marker matching by another name.
        src = self._source()
        self.assertNotIn('import re', src)
        self.assertNotIn('re.compile', src)

    def test_does_not_import_hancharacter(self):
        # AST, not text: the module docstring names hancharacter on purpose, to
        # say where voice judgement lives. Only a real import is a violation.
        import ast
        modules = set()
        for node in ast.walk(ast.parse(self._source())):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    modules.add(node.module)
        for module in modules:
            self.assertFalse(
                module == 'hancharacter' or module.startswith('hancharacter.'),
                'the consumer must not import the producer: %r' % module)

    def test_reads_state_documents_through_the_validating_loader(self):
        src = self._source()
        for line in src.splitlines():
            if 'json.load(' in line:
                self.assertIn('load_object', line)


if __name__ == '__main__':
    unittest.main()

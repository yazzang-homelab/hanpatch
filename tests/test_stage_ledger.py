"""Stage ledger: activation, token transitions, binding, and authority limits.

The tests that matter here are the negative ones. A ledger that can be talked
into showing eight passes, or into becoming a second approval authority, is
worse than no ledger: it launders an unproven claim into a green report.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hanpatch import stage_ledger as sl  # noqa: E402


class _Out:
    """Redirect config.out() at a temp dir without touching a project."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = None

    def __enter__(self):
        from hanpatch import config
        self._orig = config.out

        def fake_out(*parts):
            d = self.tmp.name
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, *parts) if parts else d

        config.out = fake_out
        return self.tmp.name

    def __exit__(self, *exc):
        from hanpatch import config
        config.out = self._orig
        self.tmp.cleanup()
        return False


class Activation(unittest.TestCase):
    def test_absent_key_is_legacy_not_an_error(self):
        # Most titles will never set this key. Absence must stay silent.
        self.assertIsNone(sl.activation({}))
        self.assertFalse(sl.enabled({}))

    def test_explicit_null_is_legacy(self):
        # classic_dungeon_x2.json already carries null-ish voice declarations;
        # an explicit null must read as "not opted in", never as "enabled".
        self.assertIsNone(sl.activation({sl.ACTIVATION_KEY: None}))
        self.assertFalse(sl.enabled({sl.ACTIVATION_KEY: None}))

    def test_bare_truthy_flag_is_refused(self):
        # A bare flag cannot carry a schema version, so accepting one would
        # silently pin the title to whatever this build happens to implement.
        for bad in (True, 1, 'yes', ['on']):
            with self.assertRaises(sl.LedgerError):
                sl.activation({sl.ACTIVATION_KEY: bad})

    def test_wrong_schema_version_is_refused(self):
        with self.assertRaises(sl.LedgerError):
            sl.activation({sl.ACTIVATION_KEY: {'schema_version': 99}})
        with self.assertRaises(sl.LedgerError):
            sl.activation({sl.ACTIVATION_KEY: {}})

    def test_valid_activation_returns_a_copy(self):
        prof = {sl.ACTIVATION_KEY: {'schema_version': sl.SCHEMA_VERSION,
                                    'note': 'opted in'}}
        act = sl.activation(prof)
        self.assertEqual(act['schema_version'], sl.SCHEMA_VERSION)
        act['mutated'] = True
        self.assertNotIn('mutated', prof[sl.ACTIVATION_KEY])


class Bootstrap(unittest.TestCase):
    def test_every_token_starts_not_run(self):
        with _Out():
            doc = sl.bootstrap(manifest_digest='abc', ruleset='3')
            self.assertEqual(set(doc['tokens']), set(sl.TOKENS))
            for token in sl.TOKENS:
                self.assertEqual(doc['tokens'][token]['status'], sl.NOT_RUN)

    def test_token_order_is_recorded(self):
        with _Out():
            doc = sl.bootstrap()
            self.assertEqual(doc['tokenOrder'], list(sl.TOKENS))

    def test_bootstrap_is_idempotent_unless_forced(self):
        with _Out():
            sl.bootstrap(manifest_digest='first')
            again = sl.bootstrap(manifest_digest='second')
            self.assertEqual(again['manifestDigest'], 'first')
            forced = sl.bootstrap(manifest_digest='second', force=True)
            self.assertEqual(forced['manifestDigest'], 'second')

    def test_ledger_is_a_sibling_file_not_a_manifest_field(self):
        with _Out() as d:
            sl.bootstrap()
            self.assertTrue(os.path.exists(os.path.join(d, sl.LEDGER_NAME)))
            self.assertFalse(os.path.exists(os.path.join(d, 'manifest.json')))


class Transitions(unittest.TestCase):
    def test_single_gate_does_not_pass_a_multi_gate_token(self):
        # SOURCE_QA needs glossary, audit and qagate. One gate is not the token.
        with _Out():
            sl.bootstrap()
            sl.record_gate_stage('glossary', checked=10)
            self.assertEqual(sl.summary()['SOURCE_QA'], sl.NOT_RUN)

    def test_token_passes_only_when_all_its_gates_reported(self):
        with _Out():
            sl.bootstrap()
            for gate in ('glossary', 'audit', 'qagate'):
                sl.record_gate_stage(gate, checked=3)
            self.assertEqual(sl.summary()['SOURCE_QA'], sl.PASS)

    def test_unmapped_gate_promotes_nothing(self):
        with _Out():
            sl.bootstrap()
            sl.record_gate_stage('some_future_gate', checked=1)
            self.assertEqual(set(sl.summary().values()), {sl.NOT_RUN})

    def test_failure_forces_every_downstream_token_not_run(self):
        with _Out():
            sl.bootstrap()
            for gate in ('glossary', 'audit', 'qagate'):
                sl.record_gate_stage(gate, checked=1)
            sl.record_failure('STATIC_BINARY_QA', 'capacity overflow')

            summary = sl.summary()
            self.assertEqual(summary['SOURCE_QA'], sl.PASS)
            self.assertEqual(summary['STATIC_BINARY_QA'], sl.FAIL)
            for later in sl.TOKENS[sl.TOKENS.index('STATIC_BINARY_QA') + 1:]:
                self.assertEqual(summary[later], sl.NOT_RUN)
                self.assertIn('STATIC_BINARY_QA', sl.load()['tokens'][later]['reason'])

    def test_runtime_and_promotion_never_pass_from_static_success(self):
        # The whole point of the staged model: a fully green static run still
        # leaves the two tokens static evidence cannot establish at NOT_RUN.
        with _Out():
            sl.bootstrap()
            for gate in sl.GATE_TOKEN:
                sl.record_gate_stage(gate, checked=1)
            summary = sl.summary()
            self.assertEqual(summary['SOURCE_QA'], sl.PASS)
            self.assertEqual(summary['STATIC_BINARY_QA'], sl.PASS)
            self.assertEqual(summary['RUNTIME_SMOKE'], sl.NOT_RUN)
            self.assertEqual(summary['CANONICAL_PROMOTION'], sl.NOT_RUN)

    def test_no_api_marks_every_token_at_once(self):
        # A sweep helper is the obvious convenience and the obvious lie. Assert
        # it does not exist, so it cannot be added without failing a test.
        for name in ('pass_all', 'mark_all', 'complete_all', 'all_pass',
                     'record_all'):
            self.assertFalse(hasattr(sl, name),
                             'stage_ledger must not expose %r' % name)

    def test_unknown_token_and_status_are_refused(self):
        with _Out():
            sl.bootstrap()
            with self.assertRaises(sl.LedgerError):
                sl.record('NOT_A_TOKEN', sl.PASS)
            with self.assertRaises(sl.LedgerError):
                sl.record('SOURCE_QA', 'GREEN')


class Authority(unittest.TestCase):
    def test_every_token_declares_an_owner(self):
        for token in sl.TOKENS:
            self.assertIn(token, sl.AUTHORITY)
            self.assertTrue(sl.AUTHORITY[token]['owner'])
            self.assertTrue(sl.AUTHORITY[token]['note'])

    def test_existing_implementations_are_marked_mapping_only(self):
        # These four are already proven by shipped code. Marking them mapping-only
        # is what stops the ledger becoming a competing authority.
        mapping_only = set(sl.mapping_only_tokens())
        for token in ('RC_READBACK_QA', 'PATCH_PACKAGE', 'RELEASE', 'SOURCE_QA'):
            self.assertIn(token, mapping_only)

    def test_ledger_module_does_not_import_authority_modules(self):
        # A ledger that imports release/channel/qagate is one refactor away from
        # calling them. Keep the observation one-directional.
        with open(os.path.join(ROOT, 'hanpatch', 'stage_ledger.py'),
                  encoding='utf-8') as fh:
            src = fh.read()
        for module in ('release', 'channel', 'qagate'):
            self.assertNotIn('import %s' % module, src)
            self.assertNotIn('from hanpatch import %s' % module, src)

    def test_ledger_exposes_no_approval_or_publish_verbs(self):
        for name in ('approve', 'authorise', 'authorize', 'publish', 'revoke',
                     'create_bundle', 'promote'):
            self.assertFalse(hasattr(sl, name),
                             'stage_ledger must not expose %r' % name)


class Binding(unittest.TestCase):
    def test_build_binding_records_the_artifact_hash(self):
        with _Out() as d:
            sl.bootstrap(manifest_digest='digest-1')
            artifact = os.path.join(d, 'built.bin')
            with open(artifact, 'wb') as fh:
                fh.write(b'artifact bytes')
            doc = sl.bind_build(artifact)
            self.assertEqual(doc['buildSha256'], sl.sha256_file(artifact))
            self.assertEqual(sl.is_stale(manifest_digest='digest-1',
                                         build_path=artifact), [])

    def test_moved_manifest_digest_is_reported_stale(self):
        with _Out():
            sl.bootstrap(manifest_digest='digest-1')
            reasons = sl.is_stale(manifest_digest='digest-2')
            self.assertEqual(len(reasons), 1)
            self.assertIn('manifest digest moved', reasons[0])

    def test_rebuilt_artifact_is_reported_stale(self):
        with _Out() as d:
            sl.bootstrap(manifest_digest='digest-1')
            artifact = os.path.join(d, 'built.bin')
            with open(artifact, 'wb') as fh:
                fh.write(b'first build')
            sl.bind_build(artifact)
            with open(artifact, 'wb') as fh:
                fh.write(b'second build')
            reasons = sl.is_stale(manifest_digest='digest-1', build_path=artifact)
            self.assertEqual(len(reasons), 1)
            self.assertIn('build hash moved', reasons[0])

    def test_staleness_is_reported_not_repaired(self):
        with _Out():
            sl.bootstrap(manifest_digest='digest-1')
            sl.is_stale(manifest_digest='digest-2')
            self.assertEqual(sl.load()['manifestDigest'], 'digest-1')


class Durability(unittest.TestCase):
    def test_written_ledger_is_valid_json_with_sorted_keys(self):
        with _Out() as d:
            sl.bootstrap(manifest_digest='abc', ruleset='3')
            with open(os.path.join(d, sl.LEDGER_NAME), encoding='utf-8') as fh:
                raw = fh.read()
            doc = json.loads(raw)
            self.assertEqual(doc['manifestRuleset'], '3')

    def test_no_temp_files_survive_a_write(self):
        with _Out() as d:
            sl.bootstrap()
            sl.record('SOURCE_QA', sl.PASS, evidence='e')
            leftovers = [f for f in os.listdir(d) if f.startswith('.stage-ledger-')]
            self.assertEqual(leftovers, [])

    def test_schema_mismatch_on_load_is_refused(self):
        with _Out() as d:
            sl.bootstrap()
            target = os.path.join(d, sl.LEDGER_NAME)
            with open(target, encoding='utf-8') as fh:
                doc = json.load(fh)
            doc['schemaVersion'] = 99
            with open(target, 'w', encoding='utf-8') as fh:
                json.dump(doc, fh)
            with self.assertRaises(sl.LedgerError):
                sl.load()

    def test_load_without_bootstrap_is_refused(self):
        with _Out():
            with self.assertRaises(sl.LedgerError):
                sl.load()


if __name__ == '__main__':
    unittest.main()


class FailureCannotBeOverwritten(unittest.TestCase):
    """The invariant the documentation promises, enforced at the door.

    `hanpatch build` and `hanpatch verify` are separate processes, so the
    cascade in `record_failure` is not enough on its own: a later run knows
    nothing about the earlier failure unless the ledger refuses it.
    """

    def test_a_later_token_cannot_pass_over_an_earlier_failure(self):
        with _Out():
            sl.bootstrap(ruleset='3')
            for gate in ('glossary', 'audit', 'qagate'):
                sl.record_gate_stage(gate, checked=1)
            sl.record_failure('STATIC_BINARY_QA', 'undeclared write')
            with self.assertRaises(sl.LedgerError) as ctx:
                sl.record('RC_READBACK_QA', sl.PASS)
            self.assertIn('STATIC_BINARY_QA', str(ctx.exception))

    def test_a_failed_token_cannot_pass_itself_through_the_gate_path(self):
        # record_gate_stage reaches record() indirectly; the guard has to cover
        # the token itself or the stage that just failed re-passes.
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.record_failure('STATIC_BINARY_QA', 'undeclared write')
            with self.assertRaises(sl.LedgerError):
                for gate in ('capacity', 'materialize', 'manifest'):
                    sl.record_gate_stage(gate, checked=1)
            self.assertEqual(sl.summary()['STATIC_BINARY_QA'], sl.FAIL)

    def test_a_genuine_rerun_still_works(self):
        # If the guard turned a failure into a dead end it would be a worse bug
        # than the one it fixes. A real re-run opens a fresh ledger.
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.record_failure('STATIC_BINARY_QA', 'undeclared write')
            sl.bootstrap(ruleset='3', force=True)
            for gate in sl.GATE_TOKEN:
                sl.record_gate_stage(gate, checked=1)
            sl.record('RC_BUILD', sl.PASS)
            self.assertEqual(sl.summary()['STATIC_BINARY_QA'], sl.PASS)
            self.assertEqual(sl.summary()['RC_BUILD'], sl.PASS)

    def test_a_stale_caller_document_cannot_erase_a_failure(self):
        # `doc=` exists so a batch can be written once, but it also hands the
        # caller a pre-failure snapshot. Writing that back wholesale erased the
        # failure the guard had just read off disk.
        with _Out():
            sl.bootstrap(ruleset='3')
            stale = sl.load()
            sl.record_failure('STATIC_BINARY_QA', 'undeclared write')
            with self.assertRaises(sl.LedgerError):
                sl.record('RC_READBACK_QA', sl.PASS, doc=stale)
            self.assertEqual(sl.summary()['STATIC_BINARY_QA'], sl.FAIL)

    def test_a_stale_document_cannot_promote_through_the_gate_path(self):
        with _Out():
            sl.bootstrap(ruleset='3')
            stale = sl.load()
            sl.record_failure('SOURCE_QA', 'audit findings')
            for gate in ('glossary', 'audit', 'qagate'):
                try:
                    sl.record_gate_stage(gate, checked=1, doc=stale)
                except sl.LedgerError:
                    pass
            self.assertEqual(sl.summary()['SOURCE_QA'], sl.FAIL)

    def test_a_doctored_document_cannot_launder_a_failure(self):
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.record_failure('STATIC_BINARY_QA', 'x')
            doctored = sl.load()
            doctored['tokens']['STATIC_BINARY_QA']['status'] = sl.NOT_RUN
            with self.assertRaises(sl.LedgerError):
                sl.record('RC_READBACK_QA', sl.PASS, doc=doctored)

    def test_a_reset_carries_prior_failures_forward(self):
        # The reset must stay possible - every run begins with one - but it is
        # not a way to make a failed run look like one that never happened.
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.record_failure('STATIC_BINARY_QA', 'undeclared write at 0x14')
            sl.bootstrap(ruleset='3', force=True)
            doc = sl.load()
            self.assertEqual(doc['tokens']['STATIC_BINARY_QA']['status'],
                             sl.NOT_RUN)
            self.assertEqual([p['token'] for p in doc['priorFailures']],
                             ['STATIC_BINARY_QA'])
            self.assertIn('0x14', doc['priorFailures'][0]['reason'])

    def test_repeated_resets_accumulate_rather_than_replace(self):
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.record_failure('STATIC_BINARY_QA', 'first')
            sl.bootstrap(ruleset='3', force=True)
            sl.record_failure('SOURCE_QA', 'second')
            sl.bootstrap(ruleset='3', force=True)
            tokens = [p['token'] for p in sl.load()['priorFailures']]
            self.assertEqual(tokens, ['STATIC_BINARY_QA', 'SOURCE_QA'])

    def test_the_module_states_its_threat_model(self):
        # A ledger that implied tamper-resistance would be worse than one that
        # says plainly what it does not defend against.
        with open(os.path.join(ROOT, 'hanpatch', 'stage_ledger.py'),
                  encoding='utf-8') as fh:
            src = fh.read()
        self.assertIn('not signed', src)
        self.assertIn('accidental corruption', src)

    def _write_raw(self, doc):
        import json
        with open(sl.path(), 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)

    def test_a_malformed_history_is_sieved_not_carried(self):
        # Reading a document this build cannot interpret means its fields cannot
        # be trusted either. A string iterates into characters and a dict into
        # its keys, and both flow onward looking like history until a reader
        # does entry['token'] and gets a TypeError.
        with _Out():
            sl.bootstrap(ruleset='3')
            for junk in ('haha', {'fake': 1}, [1, 2, 3], None):
                doc = sl.load()
                doc['priorFailures'] = junk
                self._write_raw(doc)
                sl.bootstrap(ruleset='3', force=True)
                carried = sl.load()['priorFailures']
                self.assertIsInstance(carried, list)
                for row in carried:
                    self.assertIsInstance(row, dict)
                    self.assertIn('token', row)

    def test_an_unreadable_prior_ledger_is_recorded_as_unknown(self):
        # A foreign schema may name its status field something else, so a FAIL
        # can sit there unread. An empty history would present that as clean.
        with _Out():
            sl.bootstrap(ruleset='3')
            doc = sl.load()
            doc['schemaVersion'] = 99
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            carried = sl.load()['priorFailures']
            self.assertTrue(any(r.get('unreadable') for r in carried))
            self.assertIn('could not be read', carried[0]['reason'])

    def test_an_unreadable_marker_survives_a_later_reset(self):
        with _Out():
            sl.bootstrap(ruleset='3')
            doc = sl.load()
            doc['schemaVersion'] = 99
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            sl.bootstrap(ruleset='3', force=True)
            self.assertTrue(any(r.get('unreadable')
                                for r in sl.load()['priorFailures']))

    def test_a_forged_unreadable_marker_is_dropped(self):
        # The marker is derived at each reset from the document on disk. Trusting
        # one from the file let a clean run be made to look suspect by writing
        # the row yourself.
        with _Out():
            sl.bootstrap(ruleset='3')
            doc = sl.load()
            doc['priorFailures'] = [{'token': None, 'unreadable': True,
                                     'reason': 'FABRICATED - nothing failed'}]
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            self.assertEqual(sl.load()['priorFailures'], [])

    def test_arbitrary_payload_in_a_valid_row_is_stripped(self):
        # A row that survived the sieve was still free to carry anything, so the
        # sieve checked that a row existed without checking what was in it.
        with _Out():
            sl.bootstrap(ruleset='3')
            doc = sl.load()
            doc['priorFailures'] = [{'token': 'SOURCE_QA', 'reason': 'real',
                                     'payload': 'x' * 100}]
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            carried = sl.load()['priorFailures']
            self.assertEqual(sorted(carried[0]), ['reason', 'token'])

    def test_a_broken_tokens_field_still_records_the_ledger_as_unreadable(self):
        # A non-mapping `tokens` used to skip the whole harvest, losing both the
        # rows and the fact that anything was lost.
        with _Out():
            sl.bootstrap(ruleset='3')
            doc = sl.load()
            doc['tokens'] = []
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            self.assertTrue(any(r.get('unreadable')
                                for r in sl.load()['priorFailures']))

    def test_prose_cannot_forge_the_unreadable_marker(self):
        # Stickiness used to be recovered by matching the derived row's wording,
        # which meant the file could produce it by imitation. It now lives in
        # its own boolean that only this code writes.
        with _Out():
            sl.bootstrap(ruleset='3')
            doc = sl.load()
            doc['priorFailures'] = [{
                'token': None, 'unreadable': True,
                'reason': 'a prior ledger could not be read by this build '
                          '(schemaVersion 99); its results are unknown'}]
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            self.assertEqual(sl.load()['priorFailures'], [])
            self.assertFalse(sl.load()['sawUnreadableLedger'])

    def test_a_renamed_status_field_is_unreadable_not_clean(self):
        # The schema number can match while the shape does not, leaving a FAIL
        # unread. An empty history would present that as a clean run.
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.record_failure('SOURCE_QA', 'audit findings')
            doc = sl.load()
            for name in sl.TOKENS:
                doc['tokens'][name]['state'] = doc['tokens'][name].pop('status')
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            self.assertTrue(sl.load()['sawUnreadableLedger'])

    def test_a_document_missing_one_token_is_still_readable(self):
        # Strictness has a cost: requiring every token present would call an
        # interpretable document unreadable and cry wolf.
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.record_failure('SOURCE_QA', 'audit findings')
            doc = sl.load()
            del doc['tokens']['RELEASE']
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            loaded = sl.load()
            self.assertFalse(loaded['sawUnreadableLedger'])
            self.assertEqual([r['token'] for r in loaded['priorFailures']],
                             ['SOURCE_QA'])

    def test_an_oversized_reason_is_truncated(self):
        # A history row is a note, not a payload channel.
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.record_failure('SOURCE_QA', 'x' * 100000)
            sl.bootstrap(ruleset='3', force=True)
            reason = sl.load()['priorFailures'][0]['reason']
            self.assertLess(len(reason), 600)
            self.assertTrue(reason.endswith('[cut]'))

    def test_the_flag_is_never_read_from_the_document_it_describes(self):
        # Moving stickiness into a boolean was the same trust as matching prose,
        # wearing different clothes. It is recovered from a marker row this build
        # tagged, which the sieve keeps only when it wrote the tag itself.
        with _Out():
            sl.bootstrap(ruleset='3')
            doc = sl.load()
            doc['sawUnreadableLedger'] = True
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            self.assertFalse(sl.load()['sawUnreadableLedger'])

    def test_a_status_hidden_under_another_name_is_unreadable(self):
        with _Out():
            sl.bootstrap(ruleset='3')
            doc = sl.load()
            doc['tokens']['SOURCE_QA'] = {'state': 'FAIL', 'why': 'audit'}
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            self.assertTrue(sl.load()['sawUnreadableLedger'])

    def test_an_entry_with_only_known_fields_is_not_unreadable(self):
        # Strictness that cries wolf is its own failure: an entry with no status
        # and nothing unfamiliar has no result to lose.
        with _Out():
            sl.bootstrap(ruleset='3')
            doc = sl.load()
            doc['tokens']['RELEASE'] = {'reason': 'x'}
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            self.assertFalse(sl.load()['sawUnreadableLedger'])

    def test_padding_cannot_push_the_cause_past_the_cut(self):
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.record_failure('SOURCE_QA', ' ' * 520 + 'the real cause')
            sl.bootstrap(ruleset='3', force=True)
            self.assertIn('the real cause',
                          sl.load()['priorFailures'][0]['reason'])

    def test_zero_width_padding_cannot_hide_the_cause(self):
        # str.split does not treat zero-width characters as whitespace, so 520
        # of them pushed the real cause past the cut while the row still looked
        # like a normally capped one.
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.record_failure('SOURCE_QA', '\u200b' * 520 + 'THE REAL CAUSE')
            sl.bootstrap(ruleset='3', force=True)
            self.assertIn('THE REAL CAUSE',
                          sl.load()['priorFailures'][0]['reason'])

    def test_a_dropped_status_is_unreadable_not_clean(self):
        # Keying on unknown fields missed the simplest case: drop `status` and
        # keep everything else familiar, and the FAIL vanished quietly.
        with _Out():
            sl.bootstrap(ruleset='3')
            sl.record_failure('SOURCE_QA', 'audit')
            doc = sl.load()
            doc['tokens']['SOURCE_QA'].pop('status')
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            self.assertTrue(sl.load()['sawUnreadableLedger'])

    def test_the_marker_tag_documents_that_it_is_forgeable(self):
        # A tag that implied tamper-resistance without providing it would be
        # worse than one that says what it is.
        with open(os.path.join(ROOT, 'hanpatch', 'stage_ledger.py'),
                  encoding='utf-8') as fh:
            src = fh.read()
        self.assertIn('NOT unforgeable', src)

    def test_a_foreign_verdict_word_is_unreadable(self):
        # "failed" is not FAIL. Treating an unrecognised word as absent would
        # drop a real failure while the ledger read clean.
        with _Out():
            sl.bootstrap(ruleset='3')
            doc = sl.load()
            doc['tokens']['SOURCE_QA']['status'] = 'failed'
            self._write_raw(doc)
            sl.bootstrap(ruleset='3', force=True)
            self.assertTrue(sl.load()['sawUnreadableLedger'])

    def test_blank_glyph_padding_cannot_hide_the_cause(self):
        # Each review round found another padding codepoint, so the cap now
        # measures legible characters instead of blacklisting invisible ones.
        for pad in ('\u200b', '\u3164', '\u2800', '\ufeff'):
            with _Out():
                sl.bootstrap(ruleset='3')
                sl.record_failure('SOURCE_QA', pad * 520 + 'THE REAL CAUSE')
                sl.bootstrap(ruleset='3', force=True)
                self.assertIn('THE REAL CAUSE',
                              sl.load()['priorFailures'][0]['reason'],
                              'padding %r hid the cause' % pad)

    def test_the_unreadable_marker_does_not_multiply(self):
        # One marker means one unreadable ancestor; a row per reset turns a
        # single event into a growing list that overstates what happened.
        with _Out():
            sl.bootstrap(ruleset='3')
            doc = sl.load()
            doc['schemaVersion'] = 99
            self._write_raw(doc)
            for _ in range(5):
                sl.bootstrap(ruleset='3', force=True)
            markers = [r for r in sl.load()['priorFailures']
                       if r.get('unreadable')]
            self.assertEqual(len(markers), 1)

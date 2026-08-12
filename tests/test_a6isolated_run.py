"""Focused tests for the explicit, isolated A6 DQ7 pilot runner."""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch import a6isolated as a6  # noqa: E402
from hanpatch import a6isolated_run as runner  # noqa: E402
from hanpatch import cli  # noqa: E402
from hanpatch import config  # noqa: E402


def response_for(body, target='번역됨'):
    request = json.loads(body.decode('utf-8'))
    return json.dumps({
        'protocol': a6.PROTOCOL,
        'version': a6.PROTOCOL_VERSION,
        'request_id': request['request_id'],
        'model': a6.FIXED_MODEL,
        'translations': {item['id']: target for item in request['items']},
    }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class A6PilotRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.temporary.name, 'project')
        os.makedirs(os.path.join(self.project, 'work'), exist_ok=True)
        profile = {
            'source_lang': 'ja',
            'tag_pattern': r'<[^>\n]*>|\{[A-Z0-9_]+\}',
            'movable_tags': [],
            'control_tags': [],
            'budget': {'default': 400},
            'capacity': {'dialogue': 1},
            'engine_wraps': False,
            'font_src': [],
            'register_default': 'plain',
        }
        with open(os.path.join(self.project, 'profile.json'), 'w', encoding='utf-8') as handle:
            json.dump(profile, handle)
        with open(os.path.join(self.project, 'hanpatch.json'), 'w', encoding='utf-8') as handle:
            json.dump({'title': 'isolated test', 'adapter': 'dq7', 'target': 'ko',
                       'profile': 'profile.json'}, handle)
        with open(os.path.join(self.project, 'work', 'text_src.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump({'dialogue': [
                {'key': 'row_1', 'en': '勇者'},
                {'key': 'row_2', 'en': '仲間'},
            ]}, handle, ensure_ascii=False)
        self.previous_config = (config._root, config._cfg, config._profile)
        config.set_root(self.project)

    def tearDown(self):
        config._root, config._cfg, config._profile = self.previous_config
        config.reset_module_caches()
        self.temporary.cleanup()

    def output(self):
        return os.path.join(self.temporary.name, 'pilot')

    def argv(self, output, **overrides):
        values = {
            'family': 'dialogue',
            'url': 'https://a6.invalid/translate',
            'output': output,
            'client_cert': 'test-cert.pem',
            'client_key': 'test-key.pem',
            'calls': 8,
            'request_bytes': 65536,
            'response_bytes': 8192,
            'workers': 2,
            'batch_size': 1,
            'batch_chars': 512,
            'limit': 0,
        }
        values.update(overrides)
        return [
            '--family', values['family'], '--url', values['url'], '--output', values['output'],
            '--client-cert', values['client_cert'], '--client-key', values['client_key'],
            '--calls', str(values['calls']), '--request-bytes', str(values['request_bytes']),
            '--response-bytes', str(values['response_bytes']), '--workers', str(values['workers']),
            '--batch-size', str(values['batch_size']), '--batch-chars', str(values['batch_chars']),
            '--limit', str(values['limit']),
        ]

    def client(self, transport, *, calls=8):
        return a6.A6TranslationClient(
            transport, a6.RunBudget(calls, 65536, calls * 1024), response_cap=1024)

    def review(self, output):
        with open(os.path.join(output, 'review_dialogue.json'), encoding='utf-8') as handle:
            return json.load(handle)

    def state(self, output):
        with open(os.path.join(output, 'state_dialogue.json'), encoding='utf-8') as handle:
            return json.load(handle)

    def test_cli_help_and_dispatch_never_build_the_ordinary_provider_pool(self):
        command = self.argv(self.output())
        with patch('hanpatch.providers.build_pool', side_effect=AssertionError('ordinary pool')):
            with self.assertRaises(SystemExit):
                cli.main(['a6-translate', '--help'])
            with patch('hanpatch.a6isolated_run.main', return_value=0) as isolated_main:
                self.assertEqual(cli.main(['a6-translate'] + command), 0)
        isolated_main.assert_called_once()

    def test_parser_requires_explicit_a6_options_and_exposes_no_selection_or_retry_switches(self):
        parser = runner._parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])
        destinations = {action.dest for action in parser._actions}
        self.assertTrue({'family', 'url', 'output', 'client_cert', 'client_key'} <= destinations)
        self.assertFalse({'model', 'models', 'retry', 'retries', 'failover'} & destinations)

    def test_output_inside_project_is_rejected_before_source_or_transport_access(self):
        client = self.client(lambda body: response_for(body))
        with self.assertRaises(runner.InvalidOutputNamespace):
            runner.main(self.argv(os.path.join(self.project, 'pilot')), client=client)


    def test_invalid_namespace_precedes_production_tls_loading(self):
        output = os.path.join(self.project, 'pilot')
        with patch.object(runner, '_production_client',
                          side_effect=AssertionError('TLS loaded before preflight')):
            with self.assertRaises(runner.InvalidOutputNamespace):
                runner.main(self.argv(output))

    def test_symlink_namespace_precedes_production_tls_loading(self):
        output = self.output()
        target = os.path.join(self.temporary.name, 'symlink-target')
        os.mkdir(target)
        os.symlink(target, output)
        with patch.object(runner, '_production_client',
                          side_effect=AssertionError('TLS loaded before symlink refusal')):
            with self.assertRaises(runner.InvalidOutputNamespace):
                runner.main(self.argv(output))


    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'requires POSIX FIFO support')
    def test_fifo_input_snapshot_is_rejected_without_blocking(self):
        fifo = os.path.join(self.temporary.name, 'snapshot.fifo')
        os.mkfifo(fifo)
        with self.assertRaises(runner.PilotStateError):
            runner._read_input_snapshot(fifo, self.project)
    def test_ancestor_namespace_swap_is_rejected_before_production_tls_loading(self):
        outside = os.path.join(self.temporary.name, 'outside')
        active_output = os.path.join(self.project, 'work', 'ko')
        route = os.path.join(self.temporary.name, 'route')
        os.makedirs(outside)
        os.makedirs(active_output)
        os.symlink(outside, route)
        output = os.path.join(route, 'pilot')
        original_acquire = a6.OutputNamespaceLock.acquire

        def swap_then_acquire(lock):
            os.unlink(route)
            os.symlink(active_output, route)
            return original_acquire(lock)

        with patch.object(a6.OutputNamespaceLock, 'acquire', new=swap_then_acquire):
            with patch.object(runner, '_production_client',
                              side_effect=AssertionError('TLS loaded after ancestor swap')):
                with self.assertRaises(runner.InvalidOutputNamespace):
                    runner.main(self.argv(output))

    def test_locked_and_uncreatable_namespaces_precede_production_tls_loading(self):
        output = self.output()
        os.makedirs(output)
        lock = a6.OutputNamespaceLock(output).acquire()
        try:
            with patch.object(runner, '_production_client',
                              side_effect=AssertionError('TLS loaded before namespace lock')):
                with self.assertRaises(a6.NamespaceLocked):
                    runner.main(self.argv(output))
        finally:
            lock.release()

        blocked = os.path.join(self.temporary.name, 'blocked-parent')
        with open(blocked, 'w', encoding='utf-8') as handle:
            handle.write('not a directory')
        with patch.object(runner, '_production_client',
                          side_effect=AssertionError('TLS loaded before namespace creation')):
            with self.assertRaises(a6.A6RequestError):
                runner.main(self.argv(os.path.join(blocked, 'pilot')))

    def test_second_runner_lock_is_rejected(self):
        output = self.output()
        os.makedirs(output)
        first = a6.OutputNamespaceLock(output).acquire()
        try:
            with self.assertRaises(a6.NamespaceLocked):
                runner.main(self.argv(output), client=self.client(lambda body: response_for(body)))
        finally:
            first.release()

    def test_workers_share_one_budget_and_stop_at_call_and_byte_caps(self):
        output = self.output()
        requests = []

        def transport(body):
            requests.append(body)
            return response_for(body)

        call_limited = self.client(transport, calls=1)
        with patch.object(runner.translate, 'check', side_effect=lambda en, ko, gl, kind, group: (ko, [])):
            self.assertEqual(runner.main(self.argv(output, calls=1), client=call_limited), 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(call_limited.budget.calls, 1)
        self.assertEqual(len(self.review(output)), 1)

        byte_output = os.path.join(self.temporary.name, 'byte-pilot')
        byte_requests = []

        def byte_transport(body):
            byte_requests.append(body)
            return response_for(body)

        byte_limited = a6.A6TranslationClient(
            byte_transport, a6.RunBudget(8, 65536, 1024), response_cap=1024)
        with patch.object(runner.translate, 'check', side_effect=lambda en, ko, gl, kind, group: (ko, [])):
            self.assertEqual(
                runner.main(self.argv(byte_output, calls=8, response_bytes=1024), client=byte_limited),
                1)
        self.assertEqual(len(byte_requests), 1)
        self.assertEqual(byte_limited.budget.response_bytes, 1024)
        self.assertEqual(len(self.review(byte_output)), 1)

    def test_malformed_and_validation_failed_batches_write_authoritative_review_state(self):
        malformed_output = self.output()
        malformed = self.client(lambda body: b'{}')
        with patch.object(runner.translate, 'check', side_effect=AssertionError('must not validate')):
            self.assertEqual(runner.main(self.argv(malformed_output, limit=1), client=malformed), 1)
        self.assertEqual(set(self.review(malformed_output)), {'勇者'})
        malformed_state = self.state(malformed_output)
        self.assertEqual(malformed_state['tm'], {})
        self.assertEqual(malformed_state['provenance'], {})
        self.assertEqual(malformed_state['anchor_versions'], {})
        for name in ('tm_dialogue.json', 'prov_dialogue.json', 'anchorver_dialogue.json'):
            with open(os.path.join(malformed_output, name), encoding='utf-8') as handle:
                self.assertEqual(json.load(handle), {})

        validation_output = os.path.join(self.temporary.name, 'validation-pilot')
        valid_envelope = self.client(lambda body: response_for(body))
        with patch.object(runner.translate, 'check', return_value=('번역됨', ['synthetic'])):
            self.assertEqual(runner.main(self.argv(validation_output, limit=1), client=valid_envelope), 1)
        self.assertEqual(set(self.review(validation_output)), {'勇者'})
        self.assertEqual(self.state(validation_output)['tm'], {})
    def test_input_snapshot_cannot_clear_unresolved_review(self):
        output = self.output()
        malformed = self.client(lambda body: b'{}')
        with patch.object(runner.translate, 'check', side_effect=AssertionError('must not validate')):
            self.assertEqual(runner.main(self.argv(output, limit=1), client=malformed), 1)
        snapshot = os.path.join(self.temporary.name, 'snapshot.json')
        with open(snapshot, 'w', encoding='utf-8') as handle:
            json.dump({'勇者': '용사', '仲間': '동료'}, handle, ensure_ascii=False)
        no_call = self.client(lambda body: (_ for _ in ()).throw(AssertionError('must not call')))
        self.assertEqual(
            runner.main(self.argv(output, limit=1) + ['--input-snapshot', snapshot],
                        client=no_call), 1)

    def test_input_snapshot_final_symlink_is_rejected(self):
        snapshot = os.path.join(self.temporary.name, 'snapshot.json')
        link = os.path.join(self.temporary.name, 'snapshot-link.json')
        with open(snapshot, 'w', encoding='utf-8') as handle:
            json.dump({'勇者': '용사'}, handle, ensure_ascii=False)
        os.symlink(snapshot, link)
        with self.assertRaises(runner.PilotStateError):
            runner.main(self.argv(self.output()) + ['--input-snapshot', link],
                        client=self.client(lambda body: response_for(body)))

    def test_input_snapshot_ancestor_swap_to_project_is_rejected_before_read(self):
        outside = os.path.join(self.temporary.name, 'snapshot-outside')
        active_output = os.path.join(self.project, 'work', 'ko')
        route = os.path.join(self.temporary.name, 'snapshot-route')
        os.makedirs(outside)
        os.makedirs(active_output)
        os.symlink(outside, route)
        snapshot = os.path.join(route, 'snapshot.json')
        with open(snapshot, 'w', encoding='utf-8') as handle:
            json.dump({'勇者': '용사'}, handle, ensure_ascii=False)
        with open(os.path.join(active_output, 'snapshot.json'), 'w', encoding='utf-8') as handle:
            json.dump({'仲間': '동료'}, handle, ensure_ascii=False)
        args = runner._parser().parse_args(
            self.argv(self.output()) + ['--input-snapshot', snapshot])
        original_open = os.open

        def swap_then_open(path, flags, *args, **kwargs):
            if path == snapshot:
                os.unlink(route)
                os.symlink(active_output, route)
            return original_open(path, flags, *args, **kwargs)

        with patch.object(runner.os, 'open', side_effect=swap_then_open):
            with patch.object(runner.os, 'fdopen',
                              side_effect=AssertionError('project snapshot was read')):
                with self.assertRaises(runner.PilotStateError):
                    runner._preflight(args)

    def test_pinned_input_snapshot_is_not_reopened_after_ancestor_swap(self):
        outside = os.path.join(self.temporary.name, 'snapshot-outside')
        active_output = os.path.join(self.project, 'work', 'ko')
        route = os.path.join(self.temporary.name, 'snapshot-route')
        os.makedirs(outside)
        os.makedirs(active_output)
        os.symlink(outside, route)
        snapshot = os.path.join(route, 'snapshot.json')
        with open(snapshot, 'w', encoding='utf-8') as handle:
            json.dump({'勇者': '용사'}, handle, ensure_ascii=False)
        args = runner._parser().parse_args(
            self.argv(self.output(), limit=1) + ['--input-snapshot', snapshot])
        runner._validate_options(args)
        preflight = runner._preflight(args)
        with open(os.path.join(active_output, 'snapshot.json'), 'w', encoding='utf-8') as handle:
            handle.write('{not valid JSON')
        os.unlink(route)
        os.symlink(active_output, route)
        calls = []

        def transport(body):
            calls.append(body)
            return response_for(body)

        with patch.object(runner.translate, 'check', return_value=('번역됨', [])):
            self.assertEqual(runner._run(args, self.client(transport), preflight), 0)
        self.assertEqual(len(calls), 1)

    def test_authoritative_state_resumes_and_reconciles_derived_shards(self):
        output = self.output()
        calls = []

        def transport(body):
            calls.append(body)
            return response_for(body)

        client = self.client(transport)
        checker = lambda en, ko, gl, kind, group: (ko, [])
        with patch.object(runner.translate, 'check', side_effect=checker):
            self.assertEqual(runner.main(
                self.argv(output, limit=2, batch_size=2), client=client), 0)
            with open(os.path.join(output, 'tm_dialogue.json'), 'w', encoding='utf-8') as handle:
                json.dump({}, handle)
            self.assertEqual(runner.main(
                self.argv(output, limit=2, batch_size=2), client=client), 0)
        self.assertEqual(len(calls), 1)
        state = self.state(output)
        fields = {
            'tm_dialogue.json': 'tm',
            'prov_dialogue.json': 'provenance',
            'anchorver_dialogue.json': 'anchor_versions',
            'review_dialogue.json': 'review',
        }
        for name, field in fields.items():
            with open(os.path.join(output, name), encoding='utf-8') as handle:
                self.assertEqual(json.load(handle), state[field])

    def test_legacy_accepted_shards_require_authoritative_state(self):
        output = self.output()
        os.makedirs(output)
        accepted = {'勇者': '용사', '仲間': '동료'}
        shards = {
            'tm_dialogue.json': accepted,
            'prov_dialogue.json': {source: 'legacy' for source in accepted},
            'anchorver_dialogue.json': {
                source: runner._anchor_version({}) for source in accepted
            },
            'review_dialogue.json': {},
        }
        for name, value in shards.items():
            with open(os.path.join(output, name), 'w', encoding='utf-8') as handle:
                json.dump(value, handle, ensure_ascii=False)
        no_call = self.client(lambda body: (_ for _ in ()).throw(AssertionError('must not call')))
        with self.assertRaises(runner.PilotStateError):
            runner.main(self.argv(output), client=no_call)
        self.assertFalse(os.path.exists(os.path.join(output, 'state_dialogue.json')))

    def test_review_only_legacy_state_is_migrated(self):
        output = self.output()
        os.makedirs(output)
        review = {'勇者': {'refs': ['dialogue:row_1'], 'reason': 'response_invalid'}}
        with open(os.path.join(output, 'review_dialogue.json'), 'w', encoding='utf-8') as handle:
            json.dump(review, handle, ensure_ascii=False)
        lock = a6.OutputNamespaceLock(output).acquire()
        try:
            state, authoritative = runner._load_pilot_state(
                lock, 'dialogue', 'a' * 64)
        finally:
            lock.release()
        self.assertTrue(authoritative)
        self.assertEqual(state['review'], review)

    def test_authoritative_state_rejects_profile_or_source_contract_changes(self):
        output = self.output()
        with patch.object(runner.translate, 'check', return_value=('번역됨', [])):
            self.assertEqual(runner.main(self.argv(output, limit=1),
                                         client=self.client(lambda body: response_for(body))), 0)
        original_profile_path = os.path.join(self.project, 'profile.json')
        with open(original_profile_path, encoding='utf-8') as handle:
            original_profile = json.load(handle)
        changed_profile = dict(original_profile)
        changed_profile['control_tags'] = ['<CHANGED>']
        with open(original_profile_path, 'w', encoding='utf-8') as handle:
            json.dump(changed_profile, handle)
        config.set_root(self.project)
        no_call = self.client(lambda body: (_ for _ in ()).throw(AssertionError('must not call')))
        with self.assertRaises(runner.PilotStateError):
            runner.main(self.argv(output), client=no_call)

        second = os.path.join(self.temporary.name, 'second-project')
        os.makedirs(os.path.join(second, 'work'))
        with open(os.path.join(second, 'profile.json'), 'w', encoding='utf-8') as handle:
            json.dump(original_profile, handle)
        with open(os.path.join(second, 'hanpatch.json'), 'w', encoding='utf-8') as handle:
            json.dump({'title': 'isolated test', 'adapter': 'dq7', 'target': 'ko',
                       'profile': 'profile.json'}, handle)
        with open(os.path.join(second, 'work', 'text_src.json'), 'w', encoding='utf-8') as handle:
            json.dump({'dialogue': [{'key': 'row_1', 'en': '別の勇者'}]}, handle,
                      ensure_ascii=False)
        config.set_root(second)
        with self.assertRaises(runner.PilotStateError):
            runner.main(self.argv(output), client=no_call)

    def test_accepted_anchor_versions_are_checked_before_todo_resolution(self):
        output = self.output()
        with patch.object(runner.translate, 'check', return_value=('번역됨', [])):
            self.assertEqual(runner.main(self.argv(output, limit=1),
                                         client=self.client(lambda body: response_for(body))), 0)
        state = self.state(output)
        state['anchor_versions']['勇者'] = 'obsolete-anchor-contract'
        with open(os.path.join(output, 'state_dialogue.json'), 'w', encoding='utf-8') as handle:
            json.dump(state, handle, ensure_ascii=False)
        no_call = self.client(lambda body: (_ for _ in ()).throw(AssertionError('must not call')))
        with self.assertRaises(runner.PilotStateError):
            runner.main(self.argv(output), client=no_call)

    def test_authoritative_state_binds_declared_source_font_content(self):
        output = self.output()
        font_directory = os.path.join(self.project, 'fonts')
        os.mkdir(font_directory)
        font_path = os.path.join(font_directory, 'source.bin')
        with open(font_path, 'wb') as handle:
            handle.write(b'first source font')
        profile_path = os.path.join(self.project, 'profile.json')
        with open(profile_path, encoding='utf-8') as handle:
            profile = json.load(handle)
        profile['font_src'] = ['fonts/source.bin']
        with open(profile_path, 'w', encoding='utf-8') as handle:
            json.dump(profile, handle)
        config.set_root(self.project)
        with patch.object(runner.translate, 'check', return_value=('번역됨', [])):
            self.assertEqual(runner.main(self.argv(output, limit=1),
                                         client=self.client(lambda body: response_for(body))), 0)
        with open(font_path, 'wb') as handle:
            handle.write(b'changed source font')
        no_call = self.client(lambda body: (_ for _ in ()).throw(AssertionError('must not call')))
        with self.assertRaises(runner.PilotStateError):
            runner.main(self.argv(output), client=no_call)

    def test_malformed_or_incomplete_legacy_accepted_shards_fail_closed(self):
        output = self.output()
        os.makedirs(output)
        with open(os.path.join(output, 'tm_dialogue.json'), 'w', encoding='utf-8') as handle:
            json.dump({'勇者': '용사'}, handle)
        with open(os.path.join(output, 'prov_dialogue.json'), 'w', encoding='utf-8') as handle:
            json.dump([], handle)
        with self.assertRaises(runner.PilotStateError):
            runner.main(self.argv(output), client=self.client(lambda body: response_for(body)))
        self.assertFalse(os.path.exists(os.path.join(output, 'state_dialogue.json')))

    def test_reconcile_failure_keeps_authoritative_commit_for_restart(self):
        output = self.output()
        original_replace = runner._replace_json_locked

        def fail_provenance(lock, name, value, what='pilot state'):
            if name == 'prov_dialogue.json':
                raise OSError('synthetic shard failure')
            return original_replace(lock, name, value, what)

        with patch.object(runner.translate, 'check', return_value=('번역됨', [])):
            with patch.object(runner, '_replace_json_locked', side_effect=fail_provenance):
                with self.assertRaises(OSError):
                    runner.main(self.argv(output, limit=2, batch_size=2),
                                client=self.client(lambda body: response_for(body)))
            state = self.state(output)
            self.assertEqual(set(state['tm']), {'勇者', '仲間'})
            self.assertEqual(set(state['tm']), set(state['provenance']))
            self.assertFalse(os.path.exists(os.path.join(output, 'prov_dialogue.json')))

            resumed = self.client(lambda body: (_ for _ in ()).throw(AssertionError('no retry')))
            self.assertEqual(runner.main(
                self.argv(output, limit=2, batch_size=2), client=resumed), 0)
            with open(os.path.join(output, 'prov_dialogue.json'), encoding='utf-8') as handle:
                self.assertEqual(json.load(handle), state['provenance'])

    def test_independent_runs_use_distinct_request_ids(self):
        request_ids = []

        def transport(body):
            request_ids.append(json.loads(body.decode('utf-8'))['request_id'])
            return response_for(body)

        first = self.output()
        second = os.path.join(self.temporary.name, 'second-pilot')
        checker = lambda en, ko, gl, kind, group: (ko, [])
        with patch.object(runner.translate, 'check', side_effect=checker):
            self.assertEqual(runner.main(self.argv(first, limit=1), client=self.client(transport)), 0)
            self.assertEqual(runner.main(self.argv(second, limit=1), client=self.client(transport)), 0)
        self.assertEqual(len(request_ids), 2)
        self.assertNotEqual(request_ids[0], request_ids[1])

    def test_unexpected_validation_exception_propagates(self):
        output = self.output()
        with patch.object(runner.translate, 'check', side_effect=SystemExit('synthetic failure')):
            with self.assertRaises(SystemExit):
                runner.main(self.argv(output, limit=1),
                            client=self.client(lambda body: response_for(body)))
        self.assertFalse(os.path.exists(os.path.join(output, 'review_dialogue.json')))

    def test_check_runs_before_any_translation_memory_write(self):
        output = self.output()

        def check(en, ko, gl, kind, group):
            self.assertFalse(os.path.exists(os.path.join(output, 'tm_dialogue.json')))
            return ko, []

        with patch.object(runner.translate, 'check', side_effect=check):
            self.assertEqual(
                runner.main(self.argv(output, limit=1),
                            client=self.client(lambda body: response_for(body))),
                0)


if __name__ == '__main__':
    unittest.main()

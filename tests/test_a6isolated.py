"""Focused isolation and delimiter tests for the opt-in DQ7 A6 lane."""
import json
import os
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.request



ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch import a6isolated as a6  # noqa: E402
from hanpatch import config  # noqa: E402
from hanpatch import providers  # noqa: E402
from hanpatch import translate as tr  # noqa: E402


def response_for(body, *, request_id=None, model=None):
    request = json.loads(body.decode('utf-8'))
    return json.dumps({
        'protocol': a6.PROTOCOL,
        'version': a6.PROTOCOL_VERSION,
        'request_id': request_id if request_id is not None else request['request_id'],
        'model': model if model is not None else request['model'],
        'translations': {item['id']: '번역됨' for item in request['items']},
    }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class FakeResponse:
    def __init__(self, body, headers, status=200):
        self.body = body
        self.headers = headers
        self.status = status
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size):
        self.read_sizes.append(size)
        return self.body


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return self.response


class A6IsolatedTests(unittest.TestCase):
    def client(self, transport, *, calls=8, response_cap=4096):
        return a6.A6TranslationClient(
            transport,
            a6.RunBudget(calls, 65536, calls * response_cap),
            response_cap=response_cap)

    def test_fixed_translation_envelope_and_bounded_fake_transport(self):
        seen = {}

        def fake_transport(body):
            seen['request'] = json.loads(body.decode('utf-8'))
            return response_for(body)

        client = self.client(fake_transport)
        result = client.translate(
            {'0': '勇者', '1': '{HERO}は{ITEM}を使った'},
            context=[{'source': '前', 'target': '앞'}],
            glossary={'勇者': '용사'}, feedback='용어를 지켜라', request_id='pilot-1')
        self.assertEqual(result, {'0': '번역됨', '1': '번역됨'})
        request = seen['request']
        self.assertEqual(request['protocol'], a6.PROTOCOL)
        self.assertEqual(request['version'], a6.PROTOCOL_VERSION)
        self.assertEqual(request['request_id'], 'pilot-1')
        self.assertEqual(request['source_language'], 'ja')
        self.assertEqual(request['target_language'], 'ko')
        self.assertEqual(request['kind'], a6.TRANSLATION_KIND)
        self.assertEqual(request['model'], a6.FIXED_MODEL)
        self.assertEqual(set(request), {
            'protocol', 'version', 'request_id', 'source_language', 'target_language',
            'kind', 'model', 'items', 'context', 'glossary', 'feedback'})
        self.assertNotIn('messages', request)
        self.assertNotIn('tools', request)
        self.assertNotIn('url', request)

    def test_malformed_deep_and_wrong_echo_responses_are_rejected(self):
        with self.assertRaises(a6.A6ResponseError):
            self.client(lambda body: b'{').translate({'0': '勇者'}, request_id='bad-json')

        deep = (b'{"x":' + b'[' * a6.MAX_JSON_DEPTH + b'0'
                + b']' * a6.MAX_JSON_DEPTH + b'}')
        with self.assertRaises(a6.A6ResponseError):
            self.client(lambda body: deep).translate({'0': '勇者'}, request_id='deep-json')

        def wrong_request_id(body):
            return response_for(body, request_id='not-the-request')

        with self.assertRaises(a6.A6ResponseError):
            self.client(wrong_request_id).translate({'0': '勇者'}, request_id='expected')

        def wrong_model(body):
            return response_for(body, model='other-model')

        with self.assertRaises(a6.A6ResponseError):
            self.client(wrong_model).translate({'0': '勇者'}, request_id='model-check')

    def test_response_version_rejects_boolean_and_float_aliases(self):
        for alias in (True, 1.0):
            with self.subTest(alias=alias):
                def alias_response(body):
                    response = json.loads(response_for(body).decode('utf-8'))
                    response['version'] = alias
                    return json.dumps(response, ensure_ascii=False,
                                      separators=(',', ':')).encode('utf-8')

                with self.assertRaises(a6.A6ResponseError):
                    self.client(alias_response).translate({'0': '勇者'})


    def test_https_transport_rejects_insecure_tls_context(self):
        insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        insecure.check_hostname = False
        insecure.verify_mode = ssl.CERT_NONE
        with self.assertRaises(a6.A6RequestError):
            a6.HTTPSJSONTransport('https://a6.invalid/translate', insecure)

    def test_https_transport_disables_proxy_redirects_and_compression(self):
        tls = ssl.create_default_context()
        production = a6.HTTPSJSONTransport('https://a6.invalid/translate', tls,
                                            response_cap=32)
        self.assertEqual(production.proxy_handler.proxies, {})
        no_redirect = next(h for h in production.opener.handlers
                           if isinstance(h, a6._NoRedirect))
        self.assertIsNone(no_redirect.redirect_request(None, None, 302, '', {},
                                                       'https://elsewhere.invalid/'))

        compressed = FakeResponse(b'{}', {
            'Content-Length': '2', 'Content-Encoding': 'gzip',
        })
        with self.assertRaises(a6.A6TransportError):
            a6.HTTPSJSONTransport('https://a6.invalid/translate', tls,
                                  response_cap=8, _opener=FakeOpener(compressed))(b'{}')
        self.assertEqual(compressed.read_sizes, [])

    def test_https_transport_rejects_oversized_declaration_and_read_cap_plus_one(self):
        tls = ssl.create_default_context()
        oversized = FakeResponse(b'x' * 9, {'Content-Length': '9'})
        with self.assertRaises(a6.A6TransportError):
            a6.HTTPSJSONTransport('https://a6.invalid/translate', tls,
                                  response_cap=8, _opener=FakeOpener(oversized))(b'{}')
        self.assertEqual(oversized.read_sizes, [])

        overread = FakeResponse(b'x' * 9, {'Content-Length': '8'})
        with self.assertRaises(a6.A6TransportError):
            a6.HTTPSJSONTransport('https://a6.invalid/translate', tls,
                                  response_cap=8, _opener=FakeOpener(overread))(b'{}')
        self.assertEqual(overread.read_sizes, [9])

    def test_budget_is_atomic_and_failed_attempts_remain_consumed(self):
        budget = a6.RunBudget(1, 20, 20)
        start = threading.Barrier(3)
        results = []
        result_lock = threading.Lock()

        def reserve():
            start.wait()
            try:
                budget.reserve(10, 10)
                result = 'reserved'
            except a6.BudgetExhausted:
                result = 'exhausted'
            with result_lock:
                results.append(result)

        first = threading.Thread(target=reserve)
        second = threading.Thread(target=reserve)
        first.start()
        second.start()
        start.wait()
        first.join()
        second.join()
        self.assertEqual(sorted(results), ['exhausted', 'reserved'])
        self.assertEqual(budget.snapshot(), a6.BudgetReservation(1, 10, 10))

        attempts = []

        def flaky_transport(body):
            attempts.append(body)
            if len(attempts) == 1:
                raise OSError('synthetic failure')
            return response_for(body)

        retry_budget = a6.RunBudget(2, 65536, 2048)
        client = a6.A6TranslationClient(flaky_transport, retry_budget, response_cap=1024)
        with self.assertRaises(a6.A6TransportError):
            client.translate({'0': '勇者'}, request_id='failed-attempt')
        self.assertEqual(retry_budget.calls, 1)
        self.assertEqual(retry_budget.response_bytes, 1024)
        self.assertGreater(retry_budget.request_bytes, 0)

        self.assertEqual(client.translate({'0': '勇者'}, request_id='retry'), {'0': '번역됨'})
        self.assertEqual(retry_budget.calls, 2)
        self.assertEqual(retry_budget.response_bytes, 2048)
        with self.assertRaises(a6.BudgetExhausted):
            client.translate({'0': '勇者'}, request_id='over-budget')
        self.assertEqual(len(attempts), 2)

    def test_output_namespace_lock_is_persistent_and_refuses_second_owner(self):
        with tempfile.TemporaryDirectory() as namespace:
            first = a6.OutputNamespaceLock(namespace).acquire()
            self.assertTrue(first.locked)
            self.assertEqual(os.path.basename(first.path), a6.OutputNamespaceLock.FILENAME)
            with self.assertRaises(a6.NamespaceLocked):
                a6.OutputNamespaceLock(namespace).acquire()
            first.release()
            self.assertTrue(os.path.isfile(first.path))
            second = a6.OutputNamespaceLock(namespace).acquire()
            second.release()

    @unittest.skipUnless(hasattr(os, 'fork'), 'requires POSIX fork')
    def test_abrupt_lock_owner_death_releases_kernel_lock(self):
        with tempfile.TemporaryDirectory() as namespace:
            reader, writer = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(reader)
                try:
                    a6.OutputNamespaceLock(namespace).acquire()
                    os.write(writer, b'1')
                except BaseException:
                    os.write(writer, b'0')
                finally:
                    os.close(writer)
                    os._exit(0)
            os.close(writer)
            try:
                self.assertEqual(os.read(reader, 1), b'1')
                self.assertEqual(os.waitpid(pid, 0)[1], 0)
                recovered = a6.OutputNamespaceLock(namespace).acquire()
                recovered.release()
            finally:
                os.close(reader)

    def test_locked_namespace_rejects_directory_swap(self):
        with tempfile.TemporaryDirectory() as root:
            namespace = os.path.join(root, 'pilot')
            moved = os.path.join(root, 'pilot-moved')
            replacement = os.path.join(root, 'replacement')
            os.mkdir(namespace)
            os.mkdir(replacement)
            lock = a6.OutputNamespaceLock(namespace).acquire()
            try:
                os.rename(namespace, moved)
                os.symlink(replacement, namespace)
                with self.assertRaises(a6.A6RequestError):
                    lock.replace_bytes('state_dialogue.json', b'{}')
                self.assertFalse(os.path.exists(os.path.join(replacement,
                                                             'state_dialogue.json')))
            finally:
                lock.release()

    def test_output_namespace_lock_refuses_symlink_namespace(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, 'target')
            namespace = os.path.join(root, 'pilot')
            os.mkdir(target)
            os.symlink(target, namespace)
            with self.assertRaises(a6.A6RequestError):
                a6.OutputNamespaceLock(namespace).acquire()

    def test_a6_is_absent_from_provider_and_registry_pools(self):
        self.assertNotIn('a6isolated', providers.ENDPOINTS)
        self.assertFalse(any(spec.startswith('a6isolated:')
                             for spec in providers.DEFAULT_MODELS))
        with tempfile.TemporaryDirectory() as directory:
            registry = os.path.join(directory, 'registry.json')
            with open(registry, 'w', encoding='utf-8') as handle:
                json.dump({'payload': {'models': {
                    'a6isolated:a6-dq7-translation': {
                        'state': 'ok', 'roles_allowed': ['batch_translation'],
                    },
                    'groq:openai/gpt-oss-120b': {
                        'state': 'ok', 'roles_allowed': ['batch_translation'],
                    },
                }}}, handle)
            pool_specs = providers.registry_models(path=registry)
        self.assertNotIn('a6isolated:a6-dq7-translation', pool_specs)
        self.assertEqual(pool_specs, ['groq:openai/gpt-oss-120b'])


class Dq7DelimiterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._config_state = (config._root, config._cfg, config._profile)
        cls._fits = tr.wrap.fits
        cls._in_font = tr._in_font
        cls._temporary = tempfile.TemporaryDirectory()
        profile = {
            'source_lang': 'ja',
            'tag_pattern': r'<[^>\n]*>|\{[A-Z0-9_]+\}',
            'movable_tags': ['{HERO}', '{ITEM}'],
            'source_only_pattern': r'\{[0-9]+[\u3040-\u30ff\u30fc]*\}',
            'control_tags': ['<CENTER>', '</CENTER>'],
            'budget': {'default': 400},
        }
        cls._profile = profile
        with open(os.path.join(cls._temporary.name, 'profile.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(profile, handle)
        with open(os.path.join(cls._temporary.name, 'hanpatch.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump({'title': 'DQ7 delimiter fixture', 'platform': 'threeds',
                       'adapter': 'dq7', 'target': 'ko', 'profile': 'profile.json'},
                      handle)
        config.set_root(cls._temporary.name)
        tr.wrap.fits = lambda en, ko, kind='default', group=None: (ko, [])
        tr._in_font = lambda char: True

    @classmethod
    def tearDownClass(cls):
        tr.wrap.fits = cls._fits
        tr._in_font = cls._in_font
        config._root, config._cfg, config._profile = cls._config_state
        config.reset_module_caches()
        cls._temporary.cleanup()

    def problems(self, source, target):
        _, problems = tr.check(source, target, {}, 'dialogue')
        return problems

    def test_declared_many_and_duplicate_substitutions_can_move(self):
        source = '<CENTER>{HERO}と{ITEM}を{HERO}</CENTER>'
        target = '<CENTER>{ITEM}을 {HERO}가 {HERO}</CENTER>'
        self.assertEqual(self.problems(source, target), [])
        self.assertEqual(self.problems('{HERO}', '{HERO}'), [])

    def test_control_reorder_and_unknown_or_unbalanced_delimiters_fail(self):
        reordered = self.problems('<CENTER>{HERO}</CENTER>',
                                  '</CENTER>{HERO}<CENTER>')
        self.assertTrue(any('control tag order changed' in problem for problem in reordered))
        for malformed in ('<CENTER', '<UNKNOWN>', '{unknown}', '{UNKNOWN}',
                          '{HERO', '닫음}'):
            with self.subTest(malformed=malformed):
                problems = self.problems(malformed, '용사')
                self.assertTrue(any('delimiter integrity' in problem for problem in problems))
        target_problem = self.problems('{HERO}', '{HERO}<')
        self.assertTrue(any('target delimiter integrity' in problem for problem in target_problem))
        self.assertEqual(tr.dq7_delimiter_problems('勇者와 용사'), [])

    def test_capturing_tag_pattern_preserves_full_tokens_and_delimiter_scanning(self):
        project = os.path.join(self._temporary.name, 'equivalent-profile')
        os.makedirs(project)
        profile = dict(self._profile)
        profile['tag_pattern'] = r'(<[^>\n]*>)|(\{[A-Z0-9_]+\})'
        with open(os.path.join(project, 'profile.json'), 'w', encoding='utf-8') as handle:
            json.dump(profile, handle)
        with open(os.path.join(project, 'hanpatch.json'), 'w', encoding='utf-8') as handle:
            json.dump({'title': 'equivalent DQ7 profile', 'platform': 'threeds',
                       'adapter': 'dq7', 'target': 'ko', 'profile': 'profile.json'}, handle)
        config.set_root(project)
        try:
            self.assertEqual(tr.tags('<CENTER>{HERO}</CENTER>'),
                             ['</CENTER>', '<CENTER>', '{HERO}'])
            self.assertEqual(tr.tag_skeleton('<CENTER>{HERO}</CENTER>'),
                             ['<CENTER>', '*', '</CENTER>'])
            self.assertEqual(self.problems('<CENTER>{HERO}</CENTER>',
                                           '<CENTER>{HERO}</CENTER>'), [])
            self.assertTrue(tr.dq7_delimiter_problems('<UNKNOWN>'))
            self.assertTrue(tr.dq7_delimiter_problems('{UNKNOWN}'))
        finally:
            config.set_root(self._temporary.name)
            tr.wrap.fits = lambda en, ko, kind='default', group=None: (ko, [])
            tr._in_font = lambda char: True


    def test_source_only_annotation_must_disappear_and_cannot_hide_delimiters(self):
        self.assertEqual(self.problems('{1かな}勇者', '용사'), [])
        problems = self.problems('{1かな}勇者', '{1かな}용사')
        self.assertTrue(any('source-only markup' in problem for problem in problems))

        project = os.path.join(self._temporary.name, 'broad-source-only-profile')
        os.makedirs(project)
        profile = dict(self._profile)
        profile['source_only_pattern'] = r'\{[0-9]+[^}\n]*\}'
        with open(os.path.join(project, 'profile.json'), 'w', encoding='utf-8') as handle:
            json.dump(profile, handle)
        with open(os.path.join(project, 'hanpatch.json'), 'w', encoding='utf-8') as handle:
            json.dump({'title': 'broad source-only DQ7 profile', 'platform': 'threeds',
                       'adapter': 'dq7', 'target': 'ko', 'profile': 'profile.json'}, handle)
        config.set_root(project)
        try:
            self.assertEqual(tr.dq7_delimiter_problems('{1かな}勇者'), [])
            for malformed in ('{1abc<UNKNOWN>}', '{1abc{HERO}'):
                with self.subTest(malformed=malformed):
                    self.assertTrue(tr.dq7_delimiter_problems(malformed))
        finally:
            config.set_root(self._temporary.name)
            tr.wrap.fits = lambda en, ko, kind='default', group=None: (ko, [])
            tr._in_font = lambda char: True


if __name__ == '__main__':
    unittest.main()

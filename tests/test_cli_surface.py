"""The two new CLI commands, exercised rather than assumed.

`hostrows` defaults the language axes from the profile, which is the logic that
stops a project exporting Japanese rows labelled `en`. Leaving it untested means
the guard against a transposed export is itself unguarded.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hanpatch import cli, config, manifest  # noqa: E402

SRC = {'dialogue': [{'key': '1', 'en': 'The hero arrived'},
                    {'key': '2', 'en': 'The door opened'}]}
SEALED = {'dialogue/1': '용사가 왔다', 'dialogue/2': '문이 열렸다'}


def digest(entries):
    import hashlib
    h = hashlib.sha256()
    for k in sorted(entries):
        h.update(k.encode()); h.update(b'\0')
        h.update(entries[k].encode()); h.update(b'\0')
    return h.hexdigest()


class _Project:
    """A scratch project with a source file and a sealed manifest."""

    def __init__(self, profile=None, source=None):
        self.profile = profile if profile is not None else {}
        self.source = source if source is not None else SRC

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        os.makedirs(os.path.join(base, 'work'), exist_ok=True)
        src_path = os.path.join(base, 'work', 'text_src.json')
        with open(src_path, 'w', encoding='utf-8') as fh:
            json.dump(self.source, fh)

        self.saved = (config.src_path, config.out, config.profile,
                      manifest.load, config.p)
        config.src_path = lambda: src_path
        config.out = lambda *p: (os.path.join(base, 'work', *p) if p
                                 else os.path.join(base, 'work'))
        config.p = lambda *p: os.path.join(base, *p)
        config.profile = lambda: self.profile
        manifest.load = lambda: {'entries': SEALED, 'digest': digest(SEALED),
                                 'ruleset': manifest.RULESET}
        return base

    def __exit__(self, *exc):
        (config.src_path, config.out, config.profile,
         manifest.load, config.p) = self.saved
        self.tmp.cleanup()
        return False


def run(fn, args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(args)
    return code, buf.getvalue()


class Args:
    def __init__(self, **kw):
        self.evidence_lang = None
        self.target_lang = None
        self.pivot_lang = None
        self.out = None
        for k, v in kw.items():
            setattr(self, k, v)


class HostRowsAxes(unittest.TestCase):
    def test_axes_default_from_the_profile(self):
        with _Project({'source_lang': 'ja', 'target_lang': 'ko'}) as base:
            code, out = run(cli.cmd_hostrows, Args())
            self.assertEqual(code, 0, out)
            self.assertIn('ja->ko', out)

    def test_command_line_overrides_the_profile(self):
        with _Project({'source_lang': 'ja', 'target_lang': 'ko'}):
            code, out = run(cli.cmd_hostrows,
                            Args(evidence_lang='en', target_lang='ko'))
            self.assertEqual(code, 0, out)
            self.assertIn('en->ko', out)

    def test_no_declaration_anywhere_is_refused(self):
        with _Project({}):
            code, out = run(cli.cmd_hostrows, Args())
            self.assertEqual(code, 1)
            self.assertIn('NO LANGUAGE MAP', out)

    def test_identical_axes_are_refused(self):
        with _Project({'source_lang': 'ko', 'target_lang': 'ko'}):
            code, out = run(cli.cmd_hostrows, Args())
            self.assertEqual(code, 1)
            self.assertIn('BAD LANGUAGE MAP', out)


class HostRowsSourceColumn(unittest.TestCase):
    def test_a_declared_column_is_used(self):
        source = {'dialogue': [{'key': '1', 'ja': '勇者が来た'},
                               {'key': '2', 'ja': '扉が開いた'}]}
        with _Project({'source_lang': 'ja', 'target_lang': 'ko',
                       'source_column': 'ja'}, source) as base:
            code, out = run(cli.cmd_hostrows, Args())
            self.assertEqual(code, 0, out)
            with open(os.path.join(base, 'work', 'host-rows.json'),
                      encoding='utf-8') as fh:
                doc = json.load(fh)
            self.assertEqual(doc['families']['dialogue'][0]['evidence'],
                             '勇者が来た')

    def test_a_missing_column_is_named_not_silently_empty(self):
        source = {'dialogue': [{'key': '1', 'ja': '勇者が来た'}]}
        with _Project({'source_lang': 'ja', 'target_lang': 'ko'}, source):
            code, out = run(cli.cmd_hostrows, Args())
            self.assertEqual(code, 1)
            self.assertIn('no', out.lower())
            self.assertIn('column', out)

    def test_a_malformed_source_shape_is_refused(self):
        with _Project({'source_lang': 'ja', 'target_lang': 'ko'},
                      {'dialogue': {'not': 'a list'}}):
            code, out = run(cli.cmd_hostrows, Args())
            self.assertEqual(code, 1)
            self.assertIn('BAD SOURCE', out)


class StagesCommand(unittest.TestCase):
    def test_a_legacy_title_is_told_plainly(self):
        with _Project({}):
            code, out = run(cli.cmd_stages, Args())
            self.assertEqual(code, 0)
            self.assertIn('not opted into', out)

    def test_an_opted_in_title_prints_every_token(self):
        from hanpatch import stage_ledger as sl
        profile = {sl.ACTIVATION_KEY: {'schema_version': sl.SCHEMA_VERSION}}
        with _Project(profile):
            sl.bootstrap(ruleset=manifest.RULESET, force=True)
            code, out = run(cli.cmd_stages, Args())
            self.assertEqual(code, 0)
            for token in sl.TOKENS:
                self.assertIn(token, out)
            self.assertIn('NOT_RUN', out)

    def test_a_failure_and_its_reason_are_visible(self):
        from hanpatch import stage_ledger as sl
        profile = {sl.ACTIVATION_KEY: {'schema_version': sl.SCHEMA_VERSION}}
        with _Project(profile):
            sl.bootstrap(ruleset=manifest.RULESET, force=True)
            sl.record_failure('STATIC_BINARY_QA', 'undeclared write at 0x14')
            code, out = run(cli.cmd_stages, Args())
            self.assertIn('FAIL', out)
            self.assertIn('0x14', out)

    def test_prior_failures_are_shown_after_a_reset(self):
        from hanpatch import stage_ledger as sl
        profile = {sl.ACTIVATION_KEY: {'schema_version': sl.SCHEMA_VERSION}}
        with _Project(profile):
            sl.bootstrap(ruleset=manifest.RULESET, force=True)
            sl.record_failure('SOURCE_QA', 'audit findings')
            sl.bootstrap(ruleset=manifest.RULESET, force=True)
            code, out = run(cli.cmd_stages, Args())
            self.assertIn('prior failures', out)
            self.assertIn('audit findings', out)


if __name__ == '__main__':
    unittest.main()

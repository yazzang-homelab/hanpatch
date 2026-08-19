import json
import os
import sys
import tempfile
import types
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch import config, loop  # noqa: E402


class FakeWrap:
    @staticmethod
    def budget_for(_family):
        return 100

    @staticmethod
    def rewrap(text, _budget, soft=False):
        return text

    @staticmethod
    def text_width(text):
        return len(text)

    @staticmethod
    def fits(_source, translation, _family, _group):
        if translation == 'bad':
            return translation, ['overflow: shorten the translation']
        return translation, []

    @staticmethod
    def row_draws_its_own_lines(source):
        return '\n' in source

    @staticmethod
    def row_budget(page):
        return max((len(line) for line in page.split('\n')), default=0)

    @staticmethod
    def row_line_slots(page):
        return [i for i, line in enumerate(page.split('\n')) if line.strip()]


class FakeGlossary:
    @staticmethod
    def load():
        return {'漁': '어업'}

    @staticmethod
    def relevant(gl, _texts, _family):
        return gl


class FakeTranslate:
    @staticmethod
    def check(_source, translation, _terms, _family, _group):
        if translation == 'bad':
            return translation, ['overflow: shorten the translation']
        return translation, []


class FakeCapacity:
    @staticmethod
    def group(family, key):
        return f'{family}/{key}'


class FakeRegister:
    @staticmethod
    def marker_of(_text):
        return None


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.TemporaryDirectory(prefix='hanpatch-loop-')
        self.previous_config = (config._root, config._cfg, config._profile)
        root = self.project.name
        with open(os.path.join(root, 'hanpatch.json'), 'w', encoding='utf-8') as fh:
            json.dump({'title': 'Loop Test', 'target': 'ko', 'profile': 'profile.json'}, fh)
        with open(os.path.join(root, 'profile.json'), 'w', encoding='utf-8') as fh:
            json.dump({'engine_wraps': True, 'budget': {'default': 100},
                       'capacity': {'default': 2}, 'font_src': [], 'font_out': []}, fh)
        os.makedirs(os.path.join(root, 'work', 'ko'), exist_ok=True)
        with open(os.path.join(root, 'work', 'text_src.json'), 'w', encoding='utf-8') as fh:
            json.dump({'dialogue': [
                {'key': f'k{i}', 'en': f'Source {i}', 'jp': f'원문 {i}'}
                for i in range(4)
            ]}, fh)
        with open(os.path.join(root, 'work', 'ko', 'text_ko.json'), 'w', encoding='utf-8') as fh:
            json.dump({'dialogue': {f'k{i}': f'Old {i}' for i in range(4)}}, fh)
        config.set_root(root)
        self.old_modules = (loop.wrap, loop.capacity, loop.glossary,
                            loop.translate, loop.register)
        loop.wrap = FakeWrap
        loop.capacity = FakeCapacity
        loop.glossary = FakeGlossary
        loop.translate = FakeTranslate
        loop.register = FakeRegister

    def tearDown(self):
        (loop.wrap, loop.capacity, loop.glossary,
         loop.translate, loop.register) = self.old_modules
        config._root, config._cfg, config._profile = self.previous_config
        self.project.cleanup()

    def _seed_pending(self, count=4):
        state = loop._load_state()
        state['pending'] = {'qagate': {f'dialogue/k{i}': ['needs repair']
                                       for i in range(count)}}
        loop._save_state(state)

    def test_status_without_state_is_empty_and_actionable(self):
        result = loop.status()
        self.assertEqual(result['iteration'], 0)
        self.assertEqual(result['nextAction'], 'hanpatch loop gate')

    def test_next_limits_rows_and_does_not_make_a_second_batch(self):
        self._seed_pending()
        first = loop.next_batch(2)
        second = loop.next_batch(2)
        self.assertEqual(len(first['rows']), 2)
        self.assertEqual(first['remaining'], 2)
        self.assertEqual(first['batchId'], second['batchId'])
        self.assertEqual([r['key'] for r in first['rows']],
                         [r['key'] for r in second['rows']])

    def test_a_self_measuring_row_publishes_its_own_bound(self):
        """The budget handed to the model is the row's, not the family's.

        The family number is the widest row anywhere in the family. Publishing it
        for a row that governs itself invites a rewrite that satisfies 100px while
        the row's own box is 8px wide, and the gate refuses it again for the reason
        it was refused the first time.
        """
        with open(os.path.join(config.root(), 'work', 'text_src.json'),
                  'w', encoding='utf-8') as fh:
            json.dump({'dialogue': [{'key': 'k0', 'en': 'ab cd\nef', 'jp': ''}]}, fh)
        state = loop._load_state()
        state['pending'] = {'audit': {'dialogue/k0': ['page 1 needs 3 lines']}}
        loop._save_state(state)
        row = loop.next_batch(1)['rows'][0]
        self.assertEqual(row['budget']['maxPx'], 5)
        self.assertEqual(row['budget']['lines'], 2)
        self.assertEqual(row['budget']['boundedBy'], "this row's own source lines")

    def test_next_defers_rows_that_only_need_a_reseal(self):
        """A stale-seal row is handed out only after the rows blocking the seal.

        `manifest.build` refuses wholesale, so one unstorable row keeps every row
        the rules would rewrite reporting `sealed != normalised`. Measured on
        Classic Dungeon X2: 353 of 393 pending rows were that, and handing them
        out first spends the turn rewriting text that the next successful seal
        fixes by itself.
        """
        state = loop._load_state()
        state['pending'] = {'audit': {
            'dialogue/k0': ["sealed 'a' != normalised 'b'"],
            'dialogue/k1': ["sealed 'c' != normalised 'd'"],
            'dialogue/k2': ['page 1 needs 4 lines (shorten the translation)'],
        }}
        loop._save_state(state)
        first = loop.next_batch(1)
        self.assertEqual([r['key'] for r in first['rows']], ['dialogue/k2'])

    def test_submit_replaces_failed_problem_and_keeps_row_pending(self):
        self._seed_pending(1)
        batch = loop.next_batch(1)['batchId']
        result = loop.submit(batch, {'dialogue/k0': 'bad'})
        self.assertEqual(result['accepted'], 0)
        self.assertEqual(result['rejected'], 1)
        state = loop._load_state()
        self.assertEqual(state['pending']['qagate']['dialogue/k0'],
                         ['overflow: shorten the translation'])

    def test_submit_merges_only_accepted_rows(self):
        self._seed_pending(2)
        batch = loop.next_batch(2)['batchId']
        result = loop.submit(batch, {'dialogue/k0': '새 번역', 'dialogue/k1': 'bad'})
        self.assertEqual(result['accepted'], 1)
        with open(config.out('text_ko.json'), encoding='utf-8') as fh:
            text = json.load(fh)
        self.assertEqual(text['dialogue']['k0'], '새 번역')
        self.assertEqual(text['dialogue']['k1'], 'Old 1')

    def test_corrupt_state_is_preserved(self):
        path = config.p('loop', 'state.json')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('{broken')
        result = loop.status()
        self.assertEqual(result['iteration'], 0)
        self.assertTrue(any(name.startswith('state.json.corrupt-')
                            for name in os.listdir(config.p('loop'))))

    def test_gate_failure_is_returned_as_data(self):
        class Failed(RuntimeError):
            stage = 'qagate'
            detail = 'dialogue/k0: wrong meaning'

        fake_pipeline = types.SimpleNamespace(
            GateFailed=Failed,
            gates=lambda quiet=True: (_ for _ in ()).throw(Failed()),
        )
        old_pipeline = loop.pipeline
        old_qagate = loop.qagate
        loop.pipeline = fake_pipeline
        loop.qagate = types.SimpleNamespace(
            validate=lambda quiet=True: (['dialogue/k0: wrong meaning'], [], []))
        try:
            result = loop.gate()
        finally:
            loop.pipeline = old_pipeline
            loop.qagate = old_qagate
        self.assertFalse(result['ok'])
        self.assertEqual(result['op'], 'gate')
        self.assertEqual(result['stage'], 'qagate')
        self.assertEqual(result['pending']['qagate'], 1)


if __name__ == '__main__':
    unittest.main()


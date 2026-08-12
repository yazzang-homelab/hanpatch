"""What the star nudge is allowed to do, and what it must never do.

The nag is deliberately insistent, so the tests that matter are the ones that
pin its limits: it cannot block a pipeline, cannot fail a build, cannot claim a
star was verified, and cannot record one operator's answer inside a shared
project directory.

Run: python3 tests/test_star.py
"""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hanpatch import star  # noqa: E402


class Tty(io.StringIO):
    """A stream that claims to be a terminal, which is what triggers the ask."""

    def isatty(self):
        return True


class StarNudge(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='hanpatch-star-')
        self.path = os.path.join(self.dir, 'star.json')
        self.out = Tty()

    def nudge(self, answer='n', env=None, stdin=None, stdout=None):
        return star.nudge(argv=['info'], out=self.out,
                          read_line=lambda: answer,
                          env={} if env is None else env, path=self.path,
                          stdin=stdin or Tty(), stdout=stdout or Tty())

    def test_ci_is_silenced_and_never_prompts(self):
        for env in ({'CI': '1'}, {'GITHUB_ACTIONS': 'true'},
                    {'HANPATCH_NO_STAR': '1'}):
            self.out = Tty()
            self.assertEqual(self.nudge(env=env), 'silenced')
            self.assertEqual(self.out.getvalue(), '')

    def test_non_interactive_prints_one_line_and_asks_nothing(self):
        plain = io.StringIO()                     # a pipe: isatty() is False
        self.assertEqual(self.nudge(stdin=plain, stdout=plain), 'printed')
        self.assertEqual(len(self.out.getvalue().strip().splitlines()), 1)
        self.assertIn(star.URL, self.out.getvalue())

    def test_enter_opens_the_browser_and_is_remembered(self):
        opened = []
        state = star.ask({}, self.out, lambda: '\n',
                         open_browser=lambda: opened.append(1) or True)
        self.assertEqual(state['answered'], 'opened_browser')
        self.assertEqual(len(opened), 1)

    def test_a_claimed_star_is_recorded_as_a_claim_not_a_fact(self):
        state = star.ask({}, self.out, lambda: 's', open_browser=lambda: False)
        # The wording matters: nothing here talks to GitHub, so the state file
        # must not read as though a star was verified.
        self.assertEqual(state['answered'], 'said_starred')
        self.assertNotIn('verified', json.dumps(state))

    def test_answering_once_stops_the_asking_forever(self):
        self.nudge(answer='s')
        before = self.out.getvalue()
        for _ in range(20):
            self.assertEqual(self.nudge(answer='n'), 'quiet')
        self.assertEqual(self.out.getvalue(), before)

    def test_declining_asks_again_on_a_cadence_instead_of_giving_up(self):
        asked = 0
        for _ in range(star.EVERY * 3):
            if self.nudge(answer='n') == 'declined':
                asked += 1
        self.assertEqual(asked, 3)
        self.assertEqual(json.load(open(self.path))['declines'], 3)

    def test_the_delay_arrives_only_after_many_declines_and_is_bounded(self):
        slept = []
        star.ask({'declines': star.PATIENCE - 1}, self.out, lambda: 'n',
                 sleep=slept.append, open_browser=lambda: False)
        self.assertEqual(slept, [])
        star.ask({'declines': star.PATIENCE}, self.out, lambda: 'n',
                 sleep=slept.append, open_browser=lambda: False)
        self.assertEqual(slept, [star.DELAY_S])

    def test_a_closed_stdin_is_a_decline_not_a_crash(self):
        def boom():
            raise EOFError
        state = star.ask({}, self.out, boom, open_browser=lambda: False)
        self.assertEqual(state['declines'], 1)

    def test_an_unwritable_state_directory_does_not_fail_the_run(self):
        self.assertFalse(star.save({'runs': 1}, '/proc/nope/star.json'))
        self.assertEqual(star.nudge(argv=['info'], out=self.out,
                                    read_line=lambda: 'n', env={},
                                    path='/proc/nope/star.json',
                                    stdin=Tty(), stdout=Tty()), 'declined')

    def test_a_corrupt_state_file_reads_as_unanswered(self):
        open(self.path, 'w').write('{not json')
        self.assertEqual(star.load(self.path), {})

    def test_the_answer_lives_per_user_never_inside_a_project(self):
        env = {'HANPATCH_STATE_DIR': self.dir}
        os.environ.update(env)
        try:
            self.assertEqual(star.state_path(),
                             os.path.join(self.dir, 'star.json'))
        finally:
            os.environ.pop('HANPATCH_STATE_DIR')
        default = star.state_path()
        self.assertIn(os.path.join('hanpatch', 'star.json'), default)
        self.assertNotIn(os.getcwd(), default)

    def test_nudge_swallows_anything_so_a_build_result_is_untouched(self):
        broken = object()                          # no .write, no .isatty
        self.assertEqual(star.nudge(argv=['build'], out=broken,
                                    read_line=lambda: '\n', env={},
                                    path=self.path, stdin=Tty(), stdout=Tty()),
                         'error')

    def test_the_cli_asks_after_the_command_and_keeps_its_exit_code(self):
        # The ask runs AFTER the command and must not become the exit code: a
        # release pipeline reads that number.
        import hanpatch.cli as cli
        order, seen = [], {}
        real_nudge, real_info = star.nudge, cli.cmd_info
        star.nudge = lambda **kw: order.append('nudge') or seen.setdefault('kw', kw)
        try:
            for wanted in (0, 3):
                order.clear()
                cli.cmd_info = lambda a, rc=wanted: order.append('command') or rc
                self.assertEqual(cli.main(['info']), wanted)
                self.assertEqual(order, ['command', 'nudge'])
        finally:
            star.nudge, cli.cmd_info = real_nudge, real_info
        self.assertIn('argv', seen['kw'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

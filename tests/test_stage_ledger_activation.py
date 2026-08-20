"""Opt-in isolation: a legacy title must not touch the staged ledger at all.

The existing three titles carry no `qa_upgrade` key. If merely running the gates
created a sidecar for them, this upgrade would have changed their build output -
the one thing it is not allowed to do. "No sidecar appeared" is necessary but not
sufficient, so these tests also spy on the ledger module itself: a call that
writes nothing today is still a call that could write tomorrow.
"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hanpatch import stage_ledger as sl  # noqa: E402

# Run in a child process: import side effects and monkeypatching must not leak
# into the rest of the suite, and a clean output directory has to be genuinely
# clean rather than merely emptied.
SPY_PROGRAM = r'''
import json, os, sys
sys.path.insert(0, %(root)r)

from hanpatch import config, pipeline, stage_ledger

calls = []
for name in ('bootstrap', 'record', 'record_gate_stage', 'record_failure',
             'bind_build', 'load'):
    def spy(*a, __n=name, **k):
        calls.append(__n)
        raise AssertionError('stage_ledger.%%s called for a legacy title' %% __n)
    setattr(stage_ledger, name, spy)

out_dir = %(out)r
config.out = lambda *parts: os.path.join(out_dir, *parts) if parts else out_dir
config.profile = lambda: %(profile)r

active = pipeline._ledger_active()
listing = sorted(os.listdir(out_dir))
print(json.dumps({'active': active, 'calls': calls, 'listing': listing}))
'''


def _run_spy(profile):
    with tempfile.TemporaryDirectory() as out_dir:
        src = SPY_PROGRAM % {'root': ROOT, 'out': out_dir, 'profile': profile}
        proc = subprocess.run([sys.executable, '-c', src],
                              capture_output=True, text=True)
        return proc


class LegacyTitleIsUntouched(unittest.TestCase):
    def test_absent_key_runs_no_ledger_code_and_writes_no_sidecar(self):
        proc = _run_spy({})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        import json
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(result['active'])
        self.assertEqual(result['calls'], [])
        self.assertEqual(result['listing'], [])

    def test_explicit_null_runs_no_ledger_code_and_writes_no_sidecar(self):
        proc = _run_spy({sl.ACTIVATION_KEY: None})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        import json
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(result['active'])
        self.assertEqual(result['calls'], [])
        self.assertEqual(result['listing'], [])


class ShippedProfilesAreLegacy(unittest.TestCase):
    """The three existing titles must read as not-opted-in, as shipped."""

    def _profile(self, name):
        import json
        path = os.path.join(ROOT, 'profiles', name)
        if not os.path.exists(path):
            self.skipTest('profile not present: %s' % name)
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)

    def test_dq7_is_legacy(self):
        self.assertFalse(sl.enabled(self._profile('dq7.json')))

    def test_crimson_shroud_is_legacy(self):
        self.assertFalse(sl.enabled(self._profile('crimson_shroud.json')))

    def test_classic_dungeon_x2_is_legacy(self):
        self.assertFalse(sl.enabled(self._profile('classic_dungeon_x2.json')))


if __name__ == '__main__':
    unittest.main()

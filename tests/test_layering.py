"""Layer discipline, checked by AST rather than by substring.

`tests/test_gates.py` already asserts that the container layer never imports the
wording layer, by scanning adapter sources for the literal text `import <module>`.
That check stays exactly as it is - it encodes a real regression and deleting it
would lose that history. But it has three blind spots a text scan cannot close:

* `from hanpatch import translate as t` never contains `import translate`
* `import hanpatch.translate` does not either
* a relative `from . import translate` does not either

So this module parses the same sources and inspects the resolved module paths.
Both checks run; the AST one is authoritative because it is the one that cannot
be evaded by spelling.

Two deliberate non-goals. The platform package is *not* banned wholesale: the
existing adapters legitimately import from it, and a blanket ban would fail the
three shipped titles to enforce a rule nobody asked for. And a parse failure or
an import failure is a hard failure here, never a quiet pass, because a checker
that returns clean when it could not look is worse than no checker.
"""

import ast
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: The wording layer. An adapter that reaches into these is deciding phrasing,
#: which is the core's job and not a container's.
WORDING_MODULES = ('translate', 'glossary', 'josa', 'providers', 'wrap')

#: Emulator control has no business anywhere in the pipeline. It is not a
#: dependency of this project; evidence arrives as a submitted file.
EMULATOR_MODULES = ('emucap',)

#: The container layer: adapters plus the abstract contract they implement.
def adapter_sources():
    base = os.path.join(ROOT, 'hanpatch')
    paths = [os.path.join(base, 'adapter.py')]
    adapters = os.path.join(base, 'adapters')
    for name in sorted(os.listdir(adapters)):
        if name.endswith('.py'):
            paths.append(os.path.join(adapters, name))
    return paths


#: Core modules added by this upgrade. They describe bytes and status; none of
#: them has any reason to know a ROM format or an emulator.
UPGRADE_CORE = ('stage_ledger.py', 'expected_write.py', 'runtime_evidence.py',
                'voice_gate.py', 'interop.py')


def imported_modules(path):
    """Every module path a source imports, resolved through aliases and relatives.

    A parse failure raises. Returning an empty set would report a file that
    could not be read as a file that imports nothing.
    """
    with open(path, encoding='utf-8') as fh:
        source = fh.read()
    tree = ast.parse(source, filename=path)

    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                # A relative import resolves within the package; record the
                # trailing name so `from . import translate` is visible.
                base = base or ''
            for alias in node.names:
                modules.add('%s.%s' % (base, alias.name) if base else alias.name)
            if base:
                modules.add(base)
    return modules


def offending(modules, banned):
    """Which banned names a module set actually reaches."""
    hits = set()
    for module in modules:
        tail = module.rsplit('.', 1)[-1]
        for name in banned:
            if tail == name or module == name or module.endswith('.%s' % name):
                hits.add(module)
    return hits


class AstIsAuthoritative(unittest.TestCase):
    def test_no_adapter_imports_the_wording_layer(self):
        offenders = []
        for path in adapter_sources():
            hits = offending(imported_modules(path), WORDING_MODULES)
            if hits:
                offenders.append('%s -> %s'
                                 % (os.path.relpath(path, ROOT), sorted(hits)))
        self.assertEqual(offenders, [])

    def test_no_adapter_imports_an_emulator_module(self):
        offenders = []
        for path in adapter_sources():
            hits = offending(imported_modules(path), EMULATOR_MODULES)
            if hits:
                offenders.append(os.path.relpath(path, ROOT))
        self.assertEqual(offenders, [])

    def test_upgrade_core_modules_import_neither_wording_nor_emulator(self):
        banned = WORDING_MODULES + EMULATOR_MODULES
        offenders = []
        for name in UPGRADE_CORE:
            path = os.path.join(ROOT, 'hanpatch', name)
            if not os.path.exists(path):
                continue
            hits = offending(imported_modules(path), banned)
            if hits:
                offenders.append('%s -> %s' % (name, sorted(hits)))
        self.assertEqual(offenders, [])

    def test_the_check_catches_spellings_a_substring_scan_misses(self):
        """The blind spots, demonstrated on synthetic sources."""
        import tempfile
        evasions = (
            'from hanpatch import translate as t\n',
            'import hanpatch.translate\n',
            'from . import glossary\n',
            'from hanpatch.providers import anything\n',
        )
        for source in evasions:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, 'sneaky.py')
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write(source)
                hits = offending(imported_modules(path), WORDING_MODULES)
                self.assertTrue(
                    hits, 'AST check must catch %r' % source.strip())
                # And confirm the substring scan genuinely would not.
                if 'import translate' not in source and \
                        'import glossary' not in source and \
                        'import providers' not in source:
                    self.assertNotIn('import translate', source)


class FailuresAreHardNotClean(unittest.TestCase):
    def test_a_parse_error_raises_rather_than_reporting_clean(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'broken.py')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('def (:\n')
            with self.assertRaises(SyntaxError):
                imported_modules(path)

    def test_a_missing_file_raises(self):
        with self.assertRaises(OSError):
            imported_modules(os.path.join(ROOT, 'no', 'such', 'file.py'))


class PlatformPackageIsNotBannedWholesale(unittest.TestCase):
    """Existing adapters import from platforms legitimately."""

    def test_at_least_one_adapter_imports_a_platform_module(self):
        found = False
        for path in adapter_sources():
            for module in imported_modules(path):
                if 'platforms' in module or 'formats' in module:
                    found = True
        self.assertTrue(
            found,
            'if no adapter imports a platform module, this guard is vacuous '
            'and a blanket ban would have gone unnoticed')

    def test_a_blanket_platform_ban_would_fail_the_shipped_adapters(self):
        # Stated as a test so a future edit that adds such a ban fails here
        # with the reason, instead of failing the three titles silently.
        offenders = []
        for path in adapter_sources():
            hits = offending(imported_modules(path), ('platforms',))
            if hits:
                offenders.append(os.path.relpath(path, ROOT))
        self.assertTrue(
            offenders,
            'shipped adapters do import platforms; banning it would break them')


class ChildProcessImportSmoke(unittest.TestCase):
    """Importing the upgrade core must not drag wording or emulator modules in."""

    PROGRAM = (
        'import sys\n'
        'sys.path.insert(0, %r)\n'
        'import importlib\n'
        'for name in %r:\n'
        '    try:\n'
        '        importlib.import_module("hanpatch." + name)\n'
        '    except ImportError:\n'
        '        pass\n'
        'leaked = sorted(m for m in sys.modules\n'
        '                if any(m.endswith("." + b) or m == b for b in %r))\n'
        'print(",".join(leaked))\n'
    )

    def test_importing_core_modules_leaks_no_banned_module(self):
        names = [n[:-3] for n in UPGRADE_CORE]
        banned = list(EMULATOR_MODULES)
        program = self.PROGRAM % (ROOT, names, banned)
        proc = subprocess.run([sys.executable, '-c', program],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        leaked = [x for x in proc.stdout.strip().split(',') if x]
        self.assertEqual(leaked, [])


class GenericModulesHoldNoFixtureKnowledge(unittest.TestCase):
    def test_no_production_module_imports_the_test_tree(self):
        offenders = []
        base = os.path.join(ROOT, 'hanpatch')
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != '__pycache__']
            for name in sorted(filenames):
                if not name.endswith('.py'):
                    continue
                path = os.path.join(dirpath, name)
                for module in imported_modules(path):
                    if module.split('.')[0] == 'tests':
                        offenders.append(os.path.relpath(path, ROOT))
        self.assertEqual(offenders, [])


class ExistingSubstringCheckIsPreserved(unittest.TestCase):
    """The original guard must still be there, unweakened."""

    def test_test_gates_still_contains_the_container_layer_case(self):
        with open(os.path.join(ROOT, 'tests', 'test_gates.py'),
                  encoding='utf-8') as fh:
            src = fh.read()
        self.assertIn('the container layer never imports the wording layer', src)
        self.assertIn("'translate', 'glossary', 'josa', 'providers', 'wrap'", src)


if __name__ == '__main__':
    unittest.main()

"""Documentation claims must point at something that exists.

`SKILL.md` describes behaviour. A claim written before its code exists, or left
behind after the code was renamed, reads exactly like a true one - which is how a
skill document becomes a wish list. So each claim declares what backs it, and
this test resolves the symbol claims against the real modules.

Claims that cannot be reduced to a symbol are listed as `human` with a reason.
That is not an escape hatch: a human claim still has to name where it lives and
why re-deriving it needs a person, so a reviewer knows exactly which sentences
are judgement rather than fact.
"""

import importlib
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SKILL_DIR = os.path.join(ROOT, 'skills', 'hanpatch')
MANIFEST = os.path.join(SKILL_DIR, 'references', 'claims.json')


def load_manifest():
    with open(MANIFEST, encoding='utf-8') as fh:
        return json.load(fh)


class ManifestShape(unittest.TestCase):
    def test_manifest_parses_and_declares_a_version(self):
        doc = load_manifest()
        self.assertEqual(doc['schemaVersion'], 1)
        self.assertTrue(doc['claims'])

    def test_every_claim_has_an_id_scope_statement_and_checker(self):
        for claim in load_manifest()['claims']:
            for field in ('id', 'scope', 'statement', 'checker'):
                self.assertTrue(claim.get(field),
                                'claim %r lacks %s' % (claim.get('id'), field))
            self.assertIn(claim['checker'], ('symbol', 'human'))

    def test_claim_ids_are_unique(self):
        ids = [c['id'] for c in load_manifest()['claims']]
        self.assertEqual(len(ids), len(set(ids)))


class SymbolClaimsResolve(unittest.TestCase):
    def test_every_symbol_claim_names_a_real_symbol(self):
        missing = []
        for claim in load_manifest()['claims']:
            if claim['checker'] != 'symbol':
                continue
            module = importlib.import_module(claim['module'])
            for dotted in claim['symbols']:
                target = module
                for part in dotted.split('.'):
                    if not hasattr(target, part):
                        missing.append('%s: %s.%s'
                                       % (claim['id'], claim['module'], dotted))
                        break
                    target = getattr(target, part)
        self.assertEqual(missing, [])

    def test_symbol_claims_are_not_vacuous(self):
        symbol_claims = [c for c in load_manifest()['claims']
                         if c['checker'] == 'symbol']
        self.assertGreaterEqual(len(symbol_claims), 8)
        for claim in symbol_claims:
            self.assertTrue(claim['symbols'],
                            'claim %r lists no symbols' % claim['id'])


class HumanClaimsAreAccountable(unittest.TestCase):
    def test_every_human_claim_names_its_source_and_reason(self):
        for claim in load_manifest()['claims']:
            if claim['checker'] != 'human':
                continue
            self.assertTrue(claim.get('source'),
                            'human claim %r must name where it lives' % claim['id'])
            self.assertTrue(claim.get('reason'),
                            'human claim %r must say why a person is needed'
                            % claim['id'])

    def test_human_claim_sources_exist(self):
        for claim in load_manifest()['claims']:
            if claim['checker'] != 'human':
                continue
            relative = claim['source'].split('#')[0]
            path = os.path.join(SKILL_DIR, relative)
            self.assertTrue(os.path.isfile(path),
                            'claim %r points at a missing file: %s'
                            % (claim['id'], relative))


class BodyHoldsPrinciplesNotMeasurements(unittest.TestCase):
    """Title-specific numbers belong in references, not in the body."""

    #: Numbers measured on one title. A reader working on a different title must
    #: not meet these as if they were thresholds.
    TITLE_NUMBERS = ('2416', '7795', '1084', '9810', '13788', '8856', '65836',
                     '340 KB', '249 MB', '379 files', '39.5 MB', '8 min 6 s',
                     '4.3 GB', '49,807', '4,493')

    def test_body_carries_no_title_specific_measurement(self):
        with open(os.path.join(SKILL_DIR, 'SKILL.md'), encoding='utf-8') as fh:
            body = fh.read()
        offenders = [n for n in self.TITLE_NUMBERS if n in body]
        self.assertEqual(offenders, [],
                         'move these to references/cases.md: %s' % offenders)

    def test_cases_reference_exists_and_holds_them(self):
        path = os.path.join(SKILL_DIR, 'references', 'cases.md')
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding='utf-8') as fh:
            cases = fh.read()
        found = [n for n in self.TITLE_NUMBERS if n in cases]
        self.assertGreaterEqual(
            len(found), 8,
            'cases.md should hold the measurements the body no longer states')

    #: Terms naming one platform's containers, crypto or tooling. A reader
    #: working on a Mega Drive title should not meet these as though they were
    #: general, and a body that names them has stopped being platform-neutral.
    PLATFORM_TERMS = ('LayeredFS', 'luma/titles/', 'Luma3DS', 'NCSD', 'NCCH',
                      'CIA', 'CCI', 'boot9.bin', 'seeddb.bin', 'HANPATCH_KEYS',
                      'xdelta3', 'BCFNT', 'RomFS', 'code.ips', 'TitleID')

    def test_platform_specifics_are_isolated(self):
        with open(os.path.join(SKILL_DIR, 'SKILL.md'), encoding='utf-8') as fh:
            body = fh.read()
        offenders = [t for t in self.PLATFORM_TERMS if t in body]
        self.assertEqual(offenders, [],
                         'these name one platform and belong in '
                         'references/3ds.md: %s' % offenders)

    def test_the_isolation_check_is_not_vacuous(self):
        # If the reference file did not hold these, the guard above would pass
        # by describing nothing.
        with open(os.path.join(SKILL_DIR, 'references', '3ds.md'),
                  encoding='utf-8') as fh:
            reference = fh.read()
        found = [t for t in self.PLATFORM_TERMS if t in reference]
        self.assertGreaterEqual(len(found), 8,
                                'references/3ds.md should hold the platform '
                                'facts the body no longer states')
        with open(os.path.join(SKILL_DIR, 'references', '3ds.md'),
                  encoding='utf-8') as fh:
            threeds = fh.read()
        self.assertIn('luma/titles/', threeds)


class BothSkillCopiesAgree(unittest.TestCase):
    """The repo copy and the installed copy must not drift."""

    GLOBAL = '/root/.gjc/skills/hanpatch/SKILL.md'

    def test_the_two_skill_files_are_identical(self):
        import hashlib

        def digest(path):
            with open(path, 'rb') as fh:
                return hashlib.sha256(fh.read()).hexdigest()

        repo = os.path.join(SKILL_DIR, 'SKILL.md')
        if not os.path.exists(self.GLOBAL):
            self.skipTest('no installed copy on this host')
        self.assertEqual(digest(repo), digest(self.GLOBAL),
                         'the repo and installed SKILL.md have drifted')


if __name__ == '__main__':
    unittest.main()

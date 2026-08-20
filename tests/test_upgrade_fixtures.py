"""The two fixture families, and the mutations the generic checker must refuse.

A fixture that validates itself proves nothing: the same assumption can live in
the writer, the parser and the expected plan at once, so all three agree and all
three are wrong together. Two things break that circle here.

**A golden oracle.** `FXR1_GOLDEN` and `IAR1_GOLDEN` are literal byte strings
checked into this file. The writer must reproduce them exactly. A shared bug in
writer and parser cannot hide, because the golden bytes were never produced by
either at test time.

**Adversarial mutations.** Every structural axis - protected header, per-record
prologue, alignment padding, sentinel, relocation offsets, CRC, and plain
unregistered bytes - is mutated and the generic byte-ownership checker is
required to refuse. A checker that only ever sees well-formed output is not
evidence of anything.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hanpatch import expected_write as ew  # noqa: E402
from tests.fixtures.upgrade import fxr1, iar1  # noqa: E402

RECORDS = [(1, 'alpha'), (2, 'beta')]

# Independent oracle: literal bytes, not produced by the code under test.
FXR1_GOLDEN = bytes.fromhex(
    '4658523101000200200020002000000070000000000000000000000000000000'
    '0100000000050000616c70686100000000000000000000000000000000000000'
    '0200000000040000626574610000000000000000000000000000000000000000'
    'a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5'
)

IAR1_GOLDEN = bytes.fromhex(
    '4941523101000200200000001800000080000000c00000000000000000000000'
    '010000000000000008000000050000006a39e0d0000000000200000008000000'
    '08000000040000006304918f0000000000000000000000000000000000000000'
    '0000000000000000000000000000000000000000000000000000000000000000'
    '0500616c70686100040062657461000000000000000000000000000000000000'
    '0000000000000000000000000000000000000000000000000000000000000000'
    '5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a'
)


def fxr1_plan(source, index, extra_writes=()):
    writes = list(fxr1.writable_spans(index)) + list(extra_writes)
    return ew.plan_from_writes(source, writes,
                               protected=fxr1.protected_spans(len(RECORDS)))


def iar1_plan(source, extra_writes=()):
    index_off, index_len = iar1.index_span(len(RECORDS))
    arena_off, arena_len = iar1.arena_span()
    writes = [(index_off, index_len, 'iar1:index'),
              (arena_off, arena_len, 'iar1:arena')] + list(extra_writes)
    return ew.plan_from_writes(source, writes,
                               protected=iar1.protected_spans(len(RECORDS)))


class GoldenOracle(unittest.TestCase):
    """The writer must reproduce bytes it did not produce."""

    def test_fxr1_matches_the_checked_in_bytes(self):
        self.assertEqual(fxr1.build(RECORDS), FXR1_GOLDEN)

    def test_iar1_matches_the_checked_in_bytes(self):
        self.assertEqual(iar1.build(RECORDS), IAR1_GOLDEN)

    def test_parsers_agree_with_the_golden_bytes(self):
        self.assertEqual(fxr1.parse(FXR1_GOLDEN), RECORDS)
        self.assertEqual(iar1.parse(IAR1_GOLDEN), RECORDS)

    def test_noop_rebuild_is_full_byte_equality(self):
        self.assertEqual(fxr1.build(fxr1.parse(FXR1_GOLDEN)), FXR1_GOLDEN)
        self.assertEqual(iar1.build(iar1.parse(IAR1_GOLDEN)), IAR1_GOLDEN)

    def test_families_are_structurally_different(self):
        # If both fixtures had the same shape, two families would prove one thing.
        self.assertNotEqual(len(FXR1_GOLDEN), len(IAR1_GOLDEN))
        self.assertEqual(FXR1_GOLDEN[:4], b'FXR1')
        self.assertEqual(IAR1_GOLDEN[:4], b'IAR1')


class Fxr1WriteSurface(unittest.TestCase):
    def test_a_declared_in_place_write_is_accepted(self):
        final = fxr1.write_text(FXR1_GOLDEN, 0, '알파')
        self.assertEqual(fxr1_plan(FXR1_GOLDEN, 0).verify(FXR1_GOLDEN, final), [])
        self.assertEqual(fxr1.entries(final)['fxr1/1'], '알파')

    def test_capacity_is_a_hard_ceiling(self):
        with self.assertRaises(fxr1.Fxr1Error):
            fxr1.write_text(FXR1_GOLDEN, 0, 'x' * (fxr1.SLOT_SIZE + 1))

    def test_clobbered_header_byte_is_refused(self):
        final = bytearray(fxr1.write_text(FXR1_GOLDEN, 0, '알파'))
        final[0x14] = 0x01                      # reserved header byte
        findings = fxr1_plan(FXR1_GOLDEN, 0).verify(FXR1_GOLDEN, bytes(final))
        self.assertEqual([f.reason for f in findings], [ew.UNREGISTERED_DIFF])

    def test_clobbered_record_id_is_refused(self):
        final = bytearray(fxr1.write_text(FXR1_GOLDEN, 0, '알파'))
        final[fxr1.record_offset(1)] = 0x09     # another record's stable id
        findings = fxr1_plan(FXR1_GOLDEN, 0).verify(FXR1_GOLDEN, bytes(final))
        self.assertEqual([f.reason for f in findings], [ew.UNREGISTERED_DIFF])

    def test_damaged_sentinel_is_refused(self):
        final = bytearray(fxr1.write_text(FXR1_GOLDEN, 0, '알파'))
        final[-1] = 0x00
        findings = fxr1_plan(FXR1_GOLDEN, 0).verify(FXR1_GOLDEN, bytes(final))
        self.assertEqual([f.reason for f in findings], [ew.UNREGISTERED_DIFF])
        self.assertTrue(fxr1.parse(FXR1_GOLDEN))            # source still fine
        with self.assertRaises(fxr1.Fxr1Error):
            fxr1.parse(bytes(final))                        # parser agrees

    def test_nonzero_padding_after_text_is_refused_by_the_parser(self):
        final = bytearray(fxr1.write_text(FXR1_GOLDEN, 0, 'hi'))
        slot, _ = fxr1.slot_span(0)
        final[slot + 10] = 0x41                 # stale byte past the declared text
        with self.assertRaises(fxr1.Fxr1Error):
            fxr1.parse(bytes(final))

    def test_a_plan_that_writes_the_header_is_refused_before_any_write(self):
        plan = ew.plan_from_writes(FXR1_GOLDEN, [(0, 4, 'clobber-magic')],
                                   protected=fxr1.protected_spans(len(RECORDS)))
        findings = plan.verify_source(FXR1_GOLDEN)
        self.assertIn(ew.PROTECTED_REGION, {f.reason for f in findings})


class Iar1Relocation(unittest.TestCase):
    def test_a_declared_relocating_write_is_accepted(self):
        final = iar1.rebuild_with(IAR1_GOLDEN, 0, '알파벳입니다')
        self.assertEqual(iar1_plan(IAR1_GOLDEN).verify(IAR1_GOLDEN, final), [])
        self.assertEqual(iar1.entries(final)['iar1/1'], '알파벳입니다')
        self.assertEqual(iar1.entries(final)['iar1/2'], 'beta')

    def test_relocation_actually_moves_the_later_frame(self):
        # Otherwise this family is not exercising relocation at all.
        before = int.from_bytes(
            IAR1_GOLDEN[iar1.index_offset(1) + 4:iar1.index_offset(1) + 8], 'little')
        final = iar1.rebuild_with(IAR1_GOLDEN, 0, '알파벳입니다')
        after = int.from_bytes(
            final[iar1.index_offset(1) + 4:iar1.index_offset(1) + 8], 'little')
        self.assertNotEqual(before, after)

    def test_file_length_never_changes(self):
        final = iar1.rebuild_with(IAR1_GOLDEN, 0, '알파벳입니다')
        self.assertEqual(len(final), len(IAR1_GOLDEN))

    def test_arena_overflow_is_refused(self):
        with self.assertRaises(iar1.Iar1Error):
            iar1.rebuild_with(IAR1_GOLDEN, 0, '가' * 40)

    def test_stale_index_offset_is_caught_by_the_parser(self):
        final = bytearray(iar1.rebuild_with(IAR1_GOLDEN, 0, '알파벳입니다'))
        base = iar1.index_offset(1)
        final[base + 4:base + 8] = (0).to_bytes(4, 'little')   # point at frame 0
        with self.assertRaises(iar1.Iar1Error):
            iar1.parse(bytes(final))

    def test_stale_crc_is_caught_by_the_parser(self):
        final = bytearray(iar1.rebuild_with(IAR1_GOLDEN, 0, '알파벳입니다'))
        base = iar1.index_offset(0)
        final[base + 16:base + 20] = (0).to_bytes(4, 'little')
        with self.assertRaisesRegex(iar1.Iar1Error, 'CRC'):
            iar1.parse(bytes(final))

    def test_text_length_disagreement_is_caught(self):
        final = bytearray(IAR1_GOLDEN)
        base = iar1.index_offset(0)
        final[base + 12:base + 14] = (99).to_bytes(2, 'little')
        with self.assertRaises(iar1.Iar1Error):
            iar1.parse(bytes(final))

    def test_reserved_gap_write_is_refused(self):
        final = bytearray(iar1.rebuild_with(IAR1_GOLDEN, 0, '알파벳입니다'))
        final[0x60] = 0x01                       # inside the index/data gap
        findings = iar1_plan(IAR1_GOLDEN).verify(IAR1_GOLDEN, bytes(final))
        self.assertEqual([f.reason for f in findings], [ew.UNREGISTERED_DIFF])

    def test_damaged_sentinel_is_refused(self):
        final = bytearray(iar1.rebuild_with(IAR1_GOLDEN, 0, '알파벳입니다'))
        final[-3] = 0x00
        findings = iar1_plan(IAR1_GOLDEN).verify(IAR1_GOLDEN, bytes(final))
        self.assertEqual([f.reason for f in findings], [ew.UNREGISTERED_DIFF])

    def test_header_data_end_clobber_is_refused(self):
        final = bytearray(iar1.rebuild_with(IAR1_GOLDEN, 0, '알파벳입니다'))
        final[0x14] = 0xFF                       # data_end in the protected header
        findings = iar1_plan(IAR1_GOLDEN).verify(IAR1_GOLDEN, bytes(final))
        self.assertEqual([f.reason for f in findings], [ew.UNREGISTERED_DIFF])


class BothFamiliesShareOneGenericChecker(unittest.TestCase):
    """The checker must not need to know which family it is looking at."""

    def test_same_api_verifies_both(self):
        fxr1_final = fxr1.write_text(FXR1_GOLDEN, 0, '알파')
        iar1_final = iar1.rebuild_with(IAR1_GOLDEN, 0, '알파벳입니다')
        self.assertEqual(fxr1_plan(FXR1_GOLDEN, 0).verify(FXR1_GOLDEN, fxr1_final), [])
        self.assertEqual(iar1_plan(IAR1_GOLDEN).verify(IAR1_GOLDEN, iar1_final), [])

    def test_the_two_families_fail_differently(self):
        # FXR1's characteristic defect is a clobbered fixed byte; IAR1's is an
        # index that stops describing its data. Both must be caught, by
        # different mechanisms, or one fixture is redundant.
        fxr1_final = bytearray(fxr1.write_text(FXR1_GOLDEN, 0, '알파'))
        fxr1_final[0x14] = 0x01
        byte_findings = fxr1_plan(FXR1_GOLDEN, 0).verify(
            FXR1_GOLDEN, bytes(fxr1_final))
        self.assertEqual([f.reason for f in byte_findings], [ew.UNREGISTERED_DIFF])

        iar1_final = bytearray(iar1.rebuild_with(IAR1_GOLDEN, 0, '알파벳입니다'))
        base = iar1.index_offset(0)
        iar1_final[base + 16:base + 20] = (0).to_bytes(4, 'little')
        # Byte ownership accepts it - the index row is a declared write - so the
        # structural parser is what must catch this one.
        self.assertEqual(
            iar1_plan(IAR1_GOLDEN).verify(IAR1_GOLDEN, bytes(iar1_final)), [])
        with self.assertRaisesRegex(iar1.Iar1Error, 'CRC'):
            iar1.parse(bytes(iar1_final))


class NoFixtureLeakIntoProduction(unittest.TestCase):
    """Fixture constants and imports must not reach shipped code."""

    PRODUCTION = os.path.join(ROOT, 'hanpatch')

    def _sources(self):
        for dirpath, dirnames, filenames in os.walk(self.PRODUCTION):
            dirnames[:] = [d for d in dirnames if d != '__pycache__']
            for name in sorted(filenames):
                if name.endswith('.py'):
                    yield os.path.join(dirpath, name)

    def test_no_production_module_mentions_a_fixture_magic(self):
        offenders = []
        for path in self._sources():
            with open(path, encoding='utf-8') as fh:
                src = fh.read()
            for token in ('FXR1', 'IAR1'):
                if token in src:
                    offenders.append('%s: %s' % (os.path.relpath(path, ROOT), token))
        self.assertEqual(offenders, [])

    def test_no_production_module_imports_the_fixtures(self):
        offenders = []
        for path in self._sources():
            with open(path, encoding='utf-8') as fh:
                src = fh.read()
            if 'tests.fixtures' in src or 'from tests' in src:
                offenders.append(os.path.relpath(path, ROOT))
        self.assertEqual(offenders, [])

    def test_fixtures_import_no_wording_or_emulator_module(self):
        for module in (fxr1, iar1):
            with open(module.__file__, encoding='utf-8') as fh:
                src = fh.read()
            for banned in ('translate', 'glossary', 'josa', 'providers', 'wrap',
                           'emucap'):
                self.assertNotIn('import %s' % banned, src)
                self.assertNotIn('from hanpatch import %s' % banned, src)


if __name__ == '__main__':
    unittest.main()

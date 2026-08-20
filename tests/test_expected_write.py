"""Expected Write: four refusals, and the differential proof that motivates them.

The centrepiece is `EntryVerifyVersusByteOwnership`. Adding a byte-ownership
checker is only justified if it catches something the existing entry-centric
`Adapter.verify` cannot. That class builds exactly that case and asserts both
halves: the entry verifier returns clean, and Expected Write refuses.

Independence is enforced by construction. The final bytes are read back from
disk after the writer has finished; nothing here asks the writer what it wrote.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hanpatch import expected_write as ew  # noqa: E402


# --------------------------------------------------------------------------
# A deliberately small fixed-record container, defined here and nowhere else.
# Production code never learns these constants; the generic checker only ever
# sees offsets, lengths and bytes.
# --------------------------------------------------------------------------

HEADER = 16          # magic(4) version(2) count(2) reserved(8)
SLOT = 24            # 24-byte NUL-padded UTF-8 text slot
RECORDS = 3
TAIL = 8             # trailing sentinel
TOTAL = HEADER + RECORDS * SLOT + TAIL


def build_container(texts):
    if len(texts) != RECORDS:
        raise AssertionError('fixture holds exactly %d records' % RECORDS)
    out = bytearray()
    out += b'TFX1'
    out += (1).to_bytes(2, 'little')
    out += RECORDS.to_bytes(2, 'little')
    out += bytes(8)                      # reserved: must stay zero
    for text in texts:
        raw = text.encode('utf-8')
        if len(raw) > SLOT:
            raise AssertionError('fixture text does not fit its slot')
        out += raw + bytes(SLOT - len(raw))
    out += b'\xa5' * TAIL                # sentinel
    assert len(out) == TOTAL
    return bytes(out)


def slot_offset(index):
    return HEADER + index * SLOT


def read_entries(blob):
    """Entry-centric read: exactly what a title adapter's verify would do."""
    out = {}
    count = int.from_bytes(blob[6:8], 'little')
    for i in range(count):
        start = slot_offset(i)
        raw = blob[start:start + SLOT]
        out['fixture/%d' % i] = raw.rstrip(b'\x00').decode('utf-8')
    return out


def entry_verify(blob, sealed):
    """The existing contract: did every sealed entry survive, byte for byte?

    Mirrors `Adapter.verify`: missing, truncated or differing text is a problem.
    Bytes nobody declared are outside its question entirely.
    """
    problems = []
    found = read_entries(blob)
    for key, expected in sealed.items():
        actual = found.get(key)
        if actual is None:
            problems.append('%s: missing' % key)
        elif actual != expected:
            problems.append('%s: %r != %r' % (key, actual, expected))
    return problems


def write_slot(blob, index, text):
    raw = text.encode('utf-8')
    if len(raw) > SLOT:
        raise AssertionError('text does not fit')
    out = bytearray(blob)
    start = slot_offset(index)
    out[start:start + SLOT] = raw + bytes(SLOT - len(raw))
    return bytes(out)


def protected_spans():
    return [
        (0, HEADER, 'header: magic, version, count and reserved bytes'),
        (TOTAL - TAIL, TAIL, 'trailing sentinel'),
    ]


SOURCE = build_container(['alpha', 'beta', 'gamma'])


class PlanShape(unittest.TestCase):
    def test_write_without_a_precondition_is_refused(self):
        with self.assertRaises(ew.PlanError) as ctx:
            ew.WriteEntry(offset=0, length=4, owner='x')
        self.assertIn('precondition', str(ctx.exception))

    def test_owner_is_required(self):
        with self.assertRaises(ew.PlanError):
            ew.WriteEntry(offset=0, length=4, owner='', original_hex='00' * 4)

    def test_precondition_length_must_match(self):
        with self.assertRaises(ew.PlanError):
            ew.WriteEntry(offset=0, length=4, owner='x', original_hex='00' * 3)

    def test_protected_region_needs_a_reason(self):
        with self.assertRaises(ew.PlanError):
            ew.ProtectedRegion(offset=0, length=4, reason='')

    def test_unknown_plan_key_is_refused_not_ignored(self):
        # A typo'd `orginal_hex` silently ignored is a plan with no precondition.
        doc = {'schemaVersion': 1,
               'writes': [{'offset': 0, 'length': 4, 'owner': 'x',
                           'orginal_hex': '00000000'}]}
        with self.assertRaises(ew.PlanError) as ctx:
            ew.WritePlan.from_dict(doc)
        self.assertIn('orginal_hex', str(ctx.exception))

    def test_negative_and_zero_spans_are_refused(self):
        with self.assertRaises(ew.PlanError):
            ew.WriteEntry(offset=-1, length=4, owner='x', original_hex='00' * 4)
        with self.assertRaises(ew.PlanError):
            ew.WriteEntry(offset=0, length=0, owner='x', original_hex='')

    def test_bool_is_not_an_int_offset(self):
        with self.assertRaises(ew.PlanError):
            ew.WriteEntry(offset=True, length=4, owner='x', original_hex='00' * 4)

    def test_wrong_schema_version_is_refused(self):
        with self.assertRaises(ew.PlanError):
            ew.WritePlan.from_dict({'schemaVersion': 99})


class FourRefusals(unittest.TestCase):
    """Each rejection reason, isolated so one cannot mask another."""

    def _plan(self):
        return ew.plan_from_writes(
            SOURCE,
            [(slot_offset(0), SLOT, 'fixture/0')],
            protected=protected_spans())

    def test_a_clean_declared_write_is_accepted(self):
        plan = self._plan()
        final = write_slot(SOURCE, 0, '알파')
        self.assertEqual(plan.verify(SOURCE, final), [])

    def test_wrong_original_bytes(self):
        plan = self._plan()
        # The plan was computed against a different source revision.
        other = write_slot(SOURCE, 0, 'ALPHA-DIFFERENT')
        findings = plan.verify_source(other)
        self.assertTrue(findings)
        self.assertEqual({f.reason for f in findings}, {ew.WRONG_ORIGINAL})

    def test_overlapping_writes(self):
        plan = ew.plan_from_writes(
            SOURCE,
            [(slot_offset(0), SLOT, 'owner-a'),
             (slot_offset(0) + 4, SLOT, 'owner-b')])
        findings = plan.verify_source(SOURCE)
        reasons = {f.reason for f in findings}
        self.assertIn(ew.OVERLAPPING_WRITES, reasons)
        overlap = [f for f in findings if f.reason == ew.OVERLAPPING_WRITES][0]
        self.assertIn('owner-a', overlap.owner)
        self.assertIn('owner-b', overlap.owner)

    def test_protected_region_write(self):
        plan = ew.plan_from_writes(
            SOURCE,
            [(0, 4, 'clobbers-magic')],
            protected=protected_spans())
        findings = plan.verify_source(SOURCE)
        self.assertIn(ew.PROTECTED_REGION, {f.reason for f in findings})

    def test_unregistered_final_diff(self):
        plan = self._plan()
        final = bytearray(write_slot(SOURCE, 0, '알파'))
        final[HEADER - 1] = 0xFF          # a reserved header byte nobody declared
        findings = plan.verify_final(SOURCE, bytes(final))
        self.assertEqual([f.reason for f in findings], [ew.UNREGISTERED_DIFF])
        self.assertEqual(findings[0].offset, HEADER - 1)
        self.assertEqual(findings[0].length, 1)

    def test_length_change_is_refused_plainly(self):
        plan = self._plan()
        findings = plan.verify_final(SOURCE, SOURCE + b'\x00')
        self.assertEqual([f.reason for f in findings], [ew.LENGTH_CHANGED])

    def test_each_refusal_is_independent(self):
        # All four reasons reachable without any other being present.
        seen = set()
        seen.update(f.reason for f in self._plan().verify_source(
            write_slot(SOURCE, 0, 'different')))
        seen.update(f.reason for f in ew.plan_from_writes(
            SOURCE, [(slot_offset(0), SLOT, 'a'),
                     (slot_offset(0) + 1, SLOT, 'b')]).verify_source(SOURCE))
        seen.update(f.reason for f in ew.plan_from_writes(
            SOURCE, [(0, 4, 'hdr')],
            protected=protected_spans()).verify_source(SOURCE))
        tampered = bytearray(SOURCE)
        tampered[HEADER - 2] = 0x01
        seen.update(f.reason for f in self._plan().verify_final(
            SOURCE, bytes(tampered)))
        self.assertEqual(
            seen,
            {ew.WRONG_ORIGINAL, ew.OVERLAPPING_WRITES, ew.PROTECTED_REGION,
             ew.UNREGISTERED_DIFF})


class EntryVerifyVersusByteOwnership(unittest.TestCase):
    """The differential proof: entry verify clean, Expected Write refuses.

    Without this, adding a byte-ownership checker rests on "no generic API
    exists", which is a statement about the codebase rather than about a defect
    it would catch.
    """

    def _capture_independently(self, path):
        """Read the artifact back from disk, not from the writer's return value."""
        with open(path, 'rb') as fh:
            return fh.read()

    def test_declared_text_correct_container_valid_reserved_byte_clobbered(self):
        sealed = {'fixture/0': '알파', 'fixture/1': 'beta', 'fixture/2': 'gamma'}

        with tempfile.TemporaryDirectory() as tmp:
            source_path = os.path.join(tmp, 'source.bin')
            with open(source_path, 'wb') as fh:
                fh.write(SOURCE)
            # Independent capture of the *original* boundary, before any writing.
            original = self._capture_independently(source_path)

            plan = ew.plan_from_writes(
                original,
                [(slot_offset(0), SLOT, 'fixture/0')],
                protected=protected_spans())

            # A writer that does its declared job perfectly and also corrupts one
            # reserved byte - a plausible off-by-one into the header.
            built = bytearray(write_slot(original, 0, '알파'))
            built[HEADER - 3] = 0x7F
            built_path = os.path.join(tmp, 'built.bin')
            with open(built_path, 'wb') as fh:
                fh.write(bytes(built))

            final = self._capture_independently(built_path)

            # 1. The container is still structurally valid.
            self.assertEqual(final[:4], b'TFX1')
            self.assertEqual(int.from_bytes(final[6:8], 'little'), RECORDS)
            self.assertEqual(len(final), len(original))

            # 2. Every sealed entry survived: the existing contract is satisfied.
            self.assertEqual(entry_verify(final, sealed), [],
                             'entry-centric verify must be clean for this case, '
                             'otherwise the differential proves nothing')

            # 3. Byte ownership refuses it.
            findings = plan.verify(original, final)
            self.assertEqual([f.reason for f in findings], [ew.UNREGISTERED_DIFF])
            self.assertEqual(findings[0].offset, HEADER - 3)

    def test_noop_rebuild_is_a_separate_property_and_still_holds(self):
        # Round-trip identity and byte ownership are different guarantees; the
        # first must not be quietly used as evidence for the second.
        rebuilt = build_container(['alpha', 'beta', 'gamma'])
        self.assertEqual(rebuilt, SOURCE)
        empty = ew.plan_from_writes(SOURCE, [], protected=protected_spans())
        self.assertEqual(empty.verify(SOURCE, rebuilt), [])

    def test_a_noop_rebuild_that_corrupts_a_reserved_byte_is_caught(self):
        # The round-trip property alone would pass a container whose declared
        # content is unchanged but whose reserved bytes moved.
        tampered = bytearray(SOURCE)
        tampered[HEADER - 1] = 0x02
        self.assertEqual(entry_verify(bytes(tampered),
                                      read_entries(SOURCE)), [])
        empty = ew.plan_from_writes(SOURCE, [], protected=protected_spans())
        findings = empty.verify_final(SOURCE, bytes(tampered))
        self.assertEqual([f.reason for f in findings], [ew.UNREGISTERED_DIFF])

    def test_checker_never_consults_the_writer(self):
        # verify_final's whole surface is (source, final) bytes. A checker that
        # accepted the writer's own account could only confirm self-consistency.
        import inspect
        sig = inspect.signature(ew.WritePlan.verify_final)
        self.assertEqual(list(sig.parameters), ['self', 'source', 'final'])


class Persistence(unittest.TestCase):
    def test_plan_round_trips_through_disk(self):
        plan = ew.plan_from_writes(
            SOURCE, [(slot_offset(1), SLOT, 'fixture/1')],
            protected=protected_spans())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'plan.json')
            plan.save(path)
            with open(path, encoding='utf-8') as fh:
                doc = json.load(fh)
            reloaded = ew.WritePlan.from_dict(doc)
            self.assertEqual(reloaded.as_dict(), plan.as_dict())
            final = write_slot(SOURCE, 1, '베타')
            self.assertEqual(reloaded.verify(SOURCE, final), [])

    def test_no_temp_file_survives_a_save(self):
        plan = ew.plan_from_writes(SOURCE, [], protected=protected_spans())
        with tempfile.TemporaryDirectory() as tmp:
            plan.save(os.path.join(tmp, 'plan.json'))
            leftovers = [f for f in os.listdir(tmp) if f.startswith('.write-plan-')]
            self.assertEqual(leftovers, [])

    def test_load_uses_the_validating_reader(self):
        # A list-shaped document must be named, not crash later on .get().
        from hanpatch import config
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'plan.json')
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump([], fh)
            with self.assertRaises(SystemExit) as ctx:
                ew.WritePlan.load(path)
            self.assertIn('write plan', str(ctx.exception))
            del config


class NoFixtureLeakIntoProduction(unittest.TestCase):
    def test_production_module_holds_no_fixture_constants(self):
        with open(os.path.join(ROOT, 'hanpatch', 'expected_write.py'),
                  encoding='utf-8') as fh:
            src = fh.read()
        for token in ('TFX1', 'FXR1', 'IAR1'):
            self.assertNotIn(token, src,
                             'a container magic must never reach generic code')


if __name__ == '__main__':
    unittest.main()

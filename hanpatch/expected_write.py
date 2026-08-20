"""Byte ownership for a build: every changed byte was declared, or the build fails.

`Adapter.verify` asks an entry-centric question - did each sealed string survive
the round trip? A build can answer that perfectly and still be broken: write the
right text into the right slots, and also clobber a reserved header byte, and
verify returns clean because every declared entry is exactly where it should be.
Nothing in the entry contract looks at bytes nobody declared.

This module asks the byte-centric question instead. A write plan declares, for
each region, where it writes, how long it is, what it expects to find there
first, and who owns it. Verification then refuses four separate ways:

1. **wrong original bytes** - the source did not hold what the plan expected, so
   the plan was computed against a different input than the one being built
2. **overlapping writes** - two owners claim the same bytes and the result
   depends on apply order, which is not a contract
3. **protected region** - a write lands inside a declared no-write span
4. **unregistered diff** - the final artifact differs from the source somewhere
   no plan entry covers

The fourth is the one that catches the case above, and it is why the checker
compares whole boundaries rather than sampling the declared spans.

Independence matters for the proof. `verify_final` takes the source and final
bytes as inputs; it never asks the writer what it wrote. A checker fed the
writer's own idea of the output can only confirm the writer agrees with itself.

Intervals are half-open, `[offset, offset+length)`. Preconditions are exact:
either the literal bytes or their SHA-256, never a wildcard, because a plan that
can match anything cannot prove it matched the right thing.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile

SCHEMA_VERSION = 1

#: Rejection reasons. Stable strings: tests and reports name them directly.
WRONG_ORIGINAL = 'wrong-original-bytes'
OVERLAPPING_WRITES = 'overlapping-writes'
PROTECTED_REGION = 'protected-region-write'
UNREGISTERED_DIFF = 'unregistered-final-diff'
MALFORMED_PLAN = 'malformed-plan'
LENGTH_CHANGED = 'artifact-length-changed'


class PlanError(ValueError):
    """A plan that cannot be interpreted at all."""


class Finding:
    """One refusal, with enough detail to repair it rather than retry blindly."""

    __slots__ = ('reason', 'detail', 'offset', 'length', 'owner')

    def __init__(self, reason, detail, offset=None, length=None, owner=None):
        self.reason = reason
        self.detail = detail
        self.offset = offset
        self.length = length
        self.owner = owner

    def __repr__(self):
        where = '' if self.offset is None else ' at 0x%x' % self.offset
        who = '' if self.owner is None else ' [%s]' % self.owner
        return '<%s%s%s: %s>' % (self.reason, where, who, self.detail)

    def as_dict(self):
        return {'reason': self.reason, 'detail': self.detail,
                'offset': self.offset, 'length': self.length, 'owner': self.owner}


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


class WriteEntry:
    """One declared write: where, how long, what was there, and who owns it."""

    __slots__ = ('offset', 'length', 'owner', 'original_sha256', 'original_hex')

    def __init__(self, offset, length, owner, original_sha256=None,
                 original_hex=None):
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise PlanError('offset must be a non-negative int; got %r' % (offset,))
        if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
            raise PlanError('length must be a positive int; got %r' % (length,))
        if not owner or not isinstance(owner, str):
            raise PlanError('every write needs a non-empty owner; got %r' % (owner,))
        if original_sha256 is None and original_hex is None:
            # No wildcard precondition. A write that does not say what it expects
            # to overwrite cannot detect that it was computed against a different
            # source, which is exactly the failure this is here to catch.
            raise PlanError(
                'write %s at 0x%x declares no original precondition; '
                'give original_hex or original_sha256' % (owner, offset))
        if original_hex is not None:
            try:
                raw = bytes.fromhex(original_hex)
            except ValueError as err:
                raise PlanError('original_hex for %s is not hex: %s' % (owner, err))
            if len(raw) != length:
                raise PlanError(
                    'original_hex for %s is %d bytes but length is %d'
                    % (owner, len(raw), length))
        self.offset = offset
        self.length = length
        self.owner = owner
        self.original_sha256 = original_sha256
        self.original_hex = original_hex

    @property
    def end(self):
        return self.offset + self.length

    def matches_original(self, source):
        actual = source[self.offset:self.end]
        if len(actual) != self.length:
            return False, 'source ends before the declared span'
        if self.original_hex is not None:
            expected = bytes.fromhex(self.original_hex)
            if actual != expected:
                return False, ('expected %s, found %s'
                               % (expected.hex(), actual.hex()))
            return True, None
        actual_digest = _sha256(actual)
        if actual_digest != self.original_sha256:
            return False, ('expected sha256 %s, found %s'
                           % (self.original_sha256, actual_digest))
        return True, None

    def as_dict(self):
        out = {'offset': self.offset, 'length': self.length, 'owner': self.owner}
        if self.original_hex is not None:
            out['original_hex'] = self.original_hex
        if self.original_sha256 is not None:
            out['original_sha256'] = self.original_sha256
        return out


class ProtectedRegion:
    """A span no write may touch, with the reason it is protected."""

    __slots__ = ('offset', 'length', 'reason')

    def __init__(self, offset, length, reason):
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise PlanError('protected offset must be a non-negative int')
        if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
            raise PlanError('protected length must be a positive int')
        if not reason or not isinstance(reason, str):
            raise PlanError(
                'a protected region needs a reason; an unexplained one gets '
                'deleted the first time it is inconvenient')
        self.offset = offset
        self.length = length
        self.reason = reason

    @property
    def end(self):
        return self.offset + self.length

    def as_dict(self):
        return {'offset': self.offset, 'length': self.length,
                'reason': self.reason}


class WritePlan:
    """The declared write surface for one build."""

    def __init__(self, writes=(), protected=(), source_sha256=None,
                 source_length=None):
        self.writes = list(writes)
        self.protected = list(protected)
        self.source_sha256 = source_sha256
        self.source_length = source_length

    # -- construction ----------------------------------------------------

    @classmethod
    def from_dict(cls, doc):
        if not isinstance(doc, dict):
            raise PlanError('a write plan must be an object')
        version = doc.get('schemaVersion')
        if version != SCHEMA_VERSION:
            raise PlanError('write plan schemaVersion must be %d; got %r'
                            % (SCHEMA_VERSION, version))
        writes = [WriteEntry(**_expect_keys(w, ('offset', 'length', 'owner'),
                                            ('original_sha256', 'original_hex')))
                  for w in doc.get('writes', [])]
        protected = [ProtectedRegion(**_expect_keys(p, ('offset', 'length',
                                                        'reason'), ()))
                     for p in doc.get('protected', [])]
        return cls(writes=writes, protected=protected,
                   source_sha256=doc.get('source_sha256'),
                   source_length=doc.get('source_length'))

    @classmethod
    def load(cls, path):
        """Read a plan through the validating loader.

        `config.load_object` is the sanctioned reader for a state document here;
        a bare json.load accepts a list or scalar and fails later with a bare
        AttributeError instead of naming the file.
        """
        from hanpatch import config
        return cls.from_dict(config.load_object(path, 'the write plan'))

    def as_dict(self):
        return {
            'schemaVersion': SCHEMA_VERSION,
            'source_sha256': self.source_sha256,
            'source_length': self.source_length,
            'writes': [w.as_dict() for w in self.writes],
            'protected': [p.as_dict() for p in self.protected],
        }

    def save(self, path):
        """Atomic replace: a torn plan still parses, which makes it worse than none."""
        payload = json.dumps(self.as_dict(), indent=2, sort_keys=True,
                             ensure_ascii=False) + '\n'
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix='.write-plan-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return path

    # -- verification ----------------------------------------------------

    def covers(self, index):
        for w in self.writes:
            if w.offset <= index < w.end:
                return w
        return None

    def verify_source(self, source):
        """Preconditions, overlaps and protected spans - before anything is written."""
        findings = []

        if self.source_sha256 is not None and _sha256(source) != self.source_sha256:
            findings.append(Finding(
                WRONG_ORIGINAL,
                'source digest %s does not match the plan digest %s'
                % (_sha256(source), self.source_sha256)))
        if self.source_length is not None and len(source) != self.source_length:
            findings.append(Finding(
                WRONG_ORIGINAL,
                'source is %d bytes; the plan was built against %d'
                % (len(source), self.source_length)))

        for w in self.writes:
            if w.end > len(source):
                findings.append(Finding(
                    WRONG_ORIGINAL,
                    'write runs past the end of a %d-byte source' % len(source),
                    offset=w.offset, length=w.length, owner=w.owner))
                continue
            ok, detail = w.matches_original(source)
            if not ok:
                findings.append(Finding(WRONG_ORIGINAL, detail, offset=w.offset,
                                        length=w.length, owner=w.owner))

        # Overlap is checked pairwise on a sorted copy: an ordering-dependent
        # result is not a contract, so it is refused rather than resolved.
        ordered = sorted(self.writes, key=lambda w: (w.offset, w.length))
        for a, b in zip(ordered, ordered[1:]):
            if b.offset < a.end:
                findings.append(Finding(
                    OVERLAPPING_WRITES,
                    'owners %r and %r both claim bytes [0x%x, 0x%x)'
                    % (a.owner, b.owner, b.offset, min(a.end, b.end)),
                    offset=b.offset, length=min(a.end, b.end) - b.offset,
                    owner='%s|%s' % (a.owner, b.owner)))

        for w in self.writes:
            for region in self.protected:
                start = max(w.offset, region.offset)
                stop = min(w.end, region.end)
                if start < stop:
                    findings.append(Finding(
                        PROTECTED_REGION,
                        'write %r lands in a protected span [0x%x, 0x%x): %s'
                        % (w.owner, region.offset, region.end, region.reason),
                        offset=start, length=stop - start, owner=w.owner))
        return findings

    def verify_final(self, source, final):
        """Every byte that differs must be inside a declared write.

        Takes both boundaries as bytes. It never consults the writer, because a
        checker fed the writer's own account of its output only proves the writer
        is self-consistent.
        """
        findings = []

        if len(source) != len(final):
            # A length change moves every later offset, so byte-for-byte
            # comparison would report noise. Refuse plainly instead.
            findings.append(Finding(
                LENGTH_CHANGED,
                'final artifact is %d bytes; the source was %d. This plan format '
                'describes in-place writes only.' % (len(final), len(source))))
            return findings

        index = 0
        size = len(source)
        while index < size:
            if source[index] == final[index]:
                index += 1
                continue
            owner = self.covers(index)
            if owner is not None:
                index = owner.end
                continue
            run_start = index
            while (index < size and source[index] != final[index]
                   and self.covers(index) is None):
                index += 1
            findings.append(Finding(
                UNREGISTERED_DIFF,
                'bytes [0x%x, 0x%x) changed but no write declares them '
                '(source %s -> final %s)'
                % (run_start, index,
                   source[run_start:index][:16].hex(),
                   final[run_start:index][:16].hex()),
                offset=run_start, length=index - run_start))
        return findings

    def verify(self, source, final):
        """Both halves, in the order a build performs them."""
        return self.verify_source(source) + self.verify_final(source, final)


def _expect_keys(obj, required, optional):
    if not isinstance(obj, dict):
        raise PlanError('plan entries must be objects; got %r' % type(obj).__name__)
    unknown = set(obj) - set(required) - set(optional)
    if unknown:
        # Unknown keys are refused rather than ignored: a typo'd `orginal_hex`
        # silently ignored is a plan with no precondition at all.
        raise PlanError('unknown plan keys: %s' % ', '.join(sorted(unknown)))
    missing = [k for k in required if k not in obj]
    if missing:
        raise PlanError('plan entry is missing %s' % ', '.join(missing))
    return {k: obj[k] for k in obj}


def plan_from_writes(source, writes, protected=()):
    """Build a plan whose preconditions are read from the real source.

    Convenience for callers that know their spans. Preconditions come from the
    source bytes themselves, so an entry can never disagree with the input the
    plan was built against.
    """
    entries = []
    for offset, length, owner in writes:
        chunk = source[offset:offset + length]
        if len(chunk) != length:
            raise PlanError('write %r at 0x%x runs past the source end'
                            % (owner, offset))
        entries.append(WriteEntry(offset=offset, length=length, owner=owner,
                                  original_hex=chunk.hex()))
    regions = [ProtectedRegion(offset=o, length=l, reason=r)
               for o, l, r in protected]
    return WritePlan(writes=entries, protected=regions,
                     source_sha256=_sha256(source), source_length=len(source))

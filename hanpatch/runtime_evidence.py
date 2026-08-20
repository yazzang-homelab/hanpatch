"""Runtime evidence: check the envelope, never the story inside it.

No static gate can establish that a patched game runs. Something has to observe
it, and what counts as a meaningful observation depends entirely on the title:
the first line of dialogue proves something in an RPG and nothing in a puzzle
game, and a menu that renders correctly is the whole question for one title and
irrelevant for another.

So this module validates *shape* and refuses to have an opinion about content.
``scenario``, ``expected`` and ``observed`` are opaque JSON. There is no scene
enum, no required step names, no notion of what a good smoke test looks like.
Baking any of that in would encode one genre's idea of proof into a checker that
every other genre has to satisfy - which is how a generic pipeline quietly
becomes a single-title one.

What is enforced is the part that is title-independent:

* the document says which build it describes, and that hash must match
* required fields exist and have the right JSON types
* hashes are hashes, sizes are sizes, depth and volume stay bounded
* the status is one of two words, and neither of them is inventable

And one rule that matters more than the rest: **absent evidence is never a
pass.** A missing file produces `NOT_RUN` on the ledger and nothing else. There
is no code path here that manufactures a passing envelope, because a synthetic
pass is indistinguishable from a real one once written down.

emucap is a fine way to produce these documents and this module knows nothing
about it. The pipeline stays emulator-free: an operator collects evidence by
whatever means their platform allows and submits a file.
"""

from __future__ import annotations

import json
import re

SCHEMA_VERSION = 1
DOCUMENT_KIND = 'runtime_evidence'

PASS = 'PASS'
FAIL = 'FAIL'
#: Deliberately absent from RESULTS. NOT_RUN is a ledger state meaning "nobody
#: looked", so a submitted document may never claim it: that would let a
#: non-observation be filed as an observation.
RESULTS = (PASS, FAIL)

MAX_DEPTH = 12
MAX_BYTES = 512 * 1024
MAX_EVIDENCE_ITEMS = 256
MAX_INTERVENTIONS = 256

_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_RFC3339_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$')


class EvidenceError(ValueError):
    """A document that cannot be accepted as evidence."""


def _depth(value, level=0):
    if level > MAX_DEPTH:
        return level
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()] or [level])
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value] or [level])
    return level


def _require_sha256(value, field, errors):
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        errors.append('%s must be a lowercase 64-character sha256; got %r'
                      % (field, value))


def validate(document, build_sha256=None, source_sha256=None):
    """Return a list of problems. Empty means the envelope is acceptable.

    Acceptable is not the same as true: this says the document is well formed
    and describes this build, not that the observation it reports actually
    happened. Nothing here can establish the latter, and pretending otherwise is
    the failure mode worth avoiding.
    """
    errors = []

    if not isinstance(document, dict):
        return ['evidence must be a JSON object; got %s'
                % type(document).__name__]

    if document.get('schema_version') != SCHEMA_VERSION:
        errors.append('schema_version must be %d; got %r'
                      % (SCHEMA_VERSION, document.get('schema_version')))
    if document.get('kind') != DOCUMENT_KIND:
        errors.append('kind must be %r; got %r'
                      % (DOCUMENT_KIND, document.get('kind')))

    scenario_id = document.get('scenario_id')
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        errors.append('scenario_id must be a non-empty string; got %r'
                      % (scenario_id,))

    result = document.get('result')
    if result not in RESULTS:
        errors.append(
            'result must be one of %s; got %r. NOT_RUN is a ledger state for '
            'evidence nobody submitted and cannot be claimed by a document.'
            % (', '.join(RESULTS), result))

    _require_sha256(document.get('build_sha256'), 'build_sha256', errors)
    if 'source_sha256' in document and document['source_sha256'] is not None:
        _require_sha256(document.get('source_sha256'), 'source_sha256', errors)

    if build_sha256 is not None and document.get('build_sha256') != build_sha256:
        errors.append(
            'evidence describes build %r but this build is %r'
            % (document.get('build_sha256'), build_sha256))
    if (source_sha256 is not None and document.get('source_sha256') is not None
            and document.get('source_sha256') != source_sha256):
        errors.append('evidence describes source %r but this source is %r'
                      % (document.get('source_sha256'), source_sha256))

    captured = document.get('captured_at')
    if not isinstance(captured, str) or not _RFC3339_RE.match(captured):
        errors.append('captured_at must be an RFC3339 timestamp; got %r'
                      % (captured,))

    if 'scenario' not in document:
        errors.append('scenario is required, even though its content is opaque')

    # Depth is checked over the WHOLE document, not just the opaque fields. A
    # limit that only covers the keys we thought of is no limit: a deep payload
    # smuggled under any other key sails past while the same payload under
    # `scenario` is refused.
    depth = _depth(document)
    if depth > MAX_DEPTH:
        errors.append('the document nests %d levels; the limit is %d'
                      % (depth, MAX_DEPTH))

    known = {'schema_version', 'kind', 'scenario_id', 'result', 'build_sha256',
             'source_sha256', 'captured_at', 'scenario', 'expected', 'observed',
             'evidence', 'interventions', 'collector'}
    unknown = sorted(set(document) - known)
    if unknown:
        # Refused rather than ignored: an unknown key is either a typo that
        # silently disabled a field, or a place to hide payload.
        errors.append('unknown evidence keys: %s' % ', '.join(unknown))

    evidence = document.get('evidence', [])
    if not isinstance(evidence, list):
        errors.append('evidence must be a list')
    else:
        if len(evidence) > MAX_EVIDENCE_ITEMS:
            errors.append('evidence holds %d items; the limit is %d'
                          % (len(evidence), MAX_EVIDENCE_ITEMS))
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append('evidence[%d] must be an object' % index)
                continue
            item_id = item.get('id')
            if not isinstance(item_id, str) or not item_id.strip():
                errors.append('evidence[%d].id must be a non-empty string' % index)
            _require_sha256(item.get('sha256'), 'evidence[%d].sha256' % index,
                            errors)
            size = item.get('size')
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                errors.append('evidence[%d].size must be a non-negative int'
                              % index)
            if 'kind' in item and item['kind'] is not None:
                if not isinstance(item['kind'], str):
                    errors.append('evidence[%d].kind must be a string or null'
                                  % index)

    interventions = document.get('interventions', [])
    if not isinstance(interventions, list):
        errors.append('interventions must be a list')
    elif len(interventions) > MAX_INTERVENTIONS:
        errors.append('interventions holds %d items; the limit is %d'
                      % (len(interventions), MAX_INTERVENTIONS))

    collector = document.get('collector')
    if collector is not None and not isinstance(collector, str):
        errors.append('collector must be a string or null')

    try:
        encoded = json.dumps(document, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError) as err:
        errors.append('evidence is not JSON-serialisable: %s' % err)
    else:
        if len(encoded) > MAX_BYTES:
            errors.append('evidence is %d bytes; the limit is %d'
                          % (len(encoded), MAX_BYTES))

    return errors


def load(path):
    """Read a submitted document through the validating loader."""
    from hanpatch import config
    return config.load_object(path, 'the runtime evidence')


def accept(path, build_sha256=None, source_sha256=None):
    """Validate a submitted file and return it, or raise.

    There is deliberately no variant that returns a default document when the
    file is missing: the caller must handle absence as absence.
    """
    document = load(path)
    errors = validate(document, build_sha256=build_sha256,
                      source_sha256=source_sha256)
    if errors:
        raise EvidenceError('%d problem(s): %s' % (len(errors), '; '.join(errors)))
    return document


def ledger_status(documents):
    """Fold submitted evidence into the RUNTIME_SMOKE token's status.

    No submissions means NOT_RUN with a reason, never PASS. One failure is
    enough to fail the token: partial success is not runtime proof.
    """
    from hanpatch import stage_ledger

    if not documents:
        return stage_ledger.NOT_RUN, 'no runtime evidence was submitted'
    failed = [d for d in documents if d.get('result') == FAIL]
    if failed:
        ids = ', '.join(sorted(str(d.get('scenario_id')) for d in failed))
        return stage_ledger.FAIL, 'failing scenario(s): %s' % ids
    ids = ', '.join(sorted(str(d.get('scenario_id')) for d in documents))
    return stage_ledger.PASS, 'passing scenario(s): %s' % ids

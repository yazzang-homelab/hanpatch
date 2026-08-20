"""The consumer boundary: hanpatch checks provenance, never voice.

Nothing here knows what a speech marker is. That is deliberate and load-bearing:
`hancharacter` already owns marker, density and corpus judgement, and a second
implementation in this repository would drift from it until the two disagree
about the same line and nobody can say which is right.

So this module answers three narrow questions about a verdict document produced
elsewhere:

1. is it the shape a verdict is supposed to have,
2. does it describe *this* build's contract, and
3. what authority did the translator claim.

A title that declares nothing is not a failure. Most titles have no speech
contract, and treating "undeclared" as "broken" would make the gate unusable for
exactly the projects it should not block. Absent and null both read
``NOT_DECLARED``, pass, and are imprinted in the gate summary so a reader can
never mistake silence for a clean bill of health.

Cardinality is singleton in v1: one sealed contract, one hash, compared in the
pointer, the verdict and here. Two hashes are carried for different questions -
the semantic seal versus the raw file bytes - and confusing them produces a
mismatch that has nothing to do with the text.
"""

from __future__ import annotations

import hashlib
import json
import os

SCHEMA_VERSION = 1
DOCUMENT_KIND = 'hancharacter-voice-verdict'

#: Profile key holding the pointer. Absent or null means "no declaration".
POINTER_KEY = 'voice_contract'
AUTHORITY_KEY = 'voice_authority'

NOT_DECLARED = 'NOT_DECLARED'
PASS = 'PASS'
FAIL = 'FAIL'

#: Authorities hancharacter distinguishes. They are not interchangeable: one
#: claims an established rendering, the other only claims internal consistency.
#: Silently substituting one for the other would launder a weaker claim.
AUTHORITIES = ('translator_declared', 'precedent_backed')


class VoiceGateError(RuntimeError):
    """A declared contract that cannot be honoured."""


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _canonical_sha256(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(',', ':'),
                   ensure_ascii=False).encode('utf-8')).hexdigest()


def pointer(profile=None):
    """Return the validated pointer object, or None when undeclared.

    Strict on shape, permissive on absence. A wrongly shaped declaration is a
    hard error because it was meant to enforce something; no declaration is the
    ordinary case.
    """
    if profile is None:
        from hanpatch import config
        profile = config.profile()

    raw = profile.get(POINTER_KEY) if hasattr(profile, 'get') else None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise VoiceGateError(
            '%s must be an object or null; got %s'
            % (POINTER_KEY, type(raw).__name__))

    required = ('schema_version', 'verdict_path', 'contract_path',
                'contract_sha256')
    missing = [k for k in required if not raw.get(k)]
    if missing:
        raise VoiceGateError('%s is missing %s'
                             % (POINTER_KEY, ', '.join(missing)))
    if raw['schema_version'] != SCHEMA_VERSION:
        raise VoiceGateError('%s.schema_version must be %d; got %r'
                             % (POINTER_KEY, SCHEMA_VERSION,
                                raw['schema_version']))

    authority = profile.get(AUTHORITY_KEY)
    if authority not in AUTHORITIES:
        raise VoiceGateError(
            '%s must be one of %s; got %r. These claim different things and one '
            'may not stand in for the other.'
            % (AUTHORITY_KEY, ', '.join(AUTHORITIES), authority))

    out = dict(raw)
    out[AUTHORITY_KEY] = authority
    return out


def declared(profile=None):
    return pointer(profile) is not None


def _resolve(base_dir, relative):
    candidate = relative if os.path.isabs(relative) else os.path.join(
        base_dir, relative)
    return os.path.abspath(candidate)


def evaluate(profile=None, base_dir=None):
    """Check a verdict's provenance and report its result.

    Returns a summary dict. The authority and the declaration state are always
    present, so a report can state what was and was not claimed rather than
    implying coverage by omission.
    """
    if profile is None:
        from hanpatch import config
        profile = config.profile()
    if base_dir is None:
        from hanpatch import config
        base_dir = config.root()

    ptr = pointer(profile)
    if ptr is None:
        return {
            'status': NOT_DECLARED,
            'declared': False,
            'authority': None,
            'detail': 'the profile declares no speech contract',
            'findings': [],
        }

    verdict_path = _resolve(base_dir, ptr['verdict_path'])
    contract_path = _resolve(base_dir, ptr['contract_path'])

    for label, target in (('verdict', verdict_path), ('contract', contract_path)):
        if not os.path.isfile(target):
            raise VoiceGateError('declared %s file is missing: %s'
                                 % (label, target))

    from hanpatch import config
    document = config.load_object(verdict_path, 'the voice verdict')

    if document.get('kind') != DOCUMENT_KIND:
        raise VoiceGateError('verdict kind %r is not %r'
                             % (document.get('kind'), DOCUMENT_KIND))
    if document.get('schemaVersion') != SCHEMA_VERSION:
        raise VoiceGateError('verdict schemaVersion %r is not %d'
                             % (document.get('schemaVersion'), SCHEMA_VERSION))

    contract = config.load_object(contract_path, 'the speech contract')
    semantic = _canonical_sha256(contract)

    # The pointer pins the semantic seal, not the file bytes. Comparing a file
    # hash here would fail on a reformat that changed nothing.
    if ptr['contract_sha256'] != semantic:
        raise VoiceGateError(
            'the profile pins contract %s but the contract on disk is %s'
            % (ptr['contract_sha256'], semantic))
    if document.get('contractSha256') != semantic:
        raise VoiceGateError(
            'the verdict judged contract %s but this build uses %s'
            % (document.get('contractSha256'), semantic))

    # The raw file hash is a separate claim and is only checked when the producer
    # recorded one, because it answers "same bytes", not "same contract".
    recorded_file_hash = document.get('contractFileSha256')
    if recorded_file_hash is not None:
        actual = _sha256_file(contract_path)
        if recorded_file_hash != actual:
            raise VoiceGateError(
                'the verdict was produced from contract bytes %s but the file '
                'now hashes %s' % (recorded_file_hash, actual))

    verdict = document.get('verdict')
    if verdict not in ('pass', 'fail'):
        raise VoiceGateError('verdict %r is neither pass nor fail' % verdict)

    findings = document.get('findings') or []
    if verdict == 'fail':
        return {
            'status': FAIL,
            'declared': True,
            'authority': ptr[AUTHORITY_KEY],
            'detail': '%d hard finding(s) from hancharacter'
                      % document.get('hardFindingCount', len(findings)),
            'findings': findings,
        }
    return {
        'status': PASS,
        'declared': True,
        'authority': ptr[AUTHORITY_KEY],
        'detail': 'hancharacter reported no hard findings',
        'findings': [],
    }


def summary_line(result):
    """One line that states the declaration, never implying absent coverage."""
    if result['status'] == NOT_DECLARED:
        return 'voice: NOT_DECLARED (no speech contract in this profile)'
    return 'voice: %s (authority: %s) - %s' % (
        result['status'], result['authority'], result['detail'])

"""The staged QA ledger: what each stage proved, and what it did not.

A gate report says a gate passed. It does not say *which* release-stage claim
that pass supports, and it does not say what remains unproven. Reading a green
gate run as "the patch is verified" is the mistake this module exists to make
impossible: the ledger records the eight staged tokens separately, and a token
only leaves ``NOT_RUN`` when something actually proved it.

Three rules give the file its shape.

**Existing authority is mapped, never re-implemented.** Most tokens are already
proven by code that ships today - ``pipeline.verify`` plus the title adapter for
readback, ``release.create`` for packaging, ``channel.publish`` for release,
``glossary``/``audit``/``qagate`` for source QA. Those rows are marked
``mapping-only`` and this module reports them; it never becomes a second
approval, packaging, or publishing authority. `AUTHORITY` is the machine-readable
statement of that boundary, and `mapping_only_tokens()` is what a test asserts
against.

**Pipeline success is not eight passes.** ``record_gate_stage`` moves exactly one
token. There is deliberately no "the run finished, mark everything green" path,
because the two tokens that most tempt one - ``RUNTIME_SMOKE`` and
``CANONICAL_PROMOTION`` - are precisely the ones static success cannot establish.

**A first failure stops the downstream claim.** ``record_failure`` marks the
failing token ``FAIL`` and forces every later token to ``NOT_RUN`` with a reason,
so a ledger can never show a later stage passing on top of an earlier failure.

The ledger is a sibling of ``manifest.json`` under ``config.out()``. It is *not*
a manifest field: ``manifest.RULESET`` would have to change to carry one, and
that invalidates every seal already shipped. Binding is by reference instead -
the ledger records the manifest digest and the built artifact hash it describes,
and `is_stale` rejects a ledger whose referents have moved.

Threat model, stated so it is not mistaken for something stronger. The public
API refuses to let a later stage claim a pass over an earlier failure, and a
reset carries prior failures forward. None of that survives someone editing the
JSON, or calling the private writer directly. This file is integrity-checked
against accidental corruption and programme error, not signed against a local
editor who wants a green ledger - the same limitation this project already
declares for its other JSON artifacts. A ledger is evidence about a build, and
evidence is only as good as where you got it.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import tempfile

SCHEMA_VERSION = 1
LEDGER_NAME = 'stage-ledger.json'

#: Profile key that opts a title into the staged ledger. Absent or null means
#: legacy behaviour: nothing is written and no stage code runs at all.
ACTIVATION_KEY = 'qa_upgrade'

#: Ordered staged tokens. Order is the claim: a later token cannot be proven
#: before an earlier one.
TOKENS = (
    'SOURCE_QA',
    'STATIC_BINARY_QA',
    'RC_BUILD',
    'RC_READBACK_QA',
    'RUNTIME_SMOKE',
    'CANONICAL_PROMOTION',
    'PATCH_PACKAGE',
    'RELEASE',
)

PASS = 'PASS'
FAIL = 'FAIL'
NOT_RUN = 'NOT_RUN'
STATUSES = (PASS, FAIL, NOT_RUN)

#: Which code owns each token, and whether this module may originate its verdict.
#:
#: ``mapping_only`` is the important column. It marks a token whose proof already
#: exists in shipped code; the ledger observes and reports that proof. Adding a
#: second implementation for one of these would create a competing authority -
#: two places that can say "this is packaged" - which is exactly the failure mode
#: the staged model is supposed to prevent.
AUTHORITY = {
    'SOURCE_QA': {
        'owner': 'hanpatch.pipeline gates: glossary, audit, qagate',
        'mapping_only': True,
        'note': 'Existing implementation. Term coverage, tag/register/normalisation '
                'audit, and the independent judge panel already prove this token; '
                'the ledger reports their result and adds no second check.',
    },
    'STATIC_BINARY_QA': {
        'owner': 'hanpatch.pipeline gates: capacity, materialize, audit; '
                 'expected-write verification for opted-in titles',
        'mapping_only': False,
        'note': 'Partly existing. Structural and width gates already run; byte-'
                'ownership verification is genuinely new surface for opted-in titles.',
    },
    'RC_BUILD': {
        'owner': 'hanpatch.pipeline.build',
        'mapping_only': True,
        'note': 'Existing implementation. The ledger binds the produced artifact '
                'hash; it does not re-run or re-authorise the build.',
    },
    'RC_READBACK_QA': {
        'owner': 'hanpatch.pipeline.verify + Adapter.verify',
        'mapping_only': True,
        'note': 'Existing implementation. Readback of every sealed entry is already '
                'proven here. Duplicating it as a new gate would add no guarantee.',
    },
    'RUNTIME_SMOKE': {
        'owner': 'operator-submitted runtime evidence',
        'mapping_only': False,
        'note': 'No static gate can establish this token. It stays NOT_RUN unless '
                'evidence bound to this build hash is submitted.',
    },
    'CANONICAL_PROMOTION': {
        'owner': 'operator promotion decision',
        'mapping_only': False,
        'note': 'Never automatic. Static or runtime passes do not promote; a '
                'promotion is a separate recorded decision.',
    },
    'PATCH_PACKAGE': {
        'owner': 'hanpatch.release.create',
        'mapping_only': True,
        'note': 'Existing implementation. Bundle creation already checks the '
                'approval token and digest; the ledger only observes the outcome.',
    },
    'RELEASE': {
        'owner': 'hanpatch.channel.publish',
        'mapping_only': True,
        'note': 'Existing implementation. Publication writes immutable versioned '
                'bundles; recording it here is not evidence of runtime or '
                'canonical status.',
    },
}

#: Stage name -> the token a *failure* in that stage belongs to.
#:
#: Separate from GATE_TOKEN because the two answer different questions. A gate
#: contributes to a token only once every gate mapped to it has reported;
#: a failure belongs to its token immediately. Stages that can only fail - byte
#: ownership and voice - appear here and nowhere else, and leaving them out is
#: how a refused build keeps a PASS on the token that refused it.
FAILURE_TOKEN = {
    'glossary': 'SOURCE_QA',
    'audit': 'SOURCE_QA',
    'qagate': 'SOURCE_QA',
    'voice': 'SOURCE_QA',
    'capacity': 'STATIC_BINARY_QA',
    'materialize': 'STATIC_BINARY_QA',
    'manifest': 'STATIC_BINARY_QA',
    'expected_write': 'STATIC_BINARY_QA',
    'verify': 'RC_READBACK_QA',
}

#: Gate name -> staged token. A gate that is not listed contributes to no token.
GATE_TOKEN = {
    'glossary': 'SOURCE_QA',
    'audit': 'SOURCE_QA',
    'qagate': 'SOURCE_QA',
    'capacity': 'STATIC_BINARY_QA',
    'materialize': 'STATIC_BINARY_QA',
    'manifest': 'STATIC_BINARY_QA',
}


class LedgerError(RuntimeError):
    """Raised when the ledger is asked to record something incoherent."""


def failure_token(stage):
    """The token a failure in `stage` belongs to, or None if it maps to none."""
    return FAILURE_TOKEN.get(stage)


def mapping_only_tokens():
    """Tokens whose verdict belongs to already-shipped code.

    A test asserts against this so a future edit cannot quietly promote the
    ledger into a second approval, packaging, or publishing authority.
    """
    return tuple(t for t in TOKENS if AUTHORITY[t]['mapping_only'])


def _now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def activation(profile=None):
    """Return the validated activation object, or None for a legacy title.

    Strict by construction. A title is opted in only by an object carrying a
    schema version this build understands; a bare truthy value, a string, or a
    future version is refused rather than guessed at. Absent and null are the
    legacy path and are *not* errors - most titles will never set this key.
    """
    if profile is None:
        from hanpatch import config
        profile = config.profile()

    raw = profile.get(ACTIVATION_KEY) if hasattr(profile, 'get') else None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LedgerError(
            '%s must be an object or null; got %s. A bare flag cannot carry the '
            'schema version this contract is versioned by.'
            % (ACTIVATION_KEY, type(raw).__name__))

    version = raw.get('schema_version')
    if version != SCHEMA_VERSION:
        raise LedgerError(
            '%s.schema_version must be %d; got %r'
            % (ACTIVATION_KEY, SCHEMA_VERSION, version))
    return dict(raw)


def enabled(profile=None):
    """True when this title opted into the staged ledger."""
    return activation(profile) is not None


def path():
    """Ledger location: a sibling of manifest.json, never a field inside it."""
    from hanpatch import config
    return config.out(LEDGER_NAME)


def _blank_tokens(reason):
    return {
        token: {
            'status': NOT_RUN,
            'reason': reason,
            'evidence': None,
            'checked': None,
            'findings': None,
            'updated_at': None,
        }
        for token in TOKENS
    }


#: Fields a carried history row may hold. Anything else is dropped: a row that
#: survived the sieve was still free to carry arbitrary payload, so the sieve
#: checked that a row existed without checking what was in it.
_HISTORY_FIELDS = ('token', 'reason', 'at', 'resetAt', 'unreadable',
                   'derived')


def _clean_history_row(row):
    """Return a row reduced to known fields, or None if it is not a row.

    Derived rather than trusted. `unreadable` is recomputed at each reset from
    the document actually on disk, so a forged marker cannot make a clean run
    look suspect and a missing one cannot make an unreadable run look clean.
    """
    if not isinstance(row, dict):
        return None
    token = row.get('token')
    if token is not None and token not in TOKENS:
        return None
    if token is None:
        # Only this build's own marker row survives, identified by a tag it
        # writes rather than by prose anyone can imitate. Everything else with a
        # null token is dropped.
        if row.get('derived') == _MARKER_TAG and row.get('unreadable') is True:
            return {'token': None, 'unreadable': True,
                    'derived': _MARKER_TAG,
                    'reason': _truncate(row.get('reason')),
                    'at': row.get('at'), 'resetAt': row.get('resetAt')}
        return None
    clean = {k: row[k] for k in _HISTORY_FIELDS if k in row and k != 'unreadable'}
    clean['token'] = token
    if 'reason' in clean:
        clean['reason'] = _truncate(clean['reason'])
    return clean


#: Longest reason text carried forward. A history row is a note, not a payload
#: channel; without a cap a single row grew the ledger into the megabytes.
_MAX_REASON = 500

#: Tag identifying a marker row this build derived.
#:
#: This distinguishes a row this code wrote from one that merely resembles it,
#: which is enough to stop a marker being produced by imitation through the
#: public API. It is NOT unforgeable: anyone who can write the file can write
#: the tag. Making it so would need a signature and key management this project
#: does not have, and a tag that implied tamper-resistance without providing it
#: would be worse than one that says plainly what it is. The threat model in the
#: module docstring is the honest boundary - accidental corruption and programme
#: error, not a local editor.
_MARKER_TAG = 'stage_ledger.bootstrap'

#: Fields a token entry is known to carry. An entry holding something outside
#: this set, and no `status`, may be storing the status under another name.
_ENTRY_FIELDS = frozenset(('status', 'evidence', 'checked', 'findings',
                           'reason', 'updated_at'))


#: Codepoints that occupy space while rendering nothing. Not an exhaustive list
#: of every such character - it is the set review actually found being used to
#: push a reason past its cap.
_BLANK_GLYPHS = frozenset('\u3164\u2800\u115f\u1160\uffa0\u200b\u200c'
                          '\u200d\u2060\ufeff')


def _is_blank_glyph(ch):
    return ch in _BLANK_GLYPHS


def _truncate(text):
    """Cap a carried reason, measured on its meaningful text.

    Truncating the raw string let 520 leading spaces push the real cause past
    the cut, so the row kept its length limit and lost its content.
    """
    if not isinstance(text, str):
        return text
    # Measure legible characters rather than blacklisting invisible ones. Each
    # round of review found another padding codepoint - zero-width space, then
    # Hangul filler, then braille blank - because a blacklist is a guess about
    # what someone will use next. A character that renders as nothing does not
    # count toward the cap regardless of which block it lives in.
    import unicodedata
    keep = []
    for ch in text:
        category = unicodedata.category(ch)
        if category in ('Cf', 'Cc') and ch not in '\n\t':
            continue
        if category in ('Zs', 'Zl', 'Zp'):
            keep.append(' ')
            continue
        if not unicodedata.combining(ch) and _is_blank_glyph(ch):
            continue
        keep.append(ch)
    text = ' '.join(''.join(keep).split())
    return text if len(text) <= _MAX_REASON else text[:_MAX_REASON] + '...[cut]'


def bootstrap(manifest_digest=None, ruleset=None, force=False):
    """Create (or reset) the ledger with every token at NOT_RUN.

    Starting from all-NOT_RUN is the honest default: at bootstrap nothing has
    been proven yet, and a token that reads NOT_RUN is a true statement about
    this build rather than a placeholder to be filled in later.

    A reset carries the previous run's failures forward under `priorFailures`.
    The reset itself has to stay possible - every run begins with one, and a
    ledger that could not be reset would make the first failure permanent - but
    that is no reason to lose the record. Without this, resetting was a way to
    make a failed run look like one that had never happened.
    """
    target = path()
    if os.path.exists(target) and not force:
        return load()

    prior = []
    unreadable_now = False
    if os.path.exists(target):
        # Read the raw document rather than going through `load`, which refuses a
        # foreign schema version. A ledger this version cannot interpret is
        # still a ledger that may record a failure, and dropping that history
        # because the schema moved would lose exactly what it is for.
        try:
            from hanpatch import config as _config
            previous = _config.load_object(target, 'the stage ledger')
        except (LedgerError, SystemExit):
            previous = None
        if previous is not None:
            # Sieve the carried history. Reading a document this version cannot
            # interpret means its fields cannot be trusted either: a string
            # `priorFailures` iterates into single characters and a dict into
            # its keys, both of which flow onward looking like history until a
            # reader does entry['token'] and gets a TypeError. Keep only rows
            # that are actually rows.
            carried = previous.get('priorFailures')
            prior = [_clean_history_row(row) for row in carried] \
                if isinstance(carried, list) else []
            prior = [row for row in prior if row is not None]

            # A ledger written under a schema this build does not know may name
            # its status field something else, so a FAIL can sit there unread.
            # Say that rather than presenting an empty history as a clean one.
            # The marker is derived here, never carried from the file, so it can
            # be neither forged onto a clean run nor dropped from a dirty one.
            # Stickiness lives in its own boolean, so it cannot be produced by
            # writing prose that resembles the derived row. A document missing a
            # token key is still interpretable for the tokens it does have, so
            # only a schema this build does not know, or a `tokens` field that
            # is not a mapping, makes it unreadable.
            # Never read the flag from the document it describes - that is the
            # same trust the wording match had, wearing a boolean. Stickiness is
            # recovered from a marker row this build wrote, which the sieve only
            # keeps when it also wrote the flag alongside it.
            seen_unreadable = any(
                isinstance(r, dict) and r.get('unreadable') is True
                and r.get('token') is None and r.get('derived') == _MARKER_TAG
                for r in (previous.get('priorFailures') or [])
                if isinstance(previous.get('priorFailures'), list))
            # A document can carry this build's schema number and still not be
            # shaped like it - a renamed status field leaves a FAIL sitting
            # unread, and an empty history would present that as clean. Judge
            # the structure, not the label.
            tokens_field = previous.get('tokens')
            readable = (previous.get('schemaVersion') == SCHEMA_VERSION
                        and isinstance(tokens_field, dict)
                        and any(isinstance(tokens_field.get(t), dict)
                                and 'status' in tokens_field[t]
                                for t in TOKENS)
                        # An entry that carries results but no `status` is a
                        # result this build cannot read - whether the status was
                        # renamed or simply dropped, a FAIL there goes unread. An
                        # entry holding nothing but a note has no result to lose,
                        # and calling that unreadable would cry wolf.
                        and not any(
                            isinstance(tokens_field.get(t), dict)
                            and 'status' not in tokens_field[t]
                            and set(tokens_field[t]) - {'reason'}
                            for t in TOKENS)
                        # A status this build does not recognise is a verdict it
                        # cannot read. "failed" is not FAIL, and treating an
                        # unknown word as absent would drop a real failure.
                        and not any(
                            isinstance(tokens_field.get(t), dict)
                            and tokens_field[t].get('status') not in STATUSES
                            for t in TOKENS
                            if isinstance(tokens_field.get(t), dict)
                            and 'status' in tokens_field[t]))
            unreadable_now = seen_unreadable or not readable
            if unreadable_now and not any(r.get('unreadable') for r in prior):
                prior.append({
                    'token': None,
                    'reason': 'a prior ledger could not be read by this build '
                              '(schemaVersion %r); its results are unknown '
                              'rather than absent'
                              % (previous.get('schemaVersion'),),
                    'at': previous.get('updatedAt'),
                    'resetAt': _now(),
                    'unreadable': True,
                    'derived': _MARKER_TAG,
                })

            tokens = previous.get('tokens')
            tokens = tokens if isinstance(tokens, dict) else {}
            for name in TOKENS:
                entry = tokens.get(name)
                if not isinstance(entry, dict):
                    continue
                if entry.get('status') == FAIL:
                    prior.append({
                        'token': name,
                        'reason': _truncate(entry.get('reason')),
                        'at': entry.get('updated_at'),
                        'resetAt': _now(),
                    })

    doc = {
        'priorFailures': prior,
        'sawUnreadableLedger': unreadable_now,
        'schemaVersion': SCHEMA_VERSION,
        'createdAt': _now(),
        'updatedAt': _now(),
        'manifestDigest': manifest_digest,
        'manifestRuleset': ruleset,
        'buildSha256': None,
        'evidenceSha256': None,
        'contractSha256': None,
        'tokenOrder': list(TOKENS),
        'authority': {t: dict(AUTHORITY[t]) for t in TOKENS},
        'tokens': _blank_tokens('not reached in this run'),
    }
    _write(doc)
    return doc


def load():
    """Read the ledger through the validating loader.

    `config.load_object` is the one sanctioned reader for a state document: a
    bare ``json.load`` accepts a list or a scalar, after which the first
    ``.items()`` fails deep inside a caller with a bare AttributeError instead of
    naming the file. A gate in the suite enforces this for every module here.
    """
    from hanpatch import config
    target = path()
    if not os.path.exists(target):
        raise LedgerError('no stage ledger at %s; bootstrap first' % target)
    doc = config.load_object(target, 'the stage ledger')
    if doc.get('schemaVersion') != SCHEMA_VERSION:
        raise LedgerError('stage ledger schema %r is not %d'
                          % (doc.get('schemaVersion'), SCHEMA_VERSION))
    return doc


def _write(doc):
    """Atomic replace: a crash mid-write must not leave a half-truth on disk.

    A torn ledger is worse than none, because it still parses as a claim.
    """
    doc['updatedAt'] = _now()
    target = path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix='.stage-ledger-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return doc


def _require_token(token):
    if token not in TOKENS:
        raise LedgerError('unknown stage token %r' % token)


def record(token, status, evidence=None, checked=None, findings=None,
           reason=None, doc=None):
    """Record one token's verdict. Exactly one - never a sweep."""
    _require_token(token)
    if status not in STATUSES:
        raise LedgerError('status must be one of %s; got %r' % (STATUSES, status))

    doc = doc or load()

    # The invariant this module claims - a later token never passes on top of an
    # earlier failure - has to be enforced here, not only in record_failure's
    # cascade. `hanpatch build` and `hanpatch verify` are separate commands, so a
    # build refused at byte ownership can be followed by a fresh `verify` process
    # that knows nothing about it. Without this check that second process would
    # record RC_READBACK_QA=PASS over a FAIL, and the ledger would state exactly
    # what the documentation promises cannot happen.
    # The guard reads what is on disk, never the caller's copy. `doc=` exists so
    # a batch of updates can be written once, but it also lets a caller present
    # a view of the ledger taken before a failure was recorded - and a guard that
    # trusts that view checks the caller's memory instead of the record. Every
    # route around this invariant found in review went through a stale `doc=`.
    persisted = load() if os.path.exists(path()) else doc
    if persisted is not None and token in persisted.get('tokens', {}):
        current = persisted['tokens'][token]['status']
        earlier = [t for t in TOKENS[:TOKENS.index(token) + 1]
                   if persisted['tokens'][t]['status'] == FAIL]
    else:
        current, earlier = None, []

    # A failure may not be laundered. Guarding only PASS left a two-step route
    # around the invariant: set the failed token to NOT_RUN, which looked like a
    # harmless "we have not checked this", then pass everything downstream. A
    # token leaves FAIL by fixing the cause and re-running, which opens a fresh
    # ledger through bootstrap - not by being relabelled in place.
    if current == FAIL and status != FAIL:
        raise LedgerError(
            '%s already failed and cannot be relabelled %s. Re-run from the '
            'failing stage; a fresh run starts a fresh ledger.'
            % (token, status))

    if status == PASS:
        # Earlier tokens AND this one. Checking only earlier tokens left the
        # obvious hole: the stage that just failed could report PASS over its
        # own FAIL, and `record_gate_stage` reached it without going near the
        # guard. A genuine re-run does not need this - `pipeline.gates()` opens
        # a fresh ledger with `bootstrap(force=True)` - so an overwrite here is
        # always a later claim built on top of a failure.
        blocked = earlier
        if blocked:
            raise LedgerError(
                '%s cannot pass: %s already failed. Re-run from the failing '
                'stage rather than recording over it.'
                % (token, ', '.join(blocked)))

    # Write onto the persisted document, not the caller's. A wholesale write of
    # a stale `doc=` erased the very failure the guard had just read off disk,
    # which is how every stale-doc route stayed open after the guard was added.
    # The caller's gate reports are carried over because that is the one field
    # `record_gate_stage` legitimately batches; token statuses are not the
    # caller's to supply.
    if persisted is not None and persisted is not doc:
        if doc is not None and 'gateReports' in doc:
            persisted['gateReports'] = doc['gateReports']
        doc = persisted

    entry = doc['tokens'][token]
    entry['status'] = status
    entry['evidence'] = evidence
    entry['checked'] = checked
    entry['findings'] = findings
    entry['reason'] = reason
    entry['updated_at'] = _now()
    return _write(doc)


def record_gate_stage(gate_name, checked=None, evidence=None, doc=None):
    """Fold one passing gate into its token.

    A token flips to PASS only when every gate mapped to it has reported. A gate
    that maps to no token is ignored rather than silently promoting something.
    """
    token = GATE_TOKEN.get(gate_name)
    if token is None:
        return doc or load()

    # Token statuses come from disk even when the caller supplies a document.
    # Accepting a caller's copy wholesale let a pre-failure snapshot be written
    # back, erasing the failure it did not know about; only the gate reports are
    # the caller's to carry.
    persisted = load()
    if doc is not None and doc is not persisted:
        persisted['gateReports'] = doc.get('gateReports',
                                           persisted.get('gateReports', {}))
    doc = persisted
    seen = doc.setdefault('gateReports', {})
    seen[gate_name] = {'checked': checked, 'evidence': evidence, 'at': _now()}

    required = [g for g, t in GATE_TOKEN.items() if t == token]
    if all(g in seen for g in required):
        return record(token, PASS,
                      evidence='gates: %s' % ', '.join(sorted(required)),
                      checked=checked, doc=doc)
    return _write(doc)


def record_failure(token, reason, evidence=None, doc=None):
    """Mark a token failed and refuse every downstream claim.

    Without this, a ledger could show RC_BUILD passing after SOURCE_QA failed -
    a sequence that cannot have happened, recorded as if it had.
    """
    _require_token(token)
    doc = doc or load()
    record(token, FAIL, evidence=evidence, reason=reason, doc=doc)

    doc = load()
    idx = TOKENS.index(token)
    for later in TOKENS[idx + 1:]:
        entry = doc['tokens'][later]
        entry['status'] = NOT_RUN
        entry['reason'] = 'not reached: %s failed' % token
        entry['updated_at'] = _now()
    return _write(doc)


def bind_build(build_path, doc=None):
    """Bind the ledger to the artifact it describes.

    Like `record` and `record_gate_stage`, this writes onto the persisted
    document rather than the caller's. Guarding the other two doors and leaving
    this one open was enough to erase a failure: a pre-failure `doc=` written
    back here reset the token and dropped the history with it.
    """
    doc = load()
    doc['buildSha256'] = sha256_file(build_path) if build_path else None
    doc['buildPath'] = build_path
    return _write(doc)


def sha256_file(target):
    h = hashlib.sha256()
    with open(target, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def is_stale(doc=None, manifest_digest=None, build_path=None):
    """Reasons this ledger no longer describes the current artifacts.

    Returns a list; empty means current. Staleness is reported rather than
    repaired, because silently re-binding a ledger to whatever is on disk now
    would erase the very mismatch worth knowing about.
    """
    doc = doc or load()
    reasons = []
    if manifest_digest is not None and doc.get('manifestDigest') != manifest_digest:
        reasons.append('manifest digest moved: ledger %r, current %r'
                       % (doc.get('manifestDigest'), manifest_digest))
    if build_path is not None:
        current = sha256_file(build_path) if os.path.exists(build_path) else None
        if doc.get('buildSha256') != current:
            reasons.append('build hash moved: ledger %r, current %r'
                           % (doc.get('buildSha256'), current))
    return reasons


def summary(doc=None):
    """Per-token status, for a report that states scope instead of a total."""
    doc = doc or load()
    return {t: doc['tokens'][t]['status'] for t in TOKENS}

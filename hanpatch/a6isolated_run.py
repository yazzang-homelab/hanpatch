"""Explicit, isolated DQ7 pilot runner for the fixed A6 translation lane.

This runner deliberately owns neither the ordinary provider pool nor the project's
``work/ko`` state.  Its only mutable state is a caller-selected output namespace.
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import ssl
import stat
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import uuid


from hanpatch import a6isolated as a6
from hanpatch import config
from hanpatch import glossary
from hanpatch import tm
from hanpatch import translate
from hanpatch import wrap

_STATE_VERSION = 2
_STATE_KEYS = frozenset({'version', 'family', 'contract_fingerprint', 'tm', 'provenance',
                         'anchor_versions', 'review'})
_FAMILY_RE = re.compile(r'[A-Za-z0-9_.-]+\Z')
_FINGERPRINT_RE = re.compile(r'[0-9a-f]{64}\Z')
_MAX_INPUT_SNAPSHOT_BYTES = 4 * 1024 * 1024


class A6PilotError(RuntimeError):
    """Base class for failures in the explicitly isolated pilot lane."""


class InvalidOutputNamespace(A6PilotError):
    """The pilot namespace overlaps the configured project or is unusable."""


class EmptyPilotSource(A6PilotError):
    """The requested source family has no translatable source rows."""


class PilotStateError(A6PilotError):
    """Pilot-local resume state is malformed."""


class ValidationEnvironmentError(A6PilotError):
    """The staged project lacks source-side data needed for validation."""


_ORIGINAL_CHECK = translate.check
_VALIDATION_LOCK = threading.Lock()
_PROVIDER_ID = f'{a6.A6TranslationClient.name}:{a6.FIXED_MODEL}'
_GROUP_DIGITS = re.compile(r'\d+')
_GROUP_SUFFIX = re.compile(r'_#+$')


def _parser():
    parser = argparse.ArgumentParser(
        prog='hanpatch a6-translate',
        description='Run the fixed-model A6 pilot in an isolated output namespace.')
    parser.add_argument('--family', required=True)
    parser.add_argument('--url', required=True,
                        help='explicit HTTPS endpoint for the isolated A6 service')
    parser.add_argument('--output', required=True,
                        help='pilot output namespace, outside the project tree')
    parser.add_argument('--ca-file',
                        help='PEM CA bundle; system trust is used when omitted')
    parser.add_argument('--client-cert', required=True,
                        help='PEM client certificate for the A6 TLS connection')
    parser.add_argument('--client-key', required=True,
                        help='PEM private key for the A6 TLS connection')
    parser.add_argument('--calls', type=int, default=8,
                        help='maximum A6 attempts for this pilot run')
    parser.add_argument('--request-bytes', type=int, default=262144,
                        help='maximum aggregate request bytes for this pilot run')
    parser.add_argument('--response-bytes', type=int, default=262144,
                        help='maximum aggregate reserved response bytes for this pilot run')
    parser.add_argument('--workers', type=int, default=1,
                        help='parallel batch workers (1 through 4; measured default: 1)')
    parser.add_argument('--batch-size', type=int, default=16,
                        help='maximum source items per A6 batch (measured default: 16)')
    parser.add_argument('--batch-chars', type=int, default=2600,
                        help='maximum UTF-8 source bytes per A6 batch')
    parser.add_argument('--limit', type=int, default=0,
                        help='translate at most N unresolved source strings')
    parser.add_argument('--input-snapshot',
                        help='explicit JSON TM snapshot outside the project tree')
    return parser


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise A6PilotError(f'{name} must be a positive integer')
    return value


def _inside(path, parent):
    try:
        return os.path.commonpath((path, parent)) == parent
    except ValueError:
        return False


def _require_outside_project(path, label, project):
    if not isinstance(path, str) or not path:
        raise InvalidOutputNamespace(f'{label} must be an explicit path')
    candidate = os.path.abspath(path)
    resolved = os.path.realpath(candidate)
    if _inside(resolved, project):
        raise InvalidOutputNamespace(f'{label} must be outside the configured project tree')
    return candidate


def _validate_options(args):
    parsed = urllib.parse.urlsplit(args.url) if isinstance(args.url, str) else None
    if (parsed is None or parsed.scheme != 'https' or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment):
        raise A6PilotError('A6 URL must be an explicit HTTPS URL')
    if not isinstance(args.family, str) or not _FAMILY_RE.fullmatch(args.family):
        raise A6PilotError('family must be a simple pilot namespace name')
    _positive(args.calls, 'calls')
    _positive(args.request_bytes, 'request-bytes')
    _positive(args.response_bytes, 'response-bytes')
    _positive(args.workers, 'workers')
    _positive(args.batch_size, 'batch-size')
    _positive(args.batch_chars, 'batch-chars')
    if args.workers > 4:
        raise A6PilotError('workers may not exceed the A6 pilot hard cap of 4')
    if args.batch_size > a6.MAX_ITEMS:
        raise A6PilotError(f'batch-size may not exceed {a6.MAX_ITEMS}')
    if args.batch_chars > a6.MAX_SOURCE_BYTES:
        raise A6PilotError(f'batch-chars may not exceed {a6.MAX_SOURCE_BYTES}')
    if isinstance(args.limit, bool) or not isinstance(args.limit, int) or args.limit < 0:
        raise A6PilotError('limit must be zero or a positive integer')


def _load_object(path, what):
    try:
        return config.load_object(path, what)
    except SystemExit as exc:
        raise PilotStateError(str(exc)) from exc
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise PilotStateError(f'{what} cannot be loaded') from exc


def _pinned_path(fd, mode_check, what, error_type):
    """Resolve a procfs fd reference only after confirming its inode identity."""
    try:
        held = os.fstat(fd)
        resolved = os.path.realpath(f'/proc/self/fd/{fd}')
        current = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        raise error_type(f'{what} cannot be inspected while pinned') from exc
    if (not mode_check(held.st_mode) or not mode_check(current.st_mode)
            or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
        raise error_type(f'{what} changed while pinned')
    return resolved


def _require_locked_outside_project(lock, project):
    pinned = _pinned_path(lock.directory_fd, stat.S_ISDIR,
                          'pilot output namespace', InvalidOutputNamespace)
    if _inside(pinned, project):
        raise InvalidOutputNamespace(
            'pilot output namespace must be outside the configured project tree')


def _parse_input_snapshot(value):
    try:
        snapshot = json.loads(value.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PilotStateError('input snapshot cannot be loaded') from exc
    if not isinstance(snapshot, dict) or not all(
            isinstance(source, str) and isinstance(target, str)
            for source, target in snapshot.items()):
        raise PilotStateError('input snapshot must map source strings to target strings')
    return snapshot


def _read_input_snapshot(path, project):
    candidate = _require_outside_project(path, 'input snapshot', project)
    try:
        fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                     os.O_NONBLOCK)
    except OSError as exc:
        raise PilotStateError('input snapshot cannot be opened safely') from exc
    try:
        try:
            details = os.fstat(fd)
        except OSError as exc:
            raise PilotStateError('input snapshot cannot be inspected') from exc
        if not stat.S_ISREG(details.st_mode):
            raise PilotStateError('input snapshot must be an existing JSON file')
        if details.st_size > _MAX_INPUT_SNAPSHOT_BYTES:
            raise PilotStateError('input snapshot exceeds the size limit')
        pinned = _pinned_path(fd, stat.S_ISREG, 'input snapshot', PilotStateError)
        if _inside(pinned, project):
            raise PilotStateError('input snapshot must be outside the configured project tree')
        try:
            with os.fdopen(fd, 'rb') as handle:
                fd = None
                value = handle.read(_MAX_INPUT_SNAPSHOT_BYTES + 1)
        except OSError as exc:
            raise PilotStateError('input snapshot cannot be read') from exc
        if len(value) > _MAX_INPUT_SNAPSHOT_BYTES:
            raise PilotStateError('input snapshot exceeds the size limit')
    finally:
        if fd is not None:
            os.close(fd)
    return _parse_input_snapshot(value)


def _load_namespace_object(lock, name, what):
    try:
        value = json.loads(lock.read_bytes(name).decode('utf-8'))
    except (a6.A6RequestError, UnicodeDecodeError, ValueError) as exc:
        raise PilotStateError(f'{what} cannot be loaded') from exc
    if not isinstance(value, dict):
        raise PilotStateError(f'{what} must be a JSON object')
    return value


def _load_optional_namespace_object(lock, name, what):
    return _load_namespace_object(lock, name, what) if lock.exists(name) else {}




def _atomic_replace_json(lock, name, value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, indent=1,
                             sort_keys=True, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise PilotStateError('pilot JSON record cannot be encoded') from exc
    lock.replace_bytes(name, encoded)


def _replace_json_locked(lock, name, value, what='pilot state'):
    """Atomically replace one pilot-local JSON record through the held directory FD."""
    if not isinstance(value, dict):
        raise PilotStateError(f'{what} must be a JSON object')
    _atomic_replace_json(lock, name, value)
    return value


def _pilot_names(family):
    return {
        'state': f'state_{family}.json',
        'tm': f'tm_{family}.json',
        'provenance': f'prov_{family}.json',
        'anchor_versions': f'anchorver_{family}.json',
        'review': f'review_{family}.json',
    }


def _pilot_paths(namespace, family):
    """Inspectable path names only; pilot I/O is always relative to a held directory FD."""
    return {name: os.path.join(namespace, filename)
            for name, filename in _pilot_names(family).items()}


def _empty_state(family, contract_fingerprint):
    return {
        'version': _STATE_VERSION,
        'family': family,
        'contract_fingerprint': contract_fingerprint,
        'tm': {},
        'provenance': {},
        'anchor_versions': {},
        'review': {},
    }


def _canonical_digest(value, what):
    try:
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise A6PilotError(f'{what} cannot be fingerprinted') from exc
    return hashlib.sha256(blob).hexdigest()


def _source_font_contract(project):
    """Hash declared staged source fonts without reading any target output font."""
    fonts = []
    active_output = os.path.realpath(os.path.join(project, 'work', 'ko'))
    for declared in config.prof('font_src') or ():
        if not isinstance(declared, str) or not declared:
            raise ValidationEnvironmentError('profile source font declaration is invalid')
        path = os.path.realpath(config.p(declared))
        if _inside(path, active_output):
            raise ValidationEnvironmentError('profile source font may not point into work/ko')
        record = {'declared': declared}
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            record['missing'] = True
            fonts.append(record)
            continue
        except OSError as exc:
            raise ValidationEnvironmentError('a staged source font cannot be read') from exc
        try:
            details = os.fstat(fd)
            if not stat.S_ISREG(details.st_mode):
                raise ValidationEnvironmentError('a staged source font is not a regular file')
            digest = hashlib.sha256()
            with os.fdopen(fd, 'rb') as handle:
                fd = None
                for block in iter(lambda: handle.read(65536), b''):
                    digest.update(block)
            record.update({
                'device': details.st_dev,
                'inode': details.st_ino,
                'size': details.st_size,
                'sha256': digest.hexdigest(),
            })
        finally:
            if fd is not None:
                os.close(fd)
        fonts.append(record)
    return fonts


def _contract_fingerprint(project, family, source):
    """Digest the source and every configured fact that can validate an accepted row."""
    contract = {
        'family': family,
        'source_sha256': _canonical_digest(source, 'configured extracted source'),
        'adapter': config.cfg().get('adapter'),
        'target': config.target(),
        'profile': config.profile(),
        'source_fonts': _source_font_contract(project),
    }
    return _canonical_digest(contract, 'pilot validation contract')


def _validate_string_map(value, what, *, nonempty_values=True):
    if not isinstance(value, dict):
        raise PilotStateError(f'{what} must be a JSON object')
    for source, target in value.items():
        if (not isinstance(source, str) or not source.strip() or not isinstance(target, str)
                or (nonempty_values and not target.strip())):
            raise PilotStateError(f'{what} must map non-empty source strings to strings')


def _validate_review(value):
    if not isinstance(value, dict):
        raise PilotStateError('pilot review must be a JSON object')
    for source, record in value.items():
        if not isinstance(source, str) or not source.strip():
            raise PilotStateError('pilot review keys must be non-empty source strings')
        if not isinstance(record, dict) or set(record) != {'refs', 'reason'}:
            raise PilotStateError('pilot review records must contain only refs and reason')
        refs, reason = record['refs'], record['reason']
        if (not isinstance(refs, list) or not refs
                or not all(isinstance(ref, str) and ref.strip() for ref in refs)
                or not isinstance(reason, str) or not reason.strip()):
            raise PilotStateError('pilot review records are malformed')


def _validate_state(state, family, contract_fingerprint):
    if not isinstance(state, dict) or set(state) != _STATE_KEYS:
        raise PilotStateError('pilot state has an unsupported schema')
    if (type(state['version']) is not int or state['version'] != _STATE_VERSION
            or state['family'] != family):
        raise PilotStateError('pilot state belongs to a different protocol or family')
    if (not isinstance(state['contract_fingerprint'], str)
            or not _FINGERPRINT_RE.fullmatch(state['contract_fingerprint'])):
        raise PilotStateError('pilot state has an invalid validation contract fingerprint')
    if state['contract_fingerprint'] != contract_fingerprint:
        raise PilotStateError('pilot state validation contract does not match this project')
    _validate_string_map(state['tm'], 'pilot translation memory')
    _validate_string_map(state['provenance'], 'pilot provenance')
    _validate_string_map(state['anchor_versions'], 'pilot anchor versions')
    _validate_review(state['review'])
    accepted = set(state['tm'])
    if accepted != set(state['provenance']) or accepted != set(state['anchor_versions']):
        raise PilotStateError('pilot accepted-state shards have inconsistent keys')
    if accepted & set(state['review']):
        raise PilotStateError('pilot accepted and review state must be disjoint')
    return state


def _load_pilot_state(lock, family, contract_fingerprint):
    """Load authoritative state or migrate review-only legacy state."""
    names = _pilot_names(family)
    if lock.exists(names['state']):
        state = _load_namespace_object(lock, names['state'], 'pilot state')
        return _validate_state(state, family, contract_fingerprint), True
    present = {name: lock.exists(filename)
               for name, filename in names.items() if name != 'state'}
    if not any(present.values()):
        return _empty_state(family, contract_fingerprint), False
    accepted_names = ('tm', 'provenance', 'anchor_versions')
    if any(present[name] for name in accepted_names):
        raise PilotStateError('legacy pilot accepted state requires authoritative validation')
    state = _empty_state(family, contract_fingerprint)
    if present['review']:
        state['review'] = _load_optional_namespace_object(lock, names['review'],
                                                           'pilot review shard')
    _validate_state(state, family, contract_fingerprint)
    _replace_json_locked(lock, names['state'], state, 'pilot state')
    return state, True


def _reconcile_pilot_shards(lock, state):
    """Rebuild every disposable pilot shard from the authoritative state record."""
    _validate_state(state, state['family'], state['contract_fingerprint'])
    names = _pilot_names(state['family'])
    _replace_json_locked(lock, names['tm'], state['tm'], 'pilot translation memory')
    _replace_json_locked(lock, names['provenance'], state['provenance'],
                         'pilot provenance shard')
    _replace_json_locked(lock, names['anchor_versions'], state['anchor_versions'],
                         'pilot anchor version shard')
    _replace_json_locked(lock, names['review'], state['review'], 'pilot review shard')


def _validate_anchor_versions(state, current):
    if any(version != current for version in state['anchor_versions'].values()):
        raise PilotStateError('pilot accepted rows were validated under a different hard-term contract')

def _group(family, key):
    shape = _GROUP_SUFFIX.sub('', _GROUP_DIGITS.sub('#', key))
    return f'{family}/{shape}'


def _source_rows(source, family, pilot_tm):
    entries = source.get(family)
    if not isinstance(entries, list):
        raise EmptyPilotSource('requested family is missing from the configured source')
    seen = {}
    order = []
    for item in entries:
        if not isinstance(item, dict):
            raise A6PilotError('source rows must be objects')
        text, key = item.get('en'), item.get('key')
        if not isinstance(text, str) or not isinstance(key, str):
            raise A6PilotError('source rows must contain string en and key fields')
        if tm.is_skip(text, key) or not text.strip():
            continue
        if text not in seen:
            seen[text] = {
                'en': text,
                'jp': item.get('jp', '') if isinstance(item.get('jp', ''), str) else '',
                'refs': [],
                'group': _group(family, key),
            }
            order.append(text)
        seen[text]['refs'].append(f'{family}:{key}')
    rows = [seen[text] for text in order]
    if not rows:
        raise EmptyPilotSource('requested family has no translatable source rows')
    todo = [row for row in rows if tm.lookup(pilot_tm, row['en']) is None]
    return rows, todo


def _pilot_context(rows, pilot_tm):
    context = []
    for row in rows:
        target = tm.lookup(pilot_tm, row['en'])
        if target:
            context.append({'source': row['en'], 'target': target})
        if len(context) == a6.MAX_CONTEXT_ITEMS:
            break
    return context


def _profile_glossary(source, pilot_tm):
    terms = dict(config.prof('terms') or {})
    hard_families = set(config.prof('hard_families') or ())
    for family, pattern in config.prof('name_keys') or ():
        if family not in hard_families or family not in source:
            continue
        try:
            matcher = re.compile(pattern)
        except (TypeError, re.error) as exc:
            raise A6PilotError('profile name-key pattern is invalid') from exc
        for item in source[family]:
            if not isinstance(item, dict):
                continue
            text, key = item.get('en'), item.get('key')
            if (not isinstance(text, str) or not isinstance(key, str)
                    or not matcher.fullmatch(key) or tm.is_skip(text, key)
                    or not text.strip() or len(text) > 48):
                continue
            target = tm.lookup(pilot_tm, text)
            if target:
                terms[text] = target
    return terms


def _hard_glossary(glossary_terms):
    declared = set(config.prof('hard_terms') or ())
    hard = {}
    for source, target in glossary_terms.items():
        if config.source_lang() == 'ja':
            required = source in declared
        else:
            required = (source in declared or source[:1].isupper()
                        or source.startswith(('dark', 'gnome', 'wyrm', 'paling')))
        if required:
            hard[source] = target
    return hard


def _relevant_glossary(glossary_terms, rows, family):
    relevant = glossary.relevant(glossary_terms, [row['en'] for row in rows], family)
    if len(relevant) > a6.MAX_GLOSSARY_ITEMS:
        raise A6PilotError('the batch has more relevant glossary entries than A6 accepts')
    return relevant


def _anchor_version(hard_terms):
    blob = json.dumps(hard_terms, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()[:16]


def _source_font_state(project):
    """Load only declared source fonts, never the project's target output fonts."""
    source_paths = []
    active_output = os.path.realpath(os.path.join(project, 'work', 'ko'))
    for relative in config.prof('font_src') or ():
        path = os.path.realpath(config.p(relative))
        if _inside(path, active_output):
            raise ValidationEnvironmentError('profile source font may not point into work/ko')
        if os.path.exists(path):
            source_paths.append(path)
    if not source_paths:
        raise ValidationEnvironmentError('no staged source fonts are available for validation')
    from hanpatch.platforms.threeds.bcfnt import Bcfnt

    fonts = []
    coverage = []
    for path in source_paths:
        try:
            with open(path, 'rb') as handle:
                font = Bcfnt(handle.read())
        except Exception as exc:
            raise ValidationEnvironmentError('a staged source font cannot be read') from exc
        fonts.append(font)
        characters = set()
        for start, end, mapping_type, payload in font.cmap:
            if mapping_type == 0:
                characters.update(chr(codepoint) for codepoint in range(start, end + 1))
            elif mapping_type == 1:
                characters.update(chr(start + index) for index, value in enumerate(payload)
                                  if value != 0xFFFF)
            else:
                characters.update(chr(codepoint) for codepoint, _ in payload)
        coverage.append(characters)
    allowed = set.intersection(*coverage)
    allowed.add('\n')
    return fonts[0], allowed


def _derived_capacity(source):
    """Derive capacity from configured source text without touching work/ko state."""
    capacities = {}
    for family, entries in source.items():
        if not isinstance(entries, list):
            continue
        budget = wrap.budget_for(family)
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get('en'), str):
                continue
            text = item['en']
            if not text.strip() or wrap.engine_lays_out(text):
                continue
            key = item.get('key')
            if not isinstance(key, str):
                continue
            group = _group(family, key)
            pages = wrap.pages(wrap.rewrap(text, budget))
            line_count = max(pages) if pages else 0
            capacities[group] = max(capacities.get(group, 0), line_count)
    return capacities


class _PilotValidator:
    def __init__(self, project, source, hard_terms):
        self._hard_terms = dict(hard_terms)
        self._font = None
        self._coverage = None
        self._capacities = None
        if translate.check is _ORIGINAL_CHECK:
            self._font, self._coverage = _source_font_state(project)
            with self._scope():
                self._capacities = _derived_capacity(source)

    @contextlib.contextmanager
    def _scope(self):
        old_hard = glossary.hard
        old_font = wrap._font
        old_capacity = wrap.capacity
        old_in_font = translate._in_font
        glossary.hard = lambda src_path=None: dict(self._hard_terms)
        wrap._font = self._font
        if self._capacities is not None:
            def capacity(group, family):
                if group in self._capacities:
                    return self._capacities[group]
                family_values = [value for name, value in self._capacities.items()
                                 if name.split('/', 1)[0] == family]
                if family_values:
                    return max(family_values)
                if family in wrap.CAPACITY:
                    return wrap.CAPACITY[family]
                raise ValidationEnvironmentError('no staged capacity is available')
            wrap.capacity = capacity
        if self._coverage is not None:
            translate._in_font = lambda character: character in self._coverage
        try:
            yield
        finally:
            glossary.hard = old_hard
            wrap._font = old_font
            wrap.capacity = old_capacity
            translate._in_font = old_in_font

    def check(self, source, target, terms, family, group):
        with _VALIDATION_LOCK:
            if translate.check is _ORIGINAL_CHECK:
                with self._scope():
                    return translate.check(source, target, terms, family, group)
            return translate.check(source, target, terms, family, group)


def _make_batches(rows, batch_size, batch_bytes):
    batches = []
    rejected = {}
    current = []
    used = 0
    for row in rows:
        size = len(row['en'].encode('utf-8'))
        if size > a6.MAX_SOURCE_BYTES:
            rejected[row['en']] = (row, 'source_oversized')
            continue
        if size > batch_bytes:
            rejected[row['en']] = (row, 'batch_limit_exceeded')
            continue
        if current and (len(current) >= batch_size or used + size > batch_bytes):
            batches.append(current)
            current, used = [], 0
        current.append(row)
        used += size
    if current:
        batches.append(current)
    return batches, rejected


def _reason_for_exception(exc):
    if isinstance(exc, a6.BudgetExhausted):
        return 'budget_exhausted'
    if isinstance(exc, a6.A6TransportError):
        return 'transport_failed'
    if isinstance(exc, a6.A6ResponseError):
        return 'response_invalid'
    if isinstance(exc, a6.A6RequestError):
        return 'request_invalid'
    if isinstance(exc, ValidationEnvironmentError):
        return 'validation_environment'
    raise exc


def _production_client(args):
    response_cap = min(a6.MAX_RESPONSE_BYTES, args.response_bytes // args.calls)
    if response_cap <= 0:
        raise A6PilotError('response-bytes is too small for the requested call budget')
    try:
        context = ssl.create_default_context(cafile=args.ca_file)
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_cert_chain(args.client_cert, args.client_key)
    except (OSError, ssl.SSLError) as exc:
        raise A6PilotError('A6 TLS credentials or CA configuration are invalid') from exc
    budget = a6.RunBudget(args.calls, args.request_bytes, args.response_bytes)
    transport = a6.HTTPSJSONTransport(args.url, context, response_cap=response_cap)
    return a6.A6TranslationClient(transport, budget, response_cap=response_cap)


def _preflight(args):
    """Validate project and pilot paths before opening the output namespace or TLS files."""
    project = os.path.realpath(config.root())
    if config.cfg().get('adapter') != 'dq7':
        raise A6PilotError('a6-translate is only available for DQ7 projects')
    if config.target() != 'ko':
        raise A6PilotError('a6-translate requires a Korean DQ7 target')
    namespace = _require_outside_project(args.output, 'pilot output namespace', project)
    snapshot = (_read_input_snapshot(args.input_snapshot, project)
                if args.input_snapshot else None)
    if os.path.lexists(namespace):
        try:
            details = os.stat(namespace, follow_symlinks=False)
        except OSError as exc:
            raise InvalidOutputNamespace('pilot output namespace cannot be inspected') from exc
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise InvalidOutputNamespace('pilot output namespace is not a directory')
    return project, namespace, snapshot


def _run(args, client, preflight=None, namespace_lock=None):
    if preflight is None:
        preflight = _preflight(args)
    project, namespace, snapshot = preflight
    owns_lock = namespace_lock is None
    lock = namespace_lock or a6.OutputNamespaceLock(namespace).acquire()
    try:
        _require_locked_outside_project(lock, project)
        source = _load_object(config.src_path(), 'configured extracted source')
        contract_fingerprint = _contract_fingerprint(project, args.family, source)
        state, has_authoritative_state = _load_pilot_state(lock, args.family, contract_fingerprint)
        if has_authoritative_state:
            _reconcile_pilot_shards(lock, state)
        if snapshot is not None:
            pilot_tm = dict(snapshot)
            pilot_tm.update(state['tm'])
        else:
            pilot_tm = dict(state['tm'])
        terms = _profile_glossary(source, pilot_tm)
        hard_terms = _hard_glossary(terms)
        anchor_version = _anchor_version(hard_terms)
        _validate_anchor_versions(state, anchor_version)
        rows, todo = _source_rows(source, args.family, pilot_tm)
        if args.limit:
            todo = todo[:args.limit]
        if not todo:
            return 1 if state['review'] else 0

        context = _pilot_context(rows, pilot_tm)
        validator = _PilotValidator(project, source, hard_terms)
        batches, rejected_preflight = _make_batches(todo, args.batch_size, args.batch_chars)
        run_nonce = uuid.uuid4().hex
        failed = False
        write_lock = threading.Lock()

        def persist(accepted, rejected):
            nonlocal failed, state
            with write_lock:
                if set(accepted) & set(rejected):
                    raise A6PilotError('a pilot source cannot be accepted and rejected together')
                next_state = {
                    'version': state['version'],
                    'family': state['family'],
                    'contract_fingerprint': state['contract_fingerprint'],
                    'tm': dict(state['tm']),
                    'provenance': dict(state['provenance']),
                    'anchor_versions': dict(state['anchor_versions']),
                    'review': dict(state['review']),
                }
                if accepted:
                    next_state['tm'].update(accepted)
                    next_state['provenance'].update(
                        {source: _PROVIDER_ID for source in accepted})
                    next_state['anchor_versions'].update(
                        {source: anchor_version for source in accepted})
                    for source in accepted:
                        next_state['review'].pop(source, None)
                if rejected:
                    if set(rejected) & set(next_state['tm']):
                        raise PilotStateError('pilot rejection conflicts with accepted state')
                    next_state['review'].update({
                        source: {'refs': list(row['refs']), 'reason': reason}
                        for source, (row, reason) in rejected.items()
                    })
                _validate_state(next_state, args.family, contract_fingerprint)
                _replace_json_locked(lock, _pilot_names(args.family)['state'],
                                     next_state, 'pilot state')
                state = next_state
                _reconcile_pilot_shards(lock, state)
                failed = failed or bool(rejected)

        if rejected_preflight:
            persist({}, rejected_preflight)

        def work(number, batch):
            try:
                batch_terms = _relevant_glossary(terms, batch, args.family)
                response = client.translate(
                    {str(index): row['en'] for index, row in enumerate(batch)},
                    context=context, glossary=batch_terms,
                    request_id=f'dq7-{run_nonce}-{number}')
                expected_ids = {str(index) for index in range(len(batch))}
                if (not isinstance(response, dict) or set(response) != expected_ids):
                    raise a6.A6ResponseError('A6 response translations are invalid')
            except (a6.A6Error, ValidationEnvironmentError) as exc:
                persist({}, {row['en']: (row, _reason_for_exception(exc)) for row in batch})
                return
            accepted = {}
            rejected = {}
            for index, row in enumerate(batch):
                target = response.get(str(index))
                if not isinstance(target, str):
                    rejected[row['en']] = (row, 'response_invalid')
                    continue
                try:
                    target, problems = validator.check(
                        row['en'], target.replace('\r\n', '\n'), batch_terms,
                        args.family, row['group'])
                except (a6.A6Error, ValidationEnvironmentError) as exc:
                    rejected[row['en']] = (row, _reason_for_exception(exc))
                    continue
                if problems:
                    rejected[row['en']] = (row, 'validation_failed')
                    continue
                accepted[row['en']] = target
            persist(accepted, rejected)

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [executor.submit(work, number, batch)
                       for number, batch in enumerate(batches)]
            for future in futures:
                future.result()
        return 1 if failed else 0
    finally:
        if owns_lock:
            lock.release()


def main(argv=None, *, client=None):
    """Run one isolated A6 pilot; ``client`` is test-only transport injection."""
    args = _parser().parse_args(argv)
    _validate_options(args)
    preflight = _preflight(args)
    namespace_lock = a6.OutputNamespaceLock(preflight[1]).acquire()
    try:
        _require_locked_outside_project(namespace_lock, preflight[0])
        if client is None:
            client = _production_client(args)
        return _run(args, client, preflight, namespace_lock)
    finally:
        namespace_lock.release()


if __name__ == '__main__':
    raise SystemExit(main())

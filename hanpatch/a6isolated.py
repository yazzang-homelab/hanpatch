"""Isolated, opt-in DQ7 translation client for an untrusted A6 lane.

This module deliberately is not a ``providers`` entry.  It accepts only a small
translation envelope and only talks through a caller-supplied transport.  It
never discovers credentials, dotenv files, proxy settings, or provider pools.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import fcntl
import json
import os
import re
import ssl
import stat
import threading
import urllib.parse
import urllib.request
import uuid



PROTOCOL = 'a6-dq7-translation'
PROTOCOL_VERSION = 1
SOURCE_LANGUAGE = 'ja'
TARGET_LANGUAGE = 'ko'
TRANSLATION_KIND = 'dq7_translation'
FIXED_MODEL = 'a6-dq7-translation'

MAX_ITEMS = 16
MAX_ITEM_ID_BYTES = 64
MAX_SOURCE_BYTES = 4096
MAX_CONTEXT_ITEMS = 8
MAX_GLOSSARY_ITEMS = 128
MAX_CONTEXT_TEXT_BYTES = 2048
MAX_GLOSSARY_TEXT_BYTES = 1024
MAX_FEEDBACK_BYTES = 4096
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 65536
MAX_TRANSLATION_BYTES = 8192
MAX_JSON_DEPTH = 16

_ID_RE = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')


class A6Error(RuntimeError):
    """Base class for isolated-lane failures."""


class A6RequestError(A6Error):
    """The caller attempted to construct an out-of-contract request."""


class A6ResponseError(A6Error):
    """The untrusted lane returned an out-of-contract response."""


class A6TransportError(A6Error):
    """The HTTPS transport failed without exposing remote response content."""


class BudgetExhausted(A6Error):
    """A run's local reservation cap would be exceeded."""



_PROCESS_NAMESPACE_LOCKS = set()
_PROCESS_LOCK_GUARD = threading.Lock()


class NamespaceLocked(A6Error):
    """Another owner already holds the pilot output namespace lock."""


@dataclass(frozen=True)
class BudgetReservation:
    """The permanently consumed reservation for one attempted dispatch."""

    calls: int
    request_bytes: int
    response_bytes: int


class RunBudget:
    """Thread-safe local reservations for one explicitly scoped A6 pilot run.

    Reservations are consumed before a request is dispatched and are never
    returned.  The response reservation is the caller's read cap, rather than
    a post-hoc actual count, so a response failure cannot evade the limit.  This
    is a local safety limit, not a provider-side monetary cap.
    """

    def __init__(self, max_calls, max_request_bytes, max_response_bytes):
        self.max_calls = _positive_int(max_calls, 'max_calls')
        self.max_request_bytes = _positive_int(max_request_bytes,
                                               'max_request_bytes')
        self.max_response_bytes = _positive_int(max_response_bytes,
                                                'max_response_bytes')
        self._calls = 0
        self._request_bytes = 0
        self._response_bytes = 0
        self._lock = threading.Lock()

    @property
    def calls(self):
        with self._lock:
            return self._calls

    @property
    def request_bytes(self):
        with self._lock:
            return self._request_bytes

    @property
    def response_bytes(self):
        with self._lock:
            return self._response_bytes

    def snapshot(self):
        with self._lock:
            return BudgetReservation(self._calls, self._request_bytes,
                                     self._response_bytes)

    def reserve(self, request_bytes, response_bytes):
        """Atomically consume one attempt and its worst-case byte allocation."""
        request_bytes = _positive_int(request_bytes, 'request_bytes')
        response_bytes = _positive_int(response_bytes, 'response_bytes')
        with self._lock:
            calls = self._calls + 1
            requests = self._request_bytes + request_bytes
            responses = self._response_bytes + response_bytes
            if (calls > self.max_calls or requests > self.max_request_bytes
                    or responses > self.max_response_bytes):
                raise BudgetExhausted('A6 run budget exhausted')
            self._calls = calls
            self._request_bytes = requests
            self._response_bytes = responses
            return BudgetReservation(calls, requests, responses)


class OutputNamespaceLock:
    """Kernel-held lock and pinned directory handle for one pilot namespace."""

    FILENAME = '.a6-dq7-translation.lock'
    _DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    _FILE_FLAGS = os.O_CLOEXEC | os.O_NOFOLLOW

    def __init__(self, namespace):
        if not isinstance(namespace, str) or not namespace:
            raise A6RequestError('pilot output namespace must be a directory path')
        self.namespace = os.path.abspath(namespace)
        self.path = os.path.join(self.namespace, self.FILENAME)
        self._fd = None
        self._directory_fd = None
        self._directory_identity = None
        self._process_identity = None

        self._guard = threading.Lock()

    @property
    def locked(self):
        with self._guard:
            return self._fd is not None

    @property
    def directory_fd(self):
        with self._guard:
            if self._directory_fd is None:
                raise A6RequestError('pilot output namespace is not locked')
            return self._directory_fd

    @staticmethod
    def _identity(st):
        return st.st_dev, st.st_ino

    @staticmethod
    def _name(name):
        if (not isinstance(name, str) or not name or '/' in name
                or name in {'.', '..'}):
            raise A6RequestError('pilot namespace record name is invalid')
        return name

    def _verify_namespace(self):
        if self._directory_fd is None or self._directory_identity is None:
            raise A6RequestError('pilot output namespace is not locked')
        try:
            held = os.fstat(self._directory_fd)
            current = os.stat(self.namespace, follow_symlinks=False)
        except OSError as exc:
            raise A6RequestError('pilot output namespace path changed while locked') from exc
        if (not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(current.st_mode)
                or self._identity(held) != self._directory_identity
                or self._identity(current) != self._directory_identity):
            raise A6RequestError('pilot output namespace path changed while locked')

    def acquire(self):
        with self._guard:
            if self._fd is not None:
                raise NamespaceLocked('this lock owner already holds the namespace')
            directory_fd = None
            fd = None
            identity = None
            process_claimed = False
            try:
                os.makedirs(self.namespace, mode=0o700, exist_ok=True)
                directory_fd = os.open(self.namespace, self._DIRECTORY_FLAGS)
                directory = os.fstat(directory_fd)
                current = os.stat(self.namespace, follow_symlinks=False)
                identity = self._identity(directory)
                if (not stat.S_ISDIR(directory.st_mode) or not stat.S_ISDIR(current.st_mode)
                        or identity != self._identity(current)):
                    raise A6RequestError('pilot output namespace is not a stable directory')
                with _PROCESS_LOCK_GUARD:
                    if identity in _PROCESS_NAMESPACE_LOCKS:
                        raise NamespaceLocked('A6 pilot output namespace is already locked')
                    _PROCESS_NAMESPACE_LOCKS.add(identity)
                    process_claimed = True
                fd = os.open(self.FILENAME, os.O_RDWR | os.O_CREAT | self._FILE_FLAGS,
                             0o600, dir_fd=directory_fd)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise A6RequestError('pilot output namespace lock is not a regular file')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise NamespaceLocked('A6 pilot output namespace is already locked') from None
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'a6-dq7-translation\n')
                    os.fsync(fd)
                self._fd = fd
                self._directory_fd = directory_fd
                self._directory_identity = identity
                self._process_identity = identity
                return self
            except A6Error:
                if fd is not None:
                    os.close(fd)
                if directory_fd is not None:
                    os.close(directory_fd)
                if process_claimed:
                    with _PROCESS_LOCK_GUARD:
                        _PROCESS_NAMESPACE_LOCKS.discard(identity)
                raise
            except OSError as exc:
                if fd is not None:
                    os.close(fd)
                if directory_fd is not None:
                    os.close(directory_fd)
                if process_claimed:
                    with _PROCESS_LOCK_GUARD:
                        _PROCESS_NAMESPACE_LOCKS.discard(identity)
                raise A6RequestError('pilot output namespace cannot be opened safely') from exc


    def exists(self, name):
        name = self._name(name)
        with self._guard:
            self._verify_namespace()
            try:
                os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            self._verify_namespace()
            return True

    def read_bytes(self, name):
        name = self._name(name)
        with self._guard:
            self._verify_namespace()
            try:
                fd = os.open(name, os.O_RDONLY | self._FILE_FLAGS,
                             dir_fd=self._directory_fd)
            except OSError as exc:
                raise A6RequestError('pilot namespace record cannot be opened safely') from exc
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise A6RequestError('pilot namespace record is not a regular file')
                with os.fdopen(fd, 'rb') as handle:
                    fd = None
                    value = handle.read()
            finally:
                if fd is not None:
                    os.close(fd)
            self._verify_namespace()
            return value

    def replace_bytes(self, name, value):
        name = self._name(name)
        if not isinstance(value, bytes):
            raise A6RequestError('pilot namespace record must be bytes')
        temporary = f'.{name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp'
        with self._guard:
            self._verify_namespace()
            fd = None
            created = False
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | self._FILE_FLAGS,
                             0o600, dir_fd=self._directory_fd)
                created = True
                with os.fdopen(fd, 'wb') as handle:
                    fd = None
                    handle.write(value)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, name, src_dir_fd=self._directory_fd,
                           dst_dir_fd=self._directory_fd)
                created = False
                os.fsync(self._directory_fd)
                self._verify_namespace()
            finally:
                if fd is not None:
                    os.close(fd)
                if created:
                    try:
                        os.unlink(temporary, dir_fd=self._directory_fd)
                    except FileNotFoundError:
                        pass

    def release(self):
        with self._guard:
            fd, directory_fd = self._fd, self._directory_fd
            identity = self._process_identity
            if fd is None:
                return
            self._fd = None
            self._directory_fd = None
            self._directory_identity = None
            self._process_identity = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
                os.close(directory_fd)
                with _PROCESS_LOCK_GUARD:
                    _PROCESS_NAMESPACE_LOCKS.discard(identity)

    close = release

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn every redirect into an error; endpoint changes are not acceptable."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPSJSONTransport:
    """One explicit, non-proxying HTTPS POST transport for A6 envelopes.

    ``tls_context`` and ``url`` are deliberately caller supplied.  ``opener``
    is private test injection; production uses an opener with proxies disabled
    and redirects refused.
    """

    def __init__(self, url, tls_context, *, timeout=30.0,
                 response_cap=MAX_RESPONSE_BYTES, _opener=None):
        parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        if (parsed is None or parsed.scheme != 'https' or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment):
            raise A6RequestError('A6 transport requires an explicit HTTPS URL')
        if not isinstance(tls_context, ssl.SSLContext):
            raise A6RequestError('A6 transport requires a caller-supplied TLS context')
        if (tls_context.verify_mode != ssl.CERT_REQUIRED
                or not tls_context.check_hostname):
            raise A6RequestError(
                'A6 transport requires certificate verification and hostname checking')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise A6RequestError('timeout must be positive')
        self.url = url
        self.timeout = float(timeout)
        self.response_cap = _capped_int(response_cap, 'response_cap', MAX_RESPONSE_BYTES)
        self.proxy_handler = urllib.request.ProxyHandler({})
        self.opener = _opener or urllib.request.build_opener(
            self.proxy_handler, _NoRedirect(),
            urllib.request.HTTPSHandler(context=tls_context))

    def __call__(self, body):
        if not isinstance(body, bytes) or len(body) > MAX_REQUEST_BYTES:
            raise A6RequestError('A6 request body is invalid or too large')
        headers = {
            'Accept': 'application/json',
            'Accept-Encoding': 'identity',
            'Connection': 'close',
            'Content-Type': 'application/json; charset=utf-8',
            'Content-Length': str(len(body)),
        }
        request = urllib.request.Request(self.url, data=body, headers=headers,
                                         method='POST')
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return self._read_response(response)
        except A6Error:
            raise
        except Exception:
            raise A6TransportError('A6 HTTPS transport failed') from None

    def _read_response(self, response):
        status = getattr(response, 'status', None)
        if status is None:
            status = response.getcode()
        if status != 200:
            raise A6TransportError('A6 HTTPS transport returned a non-success status')
        headers = getattr(response, 'headers', None)
        if headers is None:
            raise A6TransportError('A6 HTTPS transport omitted response headers')
        encoding = (headers.get('Content-Encoding') or '').strip().lower()
        transfer = (headers.get('Transfer-Encoding') or '').strip().lower()
        if encoding not in ('', 'identity') or transfer not in ('', 'identity'):
            raise A6TransportError('A6 HTTPS transport refused compressed or streamed data')
        declared = headers.get('Content-Length')
        if not isinstance(declared, str) or not re.fullmatch(r'[0-9]+', declared.strip()):
            raise A6TransportError('A6 HTTPS transport requires Content-Length')
        declared_bytes = int(declared.strip())
        if declared_bytes > self.response_cap:
            raise A6TransportError('A6 HTTPS response exceeds the configured read cap')
        body = response.read(self.response_cap + 1)
        if not isinstance(body, bytes) or len(body) > self.response_cap:
            raise A6TransportError('A6 HTTPS response exceeded the configured read cap')
        if len(body) != declared_bytes:
            raise A6TransportError('A6 HTTPS response Content-Length mismatch')
        return body


def https_transport(url, tls_context, **kwargs):
    """Build the production HTTPS transport without proxy inheritance."""
    return HTTPSJSONTransport(url, tls_context, **kwargs)


class A6TranslationClient:
    """Provider-shaped, DQ7-only adapter with no generic chat/prompt surface."""

    name = 'a6isolated'
    model = FIXED_MODEL
    supports_json = True
    parked_until = 0.0

    def __init__(self, transport, budget, *, response_cap=MAX_RESPONSE_BYTES):
        if not callable(transport):
            raise A6RequestError('transport must be callable')
        if not isinstance(budget, RunBudget):
            raise A6RequestError('budget must be a RunBudget')
        self._transport = transport
        self.budget = budget
        self.response_cap = _capped_int(response_cap, 'response_cap', MAX_RESPONSE_BYTES)
        self.id = f'{self.name}:{self.model}'
        self.calls = 0
        self.errors = 0
        self._counter_lock = threading.Lock()

    def translate(self, items, *, context=(), glossary=(), feedback='', request_id=None):
        """Translate a bounded DQ7 batch through the injected transport callable."""
        request_id = _request_id(request_id)
        request = {
            'protocol': PROTOCOL,
            'version': PROTOCOL_VERSION,
            'request_id': request_id,
            'source_language': SOURCE_LANGUAGE,
            'target_language': TARGET_LANGUAGE,
            'kind': TRANSLATION_KIND,
            'model': self.model,
            'items': _items(items),
            'context': _pairs(context, 'context', MAX_CONTEXT_ITEMS,
                              MAX_CONTEXT_TEXT_BYTES),
            'glossary': _pairs(glossary, 'glossary', MAX_GLOSSARY_ITEMS,
                               MAX_GLOSSARY_TEXT_BYTES),
            'feedback': _text(feedback, 'feedback', MAX_FEEDBACK_BYTES),
        }
        try:
            body = json.dumps(request, ensure_ascii=False, separators=(',', ':'),
                              allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, UnicodeEncodeError):
            raise A6RequestError('A6 request cannot be encoded as strict JSON') from None
        if len(body) > MAX_REQUEST_BYTES:
            raise A6RequestError('A6 request exceeds the byte cap')
        self.budget.reserve(len(body), self.response_cap)
        with self._counter_lock:
            self.calls += 1
        try:
            raw = self._transport(body)
            return self._response(raw, request_id, [item['id'] for item in request['items']])
        except A6Error:
            with self._counter_lock:
                self.errors += 1
            raise
        except Exception:
            with self._counter_lock:
                self.errors += 1
            raise A6TransportError('A6 transport callable failed') from None

    def _response(self, raw, request_id, item_ids):
        if not isinstance(raw, bytes) or len(raw) > self.response_cap:
            raise A6ResponseError('A6 response is invalid or exceeds the byte cap')
        envelope = _json_object(raw)
        required = {'protocol', 'version', 'request_id', 'model', 'translations'}
        if set(envelope) != required:
            raise A6ResponseError('A6 response envelope has unexpected fields')
        if (envelope['protocol'] != PROTOCOL or type(envelope['version']) is not int
                or envelope['version'] != PROTOCOL_VERSION):
            raise A6ResponseError('A6 response protocol mismatch')
        if envelope['request_id'] != request_id:
            raise A6ResponseError('A6 response request-id mismatch')
        if envelope['model'] != self.model:
            raise A6ResponseError('A6 response model mismatch')
        translations = envelope['translations']
        if not isinstance(translations, dict) or set(translations) != set(item_ids):
            raise A6ResponseError('A6 response translations do not match the request')
        out = {}
        for item_id in item_ids:
            try:
                text = _text(translations[item_id], 'translation', MAX_TRANSLATION_BYTES)
            except A6RequestError:
                raise A6ResponseError('A6 response translation exceeds the allowed bounds') from None
            if not text.strip():
                raise A6ResponseError('A6 response contains an empty translation')
            out[item_id] = text
        return out


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise A6RequestError(f'{name} must be a positive integer')
    return value


def _capped_int(value, name, maximum):
    value = _positive_int(value, name)
    if value > maximum:
        raise A6RequestError(f'{name} exceeds the hard cap')
    return value


def _text(value, name, maximum):
    if not isinstance(value, str):
        raise A6RequestError(f'{name} must be a string')
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError:
        raise A6RequestError(f'{name} is not valid UTF-8 text') from None
    if '\x00' in value or len(encoded) > maximum:
        raise A6RequestError(f'{name} exceeds the allowed bounds')
    return value


def _request_id(value):
    if value is None:
        return uuid.uuid4().hex
    value = _text(value, 'request_id', MAX_ITEM_ID_BYTES)
    if not _ID_RE.fullmatch(value):
        raise A6RequestError('request_id contains unsupported characters')
    return value


def _item_id(value):
    value = _text(value, 'item id', MAX_ITEM_ID_BYTES)
    if not _ID_RE.fullmatch(value):
        raise A6RequestError('item id contains unsupported characters')
    return value


def _items(value):
    if isinstance(value, Mapping):
        if len(value) > MAX_ITEMS:
            raise A6RequestError('items must contain between one and the maximum count')
        raw = [{'id': item_id, 'source': source}
               for item_id, source in value.items()]
    elif isinstance(value, (list, tuple)):
        if len(value) > MAX_ITEMS:
            raise A6RequestError('items must contain between one and the maximum count')
        raw = value
    else:
        raise A6RequestError('items must be a mapping or a list of item objects')
    if not raw:
        raise A6RequestError('items must contain between one and the maximum count')
    out = []
    seen = set()
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {'id', 'source'}:
            raise A6RequestError('every item must contain only id and source')
        item_id = _item_id(item['id'])
        if item_id in seen:
            raise A6RequestError('item ids must be unique')
        seen.add(item_id)
        out.append({'id': item_id,
                    'source': _text(item['source'], 'item source', MAX_SOURCE_BYTES)})
    return out


def _pairs(value, name, maximum_count, maximum_text):
    if isinstance(value, Mapping):
        if len(value) > maximum_count:
            raise A6RequestError(f'{name} exceeds the maximum item count')
        raw = [{'source': source, 'target': target}
               for source, target in value.items()]
    elif isinstance(value, (list, tuple)):
        if len(value) > maximum_count:
            raise A6RequestError(f'{name} exceeds the maximum item count')
        raw = value
    else:
        raise A6RequestError(f'{name} must be a mapping or a list of pair objects')
    out = []
    for pair in raw:
        if not isinstance(pair, Mapping) or set(pair) != {'source', 'target'}:
            raise A6RequestError(f'every {name} item must contain only source and target')
        out.append({'source': _text(pair['source'], f'{name} source', maximum_text),
                    'target': _text(pair['target'], f'{name} target', maximum_text)})
    return out



def _json_object(raw):
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise A6ResponseError('A6 response is not UTF-8 JSON') from None
    _json_depth(text)
    try:
        value = json.loads(text, object_pairs_hook=_unique_object,
                           parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise A6ResponseError('A6 response is not strict JSON') from None
    if not isinstance(value, dict):
        raise A6ResponseError('A6 response must be a JSON object')
    return value


def _unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate JSON object key')
        out[key] = value
    return out


def _reject_json_constant(value):
    raise ValueError(f'unsupported JSON constant {value!r}')


def _json_depth(text):
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in '[{':
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise A6ResponseError('A6 response JSON nesting exceeds the cap')
        elif char in ']}':
            depth -= 1
            if depth < 0:
                raise A6ResponseError('A6 response JSON is structurally malformed')
    if in_string or depth != 0:
        raise A6ResponseError('A6 response JSON is structurally malformed')

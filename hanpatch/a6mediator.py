"""Mediator relay: `a6-dq7-translation` envelope <-> an OpenAI-compatible upstream.

Trust model (2026-08-07 revision):

* The upstream provider is a reseller pool. Its *response* is hostile input.
  Source text is not confidential, so nothing here tries to hide it.
* The one thing that must not happen is the response reaching the ROM patcher
  with content the patcher does not expect. Every guard below exists for that:
  strict parse, byte caps, exact id set, and tag-preservation.
* The mediator is the sole holder of the upstream credential. It is read from a
  file or the environment, never taken from argv, and never logged.
* No generic prompt surface. The upstream request is built from a fixed
  template; callers supply source strings, not instructions.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = 'a6-dq7-translation'
PROTOCOL_VERSION = 1
SOURCE_LANGUAGE = 'ja'
TARGET_LANGUAGE = 'ko'

DEFAULT_UPSTREAM = 'https://a6.a6api.com/v1/chat/completions'

# Model order is measured, not assumed. Source: DQ7 Korean localization release
# gate, 54,071 source sentences / 205,152 adjudications, 2026-08-05
# (/var/www/share/free-mt-quality/).
#
#   deepseek-v4-flash       99.1% pass   n=20,753   <- primary
#   gpt-5.6-luna (reasoning) 92.7% pass  n=24,689   <- BLOCKED
#   deepseek-v4-pro          91.6% pass  n= 2,169   <- BLOCKED
#
# Bigger and "smarter" measured *worse*. This task is template compliance --
# preserve tags, respect capacity, honour the glossary -- not composition.
# Reasoning models paraphrase and touch placeholders, which trips `check_tags`
# and forces a fallback, costing both quality and wall-clock. Do not promote an
# unmeasured model here; measure it against the release gate first.
DEFAULT_MODEL = 'deepseek-v4-flash'
DEFAULT_FALLBACKS = ('DeepSeek-V4-Flash-0731',)

# Present in the upstream catalogue and measurably worse on this corpus.
# `Mediator` refuses to route to these even if a caller configures them.
BLOCKED_MODELS = frozenset({'gpt-5.6-luna', 'deepseek-v4-pro'})

MAX_UPSTREAM_REQUEST_BYTES = 262144
MAX_UPSTREAM_RESPONSE_BYTES = 262144
MAX_TRANSLATION_BYTES = 8192
MAX_ITEMS = 16

# profiles/dq7.json `tag_pattern`. Ruby/furigana `{1かな}` is source-only and is
# stripped before the relay, so it must never appear in a translation.
TAG_RE = re.compile(r'<[^>\n]*>|\{[A-Z0-9_]+\}')
RUBY_RE = re.compile(r'\{[0-9]+[^}\n]*\}')

SYSTEM_PROMPT = (
    'You translate Dragon Quest VII game script from Japanese to Korean.\n'
    'Rules:\n'
    '1. Output JSON only: {"translations":{"<id>":"<korean>"}}. No prose, no'
    ' markdown fence.\n'
    '2. Translate every id you are given, exactly once. Invent no ids.\n'
    '3. Copy every {PLACEHOLDER} and <TAG> from the source verbatim. Do not add,'
    ' remove, reorder or translate them.\n'
    '4. Preserve line breaks. Keep the register of a JRPG script.\n'
    '5. Never emit control characters other than newline.'
)


class MediatorError(RuntimeError):
    """The relay refused. No bytes reach the caller's pipeline."""


class UpstreamError(MediatorError):
    """The upstream transport or response was out of contract."""


class TagViolation(MediatorError):
    """A translation changed the placeholder set. The unit is rejected."""


# ---------------------------------------------------------------------------
# Credential
# ---------------------------------------------------------------------------

def load_credential(path: str | None = None, env: str = 'A6_API_KEY') -> str:
    """Read the upstream bearer token from a file or the environment.

    Never accepted from argv: an argv credential is visible in `ps` to every
    process in the container's pid namespace.
    """
    if path:
        with open(path, 'r', encoding='utf-8') as handle:
            token = handle.read().strip()
    else:
        token = (os.environ.get(env) or '').strip()
    if not token:
        raise MediatorError('upstream credential is missing')
    if '\n' in token or '\r' in token:
        raise MediatorError('upstream credential contains a line break')
    return token


def redact(text: str, token: str) -> str:
    """Guard for anything that might be logged."""
    return text.replace(token, '<redacted>') if token else text


# ---------------------------------------------------------------------------
# Tag preservation
# ---------------------------------------------------------------------------

def tag_multiset(text: str) -> tuple[str, ...]:
    return tuple(sorted(TAG_RE.findall(text)))


def check_tags(source: str, translation: str) -> None:
    want = tag_multiset(source)
    got = tag_multiset(translation)
    if want != got:
        raise TagViolation(
            f'placeholder set changed: source {want} != translation {got}')
    stray = RUBY_RE.findall(translation)
    if stray:
        raise TagViolation(f'translation reintroduced source-only ruby {stray}')
    bad = [ch for ch in translation if ord(ch) < 0x20 and ch != '\n']
    if bad:
        raise TagViolation('translation contains control characters')


# ---------------------------------------------------------------------------
# Upstream call
# ---------------------------------------------------------------------------

def build_upstream_request(model: str, items: list[dict], glossary=(),
                           context=()) -> dict:
    lines = [f'{item["id"]}\t{item["source"]}' for item in items]
    user = ['Translate each line. The id is before the tab.', *lines]
    if glossary:
        user.append('')
        user.append('Glossary (use these exact Korean terms):')
        user.extend(f'{k}\t{v}' for k, v in glossary)
    if context:
        user.append('')
        user.append('Nearby already-translated lines, for tone only:')
        user.extend(f'{k}\t{v}' for k, v in context)
    return {
        'model': model,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': '\n'.join(user)},
        ],
        'temperature': 0,
        # The A6 DeepSeek-V4 route defaults to hidden reasoning. Live screening on
        # DQ7 found a short line consuming all 1,024 completion tokens as reasoning
        # and returning empty content. This OpenAI-compatible control reduced the
        # same case to 77 completion tokens, zero reasoning, valid JSON. It is fixed
        # here so neither a caller nor a supplier can turn expensive thinking on.
        'reasoning_effort': 'none',
        'response_format': {'type': 'json_object'},
        'stream': False,
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every 30x; the authenticated POST may only hit the configured URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Upstream:
    """One non-proxying, non-redirecting HTTPS POST client."""

    def __init__(self, url: str = DEFAULT_UPSTREAM, *, token: str,
                 timeout: float = 120.0, opener=None, rpm: float = 5.0):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username:
            raise MediatorError('upstream must be an explicit HTTPS URL')
        if isinstance(rpm, bool) or not isinstance(rpm, (int, float)) or rpm <= 0:
            raise MediatorError('upstream rpm must be positive')
        self.url = url
        self._token = token
        self.timeout = float(timeout)
        self._min_interval = 60.0 / float(rpm)
        self._pace_lock = threading.Lock()
        self._last_request = 0.0
        context = ssl.create_default_context()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=context))

    def _throttle(self):
        # Live token DOE: six starts in ~20 s succeeded and the seventh got 429.
        # Five starts/minute leaves margin for supplier-pool jitter. The lock spans
        # the sleep so concurrent callers cannot wake and dispatch together.
        with self._pace_lock:
            delay = self._min_interval - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()

    def __call__(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
        if len(body) > MAX_UPSTREAM_REQUEST_BYTES:
            raise MediatorError('upstream request exceeds the byte cap')
        request = urllib.request.Request(
            self.url, data=body, method='POST',
            headers={
                'Authorization': f'Bearer {self._token}',
                'Content-Type': 'application/json; charset=utf-8',
                'Accept': 'application/json',
                'Accept-Encoding': 'identity',
                # Cloudflare rejects Python's default `Python-urllib/*` UA with an
                # empty 403 before the request reaches A6. A fixed, non-secret UA
                # makes curl and the mediator follow the same origin path.
                'User-Agent': 'hanpatch-a6-mediator/1',
                'Connection': 'close',
            })
        self._throttle()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise UpstreamError(f'upstream status {response.status}')
                raw = response.read(MAX_UPSTREAM_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise UpstreamError(f'upstream status {exc.code}') from None
        except UpstreamError:
            raise
        except Exception:
            raise UpstreamError('upstream transport failed') from None
        if len(raw) > MAX_UPSTREAM_RESPONSE_BYTES:
            raise UpstreamError('upstream response exceeds the read cap')
        return _strict_json_object(raw)


def _strict_json_object(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode('utf-8'),
                           parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        raise UpstreamError('upstream response is not strict JSON') from None
    if not isinstance(value, dict):
        raise UpstreamError('upstream response is not a JSON object')
    return value


def _reject_constant(name):
    raise ValueError(f'unsupported JSON constant {name}')


_THINK_RE = re.compile(r'<think\b[^>]*>.*?</think\s*>', re.S | re.I)


def _strip_inline_reasoning(content: str) -> str:
    """Remove a `<think>...</think>` block the model wrote INTO `content`.

    Some models put their scratchpad in a separate `reasoning` field, and some inline it
    in `content` ahead of the answer. Measured on this reseller 2026-08-11: every
    `minimax-*` model returns `<think>...</think>` followed by the JSON, and
    `reasoning_effort: none` does not suppress it - minimax refuses to disable reasoning
    at all. A strict `json.loads` on that string fails at character 0, so all three
    minimax models were recorded as "content is not JSON" and looked incapable of the
    translation contract while the SAME models scored 12/12 on the judge contract, which
    parses leniently. That was a parser gap on our side, not a model limit.

    Stripping is deliberately narrow: only a complete, well-formed `<think>` element is
    removed. An unterminated block means the reply was truncated, and that must keep
    failing rather than be silently repaired into a half-answer.
    """
    return _THINK_RE.sub('', content).strip()


def extract_translations(envelope: dict, item_ids: list[str]) -> dict:
    """Pull `{id: korean}` out of an OpenAI-shaped reply, strictly."""
    choices = envelope.get('choices')
    if not isinstance(choices, list) or not choices:
        raise UpstreamError('upstream reply has no choices')
    message = choices[0].get('message') if isinstance(choices[0], dict) else None
    content = message.get('content') if isinstance(message, dict) else None
    if not isinstance(content, str) or not content:
        raise UpstreamError('upstream reply has no message content')
    if len(content.encode('utf-8')) > MAX_UPSTREAM_RESPONSE_BYTES:
        raise UpstreamError('upstream message content exceeds the cap')

    content = _strip_inline_reasoning(content)
    try:
        payload = json.loads(content, parse_constant=_reject_constant)
    except ValueError:
        raise UpstreamError('upstream message content is not JSON') from None
    if not isinstance(payload, dict):
        raise UpstreamError('upstream message content is not an object')
    translations = payload.get('translations')
    if not isinstance(translations, dict):
        raise UpstreamError('upstream reply has no translations object')
    if set(translations) != set(item_ids):
        raise UpstreamError('upstream translations do not match the request ids')

    out = {}
    for item_id in item_ids:
        text = translations[item_id]
        if not isinstance(text, str) or not text.strip():
            raise UpstreamError(f'translation for {item_id} is empty')
        if len(text.encode('utf-8')) > MAX_TRANSLATION_BYTES:
            raise UpstreamError(f'translation for {item_id} exceeds the byte cap')
        out[item_id] = text
    return out


# ---------------------------------------------------------------------------
# Envelope relay
# ---------------------------------------------------------------------------

class Mediator:
    """Translate one `a6-dq7-translation` request envelope."""

    def __init__(self, upstream: Upstream, *, model: str = DEFAULT_MODEL,
                 fallbacks: tuple[str, ...] = DEFAULT_FALLBACKS):
        self.upstream = upstream
        models = (model, *fallbacks)
        blocked = sorted(BLOCKED_MODELS.intersection(models))
        if blocked:
            raise MediatorError(
                f'{blocked} measured below the release gate on this corpus; '
                'see /var/www/share/free-mt-quality/')
        self.models = models
        self.lock = threading.Lock()
        self.calls = 0
        self.model_errors: dict[str, int] = {}

    def handle(self, request: dict) -> dict:
        self._check_request(request)
        items = request['items']
        item_ids = [item['id'] for item in items]
        sources = {item['id']: item['source'] for item in items}

        last: Exception | None = None
        for model in self.models:
            payload = build_upstream_request(
                model, items,
                glossary=tuple(request.get('glossary') or ()),
                context=tuple(request.get('context') or ()))
            try:
                with self.lock:
                    self.calls += 1
                translations = extract_translations(
                    self.upstream(payload), item_ids)
                for item_id, text in translations.items():
                    check_tags(sources[item_id], text)
            except MediatorError as exc:
                with self.lock:
                    self.model_errors[model] = self.model_errors.get(model, 0) + 1
                last = exc
                continue
            return {
                'protocol': PROTOCOL,
                'version': PROTOCOL_VERSION,
                'request_id': request['request_id'],
                'model': request['model'],
                'translations': translations,
            }
        raise MediatorError(f'every upstream model failed; last: {last}')

    @staticmethod
    def _check_request(request: dict) -> None:
        required = {'protocol', 'version', 'request_id', 'source_language',
                    'target_language', 'kind', 'model', 'items', 'context',
                    'glossary', 'feedback'}
        if not isinstance(request, dict) or set(request) != required:
            raise MediatorError('request envelope has unexpected fields')
        if request['protocol'] != PROTOCOL or request['version'] != PROTOCOL_VERSION:
            raise MediatorError('request protocol mismatch')
        if request['source_language'] != SOURCE_LANGUAGE:
            raise MediatorError('request source language mismatch')
        if request['target_language'] != TARGET_LANGUAGE:
            raise MediatorError('request target language mismatch')
        items = request['items']
        if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
            raise MediatorError('request item count is out of contract')
        seen = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {'id', 'source'}:
                raise MediatorError('request item shape is out of contract')
            if item['id'] in seen:
                raise MediatorError('request contains a duplicate item id')
            seen.add(item['id'])
            if not isinstance(item['source'], str) or not item['source']:
                raise MediatorError('request item source is empty')

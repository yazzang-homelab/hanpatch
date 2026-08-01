"""Provider pool with per-endpoint rate limiting and rotation.

Any OpenAI-compatible base URL works. The bundled table is free-tier endpoints
plus local proxies; add your own to ENDPOINTS. Keys are never stored here — they
come from the environment, or from dotenv files listed in HANPATCH_ENV
(colon-separated) or the defaults below.

Rotation is the point: a shard that fails validation is retried on a *different*
provider, so one model's systematic blind spot does not become the shipped text.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from hanpatch import config

TIMEOUT = float(os.environ.get('MTL_TIMEOUT', '300'))
def _dotenvs():
    env = os.environ.get('HANPATCH_ENV')
    if env:
        return [p for p in env.split(os.pathsep) if p]
    home = os.path.expanduser('~')
    return [os.path.join(home, '.hanpatch', 'env'), os.path.join(home, '.env')]


DOTENVS = _dotenvs()


def load_dotenv(paths=None):
    """Load KEY=VALUE pairs, keeping the longest value seen for each key."""
    for path in (paths or _dotenvs()):
        if not os.path.exists(path):
            continue
        try:
            lines = open(path, encoding='utf-8', errors='replace').read().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:]
            if '=' not in line:
                continue
            k, v = line.split('=', 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if not v or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', k):
                continue
            if len(v) > len(os.environ.get(k, '')):
                os.environ[k] = v


class Provider:
    def __init__(self, name, base, key, model, rpm=20, max_tokens=8192, json_mode=True):
        self.name = name
        self.base = base.rstrip('/')
        self.key = key
        self.model = model
        self.min_interval = 60.0 / rpm
        self.max_tokens = max_tokens
        self.supports_json = json_mode
        self._lock = threading.Lock()
        self._last = 0.0
        self.calls = 0
        self.errors = 0
        # When a rotator parks its keys it TELLS us for how long. Ignoring that and
        # rotating back immediately is what turned a 60-string run into 211 calls with 86
        # park responses: the same endpoint was asked again every few seconds while its
        # keys sat in cooldown. Requests are not scheduled here until this passes.
        self.parked_until = 0.0

    @property
    def id(self):
        return f'{self.name}:{self.model}'

    def __repr__(self):
        return f'<{self.id}>'

    def _throttle(self):
        with self._lock:
            dt = time.time() - self._last
            if dt < self.min_interval:
                time.sleep(self.min_interval - dt)
            self._last = time.time()

    def chat(self, system, user, temperature=0.2, max_tokens=None, json_mode=None):
        self._throttle()
        payload = {
            'model': self.model,
            'messages': [{'role': 'system', 'content': system},
                         {'role': 'user', 'content': user}],
            'temperature': temperature,
            'max_tokens': max_tokens or self.max_tokens,
        }
        want_json = self.supports_json if json_mode is None else json_mode
        if want_json:
            payload['response_format'] = {'type': 'json_object'}
        if self.name == 'deepseek':
            # Reasoning OFF, and this is the whole difference between unusable and best in
            # class. By default this model puts 12500 characters into `reasoning_content`
            # and leaves `content` EMPTY, so the reply never parses: measured 8 of 24
            # strings over three trials at 29-79 seconds each, with the JSON never closed.
            # With reasoning disabled the same prompt returns a 421-character object,
            # 8 of 8, in 2.8 seconds - faster than any other endpoint here.
            # `thinking: {'type': 'disabled'}` behaves identically; `enable_thinking` and
            # `chat_template_kwargs` are silently IGNORED, which is why guessing the switch
            # rather than probing for it wasted three configurations.
            payload['reasoning_effort'] = 'none'
        headers = {'Content-Type': 'application/json',
                   'User-Agent': 'crimson-kr-mtl/1.0',
                   'Accept': 'application/json'}
        if self.key:
            headers['Authorization'] = f'Bearer {self.key}'
        req = urllib.request.Request(f'{self.base}/chat/completions',
                                     data=json.dumps(payload).encode(), headers=headers)
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            self.errors += 1
            detail = e.read()[:300].decode('utf-8', 'replace')
            if e.code == 429:
                wait = _retry_after(e.headers, detail)
                self.parked_until = time.time() + wait
            # NO json_mode=False retry here. It was tried and MEASURED WORSE: dropping
            # response_format made groq:openai/gpt-oss-120b answer with an empty content
            # AND an empty reasoning_content, and made nimproxy:google/gemma-4-31b-it
            # return HTTP 500, so a recoverable 400 became an unrecoverable blank. The
            # real cause of the parse failures was on our side - see translate.parse_json.
            raise RuntimeError(f'{self.id} HTTP {e.code}: {detail}') from None
        except Exception as e:
            self.errors += 1
            raise RuntimeError(f'{self.id} {type(e).__name__}: {e}') from None
        _account(self.name, self.model, body.get('usage'))
        if 'error' in body and 'choices' not in body:
            self.errors += 1
            raise RuntimeError(f'{self.id} error: {str(body["error"])[:300]}')
        msg = body['choices'][0]['message']
        return msg.get('content') or msg.get('reasoning_content') or ''


# Every endpoint is a LOCAL ROTATOR, never an upstream base URL. The rotators own
# the keys, the per-key budgets, the cooldowns and the vendor compatibility
# patches; calling an upstream directly throws all of that away and burns the
# shared quota. It also silently breaks: `groq:openai/gpt-oss-120b` was recorded
# here as permanently broken on HTTP 403, which was never the model - urllib's
# default User-Agent trips Cloudflare 1010 on api.groq.com, and the same spec
# answers in 0.6s through the rotator on 18096.
#
# 18091 is deliberately absent. It rotates Groq keys but forwards only
# model/messages/max_tokens/temperature, so `tools` disappears without an error.
# 18096 is the general-purpose Groq endpoint.
ENDPOINTS = {
    'nimproxy':   ('http://127.0.0.1:18090/v1', None, 30),
    'opencode':   ('http://127.0.0.1:18092/v1', None, 20),
    'openrouter': ('http://127.0.0.1:18094/v1', None, 12),
    'tokenrouter': ('http://127.0.0.1:18095/v1', None, 7),
    'groq':       ('http://127.0.0.1:18096/v1', None, 25),
    # The Antigravity gateway is a different LANE, not another free rotator: it fronts
    # subscription accounts (personal and company, the '-biz' suffix) and serves
    # gemini-3-pro, claude-opus-4.6 and friends. Measured on an 8-string batch it answers
    # 8/8 but takes 48-169s against groq's sub-second, so it is not where bulk volume
    # belongs. It is where a JUDGMENT belongs - QA of what the free pool produced, and the
    # rows the free pool cannot finish. The rpm figure is a scheduling hint only.
    'agy':        ('http://127.0.0.1:8086/v1', None, 6),
    # PAID, and deliberately the only entry that carries a key variable. Every other
    # endpoint here is a local rotator that owns its own credentials; this one bills per
    # token, so it must be named explicitly by a caller and can never be reached by a
    # registry role or a default pool. Measured price at our token profile (150 in / 25 out
    # per string): the whole 54071-string corpus costs about $1.54 without prompt caching
    # and about $0.69 with it, because DeepSeek prices a cache-hit prefix at 1/50 of a
    # miss. That is why the prompt is ordered stable-prefix-first - see translate.build.
    'deepseek':   ('https://api.deepseek.com/v1', 'DEEPSEEK_API_KEY', 60),
}

# The registry is the source of truth for which free specs exist and what each is
# allowed to do. When it is present, the pool is derived from it by role, so a
# model that loses its free tier stops being used here without anyone editing
# this file. The pinned list below is the fallback for hosts without the
# registry, and every entry IN DEFAULT_MODELS was verified with a real
# chat/completions call through its rotator - a 200 from /v1/models is not
# evidence, since dead keys and paid-only models both answer it. Specs the
# REGISTRY contributes rest on the registry's own probing rather than on that
# pass: `tokenrouter` in particular is wired as an endpoint for explicit,
# low-volume use and holds no verified translation seat here.
REGISTRY_DEFAULT = os.path.expanduser('~/free-ai-registry/registry.json')


def registry_path():
    """Resolved at CALL time, so a late env override is not inert."""
    return os.environ.get('HANPATCH_MODEL_REGISTRY') or REGISTRY_DEFAULT

# Ordered by observed Korean-literary quality; every entry is a free tier.
DEFAULT_MODELS = [
    'groq:openai/gpt-oss-120b',
    'nimproxy:google/gemma-4-31b-it',
    'opencode:nemotron-3-ultra-free',
    'opencode:mimo-v2.5-free',
    'opencode:deepseek-v4-flash-free',
    'groq:llama-3.3-70b-versatile',
    'openrouter:nvidia/nemotron-3-ultra-550b-a55b:free',
]

# Verified dead or unusable on 2026-07-31, kept as a record so they are not
# reintroduced from memory:
#   nimproxy:qwen/qwen3.5-397b-a17b   HTTP 410 Gone - model withdrawn
#   nimproxy:moonshotai/kimi-k2.6     HTTP 404 - function id no longer routed
#   nimproxy:z-ai/glm-5.2             no first byte in 90s
#   opencode:deepseek-v4-flash        refused: not in the zen free tier; the
#                                     free spec keeps its '-free' suffix
#   nimproxy:deepseek-ai/deepseek-v4-pro  answers, but the slug is the PAID
#                                     identity on api.deepseek.com and the
#                                     registry grants it no role, so it stays out
#                                     of a free pool on purpose


def registry_models(role='batch_translation', path=None, states=('ok',)):
    """Free specs the registry grants `role`, restricted to verified states.

    An ABSENT registry returns [] so a host without one falls back to the pins.
    A registry that is present but malformed fails loudly instead: silently
    treating a corrupt SSOT as "no models" would look identical to a deliberate
    configuration, and the caller would fall back to pins the registry may have
    already retired.
    """
    path = path or registry_path()
    if not os.path.exists(path):
        return []
    payload = config.load_object(path, 'the free-model registry').get('payload')
    if not isinstance(payload, dict):
        raise SystemExit(f'the free-model registry has no "payload" object: {path}')
    out = []
    for spec, meta in (payload.get('models') or {}).items():
        if not isinstance(meta, dict):
            continue
        if role not in (meta.get('roles_allowed') or []):
            continue
        if states and meta.get('state') not in states:
            continue
        if spec.split(':', 1)[0] in ENDPOINTS:
            out.append(spec)
    return out


class CodexProvider:
    """One Codex CLI account, driven as a chat provider.

    Codex has no local HTTP rotator, so this shells out to `codex exec`. Three things had
    to be measured rather than assumed:

      - Reasoning effort. The account default is `xhigh`, which is wrong for translation.
        `minimal` is REJECTED by this model (supported: none, low, medium, high, xhigh,
        max). `none` finished a 3-account parallel batch in 11s but one account returned
        1 of 8 strings. `low` took 10s and returned 24 of 24 with every line count intact.
        One second buys the reliability, so `low` it is.
      - Reply extraction. `codex exec` ECHOES the prompt before answering, and the prompt
        contains a JSON object with the SAME keys as the answer, so a naive parse scored
        the echo as the reply and reported the source text back as a translation. Scraping
        for the last `codex` banner worked but was guesswork about console formatting;
        `--output-last-message FILE` hands over exactly the final message, so the scraper
        is now only the fallback for a run that produced no file.
      - Session files. Parallel `codex exec` invocations under one CODEX_HOME write session
        state to the same place, and the upstream guidance for parallel use is
        `--ephemeral` for precisely that reason. Nothing is resumed here, so persisting
        sessions buys contention and nothing else.
      - stdin. Without `< /dev/null` the CLI waits for more input forever, and without
        `--skip-git-repo-check` it refuses to run outside a trusted directory.
    """

    def __init__(self, home, model='gpt-5.6-luna', effort='low', cwd=None, timeout=400):
        self.home = home
        self.model = model
        self.effort = effort
        self.cwd = cwd or os.getcwd()
        self.timeout = timeout
        self.id = f'codex{os.path.basename(home)}:{model}'
        self.base = f'codex-exec:{home}'
        self.calls = 0
        self.errors = 0
        self.parked_until = 0.0

    @staticmethod
    def reply_of(raw):
        i = raw.rfind('\ncodex\n')
        seg = raw[i + 7:] if i >= 0 else raw
        j = seg.find('tokens used')
        return seg[:j] if j >= 0 else seg

    def chat(self, system, user, temperature=0.2, max_tokens=None, json_mode=None):
        import subprocess
        import tempfile
        env = dict(os.environ, CODEX_HOME=self.home)
        fd, last = tempfile.mkstemp(prefix='codex-last-', suffix='.txt')
        os.close(fd)
        argv = ['codex', 'exec', '--skip-git-repo-check', '--ephemeral',
                '--model', self.model,
                '-c', f'model_reasoning_effort={self.effort}',
                '--output-last-message', last,
                f'{system}\n\n{user}']
        self.calls += 1
        try:
            r = subprocess.run(argv, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=self.timeout)
        except Exception as e:
            self.errors += 1
            raise RuntimeError(f'{self.id} {type(e).__name__}: {e}') from None
        finally:
            try:
                final = open(last, encoding='utf-8').read()
            except OSError:
                final = ''
            try:
                os.unlink(last)
            except OSError:
                pass
        out = (r.stdout or '') + (r.stderr or '')
        if final.strip():
            return final
        if r.returncode != 0:
            self.errors += 1
            raise RuntimeError(f'{self.id} exit {r.returncode}: {out[-200:]}')
        return self.reply_of(out)


def make(spec, **kw):
    prov, model = spec.split(':', 1)
    if prov.startswith('codex'):
        acct = prov[len('codex'):] or '1'
        home = f'/root/.codex-accounts/{acct}'
        if not os.path.isdir(home):
            return None
        return CodexProvider(home, model, **kw)
    base, keyvar, rpm = ENDPOINTS[prov]
    key = os.environ.get(keyvar) if keyvar else None
    if keyvar and not key:
        return None
    return Provider(prov, base, key, model, rpm=kw.pop('rpm', rpm), **kw)


_RETRY_RE = re.compile(r'retry in ([0-9]+(?:\.[0-9]+)?)s')


def _retry_after(headers, detail, default=20.0):
    """How long a 429 says to wait, from the header or the rotator's own message.

    The local rotators phrase it in the body ('all 4 Groq key(s) are parked; retry in
    34s') and some also send Retry-After. Reading both means a park is honoured whichever
    form it arrives in, and the default is deliberately not zero - a 429 with no number
    still means stop.
    """
    try:
        ra = headers.get('Retry-After') if headers else None
        if ra:
            return max(1.0, float(ra))
    except (TypeError, ValueError):
        pass
    m = _RETRY_RE.search(detail or '')
    if m:
        return max(1.0, float(m.group(1)))
    return default


# How many requests one ENDPOINT may have in flight. Concurrency is not a property of the
# worker count: a rotator fronting N keys with a per-minute cap saturates at a low number
# no matter how many workers exist, while a CLI account that spawns its own process takes
# several. Published guidance for parallel Codex use lands at three to six sessions per
# account, and the free rotators are rate-limited per minute rather than per connection,
# so one in flight each is the honest setting - a second only produces a park response.
CONCURRENCY = {
    'nimproxy': 1,
    'opencode': 1,
    'openrouter': 1,
    'tokenrouter': 1,
    'groq': 1,
    'agy': 1,
    # No local rotator, no per-minute free cap - the ceiling is the account's own rate
    # limit, so this endpoint is the one that scales with concurrency.
    'deepseek': int(os.environ.get('HANPATCH_DEEPSEEK_CONCURRENCY', '10')),
}
CODEX_CONCURRENCY = int(os.environ.get('HANPATCH_CODEX_CONCURRENCY', '6'))
_GATES = {}
_GATES_LOCK = threading.Lock()


def gate_for(prov_id):
    """A semaphore shared by every provider on the same endpoint."""
    key = prov_id.split(':', 1)[0]
    with _GATES_LOCK:
        g = _GATES.get(key)
        if g is None:
            n = (CODEX_CONCURRENCY if key.startswith('codex')
                 else CONCURRENCY.get(key, 1))
            g = threading.BoundedSemaphore(max(1, n))
            _GATES[key] = g
        return g


def available(pool, now=None):
    """The providers not currently parked, in seat order."""
    now = now if now is not None else time.time()
    return [p for p in pool if p.parked_until <= now]


# Published prices per million tokens, as (cache-miss input, cache-hit input, output).
# Recorded rather than derived: an API does not report what it charges, only what it used.
PRICES = {
    'deepseek-v4-flash': (0.14, 0.0028, 0.28),
    'deepseek-v4-pro': (0.435, 0.003625, 0.87),
}
LEDGER = os.environ.get('HANPATCH_COST_LEDGER', '')
_LEDGER_LOCK = threading.Lock()


def _account(name, model, usage):
    """Append real token usage to the cost ledger. Free endpoints are skipped.

    The point is to report what was actually BILLED rather than an estimate: the API
    returns the cache hit/miss split per call, and on this workload the split is the whole
    story - a measured call showed 1408 of 1465 prompt tokens served from cache, which is
    priced at 1/50 of a miss. An estimate built from prompt length alone would have been
    wrong by an order of magnitude in the safe direction, and wrong is wrong.
    """
    if not (LEDGER and usage and name in ('deepseek',)):
        return
    hit = int(usage.get('prompt_cache_hit_tokens') or 0)
    miss = int(usage.get('prompt_cache_miss_tokens')
               or max(0, int(usage.get('prompt_tokens') or 0) - hit))
    out = int(usage.get('completion_tokens') or 0)
    row = {'model': model, 'hit': hit, 'miss': miss, 'out': out}
    try:
        with _LEDGER_LOCK, open(LEDGER, 'a') as fh:
            fh.write(json.dumps(row) + '\n')
    except OSError:
        pass


def cost_of(path=None):
    """(total USD, token totals, call count) from the ledger."""
    path = path or LEDGER
    tot = {'hit': 0, 'miss': 0, 'out': 0}
    usd = 0.0
    calls = 0
    if not (path and os.path.exists(path)):
        return 0.0, tot, 0
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        price = PRICES.get(r.get('model'))
        if not price:
            continue
        miss_p, hit_p, out_p = price
        usd += (r['miss'] * miss_p + r['hit'] * hit_p + r['out'] * out_p) / 1e6
        for k in tot:
            tot[k] += r.get(k, 0)
        calls += 1
    return usd, tot, calls


def interleave(pool):
    """Order the pool so consecutive seats land on DIFFERENT endpoints.

    Capacity is per ENDPOINT, not per model: a rotator owns the keys, so two models
    served by the same rotator share one budget. A pool listed as
    [groq:a, groq:b, nim:c, zen:d] gives seats 0 and 1 to the same four Groq keys, and a
    run with four workers then reports 'all 4 Groq key(s) are parked; retry in 34s' while
    the other two endpoints sit idle - measured, 87 park responses in one 60-string run.
    Round-robin by endpoint makes the seat index spread real work across rotators.
    """
    groups = {}
    for p in pool:
        groups.setdefault(p.id.split(':', 1)[0], []).append(p)
    out = []
    while any(groups.values()):
        for key in list(groups):
            if groups[key]:
                out.append(groups[key].pop(0))
    return out


def build_pool(models=None, role='batch_translation'):
    """Build the rotation pool: explicit list, else the registry, else the pins.

    Preferring the registry means a model that loses its free tier or fails
    probing drops out of the pool without an edit here. Preferring the PINS over
    an empty registry answer would be worse than failing, because it would keep
    calling specs the registry has already retired.
    """
    load_dotenv()
    specs = models or registry_models(role) or DEFAULT_MODELS
    pool = interleave([p for p in (make(s) for s in specs) if p])
    if not pool:
        raise SystemExit(
            'no usable provider found. Every endpoint is a local rotator, so an '
            'empty pool usually means the rotators are not running: check '
            'curl -s localhost:18090/health, :18092/health, :18094/health, '
            ':18096/health and :18095/status')
    return pool

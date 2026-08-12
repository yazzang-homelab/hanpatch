"""Provider pool with per-endpoint rate limiting and rotation.

Any OpenAI-compatible base URL works. The bundled table is free-tier endpoints
plus local proxies; add your own to ENDPOINTS. Keys are never stored here — they
come from the environment, or from dotenv files listed in HANPATCH_ENV
(colon-separated) or the defaults below.

Rotation is the point: a shard that fails validation is retried on a *different*
provider, so one model's systematic blind spot does not become the shipped text.
"""
import json
import math
import os
import re
import tempfile
import sys
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


# How many consecutive non-429 failures retire a lane for the rest of the run.
# Three, not one: a single unparseable answer or a transient upstream 500 is
# normal and the batch retry already covers it. Three in a row on one endpoint,
# with other endpoints answering, is a broken lane rather than bad luck.
LANE_ERROR_LIMIT = int(os.environ.get('HANPATCH_LANE_ERROR_LIMIT', '3'))


def note_ok(prov):
    """One success clears the streak. Health is CONSECUTIVE failures, not total.

    A lane that fails one call in five is usable and must stay in rotation; a
    lane that fails five in a row is not, whatever its lifetime ratio looks like.
    """
    prov.consecutive_errors = 0


def note_error(prov, detail, retriable=False):
    """Count a failure against the lane, and retire it once it is clearly dead.

    `retriable` is for 429 and other self-clearing conditions: those are handled
    by parked_until, which carries the upstream's own wait, so counting them
    would retire a lane for being BUSY - the opposite of what the pool needs
    when it is trying to finish on whatever capacity is left.
    """
    if retriable:
        return
    prov.consecutive_errors = getattr(prov, 'consecutive_errors', 0) + 1
    if prov.consecutive_errors >= LANE_ERROR_LIMIT and not prov.disabled_reason:
        prov.disabled_reason = (f'{prov.consecutive_errors} consecutive failures, '
                                f'last: {str(detail)[:160]}')
        sys.stderr.write(f'lane {prov.id} retired for this run: '
                         f'{prov.disabled_reason}\n')
        sys.stderr.flush()


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
        # A park is TEMPORARY and says so. A lane can also be permanently broken -
        # a Codex account past its usage limit, a reseller key out of credit, a
        # revoked project - and those look identical to the scheduler: every seat
        # keeps being handed out, every attempt spends a timeout, and a run that
        # should have finished on the surviving lanes crawls or fails instead. One
        # Codex account hitting its limit is exactly how twelve hours were lost
        # once, with two healthy lanes live the whole time.
        #
        # So consecutive NON-429 failures are counted, and past the threshold the
        # lane is taken out of rotation for the rest of the process. 429 does not
        # count: it already has a precise, self-clearing mechanism above, and a
        # busy lane is not a broken one.
        self.consecutive_errors = 0
        self.disabled_reason = None

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
        if self.name in ('deepseek', 'a6'):
            # Reasoning OFF is load-bearing on both the official and reseller
            # DeepSeek routes. Live A6 DOE: one short DQ7 line consumed all 1,024
            # completion tokens as hidden reasoning and returned empty content;
            # `reasoning_effort=none` returned valid JSON in 77 completion tokens.
            # The reseller charges reasoning at its full $1/M output rate.
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
            note_error(self, f'HTTP {e.code}: {detail}', retriable=(e.code == 429))
            raise RuntimeError(f'{self.id} HTTP {e.code}: {detail}') from None
        except Exception as e:
            self.errors += 1
            note_error(self, f'{type(e).__name__}: {e}')
            raise RuntimeError(f'{self.id} {type(e).__name__}: {e}') from None
        _account(self.name, self.model, body.get('usage'))
        if 'error' in body and 'choices' not in body:
            self.errors += 1
            note_error(self, str(body['error'])[:200])
            raise RuntimeError(f'{self.id} error: {str(body["error"])[:300]}')
        msg = body['choices'][0]['message']
        text = msg.get('content') or msg.get('reasoning_content') or ''
        # An empty 200 is a failure, not a success: it is what a thinking model
        # returns when the budget was spent before the answer started, and
        # counting it as healthy keeps a lane in rotation that produces nothing.
        if text.strip():
            note_ok(self)
        else:
            note_error(self, 'HTTP 200 with empty content')
        return text


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
    # Google AI Studio free tier, rotated PER PROJECT. Google states plainly that
    # "rate limits are applied per project, not per API key", so this lane scales
    # only by adding keys from DISTINCT Google accounts - stacking keys on one
    # account buys literally nothing and the rotator's /status says `projects`
    # for that reason. Each free project carries its own daily request quota,
    # which is why this is the only free lane whose ceiling grows linearly with
    # accounts instead of being fixed by a shared pool. The quota figure is a
    # per-MODEL number the rotator holds as a local guard; the slug that used to
    # be quoted here (gemma-3-27b-it) has since been retired upstream and
    # returned HTTP 404 for every key, so no model is named in this comment.
    #
    # Measured 2026-08-10 with 3 usable projects, hanpatch's real DQ7 prompt on
    # gemma-4-31b-it, all requests through this rotator:
    #   throughput scales flat to C=32 - 2.8 successful calls/min at C=1, 22.6
    #   at C=8, 79.1 at C=32, with p50 latency unchanged (~17s) and zero
    #   failures in 120 calls, so the ceiling is not this endpoint
    #   batch size is the real limit: 10 rows take 65-81s and 20 rows 111s, but
    #   30 rows exceed the rotator's 180s first-byte timeout and come back 504
    #
    # `rpm` here is NOT a hint. Provider._throttle holds a lock and sleeps
    # 60/rpm between calls, so it is a hard ceiling for the whole endpoint no
    # matter how many workers exist: at the 25 this used to carry, the lane could
    # not exceed 25 calls/min even with 32 in flight - a third of what the same
    # three projects were measured delivering. It is derived from the seat count
    # instead, so it tracks the pool instead of contradicting it.
    'gemini':     ('http://127.0.0.1:18097/v1', None, None),
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
    # RETIRED 2026-08-10 by operator decision: the direct DeepSeek account is prepaid
    # and will not be topped up again. Removing the endpoint - rather than leaving it
    # and trusting every pool string to omit it - is what makes the spend impossible:
    # a stale `--models deepseek:...` in a script or a service unit now fails at pool
    # build instead of quietly billing. The same model stays reachable through `a6`
    # and `nimproxy`, so nothing is lost but the payment path.
    # 'deepseek': ('https://api.deepseek.com/v1', 'DEEPSEEK_API_KEY', 60),
    # PAID, uniform-rate reseller pool ("짱쫀쿠"). Same explicit-only rule as `deepseek`:
    # it carries a key variable, so a registry role or a default pool can never reach it.
    #
    # It prices every ordinary text model at one ratio, and that ratio is MEASURED,
    # never read off a field name. Metered 2026-08-11 against the billing endpoint:
    # 61,764 tokens moved `total_usage` by 0.0148 cents, i.e. $0.0000024 per 1K, or
    # $0.0024 per 1M tokens. A 5,374-token control call gave $0.0026/M - the same
    # number at coarser resolution.
    #
    # It was recorded here as $1.00/M for weeks, which is 417x too high and made
    # every cost comparison against other endpoints come out backwards. That figure
    # came from misreading `a6_key_set`'s `remaining_tokens_at_1usd_per_mtok`, which
    # is a hypothetical projection AT $1/M, not a rate this endpoint charges. Note
    # that `total_usage` is reported in CENTS while `used_usd` is dollars; reading
    # one as the other is a 100x error on its own.
    #
    # The rate is a property of the key's supplier GROUP, not of the endpoint:
    # `/api/pricing` returns `usable_group` per key, and a key entitled to only
    # `default` cannot reach the cheaper marketplace groups. Re-measure after every
    # key swap rather than carrying this number forward.
    #
    # `cache_ratio=1` means a reported cache hit costs exactly a
    # miss, so model choice is based on measured quality and output length only.
    # Live DQ7 DOE with reasoning disabled measured 206.83 tokens/unit at B=1,
    # 84.0 at B=8, and 74.31 at B=16: the 16-item batch cuts cost 2.78x. C=2
    # completed only 1/2 calls versus 2/2 at C=1, so this endpoint does NOT scale
    # safely with workers under the current token and supplier pool.
    'a6':         ('https://a6.a6api.com/v1', 'A6_API_KEY', 5),
}


# ---------------------------------------------------------------------------
# The Google lane sizes itself from the number of PROJECTS the rotator can
# actually use, because that is the only quantity its throughput depends on.
# ---------------------------------------------------------------------------
# 30 requests per minute per project per model. Not a guess and not from the
# docs: it is the `quotaValue` Google returns in the live free-tier 429, next to
# quotaId GenerateRequestsPerMinutePerProjectPerModel-FreeTier. Confirmed on
# every seat 2026-08-10 via `gemini-key-rotator.py --tier`.
GEMINI_RPM_PER_PROJECT = int(os.environ.get('HANPATCH_GEMINI_RPM_PER_PROJECT', '30'))
# Measured p50 for a 10-row DQ7 batch through the rotator, stable from C=1 to
# C=32. Needed because the worker count that saturates a rate limit is
# rpm * latency / 60, not the number of seats: three seats at 17s need ~23
# requests in flight to keep 81/min moving, and the old setting of 1 delivered
# 3.5/min.
GEMINI_P50_S = float(os.environ.get('HANPATCH_GEMINI_P50_S', '17'))
# Aim just under the ceiling. Measured best was 79.1 successful calls/min on 3
# seats against a theoretical 90, so 0.9 is not a arbitrary safety factor - it is
# where the endpoint actually tops out once per-request jitter is included.
GEMINI_HEADROOM = float(os.environ.get('HANPATCH_GEMINI_HEADROOM', '0.9'))

_SEATS = {}


def gemini_seats():
    """Usable Google projects, asked of the rotator rather than assumed.

    A hard-coded seat count is wrong in both directions. Too low throws away
    quota that was paid for in accounts; too high means every extra request is a
    certain 429 that parks a project and spends an attempt. The rotator already
    tracks this exactly - it quarantines a revoked project (403 PERMISSION_DENIED)
    and drops one that hit its daily cap - and reports the survivors as
    `usable`, so that is the number to schedule against.

    Cached for the life of the process: the pool is built once per run, and a
    seat count that changed mid-run would not resize the throttle anyway.
    """
    override = os.environ.get('HANPATCH_GEMINI_SEATS')
    if override:
        return max(1, int(override))
    if 'n' in _SEATS:
        return _SEATS['n']
    seats = 1
    try:
        base = ENDPOINTS['gemini'][0].rsplit('/v1', 1)[0]
        with urllib.request.urlopen(f'{base}/status', timeout=3) as r:
            # A rotator answer is an external document, so it gets the same treatment as
            # any other: a bounded read and a shape check before anything is believed.
            # Unbounded `json.load` on a socket lets a wedged or hostile endpoint decide
            # how much memory this process spends, and a non-dict body would raise
            # AttributeError from `.get` below - inside a bare `except`, that reads as
            # "rotator unreachable" and silently pins the throttle to one seat.
            body = json.loads(r.read(64 * 1024).decode('utf-8', 'replace'))
        if not isinstance(body, dict):
            raise ValueError(f'{base}/status did not return a JSON object')
        rot = body.get('rotator')
        if not isinstance(rot, dict):
            rot = {}
        # `usable` excludes permanently disabled projects; `active` also excludes
        # ones parked this minute, which is transient and must not shrink the
        # throttle for the whole run.
        seats = int(rot.get('usable') or rot.get('projects') or 1)
    except Exception:                                    # noqa: BLE001
        # An unreachable rotator is not a reason to guess high: one seat behaves
        # like every other free lane and the pool degrades instead of failing.
        seats = 1
    _SEATS['n'] = max(1, seats)
    return _SEATS['n']


def gemini_rpm():
    return max(1, int(gemini_seats() * GEMINI_RPM_PER_PROJECT * GEMINI_HEADROOM))


def gemini_concurrency():
    """Requests in flight needed to keep the rate limit, not the seat count.

    Capped at the rpm itself: past that, a worker cannot get a turn from
    Provider._throttle and only occupies a slot another lane could use.
    """
    want = math.ceil(gemini_rpm() * GEMINI_P50_S / 60.0)


    return max(1, min(want, gemini_rpm()))
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
#
# `gemini:gemma-4-31b-it` is FIRST because it is the only entry whose quality was
# measured against the two references that decide whether a lane may carry bulk
# volume, rather than against another free lane. Blind 3-arm panel on 2026-08-10,
# 90 stratified DQ7 rows, judges claudelee:sonnet + claudejung:sonnet +
# claudekuk:opus (none of them a producer, so hanpatch's own independence rule
# holds), hanpatch's own translator prompt and judge rubric:
#
#   arm                       pooled defect   adequacy   fluency   translate.check
#   shipped corpus (control)          0.073      4.874     4.846          0/90
#   gemini:gemma-4-31b-it             0.094      4.867     4.820          0/90
#   codex1:gpt-5.6-luna (low)         0.158      4.796     4.646          3/90
#
#   paired, row-level majority (McNemar exact):
#     gemma vs luna     only-gemma 4, only-luna 11, p=0.12  -> at least parity
#     gemma vs shipped  only-gemma 4, only-shipped 4, p=1.0 -> indistinguishable
#
# Read that honestly: gemma is NOT proven better than luna (p=0.12), it is proven
# not worse, and it is indistinguishable from the corpus that already shipped.
# The mechanical column is the one signal that is judge-free: luna left kana in
# two translations and broke register in one, gemma and the shipped corpus broke
# nothing. The ordering against the OTHER free entries below is unmeasured and
# inherited.
#
# `nimproxy:google/gemma-4-31b-it` is the SAME model on a different rotator and
# stays listed as a fallback, not as a duplicate: the nimproxy path has a
# recorded HTTP 500 on this model (see the response_format note above), and it
# does not scale by adding Google accounts the way the `gemini` lane does.
DEFAULT_MODELS = [
    'gemini:gemma-4-31b-it',
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
        endpoint = spec.split(':', 1)[0]
        if endpoint not in ENDPOINTS:
            continue
        # A PAID endpoint must never enter a pool by role. The registry describes
        # free tiers; an entry naming a keyed endpoint is either a mistake or a
        # compromise, and admitting it would put bulk volume on a metered lane
        # without a caller ever asking for it.
        if ENDPOINTS[endpoint][1]:
            continue
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
        self.consecutive_errors = 0
        self.disabled_reason = None

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
            note_error(self, f'{type(e).__name__}: {e}')
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
            note_ok(self)
            return final
        if r.returncode != 0:
            self.errors += 1
            # A Codex account past its usage limit exits non-zero on every call
            # for hours. That is the failure this counter exists for.
            note_error(self, f'exit {r.returncode}: {out[-160:]}')
            raise RuntimeError(f'{self.id} exit {r.returncode}: {out[-200:]}')
        reply = self.reply_of(out)
        if reply.strip():
            note_ok(self)
        else:
            note_error(self, f'no message: {out[-160:]}')
        return reply


class ClaudeProvider:
    """One Claude subscription account, driven through `claude -p`.

    Same shape as `CodexProvider` and for the same reason: the account is flat-rate, so a
    panel widens by adding an account instead of by spending. Three details are not obvious.

      - Auth precedence. `ANTHROPIC_API_KEY` (and friends) take precedence over the claude.ai
        login and turn every call into `401 Invalid bearer token`. They are stripped from the
        child environment, because a metered key silently replacing a subscription is both a
        wrong-identity bug and a billing one.
      - Reply extraction. `--output-format json` returns a result envelope whose `result`
        field is exactly the final message, so there is no console scraping and no risk of
        reading the echoed prompt back as an answer.
      - Model naming. The model is passed explicitly rather than left to the account default,
        because the judge identity recorded in a verdict has to name the model that answered;
        two accounts defaulting differently would record one name for two models.
    """

    STRIP_ENV = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                 'ANTHROPIC_MODEL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX')

    def __init__(self, home, model='sonnet', cwd=None, timeout=400):
        self.home = home
        self.model = model
        self.cwd = cwd or tempfile.gettempdir()
        self.timeout = timeout
        self.id = f'claude{os.path.basename(home)}:{model}'
        self.base = f'claude-cli:{home}'
        self.calls = 0
        self.errors = 0
        self.parked_until = 0.0
        self.consecutive_errors = 0
        self.disabled_reason = None

    @staticmethod
    def reply_of(raw):
        """The final message, from the JSON envelope when present and the raw text otherwise."""
        try:
            doc = json.loads(raw)
        except (ValueError, TypeError):
            return raw
        if isinstance(doc, dict) and isinstance(doc.get('result'), str):
            return doc['result']
        return raw

    def chat(self, system, user, temperature=0.2, max_tokens=None, json_mode=None):
        import subprocess
        env = {k: v for k, v in os.environ.items() if k not in self.STRIP_ENV}
        env['CLAUDE_CONFIG_DIR'] = self.home
        argv = ['claude', '-p', '--model', self.model, '--output-format', 'json',
                f'{system}\n\n{user}']
        self.calls += 1
        try:
            r = subprocess.run(argv, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=self.timeout)
        except Exception as e:
            self.errors += 1
            note_error(self, f'{type(e).__name__}: {e}')
            raise RuntimeError(f'{self.id} {type(e).__name__}: {e}') from None
        out = (r.stdout or '') + (r.stderr or '')
        if r.returncode != 0:
            self.errors += 1
            note_error(self, f'exit {r.returncode}: {out[-160:]}')
            raise RuntimeError(f'{self.id} exit {r.returncode}: {out[-200:]}')
        reply = self.reply_of(r.stdout or '')
        if not reply.strip():
            self.errors += 1
            note_error(self, f'no message: {out[-160:]}')
            raise RuntimeError(f'{self.id} returned no message: {out[-200:]}')
        note_ok(self)
        return reply


CLAUDE_ACCOUNTS_DIR = '/root/.claude-accounts'


def claude_accounts():
    """Authenticated Claude account directories, in stable order.

    On Linux and Windows Claude Code stores the login in ``.credentials.json``. An unauthenticated
    directory cannot answer a liveness probe and can take the CLI's full subprocess timeout, so
    skip it before constructing a lane. Quota exhaustion is deliberately not inferred here: an
    authenticated but exhausted account still needs to remain discoverable for a later run after
    its subscription resets, and the ordinary liveness probe handles that state.
    """
    try:
        names = os.listdir(CLAUDE_ACCOUNTS_DIR)
    except OSError:
        return []
    return sorted(n for n in names
                  if os.path.isfile(os.path.join(CLAUDE_ACCOUNTS_DIR, n,
                                                 '.credentials.json')))

CODEX_ACCOUNTS_DIR = '/root/.codex-accounts'


def codex_accounts():
    """Every Codex account present on this machine, in stable order.

    Discovery rather than a hardcoded list: an account is an independent identity, so adding
    one is the only way to widen a judge panel that must not reuse a judge, and a panel that
    ignores a present account silently caps itself.
    """
    try:
        names = os.listdir(CODEX_ACCOUNTS_DIR)
    except OSError:
        return []
    return sorted(n for n in names
                  if os.path.isdir(os.path.join(CODEX_ACCOUNTS_DIR, n)))


def make(spec, **kw):
    prov, model = spec.split(':', 1)
    if prov.startswith('claude'):
        acct = prov[len('claude'):] or 'default'
        home = os.path.join(CLAUDE_ACCOUNTS_DIR, acct)
        if not os.path.isdir(home):
            return None
        return ClaudeProvider(home, model, **kw)
    if prov.startswith('codex'):
        acct = prov[len('codex'):] or '1'
        home = os.path.join(CODEX_ACCOUNTS_DIR, acct)
        if not os.path.isdir(home):
            return None
        return CodexProvider(home, model, **kw)
    base, keyvar, rpm = ENDPOINTS[prov]
    key = os.environ.get(keyvar) if keyvar else None
    if keyvar and not key:
        return None
    if rpm is None:
        # A None in the table means "this endpoint's ceiling is not a constant".
        # Only the Google lane is like that: its limit is per PROJECT, and the
        # project count changes when an account is revoked or exhausts its day.
        rpm = gemini_rpm() if prov == 'gemini' else 20
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
    # Unlike the other free rotators, this ceiling is NOT fixed: quota is per Google
    # project, so N accounts means N independent 30 RPM / 14,400 RPD buckets and the
    # honest setting is one request in flight per account.
    #
    # The Google lane is the exception in BOTH directions and the old advice here
    # ("raise it to the number of accounts, never higher") was wrong. A rate limit
    # is saturated by rpm * latency / 60 requests in flight, not by one per seat:
    # three seats allow 81/min at a measured 17s p50, which needs ~23 concurrent,
    # and one-per-seat delivered 10/min. Measured on the real endpoint: 2.8
    # calls/min at C=1, 22.6 at C=8, 33.9 at C=16, 68.8 at C=24, 79.1 at C=32,
    # p50 flat throughout and zero failures - extra workers did NOT produce parks,
    # because Provider._throttle admits them at the endpoint's rate regardless.
    # Derived so it shrinks with the pool when a project is revoked.
    'gemini': int(os.environ.get('HANPATCH_GEMINI_CONCURRENCY', '0')) or None,
    # Two billing accounts behind one local gateway process, and the QA panel drives it hard:
    # measured 3-12s per call, so one lane in flight makes a corpus-scale panel take days.
    # Overridable because the ceiling belongs to the gateway, not to this table.
    'agy': int(os.environ.get('HANPATCH_AGY_CONCURRENCY', '6')),
    # No local rotator, no per-minute free cap - the ceiling is the account's own rate
    # limit, so this endpoint is the one that scales with concurrency.
    'deepseek': int(os.environ.get('HANPATCH_DEEPSEEK_CONCURRENCY', '10')),
    # Live A6 DOE (2026-08-07): C=1 completed 2/2 calls; C=2 completed only 1/2.
    # The failure was not a 429, so adding workers reduced both reliability and
    # useful throughput. Keep one request in flight until a later DOE disproves it.
    'a6': int(os.environ.get('HANPATCH_A6_CONCURRENCY', '1')),
}
CODEX_CONCURRENCY = int(os.environ.get('HANPATCH_CODEX_CONCURRENCY', '6'))
# Per ACCOUNT, like Codex: the gate key is the lane prefix, so each Claude account holds its own
# semaphore. Measured 3-6s per `claude -p` call on this box, so a few in flight per account keeps
# a panel moving without asking one subscription to behave like a fleet.
CLAUDE_CONCURRENCY = int(os.environ.get('HANPATCH_CLAUDE_CONCURRENCY', '3'))
_GATES = {}
_GATES_LOCK = threading.Lock()


def gate_for(prov_id):
    """A semaphore shared by every provider on the same endpoint."""
    key = prov_id.split(':', 1)[0]
    with _GATES_LOCK:
        g = _GATES.get(key)
        if g is None:
            n = (CLAUDE_CONCURRENCY if key.startswith('claude')
                 else CODEX_CONCURRENCY if key.startswith('codex')
                 else CONCURRENCY.get(key, 1))
            if n is None:
                # A None in CONCURRENCY means the endpoint computes its own, for
                # the same reason its rpm is not a constant.
                n = gemini_concurrency() if key == 'gemini' else 1
            g = threading.BoundedSemaphore(max(1, n))
            _GATES[key] = g
        return g


def live(pool):
    """Lanes that have not been retired for this run.

    Separate from `available` on purpose: a parked lane comes back on its own and
    is still part of the pool's capacity, while a retired one will not, so the two
    must not be collapsed. Callers that need to know whether the run can still
    finish ask this one.
    """
    return [p for p in pool if not getattr(p, 'disabled_reason', None)]


def available(pool, now=None):
    """The providers that can be asked right now, in seat order."""
    now = now if now is not None else time.time()
    return [p for p in live(pool) if p.parked_until <= now]


# Published prices per million tokens, as (cache-miss input, cache-hit input, output).
# Recorded rather than derived: an API does not report what it charges, only what it used.
PRICES = {
    'deepseek-v4-flash': (0.14, 0.0028, 0.28),
    'deepseek-v4-pro': (0.435, 0.003625, 0.87),
}

# The reseller bills every text model at one ratio, so its price is a property of the
# ENDPOINT, not of the model. The rate IS readable with an account, and reading it is the
# only honest way to set this: `/v1/dashboard/billing/usage` returns `total_usage` in
# CENTS, so metering is (usage_after - usage_before) / 100 / tokens.
#
# Measured 2026-08-11 on the `default` supplier group: 61,764 tokens for $0.000148, i.e.
# $0.0024 per 1M. A 5,374-token control call agreed at $0.0026/M.
#
# This is a DEFAULT, not a constant. `/api/pricing` shows the rate is a property of the
# key's `usable_group`, and the marketplace sells groups at different ratios, so a key
# swap can change it. Re-meter after every swap and override with the env var.
# Left at 0, the ledger still records tokens and `cost_of` reports no dollars for them -
# an unpriced row is honest, a guessed one is not.
A6_RATE = float(os.environ.get('HANPATCH_A6_USD_PER_MTOK', '0.0024') or 0)

# Endpoints whose price is uniform across models. A row from one of these is NEVER
# priced from PRICES, even when its model name appears there.
UNIFORM_RATE_ENDPOINTS = frozenset({'a6'})


def endpoint_price(endpoint):
    """Uniform per-1M rate for endpoints that do not price per model."""
    if endpoint == 'a6' and A6_RATE > 0:
        return (A6_RATE, A6_RATE, A6_RATE)
    return None

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
    if not (LEDGER and usage and name in ('deepseek', 'a6')):
        return
    hit = int(usage.get('prompt_cache_hit_tokens') or 0)
    miss = int(usage.get('prompt_cache_miss_tokens')
               or max(0, int(usage.get('prompt_tokens') or 0) - hit))
    out = int(usage.get('completion_tokens') or 0)
    if name == 'a6':
        # The reseller reports upstream cache hits, but its published
        # `cache_ratio=1` charges them exactly like misses. Fold them into miss so
        # the ledger reflects the balance rather than implying a nonexistent discount.
        miss, hit = miss + hit, 0
    row = {'model': model, 'endpoint': name, 'hit': hit, 'miss': miss, 'out': out}
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
        # Endpoint rate first: on a uniform-rate reseller the model name carries no
        # price. Falling back to PRICES there would bill `a6:deepseek-v4-flash` at
        # DeepSeek's rate - the same model name, a different vendor, a 7x error.
        endpoint = r.get('endpoint')
        if endpoint in UNIFORM_RATE_ENDPOINTS:
            price = endpoint_price(endpoint)
        else:
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


# Providers whose accounts are closed. Kept as a NAME rather than only as a deleted
# registry row so the refusal can say why, and so a grep for the old spec lands here.
RETIRED_PROVIDERS = ('deepseek',)


# Named pools, so a mixed run is one word instead of six specs typed by hand -
# and, more importantly, so the composition of a mixed run is recorded in the
# code rather than in whatever the last operator happened to paste.
#
# `all` deliberately spans three different KINDS of capacity, which is the whole
# point: they fail independently.
#   free        the local rotators. Free, rate-limited per minute, and the Google
#               lane grows with accounts.
#   metered     a6 (짱쫀쿠). Costs money per token, so it is only ever reachable
#               because it is named here explicitly - `registry_models` refuses to
#               admit a keyed endpoint by role, and that rule is not weakened.
#   subscription  codex1/2/3. Flat-rate, so throughput is free but each account
#               has a usage limit that ends the lane for hours when hit.
# A run on `all` survives losing any one kind: the free lanes cover a Codex
# usage-limit wall, Codex covers a free-tier daily exhaustion, and a6 covers the
# case where both are out.
POOL_ALIASES = {
    'free': None,          # None means "ask the registry by role", the default path
    'all': [
        'gemini:gemma-4-31b-it',
        'groq:openai/gpt-oss-120b',
        'a6:deepseek-v4-flash',
        'codex1:gpt-5.6-luna',
        'codex2:gpt-5.6-luna',
        'codex3:gpt-5.6-luna',
    ],
}


def expand_aliases(specs):
    """Turn alias names in a --models list into the specs they stand for."""
    if not specs:
        return specs
    out = []
    for spec in specs:
        name = str(spec).strip()
        if name in POOL_ALIASES:
            expansion = POOL_ALIASES[name]
            if expansion is None:
                out.extend(registry_models('batch_translation') or DEFAULT_MODELS)
            else:
                out.extend(expansion)
        elif name:
            out.append(name)
    # Preserve order, drop duplicates: 'all,free' should not build one endpoint twice.
    seen = set()
    return [s for s in out if not (s in seen or seen.add(s))]


def metered(pool):
    """Lanes in this pool that bill per token.

    Surfaced at build time rather than discovered on an invoice. A metered lane
    can only be here because a caller named it, but "I named it" and "I remember
    that I named it three hours ago" are different things.
    """
    # Keyed off the id prefix, not a `.name` attribute: CodexProvider and
    # ClaudeProvider do not have one, and reaching for it turned a cost warning
    # into an AttributeError that killed pool construction.
    return [p for p in pool
            if ENDPOINTS.get(p.id.split(':', 1)[0], (None, None, None))[1]]


def build_pool(models=None, role='batch_translation'):
    """Build the rotation pool: explicit list, else the registry, else the pins.

    Preferring the registry means a model that loses its free tier or fails
    probing drops out of the pool without an edit here. Preferring the PINS over
    an empty registry answer would be worse than failing, because it would keep
    calling specs the registry has already retired.
    """
    load_dotenv()
    specs = expand_aliases(models) or registry_models(role) or DEFAULT_MODELS
    # `make()` returns None for an unknown provider, so a retired lane would otherwise be
    # dropped in silence and the run would proceed with a smaller pool than was asked for -
    # the same class of silent shrink the QA pool refuses. Name it in the error.
    retired = [x for x in specs if str(x).split(':', 1)[0] in RETIRED_PROVIDERS]
    if retired:
        raise SystemExit(
            f'retired provider in pool {retired}: that account is closed and must not be '
            f'called. Substitutes for the same model: a6, nimproxy.')
    built = [(s, make(s)) for s in specs]
    pool = interleave([p for _, p in built if p])
    if not pool:
        raise SystemExit(
            'no usable provider found. Every endpoint is a local rotator, so an '
            'empty pool usually means the rotators are not running: check '
            'curl -s localhost:18090/health, :18092/health, :18094/health, '
            ':18096/health and :18095/status')
    # A spec that was ASKED FOR and could not be built is reported, not swallowed.
    # A missing key or a missing CLI account produces None from `make`, and a pool
    # that is quietly smaller than requested is how a mixed run ends up carrying
    # its whole volume on one lane: `a6:deepseek-v4-flash` builds only when
    # A6_API_KEY is in hanpatch's own dotenv, which it was not for a long time.
    absent = [s for s, p in built if not p]
    if absent and models:
        sys.stderr.write(
            f'pool: {len(pool)} lane(s) built; NOT built: {absent} '
            f'(missing credential or account - that capacity is not in this run)\n')
    priced = metered(pool)
    if priced:
        sys.stderr.write(
            f'pool: METERED lane(s) in this run: {[p.id for p in priced]} - '
            f'these bill per token\n')
    sys.stderr.flush()
    return pool

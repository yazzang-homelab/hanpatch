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
            raise RuntimeError(f'{self.id} HTTP {e.code}: '
                               f'{e.read()[:300].decode("utf-8", "replace")}') from None
        except Exception as e:
            self.errors += 1
            raise RuntimeError(f'{self.id} {type(e).__name__}: {e}') from None
        if 'error' in body and 'choices' not in body:
            self.errors += 1
            raise RuntimeError(f'{self.id} error: {str(body["error"])[:300]}')
        msg = body['choices'][0]['message']
        return msg.get('content') or msg.get('reasoning_content') or ''


ENDPOINTS = {
    'nim':        ('https://integrate.api.nvidia.com/v1', 'NVIDIA_NIM_API_KEY', 30),
    'nimproxy':   ('http://127.0.0.1:18090/v1', None, 30),
    'opencode':   ('http://127.0.0.1:18092/v1', None, 20),
    'groq':       ('https://api.groq.com/openai/v1', 'GROQ_API_KEY', 25),
    'groqproxy':  ('http://127.0.0.1:18091/v1', None, 25),
    'openrouter': ('https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY', 12),
}

# Ordered by observed Korean-literary quality; every entry is a free tier.
DEFAULT_MODELS = [
    'nimproxy:deepseek-ai/deepseek-v4-pro',
    'nimproxy:moonshotai/kimi-k2.6',
    'nimproxy:qwen/qwen3.5-397b-a17b',
    'nimproxy:z-ai/glm-5.2',
    'opencode:deepseek-v4-flash-free',
    'groq:openai/gpt-oss-120b',
    'openrouter:nvidia/nemotron-3-ultra-550b-a55b:free',
]


def make(spec, **kw):
    prov, model = spec.split(':', 1)
    base, keyvar, rpm = ENDPOINTS[prov]
    key = os.environ.get(keyvar) if keyvar else None
    if keyvar and not key:
        return None
    return Provider(prov, base, key, model, rpm=kw.pop('rpm', rpm), **kw)


def build_pool(models=None):
    load_dotenv()
    pool = [p for p in (make(s) for s in (models or DEFAULT_MODELS)) if p]
    if not pool:
        raise SystemExit('no usable provider found')
    return pool

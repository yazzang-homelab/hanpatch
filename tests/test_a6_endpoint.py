"""The uniform-rate reseller lane: reachability, accounting, concurrency."""
from __future__ import annotations

import importlib
import json

import pytest

from hanpatch import providers as P


# --- reachability: paid lanes are explicit-only ---------------------------

def test_a6_is_registered_as_a_keyed_endpoint():
    base, keyvar, rpm = P.ENDPOINTS['a6']
    assert base == 'https://a6.a6api.com/v1'
    assert keyvar == 'A6_API_KEY'
    assert rpm == 5


def test_every_keyed_endpoint_is_a_paid_lane():
    keyed = {n for n, (_, kv, _) in P.ENDPOINTS.items() if kv}
    assert keyed == {'deepseek', 'a6'}


def test_a6_is_not_in_the_free_default_pool():
    assert not any(spec.startswith('a6:') for spec in P.DEFAULT_MODELS)


def test_make_returns_none_without_a_key(monkeypatch):
    monkeypatch.delenv('A6_API_KEY', raising=False)
    assert P.make('a6:deepseek-v4-flash') is None


def test_make_builds_a_provider_with_a_key(monkeypatch):
    monkeypatch.setenv('A6_API_KEY', 'sk-test')
    prov = P.make('a6:deepseek-v4-flash')
    assert prov is not None
    assert prov.id == 'a6:deepseek-v4-flash'


def test_a6_provider_disables_reasoning(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({'choices': [{'message': {'content': '{}'}}]}).encode()

    seen = {}

    def open_request(request, timeout):
        seen.update(json.loads(request.data))
        return Response()

    monkeypatch.setattr(P.urllib.request, 'urlopen', open_request)
    provider = P.Provider('a6', 'https://a6.a6api.com/v1', 'sk-test',
                          'deepseek-v4-flash')
    provider.chat('system', 'user')
    assert seen['reasoning_effort'] == 'none'


def _registry(tmp_path, **specs):
    path = tmp_path / 'registry.json'
    path.write_text(json.dumps({'payload': {'models': {
        spec: {'state': 'ok', 'roles_allowed': ['batch_translation']}
        for spec in specs.get('specs', ())}}}), encoding='utf-8')
    return str(path)


def test_registry_role_cannot_reach_a_paid_endpoint(tmp_path, monkeypatch):
    path = _registry(tmp_path, specs=('a6:claude-opus-4-8',
                                      'deepseek:deepseek-v4-flash',
                                      'groq:openai/gpt-oss-120b'))
    monkeypatch.setenv('HANPATCH_MODEL_REGISTRY', path)
    monkeypatch.setenv('A6_API_KEY', 'sk-test')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'sk-test')
    assert P.registry_models('batch_translation') == ['groq:openai/gpt-oss-120b']


def test_paid_endpoints_stay_out_of_a_role_built_pool(tmp_path, monkeypatch):
    path = _registry(tmp_path, specs=('a6:claude-opus-4-8',))
    monkeypatch.setenv('HANPATCH_MODEL_REGISTRY', path)
    monkeypatch.setenv('A6_API_KEY', 'sk-test')
    pool = P.build_pool(role='batch_translation')
    assert not any(prov.id.startswith(('a6:', 'deepseek:')) for prov in pool)


def test_an_unknown_endpoint_in_the_registry_is_ignored(tmp_path, monkeypatch):
    path = _registry(tmp_path, specs=('nosuch:model-x', 'groq:openai/gpt-oss-120b'))
    monkeypatch.setenv('HANPATCH_MODEL_REGISTRY', path)
    assert P.registry_models('batch_translation') == ['groq:openai/gpt-oss-120b']


# --- concurrency ----------------------------------------------------------


def test_a6_concurrency_default_is_measured_safe_lane():
    assert P.CONCURRENCY['a6'] == 1


def test_a6_concurrency_is_env_overridable(monkeypatch):
    monkeypatch.setenv('HANPATCH_A6_CONCURRENCY', '24')
    reloaded = importlib.reload(P)
    try:
        assert reloaded.CONCURRENCY['a6'] == 24
    finally:
        monkeypatch.delenv('HANPATCH_A6_CONCURRENCY')
        importlib.reload(P)


def test_gate_is_shared_per_endpoint_not_per_model():
    assert P.gate_for('a6:claude-opus-4-8') is P.gate_for('a6:gpt-5.6-terra')
    assert P.gate_for('a6:x') is not P.gate_for('deepseek:x')


# --- accounting -----------------------------------------------------------

def ledger_rows(tmp_path, monkeypatch, endpoint, model, usage):
    path = tmp_path / 'cost.jsonl'
    monkeypatch.setattr(P, 'LEDGER', str(path))
    P._account(endpoint, model, usage)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_free_endpoints_are_not_accounted(tmp_path, monkeypatch):
    assert ledger_rows(tmp_path, monkeypatch, 'groq', 'x',
                       {'prompt_tokens': 100, 'completion_tokens': 10}) == []


def test_a6_rows_carry_the_endpoint(tmp_path, monkeypatch):
    rows = ledger_rows(tmp_path, monkeypatch, 'a6', 'claude-opus-4-8',
                       {'prompt_tokens': 1000, 'completion_tokens': 40})
    assert rows == [{'model': 'claude-opus-4-8', 'endpoint': 'a6',
                     'hit': 0, 'miss': 1000, 'out': 40}]


def test_a6_reported_cache_hits_are_billed_as_misses(tmp_path, monkeypatch):
    # A proxied upstream may echo a cache split the reseller does not bill on.
    rows = ledger_rows(tmp_path, monkeypatch, 'a6', 'deepseek-v4-flash',
                       {'prompt_tokens': 1100, 'prompt_cache_hit_tokens': 800,
                        'prompt_cache_miss_tokens': 300, 'completion_tokens': 40})
    assert rows[0]['hit'] == 0
    assert rows[0]['miss'] == 1100


def test_deepseek_rows_keep_their_cache_split(tmp_path, monkeypatch):
    rows = ledger_rows(tmp_path, monkeypatch, 'deepseek', 'deepseek-v4-flash',
                       {'prompt_cache_hit_tokens': 800,
                        'prompt_cache_miss_tokens': 300, 'completion_tokens': 40})
    assert (rows[0]['hit'], rows[0]['miss']) == (800, 300)
    assert rows[0]['endpoint'] == 'deepseek'


# --- pricing --------------------------------------------------------------

def test_unset_a6_rate_leaves_rows_unpriced(tmp_path, monkeypatch):
    path = tmp_path / 'cost.jsonl'
    path.write_text(json.dumps({'model': 'claude-opus-4-8', 'endpoint': 'a6',
                                'hit': 0, 'miss': 1_000_000, 'out': 0}) + '\n')
    monkeypatch.setattr(P, 'A6_RATE', 0.0)
    usd, tot, calls = P.cost_of(str(path))
    assert (usd, calls) == (0.0, 0)


def test_measured_a6_rate_prices_rows(tmp_path, monkeypatch):
    path = tmp_path / 'cost.jsonl'
    path.write_text(json.dumps({'model': 'claude-opus-4-8', 'endpoint': 'a6',
                                'hit': 0, 'miss': 2_000_000, 'out': 500_000}) + '\n')
    monkeypatch.setattr(P, 'A6_RATE', 0.5)
    usd, tot, calls = P.cost_of(str(path))
    assert usd == pytest.approx(1.25)
    assert calls == 1


def test_a6_row_is_never_priced_from_the_model_table(tmp_path, monkeypatch):
    # `a6:deepseek-v4-flash` shares a model name with the DeepSeek lane. Pricing it
    # from PRICES would understate the bill by roughly 7x.
    path = tmp_path / 'cost.jsonl'
    path.write_text(json.dumps({'model': 'deepseek-v4-flash', 'endpoint': 'a6',
                                'hit': 0, 'miss': 1_000_000, 'out': 0}) + '\n')
    monkeypatch.setattr(P, 'A6_RATE', 0.0)
    usd, _, calls = P.cost_of(str(path))
    assert (usd, calls) == (0.0, 0), 'unpriced beats wrongly priced'

    monkeypatch.setattr(P, 'A6_RATE', 1.0)
    usd, _, calls = P.cost_of(str(path))
    assert usd == pytest.approx(1.0)
    assert usd != pytest.approx(0.14), 'must not fall back to the DeepSeek rate'


def test_legacy_rows_without_an_endpoint_still_price(tmp_path, monkeypatch):
    # 64,171 historical rows predate the endpoint field.
    path = tmp_path / 'cost.jsonl'
    path.write_text(json.dumps({'model': 'deepseek-v4-flash',
                                'hit': 1408, 'miss': 57, 'out': 277}) + '\n')
    usd, _, calls = P.cost_of(str(path))
    assert calls == 1
    assert usd == pytest.approx((57 * 0.14 + 1408 * 0.0028 + 277 * 0.28) / 1e6)


def test_uniform_rate_endpoints_are_declared():
    assert P.UNIFORM_RATE_ENDPOINTS == frozenset({'a6'})
    assert 'deepseek' not in P.UNIFORM_RATE_ENDPOINTS

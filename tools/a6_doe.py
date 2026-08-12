#!/usr/bin/env python3
"""Bounded A6 batch/concurrency DOE. Run only inside VM 101.

The design spends at most 50,000 conservatively reserved tokens ($0.05 at the
measured A6 flat rate) and never retries. Raw translations remain in the guest;
the summary contains metrics only.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import random
import re
import threading
import time
from pathlib import Path

from hanpatch import a6mediator as med

MODELS = ('deepseek-v4-flash', 'DeepSeek-V4-Flash-0731')
MAX_CALLS = 40
MAX_RESERVED_TOKENS = 50_000
USD_PER_MTOK = 1.0
HANGUL = re.compile(r'[가-힣]')


class Budget:
    def __init__(self):
        self.calls = 0
        self.reserved = 0
        self.lock = threading.Lock()

    def reserve(self, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        # One input token cannot represent less than one non-empty byte. Adding the
        # declared output ceiling therefore overestimates, never underestimates.
        upper = len(body) + int(payload['max_tokens'])
        with self.lock:
            if self.calls + 1 > MAX_CALLS:
                raise RuntimeError('DOE call cap exhausted')
            if self.reserved + upper > MAX_RESERVED_TOKENS:
                raise RuntimeError('DOE $0.05 reservation cap exhausted')
            self.calls += 1
            self.reserved += upper
        return len(body), upper


def credential(path):
    text = Path(path).read_text(encoding='utf-8').strip()
    key, sep, value = text.partition('=')
    if key != 'A6_API_KEY' or not sep or not value or '\n' in value:
        raise RuntimeError('credential file must contain one A6_API_KEY assignment')
    return value


def chunks(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def payload(model, items):
    p = med.build_upstream_request(model, items)
    # reasoning_effort=none reduced the measured long single-item completion to
    # 77 tokens. Give that case 2.4x headroom and scale by item count; B=16 reaches
    # 1,024. This avoids reserving (and inviting) reasoning-sized output on small
    # batches while leaving room for JSON framing.
    p['max_tokens'] = min(1024, 128 + 56 * len(items))
    return p


def one_call(token, budget, model, items, treatment):
    p = payload(model, items)
    request_bytes, reserved = budget.reserve(p)
    started = time.monotonic()
    row = {
        'treatment': treatment,
        'model': model,
        'batch_size': len(items),
        'ids': [x['id'] for x in items],
        'request_bytes': request_bytes,
        'reserved_tokens': reserved,
    }
    try:
        reply = med.Upstream(token=token, timeout=90)(p)
        translations = med.extract_translations(reply, row['ids'])
        for item in items:
            med.check_tags(item['source'], translations[item['id']])
        usage = reply.get('usage') if isinstance(reply.get('usage'), dict) else {}
        row.update({
            'ok': True,
            'http_class': '200',
            'prompt_tokens': int(usage.get('prompt_tokens') or 0),
            'completion_tokens': int(usage.get('completion_tokens') or 0),
            'total_tokens': int(usage.get('total_tokens') or 0),
            'hangul_outputs': sum(bool(HANGUL.search(v)) for v in translations.values()),
            'translations': translations,
        })
    except Exception as exc:
        text = str(exc)
        status = re.search(r'status (\d{3})', text)
        row.update({'ok': False,
                    'http_class': status.group(1) if status else type(exc).__name__,
                    'error': text[:160], 'translations': {}})
    row['latency_s'] = round(time.monotonic() - started, 4)
    return row


def run_wave(token, budget, model, batches, workers, label):
    started = time.monotonic()
    if workers == 1:
        rows = [one_call(token, budget, model, b, label) for b in batches]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one_call, token, budget, model, b, label)
                       for b in batches]
            rows = [f.result() for f in futures]
    return rows, round(time.monotonic() - started, 4)


def summarize(raw, budget, wave_times, sample_digest):
    calls = raw['calls']
    actual = sum(r.get('total_tokens', 0) for r in calls)
    summary = {
        'schema': 'a6dq7.doe-summary.v1',
        'sample_sha256': sample_digest,
        'selected_model': raw.get('selected_model'),
        'caps': {'calls': MAX_CALLS, 'reserved_tokens': MAX_RESERVED_TOKENS,
                 'usd': MAX_RESERVED_TOKENS / 1_000_000 * USD_PER_MTOK},
        'spent': {'calls': budget.calls, 'reserved_tokens': budget.reserved,
                  'reported_tokens': actual,
                  'reported_usd': actual / 1_000_000 * USD_PER_MTOK},
        'errors': {}, 'batch_curve': {}, 'concurrency': {}, 'model_alias': {},
    }
    for row in calls:
        if not row['ok']:
            key = row['http_class']
            summary['errors'][key] = summary['errors'].get(key, 0) + 1

    for size in (1, 4, 8, 16):
        rows = [r for r in calls if r['treatment'] == f'batch-{size}']
        tok = sum(r.get('total_tokens', 0) for r in rows)
        units = sum(r['batch_size'] for r in rows if r['ok'])
        summary['batch_curve'][str(size)] = {
            'calls': len(rows), 'ok': sum(r['ok'] for r in rows), 'units': units,
            'tokens': tok, 'tokens_per_unit': round(tok / units, 2) if units else None,
            'latency_s_sum': round(sum(r['latency_s'] for r in rows), 3),
        }

    for workers in (1, 2, 4):
        label = f'concurrency-{workers}'
        rows = [r for r in calls if r['treatment'] == label]
        if not rows:
            summary['concurrency'][str(workers)] = {'not_run': True}
            continue
        units = sum(r['batch_size'] for r in rows if r['ok'])
        wall = wave_times[label]
        summary['concurrency'][str(workers)] = {
            'calls': len(rows), 'ok': sum(r['ok'] for r in rows),
            '429': sum(r['http_class'] == '429' for r in rows),
            'wall_s': wall, 'units_per_s': round(units / wall, 3) if wall else None,
        }

    canonical_rows = [r for r in calls if r['treatment'] == 'batch-16' and r['ok']]
    alias_rows = [r for r in calls if r['treatment'] == 'alias' and r['ok']]
    if canonical_rows and alias_rows:
        a, b = canonical_rows[0]['translations'], alias_rows[0]['translations']
        common = set(a) & set(b)
        agree = sum(a[k] == b[k] for k in common)
        summary['model_alias'] = {
            'compared': len(common), 'exact_agreement': agree,
            'exact_agreement_rate': round(agree / len(common), 4) if common else None,
            'primary_tokens': canonical_rows[0].get('total_tokens', 0),
            'alternate_tokens': alias_rows[0].get('total_tokens', 0),
        }
    return summary


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument('--samples', required=True)
    ap.add_argument('--credential', default='/etc/a6dq7.env')
    ap.add_argument('--output', required=True)
    args = ap.parse_args()

    sample_bytes = Path(args.samples).read_bytes()
    samples = json.loads(sample_bytes)
    if len(samples) != 16 or len({x['id'] for x in samples}) != 16:
        raise SystemExit('DOE requires exactly 16 unique sample ids')
    items = [{'id': x['id'], 'source': x['source']} for x in samples]
    token = credential(args.credential)
    budget = Budget()
    calls = []
    wave_times = {}
    raw = {'schema': 'a6dq7.doe-raw.v1', 'calls': calls, 'wave_times': wave_times}
    digest = hashlib.sha256(sample_bytes).hexdigest()
    out = Path(args.output)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)

    def save():
        # Checkpoint after every sequential call and every concurrency wave. A
        # process failure must not erase paid observations or tempt a full rerun.
        summary = summarize(raw, budget, wave_times, digest)
        (out / 'raw.json').write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
        (out / 'summary.json').write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(out / 'raw.json', 0o600)
        os.chmod(out / 'summary.json', 0o600)
        return summary

    # Availability preflight is also the first batch-1 observation. Try each
    # whitelisted label once; a rejected label is recorded and never called again.
    failed_models = set()
    run_model = None
    for model in MODELS:
        row = one_call(token, budget, model, items[:1], 'availability')
        if row['ok']:
            row['treatment'] = 'batch-1'
            run_model = model
            raw['selected_model'] = model
        else:
            failed_models.add(model)
        calls.append(row)
        save()
        if run_model:
            break
    if run_model is None:
        summary = save()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 2

    # Minimum estimable curve: the same first 8 units at B=1/4/8, plus all 16 at
    # B=16 to test the production ceiling. This is 12 curve calls including the
    # preflight, versus 23 in the first design, without losing a batch-size level.
    curve = []
    curve.extend((1, batch) for batch in chunks(items[:8], 1)[1:])
    curve.extend((4, batch) for batch in chunks(items[:8], 4))
    curve.extend((8, batch) for batch in chunks(items[:8], 8))
    curve.extend((16, batch) for batch in chunks(items, 16))
    random.Random(706).shuffle(curve)
    for size, batch in curve:
        calls.append(one_call(token, budget, run_model, batch, f'batch-{size}'))
        summary = save()
        if not calls[-1]['ok']:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 2

    # Compare the alternate label only if availability preflight did not already
    # reject it. Recalling a known-dead label adds no information.
    alternates = [model for model in MODELS
                  if model != run_model and model not in failed_models]
    if alternates:
        calls.append(one_call(token, budget, alternates[0], items, 'alias'))
        save()

    # Same two 4-unit batches at C=1/2/4: six calls, no retries, ramped load.
    # Two concurrent requests are enough to estimate scaling through C=2; the C=4
    # treatment tests whether four configured workers trigger routing limits.
    for workers in (1, 2, 4):
        label = f'concurrency-{workers}'
        rows, wall = run_wave(token, budget, run_model, chunks(items[:8], 4), workers, label)
        calls.extend(rows)
        wave_times[label] = wall
        save()
        if any(not row['ok'] for row in rows):
            break

    summary = save()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    critical = any(not row['ok'] and row['treatment'].startswith(('batch-', 'concurrency-'))
                   for row in calls)
    return 2 if critical else 0


if __name__ == '__main__':
    raise SystemExit(main())

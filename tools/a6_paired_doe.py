#!/usr/bin/env python3
"""Paired 200-unit A6 model DOE, paced and checkpointed."""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import time
from pathlib import Path

from hanpatch import a6mediator as med

MODELS = ('deepseek-v4-flash', 'qwen3.8-max')
BATCH = 16
MAX_CALLS = 26
MAX_REPORTED_TOKENS = 100_000  # post-response circuit breaker: $0.10 at A6 rate
HANGUL = re.compile(r'[가-힣]')


def credential(path):
    key, sep, value = Path(path).read_text(encoding='utf-8').strip().partition('=')
    if key != 'A6_API_KEY' or not sep or not value:
        raise RuntimeError('invalid credential file')
    return value


def ngrams(text, n=2):
    text = re.sub(r'\s+', '', text)
    return collections.Counter(text[i:i+n] for i in range(max(0, len(text)-n+1)))


def char_f1(actual, reference):
    a, b = ngrams(actual), ngrams(reference)
    if not a or not b:
        return 1.0 if actual == reference else 0.0
    overlap = sum((a & b).values())
    p, r = overlap / sum(a.values()), overlap / sum(b.values())
    return 2*p*r/(p+r) if p+r else 0.0


def chunks(items):
    return [items[i:i+BATCH] for i in range(0, len(items), BATCH)]


def payload(model, items):
    request = med.build_upstream_request(
        model, [{'id': x['id'], 'source': x['source']} for x in items])
    request['max_tokens'] = min(1024, 128 + 56 * len(items))
    if model.startswith('qwen'):
        request['enable_thinking'] = False
    return request


def one(upstream, model, items):
    started = time.monotonic()
    row = {'model': model, 'ids': [x['id'] for x in items], 'units': len(items)}
    try:
        reply = upstream(payload(model, items))
        translations = med.extract_translations(reply, row['ids'])
        for item in items:
            med.check_tags(item['source'], translations[item['id']])
        usage = reply.get('usage') if isinstance(reply.get('usage'), dict) else {}
        total = int(usage.get('total_tokens') or 0)
        if total <= 0:
            raise med.UpstreamError('upstream omitted billable usage')
        details = usage.get('completion_tokens_details') or {}
        row.update({
            'ok': True, 'status': 200,
            'prompt_tokens': int(usage.get('prompt_tokens') or 0),
            'completion_tokens': int(usage.get('completion_tokens') or 0),
            'reasoning_tokens': int(details.get('reasoning_tokens') or 0),
            'total_tokens': total,
            'hangul_outputs': sum(bool(HANGUL.search(v)) for v in translations.values()),
            'newline_exact': sum(item['source'].count('\n') == translations[item['id']].count('\n')
                                 for item in items),
            'reference_char_f1_sum': sum(char_f1(translations[item['id']], item['reference_ko'])
                                         for item in items),
            'translations': translations,
        })
    except Exception as exc:
        status = re.search(r'status (\d{3})', str(exc))
        row.update({'ok': False,
                    'status': int(status.group(1)) if status else type(exc).__name__,
                    'error': str(exc)[:160], 'translations': {}})
    row['latency_s'] = round(time.monotonic()-started, 4)
    return row


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument('--samples', required=True)
    ap.add_argument('--credential', default='/etc/a6dq7.env')
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    items = json.loads(Path(args.samples).read_text(encoding='utf-8'))
    if len(items) != 200 or len({x['id'] for x in items}) != 200:
        raise SystemExit('paired DOE requires 200 unique units')
    batches = chunks(items)
    if len(batches) * len(MODELS) != MAX_CALLS:
        raise SystemExit('DOE call count contract changed')

    # Alternate model order within each paired block to avoid a time trend favoring
    # whichever supplier is always called first.
    plan = []
    for i, batch in enumerate(batches):
        order = MODELS if i % 2 == 0 else tuple(reversed(MODELS))
        plan.extend((model, batch) for model in order)

    upstream = med.Upstream(token=credential(args.credential), rpm=5)
    rows = []
    out = Path(args.output)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)

    def persist():
        aggregates = {}
        for model in MODELS:
            selected = [r for r in rows if r['model'] == model]
            good = [r for r in selected if r['ok']]
            units = sum(r['units'] for r in good)
            tokens = sum(r.get('total_tokens', 0) for r in good)
            aggregates[model] = {
                'calls': len(selected), 'ok_calls': len(good), 'units': units,
                'tokens': tokens,
                'tokens_per_unit': round(tokens/units, 3) if units else None,
                'reference_char_f1': round(sum(r.get('reference_char_f1_sum', 0) for r in good)/units, 4)
                                     if units else None,
                'newline_exact_rate': round(sum(r.get('newline_exact', 0) for r in good)/units, 4)
                                      if units else None,
                'reasoning_tokens': sum(r.get('reasoning_tokens', 0) for r in good),
                'latency_s': round(sum(r['latency_s'] for r in selected), 3),
                'errors': [r.get('status') for r in selected if not r['ok']],
            }
        reported = sum(r.get('total_tokens', 0) for r in rows)
        raw = {'schema': 'a6dq7.paired-doe-raw.v1', 'rows': rows}
        summary = {'schema': 'a6dq7.paired-doe-summary.v1',
                   'limits': {'calls': MAX_CALLS, 'reported_token_breaker': MAX_REPORTED_TOKENS},
                   'spent': {'calls': len(rows), 'reported_tokens': reported,
                             'reported_usd': reported/1_000_000},
                   'models': aggregates}
        (out/'raw.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        os.chmod(out/'raw.json',0o600); os.chmod(out/'summary.json',0o600)
        return summary

    for model, batch in plan:
        row = one(upstream, model, batch)
        rows.append(row)
        summary = persist()
        if not row['ok'] or summary['spent']['reported_tokens'] > MAX_REPORTED_TOKENS:
            print(json.dumps(summary,ensure_ascii=False,indent=2)); return 2
    print(json.dumps(persist(),ensure_ascii=False,indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

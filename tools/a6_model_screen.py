#!/usr/bin/env python3
"""One-call-per-model A6 screen with strict spend and response contracts."""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import threading
import time
from pathlib import Path

from hanpatch import a6mediator as med

MAX_CALLS = 8
MAX_RESERVED_TOKENS = 20_000
MAX_TOKENS = 1024
HANGUL = re.compile(r'[가-힣]')


def credential(path):
    text = Path(path).read_text(encoding='utf-8').strip()
    key, sep, value = text.partition('=')
    if key != 'A6_API_KEY' or not sep or not value or '\n' in value:
        raise RuntimeError('invalid credential file')
    return value


def ngrams(text, n=2):
    text = re.sub(r'\s+', '', text)
    return collections.Counter(text[i:i + n] for i in range(max(0, len(text) - n + 1)))


def char_f1(actual, reference):
    a, b = ngrams(actual), ngrams(reference)
    if not a or not b:
        return 1.0 if actual == reference else 0.0
    overlap = sum((a & b).values())
    precision, recall = overlap / sum(a.values()), overlap / sum(b.values())
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


class Budget:
    def __init__(self, cap=MAX_RESERVED_TOKENS):
        self.calls = 0
        self.reserved = 0
        self.cap = int(cap)
        self.lock = threading.Lock()

    def reserve(self, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        amount = len(body) + int(payload['max_tokens'])
        with self.lock:
            if self.calls + 1 > MAX_CALLS or self.reserved + amount > self.cap:
                raise RuntimeError('model-screen budget exhausted')
            self.calls += 1
            self.reserved += amount
        return len(body), amount


def run_model(upstream, budget, model, items, max_tokens=None):
    request_items = [{'id': x['id'], 'source': x['source']} for x in items]
    payload = med.build_upstream_request(model, request_items)
    # `reasoning_effort: none` is set by the mediator and is a NO-OP on some models:
    # minimax refuses to disable reasoning at all ("Reasoning is mandatory for this
    # endpoint"). At the 1,024 default those models spend the whole completion on the
    # scratchpad and return truncated text, which reads as "cannot follow the JSON
    # contract" and is really "the cap was too small". Overridable so a reasoning model
    # is screened on capability rather than on this constant.
    payload['max_tokens'] = int(max_tokens or MAX_TOKENS)
    request_bytes, reserved = budget.reserve(payload)
    started = time.monotonic()
    row = {'model': model, 'request_bytes': request_bytes,
           'reserved_tokens': reserved, 'batch_size': len(items)}
    try:
        reply = upstream(payload)
        translations = med.extract_translations(reply, [x['id'] for x in items])
        for item in items:
            med.check_tags(item['source'], translations[item['id']])
        usage = reply.get('usage') if isinstance(reply.get('usage'), dict) else {}
        details = usage.get('completion_tokens_details') or {}
        references = [x for x in items if x.get('reference_ko')]
        row.update({
            'ok': True,
            'status': 200,
            'prompt_tokens': int(usage.get('prompt_tokens') or 0),
            'completion_tokens': int(usage.get('completion_tokens') or 0),
            'reasoning_tokens': int(details.get('reasoning_tokens') or 0),
            'total_tokens': int(usage.get('total_tokens') or 0),
            'tokens_per_unit': round(int(usage.get('total_tokens') or 0) / len(items), 3),
            'hangul_outputs': sum(bool(HANGUL.search(v)) for v in translations.values()),
            'newline_exact': sum(item['source'].count('\n') == translations[item['id']].count('\n')
                                 for item in items),
            'reference_count': len(references),
            'reference_char_f1': round(sum(char_f1(translations[x['id']], x['reference_ko'])
                                                for x in references) / len(references), 4)
                                 if references else None,
            'translations': translations,
        })
    except Exception as exc:
        status = re.search(r'status (\d{3})', str(exc))
        row.update({'ok': False,
                    'status': int(status.group(1)) if status else type(exc).__name__,
                    'error': str(exc)[:160], 'translations': {}})
    row['latency_s'] = round(time.monotonic() - started, 4)
    return row


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument('--samples', required=True)
    ap.add_argument('--models', required=True)
    ap.add_argument('--credential', default='/etc/a6dq7.env')
    ap.add_argument('--output', required=True)
    ap.add_argument('--max-tokens', type=int,
                    help='completion cap; raise it for models that cannot disable reasoning')
    ap.add_argument('--budget-tokens', type=int, default=MAX_RESERVED_TOKENS,
                    help='total reserved-token cap for this run')
    args = ap.parse_args()
    models = [x.strip() for x in args.models.split(',') if x.strip()]
    if not models or len(models) > MAX_CALLS or len(models) != len(set(models)):
        raise SystemExit('models must be 1-8 unique names')
    items = json.loads(Path(args.samples).read_text(encoding='utf-8'))
    if len(items) != 16 or len({x['id'] for x in items}) != 16:
        raise SystemExit('screen requires 16 unique samples')

    budget = Budget(args.budget_tokens)
    # One shared Upstream enforces five starts/minute across every model.
    upstream = med.Upstream(token=credential(args.credential), rpm=5)
    rows = []
    out = Path(args.output)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)

    def persist():
        raw = {'schema': 'a6dq7.model-screen-raw.v1', 'rows': rows}
        summary_rows = [{k: v for k, v in row.items() if k != 'translations'} for row in rows]
        reported = sum(row.get('total_tokens', 0) for row in rows)
        summary = {
            'schema': 'a6dq7.model-screen-summary.v1',
            'caps': {'calls': MAX_CALLS, 'reserved_tokens': MAX_RESERVED_TOKENS,
                     'usd': MAX_RESERVED_TOKENS / 1_000_000},
            'spent': {'calls': budget.calls, 'reserved_tokens': budget.reserved,
                      'reported_tokens': reported, 'reported_usd': reported / 1_000_000},
            'rows': summary_rows,
        }
        (out / 'raw.json').write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
        (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(out / 'raw.json', 0o600)
        os.chmod(out / 'summary.json', 0o600)
        return summary

    for model in models:
        rows.append(run_model(upstream, budget, model, items, args.max_tokens))
        persist()
    print(json.dumps(persist(), ensure_ascii=False, indent=2))
    return 0 if all(row['ok'] for row in rows) else 2


if __name__ == '__main__':
    raise SystemExit(main())

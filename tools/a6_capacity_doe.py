#!/usr/bin/env python3
"""Incremental A6 capacity DOE after batch-size screening.

Nine calls maximum, $0.02 conservative reservation, no retries. Waves are 65 s
apart so provider RPM and in-flight concurrency are not confounded.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import a6_doe as doe

MAX_RESERVATION = 20_000
COOLDOWN_S = 65


def safe_row(row):
    row = dict(row)
    row.pop('translations', None)
    return row


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument('--samples', required=True)
    ap.add_argument('--credential', default='/etc/a6dq7.env')
    ap.add_argument('--output', required=True)
    ap.add_argument('--model', default='deepseek-v4-flash', choices=doe.MODELS)
    args = ap.parse_args()

    doe.MAX_RESERVED_TOKENS = MAX_RESERVATION
    samples = json.loads(Path(args.samples).read_text(encoding='utf-8'))
    items = [{'id': x['id'], 'source': x['source']} for x in samples]
    if len(items) != 16:
        raise SystemExit('capacity DOE requires 16 samples')
    token = doe.credential(args.credential)
    budget = doe.Budget()
    rows = []
    waves = {}
    out = Path(args.output)
    out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def persist():
        actual = sum(r.get('total_tokens', 0) for r in rows)
        summary = {
            'schema': 'a6dq7.capacity-doe.v1', 'model': args.model,
            'caps': {'calls': 9, 'reserved_tokens': MAX_RESERVATION,
                     'usd': MAX_RESERVATION / 1_000_000},
            'spent': {'calls': budget.calls, 'reserved_tokens': budget.reserved,
                      'reported_tokens': actual, 'reported_usd': actual / 1_000_000},
            'batch8': {}, 'waves': {},
        }
        b8 = [r for r in rows if r['treatment'] == 'batch-8-incremental']
        if b8:
            r = b8[-1]
            summary['batch8'] = {
                'ok': r['ok'], 'tokens': r.get('total_tokens', 0),
                'tokens_per_unit': round(r.get('total_tokens', 0) / 8, 2) if r['ok'] else None,
                'latency_s': r['latency_s'], 'status': r['http_class'],
            }
        for workers in (1, 2, 4):
            label = f'capacity-{workers}'
            selected = [r for r in rows if r['treatment'] == label]
            if not selected:
                summary['waves'][str(workers)] = {'not_run': True}
                continue
            wall = waves[label]
            units = sum(r['batch_size'] for r in selected if r['ok'])
            summary['waves'][str(workers)] = {
                'calls': len(selected), 'ok': sum(r['ok'] for r in selected),
                '429': sum(r['http_class'] == '429' for r in selected),
                'wall_s': wall, 'units_per_s': round(units / wall, 3),
                'tokens': sum(r.get('total_tokens', 0) for r in selected),
            }
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(out, 0o600)
        return summary

    # One missing batch level from the prior screen.
    row = doe.one_call(token, budget, args.model, items[:8], 'batch-8-incremental')
    rows.append(safe_row(row)); summary = persist()
    if not row['ok']:
        print(json.dumps(summary, ensure_ascii=False, indent=2)); return 2

    plans = ((1, doe.chunks(items[:8], 4)),
             (2, doe.chunks(items[:8], 4)),
             (4, doe.chunks(items, 4)))
    for index, (workers, batches) in enumerate(plans):
        if index:
            time.sleep(COOLDOWN_S)
        label = f'capacity-{workers}'
        wave_rows, wall = doe.run_wave(token, budget, args.model, batches, workers, label)
        rows.extend(safe_row(r) for r in wave_rows)
        waves[label] = wall
        summary = persist()
        if any(not r['ok'] for r in wave_rows):
            print(json.dumps(summary, ensure_ascii=False, indent=2)); return 2

    print(json.dumps(persist(), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

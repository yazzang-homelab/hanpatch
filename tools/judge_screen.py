#!/usr/bin/env python3
"""Screen a candidate QA judge against the REAL judge contract, under a spend cap.

Why this exists: a model that translates well is not automatically a judge. The panel
counts a verdict only when it parses AND carries a disposition in the allowed set, so a
model that answers fluent Korean prose scores zero coverage. `qa.py` silently drops such
rows and retries, which reads as "the lane is slow" rather than "the lane cannot judge".

So this calls `qa.system_prompt()` and `qa.prompt()` - the production strings, not a
paraphrase - and applies the same acceptance test `qa.work()` applies. What it reports is
the number that decides admission: usable verdicts per pair sent.

Endpoint-generic on purpose. The judge pool has to hold >= REQUIRED_JUDGES DISTINCT
models that are not the translator, and no single endpoint on this box carries that many.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hanpatch import config  # noqa: E402

MAX_CALLS = 8
MAX_RESERVED_TOKENS = 60_000
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
DISPOSITIONS = ('pass', 'defect', 'policy')


class Budget:
    """A cap that is reserved BEFORE the call, not reconciled after it.

    Reconciling after the fact caps nothing: the tokens are already bought. Reserving the
    serialized request plus the full `max_tokens` is the only bound knowable in advance.
    """

    def __init__(self, max_calls=MAX_CALLS, max_tokens=MAX_RESERVED_TOKENS):
        self.max_calls, self.max_tokens = max_calls, max_tokens
        self.calls = self.reserved = 0
        self.lock = threading.Lock()

    def reserve(self, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        amount = len(body) // 4 + int(payload.get('max_tokens') or 0)
        with self.lock:
            if self.calls + 1 > self.max_calls or self.reserved + amount > self.max_tokens:
                raise RuntimeError('judge-screen budget exhausted')
            self.calls += 1
            self.reserved += amount
        return len(body), amount


def post(url, key, payload, timeout=180.0):
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(
        url, data=body, method='POST',
        headers={'Authorization': f'Bearer {key}',
                 'Content-Type': 'application/json; charset=utf-8',
                 'Accept': 'application/json',
                 'Accept-Encoding': 'identity',
                 # Cloudflare answers Python's default UA with an empty 403 on the a6
                 # host, so a fixed non-secret UA is load-bearing, not cosmetic.
                 'User-Agent': UA,
                 'Connection': 'close'})
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read(1 << 20).decode('utf-8'))
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode('utf-8', 'replace')
        raise RuntimeError(f'status {exc.code}: {detail[:300]}') from None


def parse_json(raw):
    """The lenient-but-not-guessing parse `translate.parse_json` performs.

    Imported rather than reimplemented where possible; kept here as a fallback so the
    screen still runs when a profile root is not configured.
    """
    try:
        from hanpatch import translate
        return translate.parse_json(raw)
    except Exception:                            # noqa: BLE001
        text = re.sub(r'^```(?:json)?|```$', '', str(raw).strip(), flags=re.M).strip()
        start, end = text.find('{'), text.rfind('}')
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None


def grade(obj, rows):
    """Apply `qa.work()`'s acceptance test, and report WHY each row was refused.

    `qa.work()` drops a bad row and moves on, which is right for a production sweep and
    useless for a screen: "0 verdicts" has to be attributable to a cause before a model
    can be admitted or rejected on evidence.
    """
    result = {'usable': 0, 'reject': {}, 'verdicts': {}}
    if not isinstance(obj, dict):
        result['reject']['not_an_object'] = len(rows)
        return result
    expected = {str(i) for i in range(len(rows))}
    invented = sorted(set(map(str, obj)) - expected)
    result['invented_keys'] = invented[:8]
    result['invented_key_count'] = len(invented)
    for i, row in enumerate(rows):
        value = obj.get(str(i))
        if not isinstance(value, dict):
            reason = 'missing' if value is None else 'not_an_object'
        else:
            try:
                adequacy, fluency = int(value.get('a', 0)), int(value.get('f', 0))
            except (TypeError, ValueError):
                reason = 'score_not_an_int'
            else:
                disposition = str(value.get('d', '')).strip().lower()
                if disposition not in DISPOSITIONS:
                    reason = f'bad_disposition:{disposition[:16] or "empty"}'
                elif not 1 <= adequacy <= 5 or not 1 <= fluency <= 5:
                    reason = 'score_out_of_range'
                else:
                    result['usable'] += 1
                    result['verdicts'][row['iid']] = {
                        'a': adequacy, 'f': fluency, 'd': disposition,
                        'r': str(value.get('r', ''))[:160]}
                    continue
        result['reject'][reason] = result['reject'].get(reason, 0) + 1
    return result


def baseline(pairs, recorded):
    """Majority disposition of the recorded panel, per pair.

    Recorded verdicts stay evidence even when the lane that produced them is retired -
    retirement governs the NEXT call, not what is already on disk.
    """
    out = {}
    for pair in pairs:
        votes = [v[pair['iid']]['d'] for v in recorded.values() if pair['iid'] in v]
        if votes:
            out[pair['iid']] = max(set(votes), key=votes.count)
    return out


def run(model, pairs, *, url, key, budget, effort=None, reasoning=None,
        max_tokens=None):
    from hanpatch import glossary, qa

    rows = [(p['en'], p['ko'], p.get('jp', ''), p['iid'], '', ()) for p in pairs]
    try:
        sub = glossary.relevant(glossary.load(), [r[0] for r in rows])
    except Exception:                            # noqa: BLE001 - glossary is optional here
        sub = None
    # `qa.work()` sizes the completion for a model that answers straight away. A reasoning
    # model spends this budget on its own scratchpad FIRST and returns empty content when it
    # runs out: measured on minimax-m3, 879 of 880 completion tokens were reasoning and the
    # screen read as "cannot judge". That is a budget artefact, not a capability, so the cap
    # is overridable and the shortfall is reported rather than inferred.
    payload = {'model': model,
               'messages': [{'role': 'system', 'content': qa.system_prompt()},
                            {'role': 'user', 'content': qa.prompt(rows, sub)}],
               'temperature': 0.0,
               'max_tokens': int(max_tokens or min(4000, 400 + 40 * len(rows)))}
    if effort:
        # a6 takes the flat OpenAI-style field. Measured: without it the model burns the
        # whole completion on reasoning and is billed for empty content (1307 -> 281 tok).
        payload['reasoning_effort'] = effort
    if reasoning is not None:
        # OpenRouter takes a nested object instead, and some endpoints refuse to disable
        # reasoning at all ("Reasoning is mandatory for this endpoint"). Passing the caller's
        # object through verbatim keeps that refusal visible as an upstream 400.
        payload['reasoning'] = reasoning
    request_bytes, reserved = budget.reserve(payload)
    row = {'model': model, 'pairs': len(pairs), 'request_bytes': request_bytes,
           'reserved_tokens': reserved, 'max_tokens': payload['max_tokens'],
           'reasoning_effort': effort, 'reasoning': reasoning}
    started = time.monotonic()
    try:
        reply = post(url, key, payload)
        first = (reply.get('choices') or [{}])[0]
        choice = first.get('message') or {}
        content = choice.get('content') or ''
        usage = reply.get('usage') if isinstance(reply.get('usage'), dict) else {}
        details = usage.get('completion_tokens_details') or {}
        graded = grade(parse_json(content), pairs)
        total = int(usage.get('total_tokens') or 0)
        reasoning_tokens = int(details.get('reasoning_tokens') or 0)
        completion = int(usage.get('completion_tokens') or 0)
        row.update({
            'ok': graded['usable'] == len(pairs),
            'usable_verdicts': graded['usable'],
            'coverage': round(graded['usable'] / len(pairs), 4),
            'rejects': graded['reject'],
            'invented_keys': graded.get('invented_keys'),
            'invented_key_count': graded.get('invented_key_count'),
            'empty_content': not content.strip(),
            # A model that ran out of completion budget looks identical to one that cannot
            # follow the contract unless these two are on the record: `length` plus a
            # reasoning share near 1.0 means RAISE max_tokens, not reject the model.
            'finish_reason': first.get('finish_reason'),
            'truncated': first.get('finish_reason') == 'length',
            'reasoning_share': round(reasoning_tokens / completion, 4) if completion else None,
            'prompt_tokens': int(usage.get('prompt_tokens') or 0),
            'completion_tokens': completion,
            'reasoning_tokens': reasoning_tokens,
            'total_tokens': total,
            'tokens_per_pair': round(total / len(pairs), 2) if total else None,
            'verdicts': graded['verdicts'],
        })
    except Exception as exc:                     # noqa: BLE001 - any failure is a result
        row.update({'ok': False, 'error': str(exc)[:300], 'verdicts': {}})
    row['latency_s'] = round(time.monotonic() - started, 3)
    return row


def agreement(verdicts, control):
    shared = [k for k in verdicts if k in control]
    if not shared:
        return None
    same = sum(verdicts[k]['d'] == control[k] for k in shared)
    flagged = sum(verdicts[k]['d'] != 'pass' for k in shared)
    return {'n': len(shared), 'agree': same, 'agree_rate': round(same / len(shared), 4),
            'defect_rate': round(flagged / len(shared), 4)}


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False, description=__doc__)
    ap.add_argument('--models', required=True, help='comma-separated model ids')
    ap.add_argument('--url', default='https://openrouter.ai/api/v1/chat/completions')
    ap.add_argument('--key-env', default='OPENROUTER_KEY_FALLBACK')
    ap.add_argument('--key-file', help='KEY=VALUE file sourced before reading --key-env')
    ap.add_argument('--corpus', default='/root/tmp/gemmaqa/result.json')
    ap.add_argument('--root', default='/root/tmp/gemmaqa')
    ap.add_argument('--pairs', type=int, default=12)
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--effort', default='', help="e.g. 'none' on a6, blank to omit")
    ap.add_argument('--reasoning', default='',
                    help='JSON passed through as the `reasoning` field, e.g. '
                         '\'{"enabled":false}\' on OpenRouter')
    ap.add_argument('--max-tokens', type=int,
                    help='override the completion cap; raise it for reasoning models')
    ap.add_argument('--budget-tokens', type=int, default=MAX_RESERVED_TOKENS,
                    help='total reserved-token cap for this run')
    ap.add_argument('--output')
    args = ap.parse_args()

    if args.key_file:
        for line in Path(args.key_file).read_text(encoding='utf-8').splitlines():
            name, sep, value = line.strip().partition('=')
            if sep and name.isidentifier():
                os.environ.setdefault(name, value.strip('"\''))
    key = os.environ.get(args.key_env, '').strip()
    if not key:
        raise SystemExit(f'{args.key_env} is empty; nothing to authenticate with')

    config.set_root(args.root)
    doc = json.loads(Path(args.corpus).read_text(encoding='utf-8'))
    items = doc['items']
    random.Random(args.seed).shuffle(items)
    pairs = items[:args.pairs]
    control = baseline(pairs, doc.get('verdicts') or {})

    models = [m.strip() for m in args.models.split(',') if m.strip()]
    reasoning = json.loads(args.reasoning) if args.reasoning else None
    budget = Budget(max_tokens=args.budget_tokens)
    rows = []
    for model in models:
        row = run(model, pairs, url=args.url, key=key, budget=budget,
                  effort=args.effort or None, reasoning=reasoning,
                  max_tokens=args.max_tokens)
        row['agreement_vs_recorded_panel'] = agreement(row.get('verdicts') or {}, control)
        rows.append(row)
        summary = {k: v for k, v in row.items() if k != 'verdicts'}
        print(json.dumps(summary, ensure_ascii=False), flush=True)

    report = {'schema': 'hanpatch.judge-screen.v1',
              'url': args.url, 'pairs': len(pairs), 'seed': args.seed,
              'control_judges': sorted((doc.get('verdicts') or {}).keys()),
              'spent': {'calls': budget.calls, 'reserved_tokens': budget.reserved,
                        'reported_tokens': sum(r.get('total_tokens') or 0 for r in rows)},
              'rows': rows}
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(out, 0o600)
        print(f'wrote {out}')
    return 0 if all(r.get('ok') for r in rows) else 2


if __name__ == '__main__':
    raise SystemExit(main())

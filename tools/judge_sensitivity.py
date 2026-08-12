#!/usr/bin/env python3
"""Measure a judge's DETECTION power with seeded defects of known type.

Contract coverage and detection are different properties, and only the first is easy.
Measured on this corpus: `minimax-m2.7` and `minimax-m3` both returned a well-formed
verdict for 12/12 pairs and marked every one `pass 5/5` with an empty reason, missing both
defects the recorded panel had found. A judge like that satisfies REQUIRED_JUDGES while
catching nothing, which is worse than an empty panel - it launders a defect into "two
independent models passed it".

Agreement against a recorded panel cannot separate the two: a rubber stamp agrees with
every pass, and passes are the majority of any shipped corpus. So this seeds defects whose
ground truth is constructed rather than judged, and reports recall on them alongside the
false-positive rate on untouched pairs. A model earns admission by finding planted damage,
not by agreeing.

Mutations are drawn from the defect taxonomy the recorded panel actually cited: particle
errors, dropped syllables, truncated clauses, and altered numbers.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hanpatch import config  # noqa: E402

import judge_screen as screen  # noqa: E402

PARTICLES = (('을', '를'), ('를', '을'), ('이', '가'), ('가', '이'),
             ('은', '는'), ('는', '은'), ('와', '과'), ('과', '와'))
HANGUL = re.compile(r'[가-힣]')


def mutate_particle(text, rng):
    """Swap one object/subject particle. The commonest real defect in this corpus."""
    spots = [(i, a, b) for i, ch in enumerate(text) for a, b in PARTICLES if ch == a]
    if not spots:
        return None
    i, _a, b = rng.choice(spots)
    return text[:i] + b + text[i + 1:], 'particle'


def mutate_drop_syllable(text, rng):
    """Delete one Hangul syllable: a typo that a fluent reader cannot miss."""
    spots = [m.start() for m in HANGUL.finditer(text)]
    if len(spots) < 4:
        return None
    i = rng.choice(spots[1:-1])
    return text[:i] + text[i + 1:], 'dropped_syllable'


def mutate_truncate(text, rng):
    """Cut the final clause: meaning is lost, grammar often still scans."""
    body = text.rstrip()
    if len(body) < 12:
        return None
    cut = rng.randint(len(body) // 3, max(len(body) // 3 + 1, len(body) - 4))
    return body[:cut], 'truncated_clause'


def mutate_number(text, rng):
    """Change a digit. Numbers are never a matter of taste, so a miss is unambiguous."""
    spots = [m.start() for m in re.finditer(r'(?<![{<])\d', text)]
    if not spots:
        return None
    i = rng.choice(spots)
    digit = str((int(text[i]) + 3) % 10)
    return text[:i] + digit + text[i + 1:], 'altered_number'


MUTATIONS = (mutate_particle, mutate_drop_syllable, mutate_truncate, mutate_number)


def seed_defects(pairs, rng, rate=0.5):
    """Return pairs with ground truth attached, half of them damaged.

    The damaged half keeps its original id so the model cannot infer the label from
    position or key shape - only from the Korean text.
    """
    out = []
    order = list(pairs)
    rng.shuffle(order)
    wanted = int(len(order) * rate)
    damaged = 0
    for pair in order:
        item = dict(pair)
        item['seeded'] = None
        if damaged < wanted:
            for fn in rng.sample(MUTATIONS, len(MUTATIONS)):
                result = fn(pair['ko'], rng)
                if result and result[0] != pair['ko']:
                    item['ko'], item['seeded'] = result
                    damaged += 1
                    break
        out.append(item)
    return out


def score(verdicts, pairs):
    """Recall on seeded defects, false positives on untouched pairs."""
    truth = {p['iid']: p['seeded'] for p in pairs}
    caught = missed = false_pos = clean_ok = 0
    by_kind = {}
    for iid, kind in truth.items():
        verdict = verdicts.get(iid)
        if not verdict:
            continue
        flagged = verdict['d'] != 'pass'
        if kind:
            bucket = by_kind.setdefault(kind, [0, 0])
            bucket[1] += 1
            if flagged:
                caught += 1
                bucket[0] += 1
            else:
                missed += 1
        elif flagged:
            false_pos += 1
        else:
            clean_ok += 1
    seeded = caught + missed
    clean = false_pos + clean_ok
    return {'seeded': seeded, 'caught': caught, 'missed': missed,
            'recall': round(caught / seeded, 4) if seeded else None,
            'clean': clean, 'false_positive': false_pos,
            'false_positive_rate': round(false_pos / clean, 4) if clean else None,
            'recall_by_kind': {k: f'{v[0]}/{v[1]}' for k, v in sorted(by_kind.items())}}


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False, description=__doc__)
    ap.add_argument('--models', required=True)
    ap.add_argument('--url', default='https://a6.a6api.com/v1/chat/completions')
    ap.add_argument('--key-env', default='A6_API_KEY')
    ap.add_argument('--key-file', default='/etc/a6dq7.env')
    ap.add_argument('--corpus', default='/root/tmp/gemmaqa/result.json')
    ap.add_argument('--root', default='/root/tmp/gemmaqa')
    ap.add_argument('--pairs', type=int, default=12)
    ap.add_argument('--seed', type=int, default=17)
    ap.add_argument('--effort', default='none')
    ap.add_argument('--reasoning', default='')
    ap.add_argument('--max-tokens', type=int, default=3000)
    ap.add_argument('--budget-tokens', type=int, default=400_000)
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
    control = doc.get('verdicts') or {}
    # Only pairs the recorded panel agreed were clean can carry a planted defect: seeding
    # damage into an already-defective line makes a "catch" unattributable.
    clean = []
    for item in doc['items']:
        votes = [v[item['iid']]['d'] for v in control.values() if item['iid'] in v]
        if votes and all(v == 'pass' for v in votes):
            clean.append(item)
    rng = random.Random(args.seed)
    rng.shuffle(clean)
    pairs = seed_defects(clean[:args.pairs], rng)
    planted = sum(1 for p in pairs if p['seeded'])
    print(f'{len(pairs)} pairs, {planted} seeded defects, '
          f'{len(pairs) - planted} untouched', flush=True)

    reasoning = json.loads(args.reasoning) if args.reasoning else None
    budget = screen.Budget(max_tokens=args.budget_tokens)
    rows = []
    for model in args.models.split(','):
        model = model.strip()
        if not model:
            continue
        row = screen.run(model, pairs, url=args.url, key=key, budget=budget,
                         effort=args.effort or None, reasoning=reasoning,
                         max_tokens=args.max_tokens)
        row['detection'] = score(row.get('verdicts') or {}, pairs)
        rows.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != 'verdicts'},
                         ensure_ascii=False), flush=True)

    report = {'schema': 'hanpatch.judge-sensitivity.v1', 'url': args.url,
              'seed': args.seed, 'planted': planted,
              'ground_truth': {p['iid']: p['seeded'] for p in pairs},
              'spent': {'calls': budget.calls,
                        'reported_tokens': sum(r.get('total_tokens') or 0 for r in rows)},
              'rows': rows}
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        os.chmod(out, 0o600)
        print(f'wrote {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

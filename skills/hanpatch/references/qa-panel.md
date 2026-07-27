# The QA panel

## Why more than one judge

A single judge produces correlated false negatives: it shares a family, a
tokenizer, and a set of blind spots with the producer. On the reference corpus a
human sample of 5 strings that one judge had passed contained 4 real defects.
Two judges from different providers is the minimum that catches this; the panel
size is configurable and never one.

## Verdict record

```json
{"a": 5, "f": 5, "d": "pass", "judge": "provider:model",
 "en": "…", "ko": "…", "r": "짧은 이유"}
```

- `a` adequacy 1-5, `f` fluency 1-5
- `d` disposition — exactly `pass`, `defect`, or `policy`
- `judge` must be a **configured** judge id; unknown ids are rejected
- `r` reason, required when not a pass

A verdict is keyed by `pair_key(source, translation)` — a hash of the exact pair
judged. Edit the translation by one character and every verdict for it becomes
irrelevant, so the row falls back to pending.

## Never synthesise a disposition

If a judge returns malformed JSON, or omits `d`, or gives an unknown value, the
record is **dropped**. The row stays pending and rotates to a different
provider. Inferring "probably a pass" from a Korean keyword regex was the
original design and it was wrong: it converted parse failures into approvals.

## Producer may not judge its own output

Provenance is recorded per string when it is translated. A verdict whose judge
equals the producer is rejected. For rows translated before provenance logging
existed this is best-effort — state that rather than implying coverage.

## Waivers

A waiver permits one entry to ship despite a blocking verdict. It requires:

- a key `sha1(source + '\0' + shipped translation)` — **hash-bound**
- `key`: the `family/key` it applies to, cross-checked against the manifest
- `category`: from a fixed set (e.g. `JP_NAMING`, `REGISTER`, `TERMINOLOGY`)
- `reason`: a real sentence, length-checked

Consequences that matter: editing either side of the pair makes the waiver
**stale**, and a stale waiver **blocks the build** rather than being ignored. A
waiver for a key not in the manifest is invalid. Unused waivers are reported.

Waivers are for defensible policy disagreements — a judge preferring 큐어 리프
over the project's chosen 큐어리프. They are not for defects.

## Reading the gate output

```
verdict records     6524 (>= 2 distinct judges required per entry)
disposition defect   49        <- these are waived or blocked, never silent
disposition policy    5
min(adequacy,fluency)=4: 108   <- distribution, not a gate
waivers applied     47/47      <- a shortfall here means stale waivers
```

`min(a,f)` distribution is diagnostic. The gate is the disposition and the judge
count, not the score.

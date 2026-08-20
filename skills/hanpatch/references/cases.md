# Measured cases

Every number in this file was measured on a specific title, on a specific
build. They are here rather than in `SKILL.md` because a reader working on a new
title will otherwise read another game's measurement as a threshold for theirs —
and the numbers are not thresholds. They are the evidence that a principle in
the body is real rather than plausible.

Each entry states what was measured, on what, and which principle it supports.
When you measure your own title, add an entry; do not edit someone else's.

---

## Particles welded to a runtime substitution

**Title:** Dragon Quest VII (3DS), shipped Korean corpus.
**Principle:** *A particle after a runtime substitution has no single right
answer, so stop asking the model for one.*

| Measurement | Count |
|---|---|
| Rows carrying a particle welded to a substitution token | 2,416 (`2416`) |
| Guessed `는` against `은` | 1,084 (`1084`) / 500 |
| Guessed `가` against `이` | 364 / 121 |
| Rows with a hand-written both-forms particle, in four different shapes | 506 |

Roughly half of the guessed rows read wrong for some player, because the
syllable the particle agrees with does not exist until the engine draws the
line. `josa.auto` resolves this deterministically: fixed values come from the
profile's `substitution_values`, otherwise the both-forms particle is written in
one canonical shape, and a particle with no readable both-forms shape
(`이었다`/`였다`) is refused so the row is reworded rather than shipped wrong.

## Line breaks that separate a name from its particle

**Title:** Dragon Quest VII (3DS), before the unit-aware wrapper.
**Principle:** *A line may not break inside a word, and may not open on a
closing mark.*

| Measurement | Count |
|---|---|
| Rows ending a line on a runtime name with its particle on the next line | 58 |
| Rows opening a line on punctuation | 116 |

The reader saw `아루스`, then a line beginning `에게`. The old fallback emitted a
break character by character and raised no problem, which is exactly the
complaint "단어 중간에서 줄이 넘어간다" with the gate reporting clean.

## Japanese punctuation converted twice

**Title:** Dragon Quest VII (3DS), shipped corpus.
**Principle:** *Japanese punctuation is converted, and a sentence does not end
twice.*

| Measurement | Count |
|---|---|
| Rows carrying `….` | 7,795 (`7795`) |
| Rows carrying `~.` | 470 |
| Total | 7,905 |

Every one was this pipeline's own rendering of `……。` and `～。`. Scoped to a
Japanese source, because the reference title authors fullwidth punctuation
deliberately as an inner-monologue device.

## The QA repair cycle

**Title:** Dragon Quest VII (3DS), one closed cycle.
**Principle:** *Repair, reseal and re-judge are one cycle*, and *freshness is
decided against the sealed artifact.*

| Measurement | Count |
|---|---|
| Pairs in the closed cycle | 65,836 (`65836`) |
| Flagged | 13,788 (`13788`) |
| Actionable when compared against the raw store instead of the seal | 449 |
| Rows repaired in one cycle | 9,810 (`9810`) |
| Actionable after reseal and re-judge | 13,788 → 8,856 (`13788` → `8856`) |

Comparing verdicts against the raw translation store rather than the sealed
value discarded real complaints as stale and would have reported the repair pass
complete. Print the flagged count and the actionable count together; a large
silent gap between them is an authority bug, not progress.

## Judge panel starvation

**Title:** Dragon Quest VII (3DS).
**Principle:** *Exclude judges per row, never per batch*, and *require at least
one more lane than the release rule.*

Twenty pairs sat unjudged across three full passes, because a mixed batch
excluded every lane when the producer test was applied to the batch rather than
the row.

A reviewer sample also found 4 real defects in 5 strings a single judge had
passed, which is why the minimum panel is two.

## Legacy judge identities

**Title:** Dragon Quest VII (3DS) ledger.
**Principle:** *Never delete a lane from the accepted-identity set to retire it.*

| Lane | Verdicts already recorded |
|---|---|
| `deepseek:deepseek-v4-pro` | 49,807 |
| `claudelee:sonnet` | 4,493 |

Removing an identity makes the gate read its own history as forgery. Retirement
belongs in the runtime pool, not the identity set.

## Supervisor death

**Title:** Dragon Quest VII (3DS) repair run.
**Principle:** *A supervisor cannot supervise its own death.*

One transient bad read of a 40 MB verdict file ended supervision while the
repair loop kept running unobserved. Later the supervisor was killed outright
with no log line and the repair cycle sat dead for five hours while every
artifact on disk still looked healthy and the last log line still read
`repair=up`.

## Release bundle size

**Title:** Dragon Quest VII (3DS).
**Principle:** *Ship a release bundle, not a ROM and not a binary delta.*

| Measurement | Value |
|---|---|
| Bundle size | 340 KB |
| ROM it reproduces | 249 MB |
| Rebuild time | 4 seconds |
| xdelta3 and a block differ, on the same encrypted container | ~82% of the full ROM |

CTR keystreams are position-dependent, so one shifted byte kills every
downstream match. 82% of the game is not a patch.

## LayeredFS pack

**Title:** Dragon Quest VII (3DS).
**Principle:** *On real hardware a rebuilt image is the wrong shape.*

| Measurement | Value |
|---|---|
| Files in the pack | 379 files |
| Pack size | 39.5 MB |
| `code.ips` | 70 bytes |
| Alternative | 2 GB reinstall |

Every file verified byte-identical to the same path inside the rebuilt ROM.

## Browser application

**Title:** Dragon Quest VII (3DS), 2 GB container.
**Principle:** *Do not answer "make it work in the browser" by porting the
container and crypto code to JavaScript.*

| Measurement | Value |
|---|---|
| wasm (Pyodide) apply time | 8 min 6 s |
| Native apply time | 2 min 14 s |
| Output | identical sha256 |
| Scratch space required | 4.3 GB |

The scratch requirement is why `web/apply/opfs-bridge.js` gives Emscripten a
real disk-backed filesystem: Pyodide's own native-FS mount mirrors everything
into RAM.

---

## Adding your own

State the title and platform, the build or corpus measured, the counts, and
which body principle the numbers support. If a measurement contradicts a
principle, say so — the principle is what changes.

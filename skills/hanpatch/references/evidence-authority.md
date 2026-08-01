# Evidence authority

The gates prove properties of an artifact. They do not tell you which claim wins
when two records disagree, and they do not tell you how much of the game is
actually done. That is what this file is for.

## Authority order

Highest first. A lower rank never overrides a higher one by being newer.

1. **Runtime observation of the patched build**, tied to the output hash — a
   capture, a save-state checkpoint, or a reproducible user report that names
   release, platform, and scene.
2. **Artifact readback** — `hanpatch verify` re-reading every string out of the
   shipped ROM, the packed font read back out of RomFS, the identity rebuild
   diff, the release bundle's recorded input/output hash pair.
3. **A sealed manifest digest** and the gate verdicts derived from it.
4. **Current passing tests** tied to that artifact.
5. **The profile** — the recorded policy, budgets, and forced terms.
6. **Waivers with a live hash**, and their written reasons.
7. Source, comments, README.
8. Old logs, prior runs, superseded notes, and any model prose without an
   artifact behind it.

## Conflict rules

- **A green gate run is not runtime proof.** Layout is checked against font
  metrics; the renderer was never consulted.
- **A build that succeeded proves the packer ran**, nothing about what a player
  sees.
- **A screenshot without an output hash proves one frame of an unidentified
  build.** Ask which ROM produced it before it counts.
- **A stale gate log loses to a fresh readback**, even if the log is a paragraph
  and the readback is one line.
- **A waiver is not evidence.** It is a recorded decision to ship a known
  disagreement. Counting waivers as passes inflates quality.
- **A user report without version and scene is a lead, not a defect.** Several
  reports may share one root cause: keep the chain, count the cause once.
- When two records stay incompatible, report both and mark it unresolved. Do not
  pick the more convenient one.

## Confidence labels

Attach one to every claim in a status report.

- **High** — runtime or readback proof, current output hash named, reproducible.
- **Medium** — consistent gate and artifact records, no runtime observation.
- **Low** — single narrative claim, missing build identity, stale evidence, or
  an unresolved contradiction.

## Progress is not one number

Never report "N% done". Report these axes separately, because they fail
independently and averaging them hides the one that is blocking:

| Axis | Answered by |
|---|---|
| pipeline readiness | identity rebuild, extract/inject/verify round trip |
| coverage | translated rows / shippable rows in the seal |
| translation quality | judge dispositions and judge count, not `min(a,f)` |
| runtime verification | scenes observed on a named build — usually the lowest |
| release readiness | bundle reproduces byte-identically from a clean input |
| remaining human work | rows needing judgement a machine cannot make |

Gate pass rate is **pipeline readiness**. Quoting it as overall progress is the
most common way this project lies to itself.

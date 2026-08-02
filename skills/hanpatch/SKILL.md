---
name: hanpatch
description: Build a fan translation patch for a game ROM with structural consistency gates — extract text, machine-translate through free endpoints, enforce a glossary and text-box capacity, seal a manifest, verify with an independent multi-judge QA panel, inject, and prove the round trip. Use when asked to localise/한글패치 a game, or to audit an existing translation pipeline.
---

# hanpatch — gate-enforced game localisation

## What this is for

Producing a translation patch that is *provably* consistent, not one that looks
fine in a spot check. The premise: machine translators are cheap and plentiful,
and they are unreliable in ways that are individually invisible and collectively
fatal — a name spelled three ways, a line that overflows its text box on the
third page, a control tag silently dropped, a judge that rubber-stamps its own
output. None of that is fixed by a better prompt. It is fixed by refusing to
build until a machine can prove it did not happen.

Use this skill when the task is "translate game X into language Y", when a
translation exists but nobody can say whether it is correct, or when a
localisation pipeline needs review.

Do **not** reach for it to translate a handful of strings, or a document with no
container, no layout budget, and no glossary. The ceremony only pays for itself
across thousands of interdependent strings.

## The non-negotiable rule

**Consistency is enforced structurally, never by trusting a model.**

Every claim the pipeline makes is re-derived from the shipped artifact. If a
check cannot be automated, it is recorded as a known limitation instead of being
asserted. A gate that can be skipped is not a gate.

## Layers

```
config / profile   what this title is, where its files are, its markup grammar
core               glossary, translation, layout, audit, manifest, QA gates
                   — no knowledge of any container format
adapter            extract / inject / verify for one title on one platform
platform+format    CIA/NCCH/RomFS/BCFNT, archive and message readers
```

The core never reads a ROM. An adapter never decides wording — the test suite
asserts this by rejecting any adapter that imports the wording modules. This is
what makes a new title cheap: write an adapter and a profile, change nothing
else.

## Workflow

```bash
hanpatch init  --title "Game" --adapter my_game --profile profiles/my_game.json
hanpatch extract                    # ROM -> work/text_src.json
hanpatch fonts                      # target-script glyphs into the game's font
hanpatch translate --family dialogue --workers 4
hanpatch qa --judges 2 --workers 4  # independent judge panel, resumable
hanpatch gates                      # all gates, seals a manifest digest
hanpatch build                      # gates + inject -> patched ROM
hanpatch verify                     # re-read the ROM, prove every string
hanpatch book                       # bilingual script book (static site)
```

Translation and QA are long-running and resumable — shard files are `fcntl`
locked, so run them under `setsid nohup` and poll the log. Everything else is
minutes.

## Gate order — this sequence is the safety argument

| # | Gate | Rejects |
|---|------|---------|
| 1 | `glossary` | a proper noun rendered two ways; a UI label leaking into prose |
| 2 | `capacity` | text exceeding the largest page that layout group ever renders |
| 3 | `materialize` | rule-derived rows that do not survive their own validator |
| 4 | `audit` | untranslated rows, tag damage, register drift, duplicate meanings |
| 5 | `manifest` | nothing — it *seals* every shippable string into one digest |
| 6 | `qagate` | any entry lacking N independent judge passes for that exact pair |

Then the packer **re-runs the QA validation in-process** before writing a byte.
The approval token is a convenience; the authority is the fresh revalidation.
Editing the manifest and the token together still fails.

## Ideas worth stealing even if you never run this

**Capacity comes from the shipped text, not a guess.** The widest page the
original ever renders in a layout group is the proven bound. Group by
`family/key-shape` with digits folded, so `system/treasure` is bounded by the
one line it renders instead of borrowing its family's maximum.

**The glossary is scoped.** Short polysemous labels (`Dead`, `Key`, `Cure`) are
mandatory in the families that render them as UI labels and *forbidden* as
mandates inside narrative prose, where they are ordinary words.

**Two judges minimum, and a producer may not judge its own output.** One judge
produces correlated false negatives — a reviewer sample found 4 real defects in
5 strings a single judge had passed. Judge verdicts are structured
(`pass|defect|policy`), never keyword-sniffed from prose, and an invalid or
missing disposition is *dropped* so the row stays pending and rotates providers,
rather than being synthesised into a pass.

**Waivers are hash-bound.** A waiver keys on `sha1(source + '\0' + shipped
text)`. Edit either side and the waiver goes stale and blocks the build. A
waiver needs a category and a real reason.

**Glyph authority is the built font.** A character is renderable because it
exists in the font that ships, verified by reading the packed font back out of
the ROM — not because it is in some Unicode range.

**Measure the format before writing it.** The 3DS font sheets are RGBA4444
where `A` is ink coverage and `RGB` is a shading mask the engine multiplies with
the text colour. The naive `255 - coverage` inverse yields flat-black glyphs with
a bright rim. The correct LUT was *measured off the shipped font*. Guessing a
binary format's semantics costs a whole rebuild cycle.

## Adding a title

1. **Find the text.** Unpack the ROM; look for a message archive. Round-trip it
   byte-for-byte before touching anything — if repacking the untouched original
   does not reproduce the input exactly, the reader is wrong.
2. **Write the profile** (`profiles/<title>.json`): markup grammar, name-key
   patterns, forced terms, UI-only scoping, per-family width budgets, font
   paths, register rules.
3. **Write the adapter** (`hanpatch/adapters/<title>.py`): `extract`, `inject`,
   `verify`. Subclass `Adapter`, decorate with `@register('name')`.
4. **Prove the identity rebuild.** Build with an empty translation and diff
   against the original ROM. Bit-exact or the adapter is not done.
5. Then translate.

For a non-3DS platform, add `hanpatch/platforms/<name>/` with the container
crypto and filesystem. The core does not change.

## Localisation policy is a decision, not a default

Record it in the profile and enforce it. The reference title uses: **prose
follows the English release; item/spell/weapon names transliterate the Japanese
original; character names follow English.** That mixture is deliberate — the
English prose is the better read, but the Japanese item names are what players
recognise. Whatever you choose, it must be mechanically checkable, or it is a
preference rather than a policy.

## Source-only markup is a first-class profile fact

Some containers carry annotations that belong to the source language only: furigana,
pronunciation hints, editorial markers, or other reading aids. Do not leave these in the
ordinary tag pattern and do not treat them as ordinary prose.

Declare a `source_only_pattern` in the title profile. The pipeline MUST:

- recognise the token when validating the source, so a shipped source is not reported as
  malformed merely because the recogniser did not know its markup;
- exclude it from the ordinary tag multiset and ordered skeleton, because disappearing is
  the correct translation behaviour;
- reject it when it survives in the target, including when a model translated the token's
  contents but kept its wrapper (for example `{2족장}`), so matching only source-script
  characters is insufficient;
- return `None` for titles without this declaration. Missing declaration means the title
  has no source-only class; it does not mean match everything.

Run the new source rule against the source corpus before trusting it. If a validator rejects
large numbers of source records, the validator is missing a container fact. If a failed
output contains only removable source-only wrappers and the surrounding translation is
sound, repair it with a deterministic sweep and re-run the gates; do not retranslate the
whole corpus by reflex.

A raw `<`, `>`, `{`, or `}` is a separate container fact. If the source stores one as literal
content beside a real tag, declare it as `literal_delimiters` and measure the exception; do
not make the delimiter checker permissive globally. The default is an empty list, and the
same declared exception is checked on target output. A single observed extra brace in one
DQ7 record was enough to block the last row until this fact was recorded.

## Choosing judge panel lanes

Panel cost is *corpus x panel size*, not corpus. A per-token lane that looks cheap for one
translation pass is multiplied by the number of required verdicts and by every repair cycle,
so lane choice for a panel is a cost decision before it is a quality one. Prefer flat-rate
accounts for judging and keep metered lanes for translation and repair, where each row is
paid for once.

Scale the panel by *identity*, not by spend: discover the accounts that exist rather than
hardcoding a list, because a panel that ignores a present account silently caps itself.

A judge may not score its own output, so a panel with exactly as many lanes as the release
rule requires starves every pair whose producer is one of those lanes. Require at least one
more lane than the rule, and refuse to start otherwise — the alternative is discovering it
hours later as batches that report "unjudged".

Never delete a lane from the accepted-identity set to retire it. Verdicts already recorded
by that lane are still true; separate the identity set from the runtime pool and change only
the pool.

## When a gate fails

Fix the translation. Do not widen the gate, do not add a waiver to make a red
line green, do not lower the judge count. A waiver is for a *defensible policy
disagreement* with a written reason — not for a defect you would rather not
address.

## Judging state, and what a green run does not prove

The gates prove properties of an artifact. They do not adjudicate between two
records that disagree, and they are not a progress percentage. Read
`references/evidence-authority.md` before resolving a conflict or reporting
status: runtime observation outranks readback, readback outranks a sealed
digest, and nothing outranks anything merely by being newer.

Report readiness on separate axes — pipeline, coverage, quality, runtime
verification, release. Quoting the gate pass rate as overall progress is the
most common way this project lies to itself.

A player-found defect is by definition one the gates missed, so fixing the
string is half the work: it has to land somewhere a machine re-checks it, or it
comes back on the next rebuild. Failed hypotheses get recorded with their
revisit condition rather than deleted. Read
`references/reports-and-failures.md`.

## Honest limitations

State these rather than implying coverage:

- No gate substitutes for playing the game. Layout is verified against font
  metrics, not against a running renderer.
- Swapping the text of two strings inside one control span is not structurally
  detectable.
- JSON artifacts are integrity-checked, not signed. The threat model is
  accidental corruption and model error — not a malicious local editor.
- Judge/producer separation is best-effort for rows translated before provenance
  logging existed.

## Containers and keys

Accepts CIA, CCI/`.3ds` cartridge dumps, and bare NCCH. Handles every documented
NCCH crypto method (0, 1, 10, 11), fixed/zero key, seed crypto, and title-key
encrypted CIA content.

**Key material is the operator's.** Nothing is bundled. Point `HANPATCH_KEYS` at
a directory with `boot9.bin`, `keys.txt` or `seeddb.bin`, or drop them in
`<project>/keys/`, and run `hanpatch keys` to see which slots resolved. Crypto
method 0 needs nothing at all.

Two habits worth copying: the bootROM keyblob is located by **searching for the
one KeyX that is public knowledge** and indexing off it, so no hardcoded file
offset can silently drift; and every derived key is **validated by decrypting a
section and checking its magic**, so a wrong slot fails loudly rather than
producing plausible garbage.

## Distributing the result

Ship a **release bundle**, not a ROM and not a binary delta:

```bash
hanpatch release --out MyPatch.hpk      # manifest + fonts + profile
hanpatch apply MyPatch.hpk --rom their.cia
```

Because the pipeline is deterministic, the recipient's rebuild is byte-identical
to yours, and the bundle records both hashes so it can prove it. On the reference
title that is 340 KB reproducing a 249 MB ROM in four seconds.

Do not reach for a binary delta on an encrypted container. CTR keystreams are
position-dependent, so one shifted byte kills every downstream match — both
xdelta3 and a block differ come out at ~82% of the full ROM, which is not a patch,
it is the game.

## Legal

**The operator decides what is lawful for them. Do not make that call for them,
and do not withhold a working capability as a guess about it.**

Ship tooling and your own translation. Do not redistribute someone else's game,
extracted text, key material, or licensed fonts — not as a legal judgement, but
because none of it is yours to hand out. Copyright and anti-circumvention law
varies by jurisdiction and is unsettled in several; say so and move on rather
than pretending to advise.

## Related

Text is not the only thing that needs translating. Logos, texture fonts, and
baked-in graphics live in image assets and need a separate pipeline — if a
`texture-logo-kr`-style skill is available, use it for those and this one for the
script. They compose: this pipeline owns the message containers and fonts, that
one owns the texture assets.

## Where the code is

<https://github.com/yazzang-homelab/hanpatch>

```bash
git clone https://github.com/yazzang-homelab/hanpatch && cd hanpatch
pip install -e .
```

Keep the game, its extracted text, and the built patch **outside** the
repository — a pre-commit hook refuses them.

## Reference

- `references/adapters.md` — the adapter contract in detail
- `references/qa-panel.md` — judge panel, dispositions, waivers
- `references/3ds.md` — CIA/NCCH/RomFS/BCFNT notes and pitfalls
- `references/scriptbook.md` — generating a bilingual script book from the seal
- `references/evidence-authority.md` — evidence order, conflict rules, readiness axes
- `references/reports-and-failures.md` — report triage, regression cases, failure ledger

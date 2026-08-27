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
platform+format    container, filesystem and font readers; archive and message formats
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
| 7 | `voice` | a sealed speech-style contract the shipped text no longer satisfies |

Then the packer **re-runs the QA validation in-process** before writing a byte.
The approval token is a convenience; the authority is the fresh revalidation.
Editing the manifest and the token together still fails.

Gate 7 runs **only for a title that opted into the staged ledger** — running it for
a legacy title would let a malformed `voice_contract` hard-fail a build that never
asked for the check. And it is a gate, not a report: `voice_gate.FAIL` raises and
the build stops. What it does *not* do is judge voice. This repository owns
provenance only — shape, which build the verdict describes, and the declared
authority — because a second marker implementation here would drift from the one
that already exists until the two disagree about the same line and nobody can say
which is right. A title that declares no contract passes as `NOT_DECLARED`, and
that word is stamped in the gate summary so silence is never read as clean.

## Staged QA state, for titles that opt in

A gate report says a gate passed. It does not say which release-stage claim that
pass supports, and it does not say what remains unproven. Reading a green gate
run as "the patch is verified" is the mistake the staged ledger exists to make
impossible.

`stage_ledger` records eight staged tokens separately — source QA, static binary
QA, RC build, RC readback, runtime smoke, canonical promotion, patch package,
release — and a token only leaves `NOT_RUN` when something actually proved it.
Three rules give it its shape.

**Existing authority is mapped, never re-implemented.** Most tokens are already
proven by code that ships: readback by `pipeline.verify` plus the title adapter,
packaging by `release.create`, publication by `channel.publish`, source QA by the
glossary/audit/qagate gates. Those rows are marked `mapping_only`; each of those
functions reports to the ledger after it has decided, and the ledger records what
it decided. It never becomes a second approval, packaging or publishing
authority, and a test asserts it exposes no verb that could.

`CANONICAL_PROMOTION` is the exception and stays `NOT_RUN` unless someone records
it. Promoting a release candidate to canonical is a judgement about whether the
patch is good enough to be the one people install, and no function in this
pipeline makes it. A token that filled itself in would be asserting that
judgement had happened.

**Pipeline success is not eight passes.** One gate moves one token. There is
deliberately no "the run finished, mark everything green" path, because the two
tokens that most tempt one — runtime smoke and canonical promotion — are exactly
the ones static success cannot establish. A fully green static run still leaves
both at `NOT_RUN`.

**A first failure stops the downstream claim.** A failed token forces every later
token to `NOT_RUN` with the failing token named, so a ledger can never show a
later stage passing on top of an earlier failure.

The ledger is a sibling of `manifest.json`, not a field inside it: carrying one
would change `RULESET`, and that invalidates every seal already shipped. Binding
is by reference — the ledger records the manifest digest and the built artifact
hash it describes, and reports staleness rather than silently re-binding.

All of this is opt-in through a versioned profile object. Absent or null is the
legacy path: no ledger code runs and no sidecar appears, proven in a subprocess
with call spies and a clean output directory.

### What a new title declares

Everything above is opt-in, and a title that declares nothing keeps the legacy
behaviour exactly. Five declarations exist; none requires any of the others.

| Declaration | Where | Effect when absent |
|---|---|---|
| `qa_upgrade: {schema_version: 1}` | profile | no ledger, no sidecar, no calls |
| `write_plan(rom, entries)` | adapter method | byte ownership not checked, and the ledger says so |
| `voice_contract` + `voice_authority` | profile | voice reads `NOT_DECLARED` and passes |
| `runtime_evidence` | profile, path or list of paths | `RUNTIME_SMOKE` stays `NOT_RUN` |
| `source_lang` / `target_lang` | profile | `hostrows` requires the axes on the command line |

`qa_upgrade` is the only one that gates the others: without it no staged QA runs
at all. The rest are independent, so a title can adopt byte ownership without
having a voice contract, or submit runtime evidence without declaring a write
plan.

Read the result with `hanpatch stages`. It prints every token with its status
and, for anything that did not run, the reason — which is the point: a token
that reads `NOT_RUN` is telling you what nobody proved.

## Byte ownership, beside entry verification

`Adapter.verify` asks whether each sealed string survived the round trip. A build
can answer that perfectly and still be broken: write the right text into the right
slots, and also clobber a reserved header byte. Verify returns clean, because
nothing in the entry contract looks at bytes nobody declared.

`expected_write` asks the byte-centric question. A write plan declares, per
region, where it writes, how long it is, what it expects to find there first, and
who owns it — then refuses four separate ways: the source did not hold what the
plan expected, two owners claim the same bytes, a write lands in a protected span,
or the final artifact differs somewhere no entry covers.

Preconditions are exact — literal bytes or their digest, never a wildcard —
because a plan that can match anything cannot prove it matched the right thing.
And the checker takes the source and final bytes as inputs; it never asks the
writer what it wrote, because a checker fed the writer's own account can only
confirm the writer agrees with itself.

One limit worth stating: a v1 plan describes writes in place, so a build that
changes the artifact's length is refused rather than checked. That covers the
fixed-slot and fixed-arena containers this was built against; a format that
grows its payload needs the plan to carry relocation, which v1 does not.

The two questions are complementary, not redundant, and the difference is
demonstrated rather than asserted: a fixture where declared text is entirely
correct and container integrity is valid, one reserved byte is clobbered, entry
verification returns clean and byte ownership refuses.

## Runtime evidence: shape enforced, story not

No static gate can establish that a patched game runs, and what counts as a
meaningful observation depends entirely on the title. The first line of dialogue
proves something in an RPG and nothing in a puzzle game.

So `runtime_evidence` validates shape and refuses to have an opinion about
content. Scenario, expected and observed are opaque JSON: no scene enum, no
required step names, no notion of what a good smoke test looks like. Baking any
of that in encodes one genre's idea of proof into a checker every other genre has
to satisfy.

What is enforced is title-independent: the document says which build it describes
and that hash must match, required fields exist with the right types, hashes are
hashes, depth and volume stay bounded, and the result is one of two words.

`NOT_RUN` is deliberately not one of them. It is a ledger state meaning nobody
looked, so a submitted document may never claim it. Absent evidence produces
`NOT_RUN` and nothing else — there is no code path that manufactures a passing
envelope, because a synthetic pass is indistinguishable from a real one once
written down.

Collecting that evidence is the operator's job, by whatever means the platform
allows. The pipeline stays emulator-free and has no dependency on any emulator
tooling; where one is useful, the skill points at it and stops there — see
`emucap` under Related, including the platforms it does *not* cover.

## Ideas worth stealing even if you never run this

**Capacity comes from the shipped text, not a guess.** The widest page the
original ever renders in a layout group is the proven bound. Group by
`family/key-shape` with digits folded, so `system/treasure` is bounded by the
one line it renders instead of borrowing its family's maximum.

**The glossary is scoped.** Short polysemous labels (`Dead`, `Key`, `Cure`) are
mandatory in the families that render them as UI labels and *forbidden* as
mandates inside narrative prose, where they are ordinary words.

**A particle after a runtime substitution has no single right answer, so stop
asking the model for one.** Where a row carries a substitution token, the
syllable an agreeing particle attaches to does not exist until the engine draws
the line, so any particle written at build time is a guess that is wrong for some
player. Resolve it deterministically instead: take the rendering from the
profile where the title declares the value FIXED (a party member the player
cannot rename), otherwise write the both-forms particle in one canonical shape,
and REFUSE a particle with no readable both-forms shape so the row is reworded
rather than shipped wrong. Example-rendering fields are not evidence of a fixed
value. Keep the resolver behind one seam, so an engine-side run-time hook can
replace it later without the pipeline pretending it already has one.
Measured counts: `references/cases.md`.

**A line may not break inside a word, and may not open on a closing mark.** The
wrapper breaks between *units*, and a unit is everything with no whitespace in
it — a substitution token and the particle welded to it move together. A word
wider than the box is *refused*, not split: a fallback that breaks character by
character raises no problem while producing exactly the defect a reader
reports, with the gate claiming clean. Measured counts: `references/cases.md`.

**Source punctuation is converted, and a sentence does not end twice.** Where the
source script uses punctuation the target language renders differently, convert
it — and then drop the converted stop where the sentence already ended in an
ellipsis or an exclamation. Scope this to the source language that actually needs
it; a title that authors such punctuation deliberately as a stylistic device must
not have it normalised away. Whitespace in front of a closing mark goes with it,
because a wrapper that breaks at spaces is how a leading ellipsis appears.
Measured counts: `references/cases.md`.

**A gate that only normalises cannot fail, so audit compares the seal against
its own rules.** `translate.check` returns a repaired string, and `audit` used to
throw that return value away and look only at the problem list — so every rule
added after a manifest was sealed reported the corpus clean while the ROM shipped
text the build disagreed with. `audit` now reports `normalisation-drift` for any
sealed value that is not equal to what today's rules produce from it, and the
manifest `RULESET` is bumped whenever those rules change so an old seal cannot be
packed.

**Two judges minimum, and a producer may not judge its own output.** One judge
produces correlated false negatives: a reviewer sample found real defects in strings a
single judge had passed (`references/cases.md`). Judge verdicts are structured
(`pass|defect|policy`), never keyword-sniffed from prose, and an invalid or
missing disposition is *dropped* so the row stays pending and rotates providers,
rather than being synthesised into a pass.

**Waivers are hash-bound.** A waiver keys on `sha1(source + '\0' + shipped
text)`. Edit either side and the waiver goes stale and blocks the build. A
waiver needs a category and a real reason.

**Glyph authority is the built font.** A character is renderable because it
exists in the font that ships, verified by reading the packed font back out of
the ROM — not because it is in some Unicode range.

**Measure the format before writing it.** A pixel format's field names do not
tell you what the engine does with them: a channel that looks like alpha may be
ink coverage the renderer multiplies against a shading mask, and the obvious
inverse then produces glyphs that are visibly wrong in a way no gate catches.
Derive the mapping from the shipped asset rather than from the format name.
Guessing a binary format's semantics costs a whole rebuild cycle. The measured
3DS case is in `references/3ds.md`.

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

## A player-entered proper name is settled by evidence, in this order

Do not choose a player-entered name from taste or from a string search. A mistaken
hardcode turns a player choice into a different character name throughout the patch.
Record the title, retail version, platform, save state, and captures or notes for the
evidence below; stop at the first step that answers.

**1. Observe the actual new-game input path at runtime.** Start a fresh retail game and
reach the name-entry UI. Record whether the field is prefilled, whether it accepts an
empty value, what action advances it, and the available input method. Only a prefill or
default value *proven in that actual retail path* may be hardcoded as a default. The
field's contents before the player edits it, not a ROM string, establish that fact.

DQ7 is the counterexample: its retail name field is empty, the game cannot proceed with
an empty field, and its input method is Japanese-only. Standalone `アルス` literals still
exist in its data. They therefore do **not** prove that `アルス` is a canonical or default
player-entered name. This settles the source fact ("no retail default"), but it does not
settle the localisation defect ("the target-script player cannot enter a name").

**2. Classify ROM literals by their rendering context.** Search extracted text and trace
each candidate through the renderer. Label it as a demo, sample, save preview, system
example, or another fixed-context literal, versus a runtime player-name substitution
such as `{HERO}` or `{PLAYER}`. A literal in a fixed context says only what that context
renders; it is not evidence of the new-game default. A runtime substitution establishes
that the name is player-controlled, not what its default is. Keep the counts and context
examples so later changes can update every affected fixed rendering without confusing it
with the player choice.

When the source has no default and the shipped input method cannot produce the target
script, treat usability as a separate product decision. First localise the input method
without removing player choice if that is technically safe. If it is not, search official
publisher/settings material for a canonical character name and use it as an explicit
target-build prefill or hardcoded fallback. Record that intervention as a localisation
fallback, never as the source game's default. If no official name is established, ask the
user before choosing one.

**3. Search official publisher or settings sources when the runtime default is
inconclusive, or when an unusable source-language input method requires a target-build
fallback.** Prefer publisher documentation, official settings material, or the official
site; exclude fan spellings and self-invented transliterations. Record the source beside
the profile term and distinguish an officially named character or localisation fallback
from a proven retail input default.

**4. Ask the user if both runtime and official evidence are inconclusive.** State which
checks were inconclusive. Do not invent a name, infer one from the most common literal,
or quietly select a fan spelling.

Whatever the answer, apply it to the **profile source** (`profiles/<title>.json:terms`)
and then to every existing fixed rendering that the context classification requires.
`work/ko/glossary.json` is derived; a fix that lives only there is lost on the next
rebuild. After changing a name, re-run the divergence scan that compares one source
string across families — a name promoted into the glossary can still disagree with a
row translated before the promotion.

Transliteration, when it is your call: follow the receiving language's official
transcription rules rather than what reads nicely, and write down which rule you
followed. `アルス` is `아루스` under the Korean standard (`ル` → `루`); `아르스` is the
common habit. Either is defensible; only one may ship.

## The QA repair cycle

A judge verdict is about one exact pair: the source and *the value that ships*. Everything
below was measured on a closed cycle, not inferred; the counts are in
`references/cases.md`.

**Freshness is decided against the sealed artifact.** A build that resolves overrides or
post-processes text makes the sealed value differ from the raw translation store, so
comparing verdicts against the raw store discards real complaints as stale and can report a
repair pass complete while most of it is untouched. Print the flagged count and the
actionable count together; a large silent gap between them is an authority bug, not progress.

**Repair, reseal and re-judge are one cycle.** The panel only ever reads the sealed
artifact, so a repaired value that was never resealed is invisible and the next pass
reproduces the same complaints forever.

A repaired value is a NEW pair and therefore arrives with no verdicts, which is what keeps
the loop honest — an unhelpful repair is judged again from scratch instead of inheriting the
old complaint. It is also why a pass that repairs nothing must stop and escalate rather than
retry an identical prompt.

**One pass adds one verdict per pair.** Requiring N distinct judges takes N passes; a run
that reports every pair at one judge has not failed, it has finished one pass.

**Exclude judges per row, never per batch.** A judge may not score its own output. Applying
that test to the whole batch starves a small pool: a mixed batch excludes every lane, and
those pairs never reach the required panel size no matter how many passes run. Pairs sat
unjudged across full passes until the test moved to the row (`references/cases.md`).

**One panel per verdict file.** Each batch rewrites the whole document from the process's
own copy, so two concurrent panels do not merge — the later writer discards the other's
verdicts. Take an exclusive lock at startup, and flush before the atomic rename so a crash
cannot lose verdicts the log already reported.

### When repair stops converging

A row whose field is intrinsically too small for any faithful wording cannot be repaired,
only re-worded, and each rewrite draws a fresh preference from a different judge. Measured
on a fixed-width label corpus: six full cycles moved the blocked count 447 -> 185 -> 38 ->
39 -> 38 and then oscillated, because the remaining rows were compact technical identifiers
with two or three syllables of room.

The honest exit is a **waiver bound to the exact pair**, carrying a category and a reason an
auditor can check per row, never a widened budget and never a relaxed floor. A waiver says
"this wording is what the field can hold", so it must go stale the moment the value changes:
re-check that no waiver is unused after every reseal, because a stale waiver is a claim about
text that no longer ships.

Two failure modes look identical in the log and are not. A pass reporting `ok=0 calls=0` per
family is the selector finding nothing actionable — usually a staleness guard comparing
against the sealed value while repairs land in the translation store. A pass reporting
`ok=N calls=N` whose blocked count does not move is genuine non-convergence, and that is the
one a waiver answers.

### A slot another runtime owns is not yours to translate

A binary can hand a string to a renderer you do not control — a firmware dialog, a system
font, an OS toast. Retargeted glyph codes travel as the code of the cell they were baked
into, so the game's own renderer draws the new script while the foreign renderer draws that
code's *original* character. One correct-looking string on the same screen proves nothing
about the rest: both classes live in the same binary and arrive in the same extraction pass.

Identify the class by reverse-mapping the garbled output through the font map; if it returns
a sentence shape in the target language, those codes are yours and the renderer is not. Then
declare the owning slots as a **profile fact** — a per-title skip list keyed by slot — not as
a deletion. The source string stays, untranslated, and the manifest reports it as
deliberately skipped rather than missing. A keyword list over the dialog vocabulary tells you
where to look, never which rows to exclude.

The measured PSP case, including the two proven firmware slots and the glyph-table rows that
cannot encode isolated letters at all, is in `references/runtime-ownership.md`.

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

The one case where deletion is correct is a lane that recorded **nothing**. A lane can also be
worse than slow: a gateway account that serves one model for every model you ask for turns
several lane names into a single opinion, which is the opposite of what a panel is for. Retire
that by *deleting* the names, but only after proving no verdict carries them — count them in
the ledger first, relabel any that exist to the model that actually answered, and keep that
true model as an accepted identity, because the gate rejects an unknown judge and would
otherwise read your own history as forgery. Leave the deleted names behind as a denylist with a
test asserting they appear in neither the runtime pool nor the identity set, or they come back
by copy-paste.

## Supervising a run nobody is watching

A supervisor exists to notice that work stopped, so it must be harder to kill than the work
it supervises. Catch the *exits*, not just the exceptions: loaders that raise `SystemExit`
on a malformed document are right for a CLI and fatal inside a supervisor. One transient bad
read of a large verdict file ended supervision while the repair loop kept running
unobserved; after the fix the supervisor detected a dead loop and restarted it on its own
(`references/cases.md`).

**A supervisor cannot supervise its own death.** In-process robustness only covers failures
the process survives. This one was killed outright with no log line, and the repair cycle
then sat dead for hours while every artifact on disk still looked healthy and the last log
line still read `repair=up` (`references/cases.md`). Run the supervisor under an out-of-process manager with
automatic restart, and verify the restart by killing it — not by reading the config.

**A hung unit must not end the pass.** A per-unit timeout that raises out of the loop turns
one stuck family into a dead run. Catch it, record the unit, and continue: the work is still
flagged, so the next pass retries it. Size the timeout against the measured unit, not against
the whole job — an hour-long limit on a twenty-second unit is a stall, not a safety margin.

Repeated `SIGSEGV` from unrelated binaries — the interpreter and a vendor CLI both — is a
machine-stability signal, not evidence about the corpus. Lower concurrency, record the
crashed units, and let the next pass retry them.

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

A patched build has to be repackaged into whatever container the platform ships,
and that container is usually encrypted. Two habits are worth copying wherever
you meet one.

**Key material is the operator's.** Bundle none of it. Take a path from the
environment or the project directory, and give the operator a command that says
which slots resolved, so a missing key is a clear message rather than a corrupt
output.

**Locate structures by search, and validate by decryption.** Index off a value
that is public knowledge rather than a hardcoded file offset, which silently
drifts between revisions; then prove a derived key by decrypting a section and
checking its magic. A wrong key should fail loudly instead of producing
plausible garbage.

The 3DS specifics — the container formats accepted, the crypto methods, the
key filenames, and the `hanpatch keys` command — are in `references/3ds.md`.

## Distributing the result

Ship a **release bundle**, not a ROM and not a binary delta:

```bash
hanpatch release --out MyPatch.hpk      # manifest + fonts + profile
hanpatch apply MyPatch.hpk --rom their.cia
```

Because the pipeline is deterministic, the recipient's rebuild is byte-identical
to yours, and the bundle records both hashes so it can prove it. The bundle is orders of
magnitude smaller than the image it reproduces (`references/cases.md`).

Do not reach for a binary delta on an encrypted container. Position-dependent keystreams
mean one shifted byte kills every downstream match, and the delta comes out close to the
size of the whole image, which is not a patch, it is the game. Container specifics and the
measured ratio: `references/3ds.md`, `references/cases.md`.

Publish the bundle to an **update channel** so a fix reaches the people already
running the old text:

```bash
hanpatch publish dist/MyPatch.hpk --root /srv/hpk \
    --url-base https://krpatch.duckdns.org/hpk/   # writes index.json, a page, hpk-update.py
hanpatch update --dir ~/patches                    # the receiving side
hanpatch update --check                            # exit 10 when an update waits
```

The channel is a static directory: `index.json` is derived from the per-release
sidecars, so it can be rebuilt from what is served, and a published version is
never rewritten — republishing identical bytes is a no-op and different bytes
under a used version name are refused. The client verifies the announced size and
SHA-256 and deletes anything that does not match, which is what makes the
standalone `hpk-update.py` safe to hand to a stranger. Do not answer "is there a
newer patch" with a service that must be operated and authenticated; the answer
is a file.

**On real hardware a rebuilt image can be the wrong shape.** Where the medium is
read-only and a signature covers the headers, a rebuilt image is refused by a
retail console no matter how correct its contents are. Ship a runtime file-
replacement pack instead, if the platform's custom firmware offers one: the
changed asset files plus a code patch, read off storage at boot. The pack
contains game data, so it is generated by the player and never published. The
3DS form of this, its two traps, and the measured pack size are in
`references/3ds.md`.

**Do not answer "make it work in the browser" by porting the container and crypto
code to JavaScript.** A second implementation of a verified pipeline drifts, and
the drifting copy is the one that ships a corrupt image. Run the same pipeline in
wasm instead, and give it real storage: an in-memory filesystem mirrors the whole
scratch space into RAM, which for a large container is several times the image
size. Measured timings: `references/cases.md`.

## Legal

**The operator decides what is lawful for them. Do not make that call for them,
and do not withhold a working capability as a guess about it.**

Ship tooling and your own translation. Do not redistribute someone else's game,
extracted text, key material, or licensed fonts — not as a legal judgement, but
because none of it is yours to hand out. Copyright and anti-circumvention law
varies by jurisdiction and is unsettled in several; say so and move on rather
than pretending to advise.

## Related

**`krpatch` is the front door.** It owns no format code — only the order the five
tracks run in and the acceptance criteria that stop a track being skipped. Start
there when the job is "localise this ROM" rather than a specific stage, because
the failure it exists to prevent is the one this pipeline cannot see on its own: a
build at 99.98% coverage with every gate green that still drew Japanese on
hardware, because three surfaces had never had their denominator counted.

Four companions below, each owning something this pipeline deliberately does not.
Read them when the work touches their axis; none of them is optional in the sense
of "nice to have" — the code here has consumer boundaries built for two of them.

**`hancharacter` — speech-style preservation.** Gate 7 above consumes a verdict
this pipeline cannot produce. The handoff is concrete and runs in both directions:
`hanpatch hostrows` exports the sealed text as host rows for a voice reviewer
(`interop.export_host_rows`, with the language map **declared, never inferred** —
guessing which language fills the evidence column yields a document that looks
correct from both sides while the axes are transposed), and `hancharacter`'s
manifest adapter can instead read the seal directly and re-derive the digest.
`voice_gate.py` then checks provenance and nothing else. If a title declares a
`voice_contract`, load that skill; without it there is no way to produce a passing
verdict and the build will sit at `NOT_DECLARED` or fail.

**`texture-logo-kr` — baked-in graphics.** Text is not the only thing that needs
translating. Logos, texture fonts and baked-in art live in image assets and need a
separate pipeline; use that skill for those and this one for the script. They
compose: this pipeline owns the message containers and fonts, that one owns the
texture assets.

**`krpatch-publish` — distribution and feedback.** `hanpatch release` and
`hanpatch publish` produce and post the bundle, but the site that serves it, its
`data/games.json` registry, the independent apply round-trip check and the
feedback triage live in that skill. Use it for anything past `--url-base`; this
pipeline's job ends at a verified bundle.

**`emucap` — runtime evidence collection.** The runtime section above says the
evidence is the operator's job and that the skill points at a tool where one is
useful. This is that pointer: `emucap` drives an emulator and reads its memory,
which is what turns "the build boots" into a `runtime_evidence` document. Check
the scope before planning around it — its adapters are `mgba`, `mednafen`,
`mesen2`, `pcsx-redux`, `flycast` and `mame-pc98`, so **it does not cover PSP or
3DS**, and the titles currently in `recipes/` and `profiles/` are on those two
platforms. For them, collect evidence with the platform's own emulator and keep
the pipeline emulator-free as documented. `status.methods` on a live connection is
the authority on what that host can actually do — not a static guess from here.

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
- `references/3ds.md` — 3DS container, filesystem and font notes, with the pitfalls
- `references/scriptbook.md` — generating a bilingual script book from the seal
- `references/evidence-authority.md` — evidence order, conflict rules, readiness axes
- `references/reports-and-failures.md` — report triage, regression cases, failure ledger

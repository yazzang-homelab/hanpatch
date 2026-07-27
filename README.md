# hanpatch

Gate-enforced localisation pipeline for game ROMs. Extract a title's text,
translate it with whatever models you have, and then **refuse to build the patch
until a machine can prove the result is consistent**.

Reference implementation: a complete Korean patch for *Crimson Shroud* (3DS) —
3262 strings, two independent judges per string, byte-identical rebuild.

```bash
pip install -e .
hanpatch init --title "My Game" --adapter my_game --profile profiles/my_game.json
hanpatch extract && hanpatch fonts
hanpatch translate --family dialogue --workers 4
hanpatch qa --judges 2 --workers 4
hanpatch build && hanpatch verify
```

## Why gates instead of a better prompt

Machine translation of a game script fails in ways a spot check cannot see: a
character's name spelled three ways across 500 lines, a line that fits on page 1
and overflows on page 3, a `<player>` tag silently dropped, a judge that passes
its own output. Prompts do not fix any of it. The only thing that does is making
the build impossible until each failure mode is mechanically excluded.

Every check re-derives its claim from the artifact that ships. A gate that can be
skipped is not a gate — there is no `--force`.

## Gate order

| # | Gate | Rejects |
|---|------|---------|
| 1 | `glossary` | a proper noun rendered two ways; a UI label mandated inside prose |
| 2 | `capacity` | text longer than the widest page that layout group ever renders |
| 3 | `materialize` | rule-derived rows that fail their own validator |
| 4 | `audit` | untranslated rows, tag damage, register drift, duplicate meanings |
| 5 | `manifest` | nothing — it seals every shippable string into one digest |
| 6 | `qagate` | any entry without N independent judge passes **for that exact pair** |

The packer then re-runs QA validation in-process before writing a byte. The
approval token is a convenience; the authority is the fresh revalidation, so
editing the manifest and the token together still fails.

## Architecture

```
hanpatch/config.py       project + title profile resolution
hanpatch/adapter.py      the extract/inject/verify contract
hanpatch/pipeline.py     the fail-closed gate runner
hanpatch/*.py            glossary, translation, layout, audit, manifest, QA
hanpatch/platforms/      container layers (threeds: CIA/NCCH/RomFS/BCFNT/BLZ)
hanpatch/formats/        message and archive readers
hanpatch/adapters/       one module per title
profiles/                one JSON per title: markup, terms, budgets, fonts
```

The core never reads a ROM. An adapter never decides wording — the test suite
fails any adapter that imports the wording modules. Adding a title is a profile
plus an adapter; nothing else changes. Adding a platform is one directory.

## What is actually reusable

**Any title, any platform.** The glossary/scoping model, layout capacity
derivation, tag-skeleton preservation, sharded resumable translation with
provider rotation, the audit gates, the sealed manifest, the multi-judge QA panel
with hash-bound waivers, and the script-book generator. None of it knows what a
ROM is.

**3DS titles.** The whole container layer: CIA/NCCH crypto (method 0, no boot9),
IVFC RomFS read and rebuild, BLZ, BCFNT with correct RGBA4444 shading semantics.

**Per title.** The message/archive format and the profile. For *Crimson Shroud*
that is 250 lines of adapter and one JSON file.

## Ideas worth stealing

- **Capacity is measured, not guessed.** The widest page the original ever renders
  in a layout group is the proven bound. Group by `family/key-shape` with digits
  folded so `system/treasure` is bounded by its own single line.
- **The glossary is scoped.** `Dead`, `Key`, `Cure` are mandatory as UI labels and
  forbidden as mandates in prose, where they are ordinary words.
- **Two judges, and a producer may not judge its own output.** One judge yields
  correlated false negatives — a sample of 5 strings it passed held 4 real
  defects.
- **Dispositions are structured, never keyword-sniffed.** A malformed verdict is
  dropped so the row stays pending and rotates providers, rather than being
  synthesised into a pass.
- **Waivers are hash-bound** to `sha1(source + '\0' + shipped text)`. Edit either
  side and the waiver goes stale and blocks the build.
- **Glyph authority is the packed font**, read back out of the ROM — not a Unicode
  range.
- **Measure a binary format before writing it.** 3DS font sheets are RGBA4444
  where `A` is coverage and `RGB` is a shading mask multiplied with the text
  colour. The naive `255 - coverage` gives flat-black glyphs with a bright rim:
  fine in a PNG, unreadable on hardware.

## Tests

```bash
python3 tests/test_gates.py                                  # logic only
HANPATCH_PROJECT=/path/to/project python3 tests/test_gates.py # + corpus cases
```

Each case is a concrete attack that once slipped through: fullwidth-Latin
corruption, reordered control tags, text moved out of a control span, capacity
overflow, glossary drift, shard races, manifest tampering, forged judge ids,
foreign-key waivers, coordinated manifest+token edits.

## Skill

`skills/hanpatch/` is an agent skill (Claude Code / GJC format) carrying the
doctrine, the adapter contract, the QA panel design, and the 3DS format notes.
Copy it to `~/.claude/skills/` or `~/.gjc/skills/`.

## Requirements

Python 3.9+, `pycryptodome`, `pillow`. Translation and judging need model
endpoints; any OpenAI-compatible base URL works, including free tiers. Configure
them in `hanpatch/providers.py`.

## Scope and honesty

- No gate substitutes for playing the game. Layout is checked against font
  metrics, not a live renderer.
- Swapping the text of two strings inside one control span is not structurally
  detectable.
- JSON artifacts are integrity-checked, not signed. The threat model is
  accidental corruption and model error, not a malicious local editor.
- Judge/producer separation is best-effort for rows translated before provenance
  logging existed.

## Legal

This repository contains **tooling only**. It ships no game data: no ROM, no
extracted text, no translated script, no patched image. `.gitignore` excludes
those paths, and a pre-commit hook is provided to keep it that way.

You are responsible for owning the game you patch and for the licence of any font
you embed. Fonts must be redistributable — the reference build uses
[NeoDunggeunmo](https://github.com/Dalgona/neodgm) (OFL-1.1) and cites it.

## Licence

MIT — see [LICENSE](LICENSE).

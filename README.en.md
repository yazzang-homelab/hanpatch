# hanpatch

Gate-enforced localisation pipeline for game ROMs. Extract a title's text,
translate it with whatever models you have, and then **refuse to build the patch
until a machine can prove the result is consistent**.

Reference implementation: a complete Korean patch for *Crimson Shroud* (3DS) —
3262 strings, two independent judges per string, byte-identical rebuild.

한국어 안내서: [README.md](README.md)

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

**3DS titles.** The whole container layer: CIA, CCI/`.3ds` cartridge dumps and
bare NCCH; every documented NCCH crypto method (0, 1, 10, 11), fixed/zero key,
seed crypto, and title-key encrypted content; IVFC RomFS read and rebuild; NCSD
partition rebuild; BLZ; BCFNT with correct RGBA4444 shading semantics.

Key material is never bundled — supply `boot9.bin`, `keys.txt` or `seeddb.bin`
and `hanpatch keys` reports exactly which slots are present. Titles using crypto
method 0 need nothing. Every derived key is validated by decrypting a section and
checking its magic, so a wrong slot fails loudly instead of yielding garbage.

**Per title.** The message/archive format and the profile. For *Crimson Shroud*
that is 250 lines of adapter and one JSON file.

## Ideas worth stealing

- **Capacity is measured, not guessed.** The widest page the original ever renders
  in a layout group is the proven bound. Group by `family/key-shape` with digits
  folded so `system/treasure` is bounded by its own single line.
- **Reproduce the script's own line breaks, don't invent your own.** Which tags
  break a line is a per-title fact (`hard_break`, and it may be empty — Crimson
  Shroud's `<br>` only advances the message). Where the script wants a new line it
  writes a break right after a control tag; that position is layout, so it is
  transplanted onto the translation while only mid-sentence breaks are re-flowed.
  Folding one away merges two sentences into a line that runs off the screen.
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
python3 tests/test_gates.py                                   # logic only
HANPATCH_PROJECT=/path/to/project python3 tests/test_gates.py  # + corpus cases
python3 tests/test_containers.py                              # crypto/container/delta
```

Each case is a concrete attack that once slipped through: fullwidth-Latin
corruption, reordered control tags, text moved out of a control span, capacity
overflow, glossary drift, shard races, manifest tampering, forged judge ids,
foreign-key waivers, coordinated manifest+token edits.

The container suite synthesises its own crypto inputs — a title key of its own
choosing, wrapped with its own common key — so the CBC/IV layout, the common-key
search, the bootROM anchor scan and the seed derivation are all covered without
any real key material. It caught a live bug: the key scrambler's addition can
carry past bit 127, and rotating the unmasked sum folded that carry back in and
collapsed distinct KeyY values onto one key.

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

## Distribution

`hanpatch release` bundles your translation — the sealed manifest, the built
fonts, the profile — and the recipient applies it to their own copy:

```bash
hanpatch release --out "MyPatch.hpk"        # 0.34 MB for the reference title
hanpatch apply "MyPatch.hpk" --rom their.cia
```

The bundle records the expected input and output hashes, so applying it either
reproduces the author's build byte-for-byte or says so. Measured on the reference
title: a 340 KB bundle rebuilds a 249 MB ROM to the author's exact sha256 in four
seconds.

A raw binary delta is also available (`hanpatch delta`, xdelta3 or a built-in
block format with a dependency-free applier), but for a CTR-encrypted container
it is the wrong tool: the keystream is position-dependent, so one shifted byte
destroys every downstream match. Both backends measure ~82% of the full ROM on
the reference title. Use the bundle.

### The update channel

A file copied by hand rots: whoever downloaded it in March cannot learn that the
March text mistranslated a boss name. `hanpatch publish` puts a bundle in a
static directory; the recipient pulls changes with `hanpatch update` or with the
dependency-free `hpk-update.py`.

```bash
# publishing side — a directory, not a service
hanpatch publish "dist/MyPatch.hpk" --root /srv/hpk \
    --url-base https://krpatch.duckdns.org/hpk/

# receiving side
hanpatch update --dir ~/patches       # fetch what changed
hanpatch update --check               # report only; exit 10 if something changed
python3 hpk-update.py --dir ~/patches # the same, without installing hanpatch
```

`index.json` is *derived* from the published sidecar files, so the channel has no
database to lose and can be rebuilt from the directory it serves. Bundles are
published under version-stamped names and overwriting one is refused, so a web
server may mark them immutable and a mirror can be a dumb copy. The client
trusts the channel for *what exists* and nothing else: every download is checked
against the announced size and SHA-256, and a bundle that fails is deleted
instead of installed. That is why the updater can be a script you paste into a
terminal.

Reference channel: <https://krpatch.duckdns.org/hpk/>

### Applying it in a browser

`web/apply` is the same pipeline, in wasm. Drag a ROM in, get the patched file
out, with nothing installed and nothing uploaded. It does not reimplement the
pipeline in JavaScript on purpose: a second copy of verified code drifts, and the
drifting copy fails quietly. Here the result hash is checked against the
author's build every run.

```bash
python3 tools/deploy_web.py --root /srv/krpatch-web --fetch-pyodide
python3 web/serve.py --port 8123     # dev server, same COOP/COEP as production
node web/run_headless.mjs "http://127.0.0.1:8123/selftest/engine.html?rom=…&bundle=…&keys=…"
```

Measured (headless Chromium 147, i5-9500):

| target | command line | browser | result |
|---|---|---|---|
| reference title, 249 MB CIA | 9.7 s | 66–80 s | identical hash |
| DQ7, 2.0 GB 3DS | 2 min 14 s | 8 min 6 s | identical hash |

Three things were needed to make that work in a browser. Pyodide's
`mountNativeFS` mirrors a directory into RAM, which is useless when the scratch
space is 4.3 GB, so `web/apply/opfs-bridge.js` bridges OPFS into Emscripten's
filesystem: a proxy worker owns the synchronous access handles and the Pyodide
worker blocks on `SharedArrayBuffer` + `Atomics.wait`, which is why the path is
served cross-origin isolated. Python reads and writes in 8 KB units and one
round trip costs 19 µs, so each open file gets a 4 MB coalescing window and each
node remembers its size — measured read amplification dropped from 6.2 GB to
0.4 GB. And OPFS has no symlinks while the adapters stage untouched RomFS as
links (DQ7 copies two directories out of 1.4 GB), so the bridge keeps an
in-memory link table.

Needs a current Chrome or Edge (OPFS synchronous access handles,
`SharedArrayBuffer`). The server only has to serve static files with COOP/COEP
headers on that path.

## Legal

**You decide what you may lawfully do; this project does not decide it for you.**

This repository ships source only — no game data, no ROMs, no extracted or
translated text, no key material, no fonts. You supply the game you patch, any
key material your dump needs, and any font you embed, and you are responsible for
all three plus whatever you distribute. Copyright and anti-circumvention law
varies by country; check yours.

No capability here is withheld on a guess about legality. See
[NOTICE.md](NOTICE.md) for the full statement, and the MIT licence for the
warranty disclaimer, which applies in full.

The reference build uses [NeoDunggeunmo](https://github.com/Dalgona/neodgm)
(OFL-1.1) and cites it.

## Licence

MIT — see [LICENSE](LICENSE).

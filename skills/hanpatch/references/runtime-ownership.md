# Slots owned by another runtime — the measured PSP case

Title: Classic Dungeon X2 (PSP). Corpus 11,335 shippable strings, 465 of them in
`eboot.elf`.

## Two renderers in one binary

Hangul ships by retargeting kanji-area font cells, so a Korean syllable travels as the
Shift-JIS code of the cell it replaced. The game's own text renderer draws the baked
glyph. The PSP firmware dialog (`sceUtilityMsgDialogInitStart`,
`sceUtilitySavedataInitStart`) uses the system font and therefore draws that code's
original kanji — so one build shows correct Korean in `뒤로가기` and garbage in the
install dialog on the *same screen*.

That one correct string caused the original mistake: all 465 EBOOT rows were wired as
translatable after the title-menu item rendered correctly on device.

Identification that worked: reverse-map the garbled characters through `font_map.json`.
It returned a Korean sentence shape, which proves the codes are ours and the renderer
is not.

## Proven firmware-owned slots

| slot | source |
|---|---|
| `eboot.elf/off2410c4` | `ゲームデータがロードできませんでした。\nインストール機能を無効にしました。` |
| `eboot.elf/off241110` | `ゲームデータを検出しました。\nインストール機能を有効にしました。` |

Both are declared in the profile's `skip_keys` and ship as original Shift-JIS. The
manifest drops from 11,335 to 11,333 entries and the build reports `2 left as source` —
the difference between "deliberately skipped" and "missing".

The wider savedata/install vocabulary (メモリースティック, 記録メディア, セーブデータ,
インストール, 上書き, 空き容量) is where to look; only these two were proven on device.

## Not a defect

"Install, then everything is Japanese" was stale installed data on the Memory Stick.
Clearing it restored Korean. Nothing in the patch to fix.

## Glyph tables cannot hold isolated letters

Two EBOOT rows are font-coverage specimens listing the characters a sheet can draw.
Translating them to standalone Jamo (`ㄱㄴㄷ…`, `ㅏㅑㅓ…`) produced rows the baked font
cannot encode at all, because the charset is derived from the seal and the seal holds
composed syllables:

```
UNENCODABLE eboot.elf/off248bd3 'ㄱ' is neither in the font map nor Shift-JIS
```

Keep the row a *specimen* and write it in composed syllables. The per-title spacing
rule still applies — the measured longest word bounded these rows to eight-character
runs, so the specimen is spaced into groups instead of one long run.

## Two fixed-width traps found in the same pass

- A name field storing 6 bytes cannot hold 오가사와라 (10 bytes). The three-syllable
  abbreviation is not a style choice, it is the field.
- A 14-byte music-title field cannot carry both 초절기교 and 모래폭풍. Dropping the
  wrong half changes the meaning and a judge then reports it as a defect, so keep the
  half that carries the referent.

## Final state

Gate `ok:true` at iteration 93, manifest digest `5ca0824c9bd3ec06`, 11,333 translated
entries, 35 exact-pair waivers, 0 unencodable rows, `hanpatch verify` 0 problems.

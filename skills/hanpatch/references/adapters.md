# The adapter contract

An adapter is the only code that knows how a title stores text. It converts
between the game's containers and two normalised documents.

## Documents

`work/text_src.json` — written by `extract()`

```json
{
  "dialogue": [
    {"key": "mes_ch1_1_001", "en": "The rain has not let up.", "jp": "雨は止まない。"}
  ]
}
```

`en` is the translation source. `jp` is optional and used for two things: as the
source for rows whose English is a placeholder, and as reference context in the
script book. `note` is free-form and shown to the translator.

`work/<lang>/manifest.json` — read by `inject()`

```json
{"digest": "a23200220a743754…", "entries": {"dialogue/mes_ch1_1_001": "비는 그치지 않는다."}}
```

Keys are `family/key`. The digest covers every entry; the QA gate binds its
approval to it.

## Methods

```python
@register('my_game')
class MyGame(Adapter):
    platform = 'threeds'

    def extract(self, rom):           -> int   # entries written
    def inject(self, entries, rom, out) -> dict  # stats
    def verify(self, rom, entries)    -> list  # problems; [] means clean
    def build_fonts(self)             -> list  # optional
    def font_paths(self)              -> (src, out)  # optional
```

### extract

Unpack everything the later stages need into `extracted/`: the fonts to measure
against and the untouched containers to rebuild from. Write `text_src.json`.
Read *both* language trees if the ROM ships them — the Japanese text is free
context and costs one extra file read.

### inject

Take text **only** from the passed `entries`, which come from the sealed
manifest — never from the working translation memory. Fail closed on both sides
of the ledger:

- a shippable source key with no manifest entry → **blocked**
- a manifest entry that no container consumed → **blocked**

The second case is the one people forget. It catches renamed keys, stale
manifests, and family typos.

Write the translation into every language tree the title might select, so
behaviour does not depend on the console's language setting.

### verify

Re-read the artifact you just built and prove the text survived. Check container
hashes, then compare every entry byte-for-byte after a full round trip, then
confirm every non-ASCII character the translation uses exists in the font *as
packed in the ROM*. Return problems; do not print and swallow them.

## Rules

1. **Never import the wording layer.** No `translate`, `glossary`, `josa`,
   `providers`, `wrap`. The test suite enforces this. An adapter that decides
   wording has moved policy into the container layer where no gate can see it.
2. **Round-trip before you write.** Rebuild the untouched original and diff
   against the input. Not bit-exact means the reader is wrong, and every later
   failure will be misattributed.
3. **Link, do not copy, untouched bulk.** Movie and audio directories dominate
   ROM size; symlink them into the staging tree.
4. **Preserve entry order and metadata.** Archive rebuilds must keep the original
   order, names, and per-entry metadata; only offsets and sizes change.

# Script book

`hanpatch book` renders the corpus as a bilingual static site. The point is that
it is generated from `work/text_src.json` plus the **sealed manifest** — the same
bytes the ROM ships — so the document and the game cannot disagree, and the
digest printed on the cover identifies which build it describes.

## Output

- `index.html` — cover, statistics, translation policy, reading instructions
- `story.html` — story sections in narrative order, then per-area messages, then
  rules narration
- `appendix.html` — characters, places, enemies, equipment, items, spells, UI
- `script.md` — the same content as Markdown, for archiving
- `style.css`, `app.js`, `robots.txt` (noindex)

## Reading order is a feature

Key patterns are what recover order. Chapter/scene keys sort into narrative
order; area-message families do not — they fire when a player visits a place, in
whatever order that happens. Mixing them into one stream breaks the story.
Present the story sequence numbered (`n/38`), area messages grouped by area, and
tutorial/rules text last.

Give the reader a scope switch (story only / story+areas / everything),
prev-next links per section, and a remembered scroll position. Without those a
600 KB page is unnavigable.

## Publishing

Serve it as static files, read-only, `noindex`. Publish your translation and the
tooling; never the extracted source text or the ROM.

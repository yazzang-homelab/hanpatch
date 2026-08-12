# Notice — operator responsibility

**You decide what you may lawfully do. This project does not decide it for you,
and it does not restrict you to a subset it guessed was safe.**

hanpatch is a general-purpose tool for reading, translating and rebuilding game
data. It implements the container and crypto handling those files actually use,
including encrypted retail dumps and title-key encrypted installables, because a
tool that only handles already-decrypted files is not a working tool.

## What ships here

Source code. No game data, no ROMs, no extracted text, no translated scripts, no
key material, no fonts. Nothing in this repository is derived from a copyrighted
game.

The browser patcher under `web/apply` also ships no game data. It carries third
party runtime pieces, all under permissive licences and all served from the same
origin so nothing about your files reaches a third party:
[Pyodide](https://pyodide.org) (MPL-2.0, CPython + wasm),
[PyCryptodome](https://www.pycryptodome.org) (BSD-2/public domain),
[Pillow](https://python-pillow.org) (HPND) and
[hash-wasm](https://github.com/Daninet/hash-wasm) (MIT, self-test only).
`tools/deploy_web.py` fetches them and records every file's SHA-256 in
`build-manifest.json`.

## What you supply, and are responsible for

- **The game.** Obtaining, dumping and holding a copy of any title you patch.
- **Key material.** `boot9.bin`, `keys.txt`, `seeddb.bin`, common keys. These are
  extracted from hardware you own; they are not distributed here and requests to
  add them will be declined because they are not ours to give.
- **Fonts.** Anything you embed must be licensed for it.
- **Distribution.** What you publish, to whom, and under which jurisdiction's
  rules.

Copyright, anti-circumvention, and fan-translation law differ by country and are
unsettled in several. Some activities this tool makes technically possible are
lawful in one place and not in another. **Check your own jurisdiction.** The
authors provide no legal advice and accept no liability — see the MIT licence's
warranty disclaimer, which applies in full.

## Design choices that follow from this

- **Nothing is blocked on our guess about legality.** Where a capability is
  technically sound, it is implemented and documented.
- **Recommended distribution is a release bundle** (`hanpatch release`) — your
  translation plus fonts, typically well under a megabyte, applied by the
  recipient to their own copy. This is smaller, verifiable, and reproducible.
  For a CTR-encrypted 3DS container a binary delta comes out around 82% of the
  full ROM, which is not a patch by any useful definition.
- **The commit guard is a default, not a policy.** A pre-commit hook keeps game
  data out of *this* repository by accident. In your own repository, set
  `allow_game_data` in `hanpatch.json`, or simply do not install the hook.

## Not supported

Requests for key material, for pre-patched ROMs, or for links to game files.
Those are outside the scope of a tooling project, and asking here wastes your
time.

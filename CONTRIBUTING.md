# Contributing

## Ground rules

1. **No game data or key material in *this* repository.** No ROMs, extracted
   text, translated scripts, patched images, bootROMs, key files or licensed
   fonts — not because the project judges your use, but because none of it is
   ours to redistribute. `git config core.hooksPath .githooks` installs a
   pre-commit hook; it stands down on `HANPATCH_ALLOW_GAME_DATA=1` or
   `"allow_game_data": true`, which is the right setting for your own project.
   See NOTICE.md.
2. **A new gate needs an adversarial test.** Add the concrete input that used to
   slip through to `tests/test_gates.py`. A gate without a test that fails
   without it is not a gate.
3. **Never widen a gate to make a build pass.** Fix the translation, or record a
   waiver with a category and a real reason.
4. **Adapters must not import the wording layer.** The test suite enforces this.

## Adding a title

```
profiles/<title>.json          markup grammar, terms, budgets, fonts, register
hanpatch/adapters/<title>.py   extract / inject / verify
```

Prove the identity rebuild first: repack the untouched original and diff against
the input. Not bit-exact means the format reader is wrong, and every later bug
will be misattributed.

## Adding a platform

`hanpatch/platforms/<name>/` with the container crypto and filesystem. Keep it
title-independent; anything title-specific belongs in an adapter or a profile.
The core must not change.

## Tests

```bash
python3 tests/test_gates.py
HANPATCH_PROJECT=/path/to/project python3 tests/test_gates.py
```

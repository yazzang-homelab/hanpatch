# 3DS container notes

## Containers

Three shapes, all with the magic at +0x100: `NCSD` is a CCI/`.3ds` cartridge
dump with an 8-entry partition table; `NCCH` is a bare partition; a CIA has a
header, cert, ticket, TMD, then contents. Detect, do not assume from the
extension.

## Two layers of encryption

Do not conflate them.

1. **Title-key encryption** wraps whole CIA contents in AES-CBC with IV = the
   content index. The title key comes from the ticket, wrapped with a common key
   (slot 0x3D KeyX + one of several common KeyY values). The per-content type
   flag bit 0x1 says whether it is applied. Files already processed by a tool
   have it off.
2. **NCCH encryption** is AES-CTR per section, with an IV built from the
   partition id and the section number.

A file can have both, one, or neither. Probe rather than believe the flags: if
the first 0x200 bytes already contain `NCCH` at +0x100, no title-key layer is
present regardless of what the TMD says.

## NCCH keys

`flags[7]` bit 0x04 means plaintext, bit 0x01 means fixed key (zero key for
normal titles, the fixed system key for system ones), bit 0x20 means seed crypto
(KeyY becomes `sha256(original KeyY || seed)[:16]`, seed from `seeddb.bin`).

`flags[3]` selects the secondary key slot: 0x00→0x2C, 0x01→0x25, 0x0A→0x18,
0x0B→0x1B. Under any secondary method the **exheader, the exefs header, and
icon/banner/logo stay on the primary 0x2C key** while exefs file data and the
RomFS use the secondary. Get that split wrong and the content decrypts *almost*
correctly, which is far worse than failing.

The scrambler is `rol((rol(KeyX, 2) ^ KeyY) + C, 87)` with **every operation mod
2^128**. That addition carries past bit 127 for about half of all KeyY values; if
you rotate the unmasked sum, the carry folds back in at bit (128 - 87) and
distinct KeyY values collapse onto the same key. This was a real bug in this
codebase, caught by a test that only asserted "two different KeyY values give two
different keys".

Only slot 0x2C's KeyX is public. Everything else comes from the operator's own
hardware. Locate a supplied bootROM's keyblob by **searching for the public 0x2C
KeyX** and indexing off it rather than hardcoding an offset, and **validate every
derived key** by decrypting a section and checking its magic.

## CIA specifics

Read the TMD content chunks for offsets, sizes, and declared SHA-256 hashes.

Rebuilding a content requires fixing, in order:

1. the RomFS image
2. the NCCH `romfs` superblock hash (header `0x1E0`)
3. the NCCH content size and RomFS size fields
4. the TMD content chunk hash and size
5. the CIA content chunk record

Skip any of these and the console rejects the title with no useful error.

## RomFS

IVFC level 3 holds file data; the level 1/2 hash blocks follow it. The header's
master-hash size is what locates level 3 — compute it, do not assume `0x1000`.
`walk()` yields absolute offsets into the image.

When rebuilding, directory and file metadata tables must be laid out in the same
hash-bucket order the original used, or lookups fail for names that collide.

## BCFNT

Glyph sheets are RGBA4444 in **tiled** order (8×8 Morton), not linear. Decode
tiles before touching pixels.

The critical semantic, measured from the shipped font rather than guessed:

- `A` = ink coverage
- `RGB` = a shading mask the engine multiplies with the text colour

`font_text` keeps RGB white in the glyph body and dark on the antialiased rim —
that rim is what gives text its outlined look on the device. `font_system` is
white everywhere. Writing `255 - coverage` produces a flat-black body with a
bright rim: legible in a PNG preview, unreadable on hardware.

Three tables must stay consistent: the CMAP (direct / table / scan formats), the
CWDH width table, and the sheet count. Adding glyphs means extending all three
plus the sheet images.

## Pixel fonts beat antialiased ones at these sizes

At an 18×19 cell, an antialiased gothic hangul syllable becomes a grey blob —
there are not enough pixels for the strokes. A 1-bit pixel face thresholded at
around 110 stays crisp. Build both, render a preview strip, and look at it at
1× before deciding.

## Rebuilding a CCI

Partition offsets and lengths are in media units (0x200). A rebuilt partition 0
that changed size means every later partition moves, the table is rewritten, and
the declared media size grows to the next card size. Pad partitions with 0xFF,
which is what a real card image uses.

## Verification worth doing

- both content chunks match their TMD hashes
- all three NCCH superblock hashes match
- the rebuilt RomFS still contains the archive, and the archive still contains
  every message file
- every message entry equals what the manifest sealed
- every non-ASCII character used appears in the font *as read back from the ROM*

## LayeredFS packs

A rebuilt NCSD no longer matches the signature covering its headers, and the
cartridge is read-only, so a retail console refuses a rebuilt image regardless of
how correct its contents are. `hanpatch luma` writes the changed RomFS files plus
a `code.ips` under `luma/titles/<TitleID>/`, which Luma3DS reads off the SD card
with game patching enabled.

Measured on DQ7: 379 files, 39.5 MB and a 70-byte IPS, against a 2 GB reinstall.
Every file verified byte-identical to the same path inside the rebuilt ROM with
`--verify-rom`.

Two traps:

- **The title id must come from the extracted NCCH header.** A wrong one makes
  Luma patch nothing, which looks exactly like "the patch does not work".
- **Files rewritten through a staged symlink are invisible to a staged-tree
  diff.** DQ7's font archives are written through such a symlink, so the write
  lands in the source and diffing the staged tree against the source finds
  nothing. The adapter reports those paths instead.

A pack contains game data. It is generated by the player, never published.

## Binary deltas do not work on this container

CTR keystreams are position-dependent, so one shifted byte kills every downstream
match. On DQ7 both xdelta3 and a block differ came out at roughly 82% of the full
ROM. That is not a patch, it is the game.

## Font sheet pixel format

The 3DS font sheets are RGBA4444 where `A` is ink coverage and `RGB` is a
shading mask the engine multiplies with the text colour. The naive
`255 - coverage` inverse yields flat-black glyphs with a bright rim. The correct
LUT was measured off the shipped font.

## Containers and keys

Accepts CIA, CCI/`.3ds` cartridge dumps, and bare NCCH. Handles every documented
NCCH crypto method (0, 1, 10, 11), fixed/zero key, seed crypto, and title-key
encrypted CIA content.

Nothing is bundled. Point `HANPATCH_KEYS` at a directory with `boot9.bin`,
`keys.txt` or `seeddb.bin`, or drop them in `<project>/keys/`, and run
`hanpatch keys` to see which slots resolved. Crypto method 0 needs nothing at
all.

The bootROM keyblob is located by searching for the one KeyX that is public
knowledge and indexing off it, so no hardcoded file offset can silently drift.
Every derived key is validated by decrypting a section and checking its magic,
so a wrong slot fails loudly rather than producing plausible garbage.

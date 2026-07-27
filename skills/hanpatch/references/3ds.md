# 3DS container notes

## CIA / NCCH

eShop CIA contents are encrypted with the fixed system key (crypto method 0),
which needs no `boot9`. Read the TMD content chunks for offsets, sizes, and
declared SHA-256 hashes.

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

## Verification worth doing

- both content chunks match their TMD hashes
- all three NCCH superblock hashes match
- the rebuilt RomFS still contains the archive, and the archive still contains
  every message file
- every message entry equals what the manifest sealed
- every non-ASCII character used appears in the font *as read back from the ROM*

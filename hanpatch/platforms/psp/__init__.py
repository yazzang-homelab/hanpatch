"""PSP (UMD / ISO 9660) platform support.

The container stack for a PSP title is three layers deep and only the outer two
are generic:

    ISO 9660 volume          <- iso9660.py, published standard (ECMA-119)
      PSP_GAME/SYSDIR/EBOOT.BIN or EBOOT.PBP
        PBP wrapper          <- pbp.py, documented homebrew layout
          DATA.PSP / DATA.PSAR

Everything below that is per-title and has to be dumped, not assumed. Nothing in
this package knows what a particular game keeps in its data files.

One exception earns its place here rather than in a title profile: `imy.py`, the
LZ container a title's bulk assets are wrapped in. It is per-title in origin but
it is a pure codec — bytes in, bytes out, no knowledge of what a block holds —
and it was measured out of a decrypted EBOOT and checked against every block on
the disc in both directions.

Address spaces: a PSP ISO is Mode 1 / 2048-byte sectors, so the existing
`lba-2048` space kind in `recipe.py` addresses it directly and `member` addresses
files inside it. No schema change is needed for this platform, which is
deliberate - `SPACE_KINDS` is closed and a new kind would be a migration.
"""

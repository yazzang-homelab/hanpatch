"""Player-visible text stored inside the game's executable.

The disc ships `EBOOT.BIN` under the `~PSP` encryption header, so the strings in
it are invisible to every scan of the image: measured on this disc, `インストール`
returns 0 hits across the whole ISO in Shift-JIS, UTF-16 and UTF-8, and 0 hits
across all 547 extracted archive members. It is nonetheless on screen - it is the
fourth title-menu item, drawn in the same pixel font as the three items above it,
which come from the string table.

Reaching it needs the decrypted ELF as a declared build input. With that in hand
the text is ordinary NUL-terminated Shift-JIS in the read-only data, and patching
it in place moves nothing: no offset, no relocation and no length changes, so the
loader sees the same layout it always did. The patched ELF is then written into
the image AS `EBOOT.BIN`, which both CFW and the emulators accept - a patched ISO
is already unsigned, so a plain-ELF boot image costs nothing this patch has not
already spent. Proven on the device: the fourth menu item renders `설치`.

A slot cannot grow. The bytes after the NUL are whatever the compiler put there,
and a zero is not proof of free space - it is equally the first byte of an aligned
pointer. So the budget is the Japanese byte length exactly, the same fail-closed
rule the record tables use, and a translation that does not fit is refused rather
than allowed to run into its neighbour.
"""
import re

#: A run of bytes that decodes as Shift-JIS and holds at least one kana.
#:
#: Kana is the discriminator, not "decodes as Shift-JIS": ordinary machine code
#: decodes as kanji constantly. Measured on this ELF - a plain decode test found
#: 13832 candidates, of which all but a few hundred were code bytes that happen to
#: form kanji, while requiring one kana leaves 354 strings that are all real UI
#: prose. Half-width katakana (0xA1-0xDF) is deliberately NOT counted: binary
#: fields decode into it constantly and it produced ~200 false slots on the
#: database side.
_KANA = re.compile(
    r'[\u3040-\u309f\u30a0-\u30ff\u3005\u3006\u30fc]')

#: Shift-JIS lead bytes for the two-byte planes this disc's text uses.
_LEAD = set(range(0x81, 0xA0)) | set(range(0xE0, 0xFD))

MIN_LEN = 4

#: ELF constants: section type PROGBITS, and the executable-section flag.
_SHT_PROGBITS = 1
_SHF_EXECINSTR = 0x4
_ELF_MAGIC = b'\x7fELF'


class EbootError(Exception):
    pass


def _decode(chunk):
    try:
        return chunk.decode('shift_jis')
    except UnicodeDecodeError:
        return None


def data_ranges(blob):
    """[(start, end)] of the sections that may hold text, from the ELF headers.

    Structural, not heuristic. Machine code decodes as Shift-JIS kana often
    enough to matter: measured on this ELF, scanning the whole file found 16
    kana-bearing runs inside executable sections (`'\u3046% '`, `'h\u5407\u30da'`,
    `'l\u81cd\u30e0F'`) against 442 in the data sections. Every one of the 16 was
    noise, and every one of the 442 was real. Filtering by section flag removes
    that whole class instead of tuning a heuristic against it - and it also means
    a patch can never write into code.

    Section names are stripped in this binary, so the flag is the only evidence
    available; that is sufficient, because SHF_EXECINSTR is exactly the property
    that matters.
    """
    if blob[:4] != _ELF_MAGIC:
        raise EbootError('not an ELF: expected %r, found %r'
                         % (_ELF_MAGIC, bytes(blob[:4])))
    if blob[4] != 1 or blob[5] != 1:
        raise EbootError('only 32-bit little-endian ELF is supported '
                         '(class=%d data=%d)' % (blob[4], blob[5]))
    shoff = int.from_bytes(blob[0x20:0x24], 'little')
    shentsize = int.from_bytes(blob[0x2e:0x30], 'little')
    shnum = int.from_bytes(blob[0x30:0x32], 'little')
    if not shoff or not shnum:
        raise EbootError('ELF has no section table, so text cannot be located '
                         'without guessing at code')
    out = []
    for i in range(shnum):
        h = shoff + i * shentsize
        typ = int.from_bytes(blob[h + 4:h + 8], 'little')
        flags = int.from_bytes(blob[h + 8:h + 12], 'little')
        off = int.from_bytes(blob[h + 0x10:h + 0x14], 'little')
        size = int.from_bytes(blob[h + 0x14:h + 0x18], 'little')
        if typ != _SHT_PROGBITS or flags & _SHF_EXECINSTR:
            continue
        if off and size and off + size <= len(blob):
            out.append((off, off + size))
    if not out:
        raise EbootError('no non-executable PROGBITS section holds data')
    return sorted(out)


def strings(blob, min_len=MIN_LEN):
    """[(offset, raw_bytes, text)] for every whole C string that holds kana.

    A slot is delimited by NUL on BOTH sides, which is what the compiler emitted
    and what the code that reads it expects. An earlier version anchored the scan
    on the first Shift-JIS lead byte instead, and that is wrong in two ways that
    both corrupt a patch rather than merely mis-report it:

      * a leading ASCII run is dropped, so `'%s \u30b2\u30fc\u30e0\u30c7\u30fc\u30bf'` was
        recorded as `'\u30b2\u30fc\u30e0\u30c7\u30fc\u30bf'` at an offset three bytes past
        the string's real start. Writing Korean there leaves the `%s ` in place
        and the format argument then lands inside the translated text.
      * a string whose first characters are ASCII-free but which continues from
        an earlier sentence yields a MID-SENTENCE fragment. Measured on this
        ELF: `'\u8a18\u9332\u30e1\u30c7\u30a3\u30a2\u306e\u7a7a\u304d\u5bb9\u91cf\u304c\u3042\u3068%d KB\u305f\u308a\u307e\u305b\u3093\u3002\\n\u65b0\u3057\u304f\u2026'`
        was cut to start at `'\u65b0\u3057\u304f\u2026'`, and patching that writes Korean into
        the middle of a Japanese sentence.

    Scanning between NULs removes both classes at once and needs no heuristic.
    """
    out = []
    for lo, hi in data_ranges(blob):
        out += _strings_in(blob, lo, hi, min_len)
    return out


def _strings_in(blob, lo, hi, min_len):
    """Whole C-strings in [lo, hi), never a suffix of one.

    The cell boundary is the NUL, so a candidate must START where the previous
    string ended. Anchoring on the first Shift-JIS lead byte instead produced
    fragments beginning mid-sentence: measured on this ELF,
    `記録メディアの空き容量があと%d KBたりません...` was cut to begin at `新しく...`,
    and `%s ゲームデータ` lost its `%s ` prefix because the run before the lead
    byte was ASCII. Writing Korean into such a slot leaves the dropped head
    behind, so the sentence would ship half Japanese.
    """
    out = []
    start = i = lo
    while i < hi:
        if blob[i] != 0:
            i += 1
            continue
        raw = bytes(blob[start:i])
        if len(raw) >= min_len:
            text = _decode(raw)
            if text and _KANA.search(text):
                out.append((start, raw, text))
        i += 1
        start = i
    return out


def budgets(blob, min_len=MIN_LEN):
    """{text: bytes} - the tightest slot any occurrence of `text` occupies.

    One translation serves every slot sharing a source, so the budget is the
    MINIMUM across them; honouring the average would overrun the tightest.
    """
    out = {}
    for _off, raw, text in strings(blob, min_len):
        n = len(raw)
        if text not in out or n < out[text]:
            out[text] = n
    return out


def build(blob, replacements, encode, min_len=MIN_LEN):
    """`blob` with every slot whose source is in `replacements` rewritten.

    `encode` turns Korean into the bytes this title's font expects. The slot is
    overwritten in place and padded with NUL to its original length, so nothing
    downstream of it moves. A replacement that does not fit is an error, never a
    truncation: half a syllable is a wrong glyph, not a short line.
    """
    buf = bytearray(blob)
    applied = 0
    for off, raw, text in strings(blob, min_len):
        ko = replacements.get(text)
        if ko is None:
            continue
        enc = encode(ko)
        if len(enc) > len(raw):
            raise EbootError(
                '%d bytes at 0x%x exceed the %d the slot holds: %r -> %r'
                % (len(enc), off, len(raw), text, ko))
        buf[off:off + len(raw)] = enc + b'\x00' * (len(raw) - len(enc))
        applied += 1
    return bytes(buf), applied

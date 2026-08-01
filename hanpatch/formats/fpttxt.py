"""The message record inside an FPT0 '.txt' entry (Dragon Quest VII, 3DS).

Measured over all 66208 .txt payloads in /MESS: every one is valid UTF-8, uses
CRLF exclusively and ends with CRLF. A record is

    <head>CRLF <line> CRLF <line> CRLF ... <line> CRLF

with exactly four CRLF-terminated lines in 66153 records and five in 55, so a
record is a head line plus three or four display lines. The head is '#<digits>'
in 56098 records and 'TALKER=<digits>' in 10110; those two shapes account for
all 66208 - there is no third shape, and the digits are always ASCII.

Both shapes are ENFORCED on read and on write. An earlier draft accepted and
generated arbitrary heads and arbitrary line counts, which is a writer surface no
cartridge evidence supports: nothing here knows what a six-line record does to a
text window, so this module refuses to produce one.

The line count is preserved PER RECORD, not merely kept inside the corpus-wide
envelope. Four-or-five is measured across 66208 records but exercised one window
at a time, so permitting a four-line record to be rebuilt as five would rest on
55 unrelated records in unrelated windows. `build` refuses a change in line count
unless a caller states the new count explicitly via `expect_lines`, and the
envelope remains the outer bound in either case.

PROVENANCE TRAP FOR AN ADAPTER - the line-count rule binds to records that came
from `parse`. A `Record` built from scratch has `source_lines is None` and is
checked only against the corpus envelope, which is right for a genuinely new
record and wrong for a rebuilt cartridge record. An adapter must therefore parse
the source record and edit `rec.lines` in place, NOT construct a fresh `Record`
from a translated string, or it opts itself out of the per-record check.

REQUIRED MAPPING FOR A LAYOUT GATE - read this before writing an adapter. A
single display line contains no newline, so handing `Record.lines` to
`wrap.fits()` one line at a time makes every string look freeform, and
`wrap.fits` short-circuits on freeform input before measuring anything. That
would silently disable the pixel budget and the line-capacity check for the whole
title. Feed `Record.text` instead: the display lines joined with '\\n', which is
the shape the layout gate expects.

Markup found, all ASCII and therefore format machinery rather than prose:
  <NOTICE> <CENTER> </CENTER>  and <NOITCE>, a typo present twice in the shipped
  game data. It is carried through unchanged - a localisation does not get to
  correct the source's typos in machinery the engine may match literally.
  135 runtime placeholders of the form {[A-Z0-9_]+}, such as {HERO} and {LEADER},
  which the engine substitutes. A permissive '{ASCII}' scan reports 136 matches,
  but the 136th is '{MALIBELL}}' with a single occurrence, which that scan cannot
  distinguish from '{MALIBELL}' followed by a literal '}'; the placeholder count
  is 135 and the tokenizer limitation is named here for the same reason the
  archive module names its top-byte ambiguity.

Content boundary: this module carries line text as OPAQUE strings. It never
prints them, and the format work recorded only counts, lengths and hashes.
"""
import re

CRLF = '\r\n'
# ASCII digits only. Python's \d also matches full-width and Arabic-Indic
# digits, which no measured record contains and which a Korean pipeline can
# plausibly introduce as a normalisation artifact.
HEAD = re.compile(r'#[0-9]+|TALKER=[0-9]+')
LINE_COUNTS = (4, 5)


class RecordError(Exception):
    pass


class Record:
    """A parsed message record.

    `source_lines` is the CRLF-terminated line count `parse` saw, or None for a
    record assembled from scratch. `build` uses it to refuse a silent change in
    record geometry.
    """

    __slots__ = ('head', 'lines', 'source_lines')

    def __init__(self, head, lines, source_lines=None):
        self.head = head
        self.lines = list(lines)
        self.source_lines = source_lines

    @property
    def text(self):
        """Display lines joined with '\\n' - the shape a layout gate measures."""
        return '\n'.join(self.lines)


def _check(head, lines, where):
    if not HEAD.fullmatch(head):
        raise RecordError(f'{where}: head line {head!r} is neither #<digits> nor '
                          f'TALKER=<digits> in ASCII digits, the only two shapes in '
                          f'the 66208 records on the cartridge')
    total = 1 + len(lines)
    if total not in LINE_COUNTS:
        raise RecordError(f'{where}: {total} CRLF-terminated lines, but every record '
                          f'on the cartridge has {" or ".join(map(str, LINE_COUNTS))} '
                          f'- refusing a shape with no evidence behind it')
    return total


def parse(blob, where='<entry>'):
    """Decode a .txt payload into a Record. Refuses anything it cannot rebuild."""
    try:
        s = blob.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise RecordError(f'{where}: payload is not UTF-8: byte {blob[exc.start]:#04x} '
                          f'at position {exc.start}') from None
    if not s.endswith(CRLF):
        raise RecordError(f'{where}: payload does not end with CRLF')
    stripped = s.replace(CRLF, '')
    if '\n' in stripped:
        raise RecordError(f'{where}: a bare LF appears outside a CRLF pair')
    if '\r' in stripped:
        raise RecordError(f'{where}: a bare CR appears outside a CRLF pair')
    parts = s.split(CRLF)[:-1]
    if not parts:
        raise RecordError(f'{where}: no lines')
    total = _check(parts[0], parts[1:], where)
    return Record(parts[0], parts[1:], source_lines=total)


def build(rec, where='<entry>', expect_lines=None):
    """Serialise a Record back to payload bytes.

    A record parsed from the cartridge must keep its line count. Pass
    `expect_lines` to state a different geometry deliberately; it still has to
    sit inside the measured envelope.
    """
    total = _check(rec.head, rec.lines, where)
    want = expect_lines if expect_lines is not None else rec.source_lines
    if want is not None and total != want:
        raise RecordError(f'{where}: this record has {want} CRLF-terminated lines and '
                          f'the rebuild has {total}; a window proven to draw {want} '
                          f'lines is not evidence that it draws {total}. Pass '
                          f'expect_lines={total} to state the change deliberately')
    for i, line in enumerate([rec.head] + rec.lines):
        for bad, label in (('\r\n', 'CRLF'), ('\r', 'CR'), ('\n', 'LF')):
            if bad in line:
                raise RecordError(f'{where}: line {i} contains a {label} at position '
                                  f'{line.index(bad)}; lines are separated by the '
                                  f'writer, never embedded')
    return (CRLF.join([rec.head] + rec.lines) + CRLF).encode('utf-8')

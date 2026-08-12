"""The plain-text string tables DQ7 keeps outside /MESS.

Two grammars, both UTF-8 with CRLF line endings, measured over all 28 files in the
cartridge (23182 MENULIST rows, 1475 TEXT rows, zero exceptions):

    /MENULIST/*.txt   `#id,flag,text`   no BOM, exactly three comma-separated fields,
                      so the text field never contains a comma.
    /TEXT/*.txt       `id,"text"`       UTF-8 BOM, the text is always quoted and never
                      contains a quote of its own.

These carry every menu label, command, item, weapon, monster, job, place and speaker
name in the game - 105,029 Japanese characters that the /MESS pipeline never saw, which
is why a fully translated dialogue corpus still shows Japanese menus and Japanese names
inside translated lines.

The text uses the same conventions as the message records (`{1かん}` furigana, `　`
padding, `{PRE_WORD}` substitution tags), so once a row is lifted out of its line it goes
through exactly the same translation, glossary, capacity and QA path as dialogue.

Round-trip is byte-exact by construction: everything except the text field - the BOM, the
line terminator, the id, the flag, the quoting, the blank lines and the trailing
terminator - is preserved verbatim from the source file rather than re-rendered.
"""
import re

MENULIST_DIR = 'MENULIST'
TEXT_DIR = 'TEXT'
BOM = '\ufeff'
CRLF = '\r\n'

# `#id,flag,text`: the flag is empty in some files (`#1,,たたかう`) and numeric in others.
_MENU_RE = re.compile(r'^(#\d+),([^,]*),(.*)$', re.S)
_TEXT_RE = re.compile(r'^(\d+),"(.*)"$', re.S)


class Table:
    """One parsed string table, able to rebuild its own file byte-for-byte."""

    __slots__ = ('style', 'bom', 'lines', 'rows')

    def __init__(self, style, bom, lines, rows):
        self.style = style          # 'menu' | 'text'
        self.bom = bom              # the exact BOM string, '' when absent
        self.lines = lines          # every source line, verbatim
        self.rows = rows            # {line index: (key, text)}


def family_of(rel):
    """`MENULIST/command_menu.txt` -> `@MENULIST_command_menu`.

    A family id may not contain '/': the manifest key is `family/key` and every reader
    splits it once. The '@' keeps these apart from the `#NNNNNN` message families and
    makes a table row obvious in a QA report.
    """
    directory, _, name = rel.replace('\\', '/').partition('/')
    return f'@{directory}_{name[:-4] if name.endswith(".txt") else name}'


def parse(rel, data):
    """bytes -> Table. `rel` is the RomFS-relative path, used only for diagnostics."""
    text = data.decode('utf-8')
    bom = ''
    if text.startswith(BOM):
        bom, text = BOM, text[1:]
    # `split` rather than `splitlines`: a record's text may contain no terminator of its
    # own, and splitlines would also break on U+2028 and friends.
    lines = text.split(CRLF)
    style = 'text' if rel.replace('\\', '/').startswith(TEXT_DIR + '/') else 'menu'
    pattern = _TEXT_RE if style == 'text' else _MENU_RE
    rows = {}
    for i, line in enumerate(lines):
        if not line:
            continue
        m = pattern.match(line)
        if not m:
            raise SystemExit(f'{rel}:{i + 1}: unrecognised table row {line[:60]!r}')
        if style == 'text':
            rows[i] = (m.group(1), m.group(2))
        else:
            rows[i] = (m.group(1), m.group(3))
    return Table(style, bom, lines, rows)


def build(table, rel='<table>'):
    """Table -> bytes, identical to the input unless a row's text was replaced."""
    out = list(table.lines)
    for i, (key, text) in table.rows.items():
        line = table.lines[i]
        pattern = _TEXT_RE if table.style == 'text' else _MENU_RE
        m = pattern.match(line)
        if not m:                                  # unreachable; parse would have raised
            raise SystemExit(f'{rel}:{i + 1}: row became unparseable')
        if table.style == 'text':
            out[i] = f'{m.group(1)},"{text}"'
        else:
            out[i] = f'{m.group(1)},{m.group(2)},{text}'
    return (table.bom + CRLF.join(out)).encode('utf-8')


def set_text(table, key, text):
    for i, (k, _old) in table.rows.items():
        if k == key:
            table.rows[i] = (k, text)
            return True
    return False

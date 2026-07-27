"""Tag-aware line measurement and re-wrapping using the real BCFNT metrics.

The engine renders proportional text; English source lines are hand-wrapped for
English metrics, so Korean output must be re-wrapped against the actual glyph
advances of the patched font.
"""
import json
import os
import re
import sys

from hanpatch.platforms.threeds.bcfnt import Bcfnt

from hanpatch import config

TAG = config.tag_re()
HARD_BREAK = set(config.prof('hard_break'))
PAGE = set(config.prof('page_break'))

# measured from the shipped English text (max authored line width per family)
# Proven text-box capacity: the largest page the shipped English text actually
# renders in each family. A translation may use up to this many lines even if
# its own source page is shorter, but never more.
CAPACITY = config.prof('capacity')

BUDGET = dict(config.prof('budget'))
BUDGET.setdefault('default', 384)
# Whether a string is engine-wrapped is decided per string from the source: the
# shipped English text leaves engine-wrapped entries as one long line (up to
# 1691px, far wider than the 400px screen) and hand-wraps everything else.
# Widths are always measured with font_text, the widest font we ship, so the
# check is conservative for the smaller font_system UI.

_font = None


def font(path=None, ko=None):
    """Measurement font: the built target font when present, else the source.

    Widths must come from the font that actually ships, otherwise a line that
    measures safe here overflows on the device."""
    global _font
    if _font is None:
        cands = []
        if ko:
            cands.append(ko)
        cands += [config.p(x) for x in config.prof('font_out')]
        if path:
            cands.append(path)
        cands += [config.p(x) for x in config.prof('font_src')]
        for c in cands:
            if c and os.path.exists(c):
                _font = Bcfnt(open(c, 'rb').read())
                return _font
        raise SystemExit('no font to measure against; set font_src/font_out '
                         'in the title profile')
    return _font


def reset():
    """Re-read profile-derived layout constants (after config.set_root)."""
    global _font, TAG, HARD_BREAK, PAGE, CAPACITY, BUDGET, _CAP
    _font = None
    _CAP = None
    TAG = config.tag_re()
    HARD_BREAK = set(config.prof('hard_break'))
    PAGE = set(config.prof('page_break'))
    CAPACITY = config.prof('capacity')
    BUDGET = dict(config.prof('budget'))
    BUDGET.setdefault('default', 384)


def char_width(ch):
    f = font()
    i = f.char_to_index(ch)
    if i is None:
        return f.def_cw
    return f.width_of(i)[2]


def text_width(s):
    return sum(char_width(c) for c in TAG.sub('', s) if c != '\n')


def tokenize(s):
    out = []
    pos = 0
    for m in TAG.finditer(s):
        if m.start() > pos:
            out.append(('t', s[pos:m.start()]))
        out.append(('g', m.group()))
        pos = m.end()
    if pos < len(s):
        out.append(('t', s[pos:]))
    return out


def soften(s):
    """Turn cosmetic English line breaks into spaces.

    Source `\\n` positions were chosen for English word lengths; keeping them
    would leave orphan Korean fragments. `<br>` / `<page>` stay hard breaks, and
    a `\\n` directly attached to one of them is structural so it is kept too.
    """
    s = re.sub(r'(<br>|<page>)\n', lambda m: m.group(1) + '\x00', s)
    s = s.replace('\n', ' ')
    return s.replace('\x00', '\n')


def rewrap(s, budget, soft=False):
    """Re-flow `s`, preserving every tag in order; \\n and <br> stay hard breaks."""
    if soft:
        s = soften(s)
    out = []
    cur = 0.0

    def newline():
        nonlocal cur
        out.append('\n')
        cur = 0

    for kind, val in tokenize(s):
        if kind == 'g':
            out.append(val)
            if val in HARD_BREAK or val in PAGE:
                cur = 0
            continue
        # plain text: split keeping separators
        for piece in re.split(r'(\n| )', val):
            if piece == '':
                continue
            if piece == '\n':
                newline()
                continue
            if piece == ' ':
                if cur > 0:
                    w = char_width(' ')
                    if cur + w <= budget:
                        out.append(' ')
                        cur += w
                continue
            w = sum(char_width(c) for c in piece)
            if cur > 0 and cur + w > budget:
                # drop a trailing space we may have just emitted
                while out and out[-1] == ' ':
                    out.pop()
                newline()
            if w > budget:
                for c in piece:
                    cw = char_width(c)
                    if cur + cw > budget:
                        newline()
                    out.append(c)
                    cur += cw
                continue
            out.append(piece)
            cur += w
    txt = ''.join(out)
    txt = re.sub(r' +\n', '\n', txt)
    return txt


def pages(s):
    """Split into pages on <page>; returns list of line counts."""
    parts = re.split(r'<page>', s)
    counts = []
    for p in parts:
        p = p.replace('<br>', '\n')
        lines = [ln for ln in p.split('\n')]
        while lines and not TAG.sub('', lines[-1]).strip():
            lines.pop()
        counts.append(len(lines))
    return counts


def is_freeform(en):
    """True when the source is one unwrapped line, i.e. the engine wraps it."""
    return '\n' not in en and '<br>' not in en


_CAP = None


def capacity(group, kind):
    """Proven line capacity for a layout group, falling back to the family."""
    global _CAP
    if _CAP is None:
        path = config.out('capacity.json')
        _CAP = json.load(open(path)) if os.path.exists(path) else {}
    if group and group in _CAP:
        return _CAP[group]
    fam = [v for k, v in _CAP.items() if k.split('/')[0] == kind]
    if fam:
        return max(fam)
    return CAPACITY.get(kind, max(CAPACITY.values()) if CAPACITY else 10)


def fits(en, ko, kind, group=None):
    """Return (rewrapped_ko, problems)."""
    budget = BUDGET.get(kind, BUDGET['default'])
    if is_freeform(en):
        # engine-wrapped: forbid newlines the source does not have
        return ko.replace('\n', ' '), []
    new = rewrap(ko, budget, soft=True)
    src = rewrap(en, budget)
    p_new, p_src = pages(new), pages(src)
    probs = []
    if len(p_new) != len(p_src):
        probs.append(f'page count {len(p_new)} != {len(p_src)}')
    else:
        limit = capacity(group, kind)
        for i, (a, b) in enumerate(zip(p_new, p_src)):
            if a > max(b, limit):
                probs.append(f'page {i + 1} needs {a} lines, box holds {limit} '
                             f'(shorten the translation)')
    return new, probs

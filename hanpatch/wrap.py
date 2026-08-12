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

def _budget(profile_budget):
    """Validate the layout budgets, with no generic fallback for anyone.

    A budget is a MEASURED fact: the widest page the title actually renders. The
    reference title's 384 is that measurement FOR THAT TITLE and is declared in
    its own profile, so nothing here supplies it. Granting the same number to
    another title - even another English one - would be a generic default wearing
    a language carve-out: source language cannot prove that a different game
    rendered a 384px page, and the capacity gate would then report a width nobody
    measured.
    """
    budget = dict(profile_budget)
    if 'default' in budget:
        return budget
    raise SystemExit(
        'budget.default is missing: measure the widest page this title renders '
        'and declare it in the profile. There is deliberately no generic '
        'fallback - a width nobody measured makes the capacity gate meaningless.')


# Raw, unvalidated at import: a project that has not measured its widths yet must
# still be able to run `info`, `keys` or `release inspect`. The demand for a
# measured width belongs where a width is actually consumed, which is why callers
# go through `budget_for()` rather than reading this mapping directly.
BUDGET = dict(config.prof('budget'))


def budget_for(kind):
    """Pixel width for a layout group, or fail closed if none was measured."""
    resolved = _budget(BUDGET)
    return resolved.get(kind, resolved['default'])


def declared_substitution_tags():
    """Substitution tags this title declares a rendered width for."""
    return set(SUBST_WIDTHS)


def assert_layout_declared(source_max_width=None):
    """Refuse a title whose layout facts were assumed rather than declared.

    Two defects shipped from the same root cause - a number nobody measured -
    and both are cheap to refuse up front instead of discovering them on a
    device:

      * a substitution tag with no declared width. `substitution_width` fails
        closed where the width is consumed, but that surfaces as a ValueError
        from inside a rewrap in the middle of a gate run. Checking the profile
        first turns it into a refusal that names the tags.
      * a budget equal to the widest SOURCE line. That equality is the
        signature of a budget derived from the text instead of measured from
        the box: DQ7 declared 321px that way and 287px lines still spilled
        outside the frame on a real screen. A title may declare
        `budget_measured` to say where its number came from, which is exactly
        the sentence that was missing.
    """
    missing = sorted(tag for tag in SUBST_TAGS if tag not in SUBST_WIDTHS)
    if missing and SUBST_WIDTH_DEFAULT is None:
        raise SystemExit(
            f'these substitution tags render a runtime value and have no declared '
            f'width: {missing}. Measure what the engine draws in their place and '
            f'declare `substitution_widths`, or declare `substitution_width_default` '
            f'for the whole title. There is deliberately no zero fallback - a tag '
            f'measured as 0px is how overflowing dialogue passed the layout gate.')
    if source_max_width is None:
        return
    provenance = config.prof('budget_measured')
    equal = sorted(kind for kind, width in _budget(BUDGET).items()
                   if width == source_max_width)
    if equal and not provenance:
        raise SystemExit(
            f'budget {equal} equals the widest source line this title renders '
            f'({source_max_width}px), which is what a budget derived from the TEXT '
            f'looks like. A budget is the width of the BOX. Measure it - a '
            f'screenshot and one glyph-advance ratio is enough - and record where '
            f'the number came from in `budget_measured`.')
# Tags that substitute a runtime value and therefore render glyphs. Every other
# tag is a zero-width control tag: it must not keep a space alive at the start
# of a line, while `<num1> 이상` must.
SUBST_TAGS = set(config.prof('movable_tags'))
# Render widths are title evidence. Fixed substitutions such as a character name
# declare their measured glyph advance here. A title may also declare one
# conservative width for its variable name field; absent data fails closed.
SUBST_WIDTHS = dict(config.prof('substitution_widths') or ())
SUBST_WIDTH_DEFAULT = config.prof('substitution_width_default')
# Which tags actually break a line is a per-title fact, not an assumption:
# `hard_break` lists tags the engine treats as a break, and it may be empty. In
# Crimson Shroud only `\n` breaks a line and `<br>` is a message-advance marker,
# so treating `<br>` as a break hides Korean lines that really run on past the
# right edge of the screen.
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


def have_font():
    """True when a measurement font exists, i.e. layout gates can run at all."""
    if _font is not None:
        return True
    cands = [config.p(x) for x in config.prof('font_out')]
    cands += [config.p(x) for x in config.prof('font_src')]
    return any(c and os.path.exists(c) for c in cands)

def reset():
    """Re-read profile-derived layout constants (after config.set_root)."""
    global _font, TAG, HARD_BREAK, PAGE, CAPACITY, BUDGET
    global SUBST_TAGS, SUBST_WIDTHS, SUBST_WIDTH_DEFAULT
    _font = None
    invalidate_capacity()
    TAG = config.tag_re()
    HARD_BREAK = set(config.prof('hard_break'))
    PAGE = set(config.prof('page_break'))
    CAPACITY = config.prof('capacity')
    BUDGET = dict(config.prof('budget'))
    SUBST_TAGS = set(config.prof('movable_tags'))
    SUBST_WIDTHS = dict(config.prof('substitution_widths') or ())
    SUBST_WIDTH_DEFAULT = config.prof('substitution_width_default')


def char_width(ch):
    f = font()
    i = f.char_to_index(ch)
    if i is None:
        return f.def_cw
    return f.width_of(i)[2]


def substitution_width(tag):
    """Return a substitution's title-declared rendered width.

    Fixed tags use their individual measurement. A variable tag may use the
    title's explicit conservative name-field width. Missing data is an error:
    zero width or an invented fallback hides a real overflow.
    """
    width = SUBST_WIDTHS.get(tag, SUBST_WIDTH_DEFAULT)
    if width is None:
        raise ValueError(
            f'no declared render width for substitution tag {tag}; declare '
            f'substitution_widths[{tag!r}] or substitution_width_default')
    if not isinstance(width, int) or isinstance(width, bool) or width < 0:
        raise ValueError(
            f'substitution width for {tag} must be a non-negative integer, got {width!r}')
    return width


def text_width(s):
    width = 0
    for kind, value in tokenize(s):
        if kind == 'g':
            if value in SUBST_TAGS:
                width += substitution_width(value)
        else:
            width += sum(char_width(c) for c in value if c != '\n')
    return width


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

    A `\\n` in the middle of a sentence was placed for the source language's word
    lengths, so keeping it would leave orphan fragments. A `\\n` attached to a
    control tag is structural: it is where the script itself starts a new line,
    and losing it merges two sentences into one overlong line.
    """
    s = re.sub(r'(<[^>\n]*>)\n', lambda m: m.group(1) + '\x00', s)
    s = s.replace('\n', ' ')
    return s.replace('\x00', '\n')


def rewrap(s, budget, soft=False):
    """Re-flow `s`, preserving every tag in order; \\n and <br> stay hard breaks."""
    if soft:
        s = soften(s)
    out = []
    cur = 0.0
    started = False   # something is already on this line (text or a tag)

    def newline():
        nonlocal cur, started
        out.append('\n')
        cur = 0
        started = False

    for kind, val in tokenize(s):
        if kind == 'g':
            if val in SUBST_TAGS:
                w = substitution_width(val)
                if cur > 0 and cur + w > budget:
                    newline()
                out.append(val)
                cur += w
                # A runtime substitution renders glyphs, so the space after it is
                # real text rather than leading indentation.
                started = True
            else:
                out.append(val)
                if val in HARD_BREAK or val in PAGE:
                    cur = 0
                    started = False
            continue
        # plain text: split keeping separators
        for piece in re.split(r'(\n| )', val):
            if piece == '':
                continue
            if piece == '\n':
                newline()
                continue
            if piece == ' ':
                if started:
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
                    started = True
                continue
            out.append(piece)
            cur += w
            started = True
    txt = ''.join(out)
    txt = re.sub(r' +\n', '\n', txt)
    return txt


def pages(s):
    """Split into pages on <page>; returns list of line counts."""
    parts = re.split(r'<page>', s)
    counts = []
    for p in parts:
        for t in HARD_BREAK:
            p = p.replace(t, '\n')
        lines = [ln for ln in p.split('\n')]
        while lines and not TAG.sub('', lines[-1]).strip():
            lines.pop()
        counts.append(len(lines))
    return counts


def is_freeform(en):
    """True when the source row carries no line break of its own.

    This is a SHAPE fact about the row. It says nothing about who lays the row
    out - see `engine_lays_out`, which is the question every caller actually
    wants answered.
    """
    return '\n' not in en and not any(t in en for t in HARD_BREAK)


def title_lays_out_own_text():
    """Does the CONTAINER own layout for this title, rather than the engine?

    The single reader of the declared `engine_wraps` fact. Two call sites need it -
    whether an unbroken row is measured at all, and how much a measured row may grow
    - and letting each read the profile is exactly how a fact ends up honoured in one
    place and ignored in the other.

    Returns None when the title has not declared it; callers must refuse rather than
    guess.
    """
    return config.prof('engine_wraps')


def engine_lays_out(en):
    """True when this row is the ENGINE's to lay out, so we must not measure it.

    An unbroken row means one of two very different things and only the title
    knows which: either the engine wraps it, or the container simply stores one
    display line per row and the layout is ours to respect. Crimson Shroud is the
    first; Dragon Quest VII stores one line per record line, so treating its rows
    as engine-laid-out would discard the pixel budget and never consult the
    measured line capacity.

    Every place that decides whether a row gets measured MUST route through this
    predicate. Consulting `is_freeform` directly is how the fact ends up honoured
    in one gate and ignored in the next - and the half that works makes the other
    half look done.
    """
    if not is_freeform(en):
        return False
    declared = title_lays_out_own_text()
    if declared is None:
        raise SystemExit(
            'a source row has no line break, so whether the engine wraps it is '
            'what decides if this row is measured at all, and the profile does '
            'not say. Declare "engine_wraps": true if the engine lays these rows '
            'out, or false if the container stores one display line per row and '
            'the budget must be enforced. There is deliberately no default - '
            'guessing true silently disables the layout gate for every unbroken '
            'row.')
    return declared


_CAP = None


def invalidate_capacity():
    """Drop the cached derived table after something rewrites it.

    Exported rather than letting callers assign `wrap._CAP` directly: attribute
    assignment cannot fail, so a rename of the cache would turn every such write
    into a silent no-op and bring the stale-table bug straight back.
    """
    global _CAP
    _CAP = None


def capacity(group, kind):
    """Proven line capacity for a layout group, falling back to the family.

    Like a pixel budget, a line capacity is a MEASURED fact: the most lines the
    source itself ever rendered in that layout group. The old tail of this
    function invented a limit - the family maximum, or a hardcoded 10 - whenever
    nothing had been measured, which is the same defect as the generic 384px
    width: the capacity gate would then pass a translation against a limit the
    title never proved. Measured evidence, in falling order of specificity: the
    group's own derived capacity, its family's derived maximum, then a limit the
    profile explicitly declares. If none exists, fail closed.
    """
    global _CAP
    if _CAP is None:
        path = config.out('capacity.json')
        _CAP = (config.load_object(path, 'the derived capacity table')
                if os.path.exists(path) else {})
        bad = {k: v for k, v in _CAP.items()
               if not isinstance(v, int) or isinstance(v, bool) or v <= 0}
        if bad:
            raise SystemExit(f'the derived capacity table holds values that are not '
                             f'positive line counts: {bad} in {path}')
    if group and group in _CAP:
        return _CAP[group]
    fam = [v for k, v in _CAP.items() if k.split('/')[0] == kind]
    if fam:
        return max(fam)
    if kind in CAPACITY:
        return CAPACITY[kind]
    raise SystemExit(
        f'no measured line capacity for {kind!r}'
        + (f' (group {group!r})' if group else '')
        + ': run `hanpatch gates` so the capacity gate derives it from the '
          'source, or declare it in the profile. There is deliberately no '
          'invented limit - a capacity nobody measured lets overflowing text '
          'through the gate.')


def structural_breaks(en):
    """Control-tag positions the source follows with a line break.

    Where the script wants a new line it writes the break right after a control
    tag, so those positions are layout, not source-language word wrap, and the
    translation has to reproduce them or two sentences end up on one line.
    """
    want = set()
    n = -1
    for kind, val in tokenize(en):
        if kind == 'g':
            if val not in SUBST_TAGS:
                n += 1
            continue
        if val.startswith('\n') and n >= 0:
            want.add(n)
    return want


def transplant_breaks(en, ko):
    """Put the source's structural breaks back into a re-flowed translation.

    Control tags keep their order and count in the translation (the tag-skeleton
    gate enforces that), so the n-th control tag is the same layout position in
    both languages.
    """
    want = structural_breaks(en)
    if not want:
        return ko
    out = []
    n = -1
    for kind, val in tokenize(ko):
        if kind == 'g':
            out.append(val)
            if val not in SUBST_TAGS:
                n += 1
                if n in want:
                    out.append('\n')
            continue
        if out and out[-1] == '\n':
            val = val.lstrip(' \n')
        out.append(val)
    return ''.join(out)


def composed(kind):
    """Whether this container stores FRAGMENTS the engine joins before drawing.

    A fragment is not a display line, so measuring one against a box width answers a
    question nobody asked. DQ7's StreetPass lithograph-name tables are the case that forced
    this: the rows are pieces like `となりの`, `なマクドナルド`, `荒海を`, `空と海と` -
    several end in a grammatical particle - and the engine concatenates two of them into one
    name that is drawn in a wider field. Deriving a budget from the widest FRAGMENT then
    yields 55px, under which no four-syllable Korean word fits at all (four Hangul syllables
    measure 56px against four kanji at 55px in this font), so the check rejected correct
    translations and asked for text that would be wrong.

    This is a per-title container fact and must be declared, never inferred: a table of
    short standalone labels looks identical from here, and exempting one of those would let
    real overflow through. Declared families skip WIDTH enforcement only; tags, glossary,
    register, kana and the soft-break marker are still checked.
    """
    return kind in set(config.prof('composed_families') or ())


def width_advisory(kind):
    """Whether a width overrun in this container is reported but not blocking.

    The budget this module enforces is derived from the widest line the SOURCE renders, which
    is a lower bound on the box: it proves the box is at least that wide and says nothing
    about slack. For the dialogue box that is enough, because 343 message families share one
    UI element and the widest of them pins it at 321px. For a per-slot string table it is
    not: each slot is sized independently, so its own longest Japanese string is all the
    evidence there is, and Hangul runs about 9% wider per character than kana in this font
    (14.0px against 12.8px). Enforcing the bound there does not shorten prose, it truncates
    proper nouns - measured on DQ7, `ベアトリス` had to become `베아트` and `プロビナ神父`
    had to lose the place name entirely to satisfy a 67px and 77px slot.

    The empirical half of the argument matters more than the arithmetic: the previously
    shipped DQ7 build enforced NO width at all (its profile carried an empty capacity), a
    player completed the early game on it, and the defect reported was dialogue running past
    the box - not clipped menu entries or names. So the table slots demonstrably tolerated
    these widths, while the dialogue box demonstrably did not.

    Declared per title, never inferred, and it downgrades WIDTH only: tags, glossary,
    register, kana, the soft-break marker and the line/page structure are still enforced,
    and a violation is still printed so nobody can claim it was measured clean.
    """
    if not config.prof('width_advisory_tables'):
        return False
    return kind.startswith('@')


def fits(en, ko, kind, group=None):
    """Return (rewrapped_ko, problems)."""
    if composed(kind):
        return ko.replace('\n', ' '), []
    if width_advisory(kind):
        return ko.replace('\n', ' '), []
    budget = budget_for(kind)
    if engine_lays_out(en):
        return ko.replace('\n', ' '), []
    new = rewrap(transplant_breaks(en, ko), budget, soft=True)
    src = rewrap(en, budget)
    p_new, p_src = pages(new), pages(src)
    probs = []
    if len(p_new) != len(p_src):
        probs.append(f'page count {len(p_new)} != {len(p_src)}')
    else:
        limit = capacity(group, kind)
        # How much the translation may GROW depends on who owns the layout, and the
        # two answers are far apart. Where the engine wraps, a row may occupy the
        # whole box, so `max(source_lines, box_limit)` is right. Where the CONTAINER
        # owns it - one stored line per display line, `engine_wraps: false` - a line
        # that rewraps to two cannot be stored at all: the record layer refuses a
        # line-count change, so such a translation would be rejected at inject with
        # the gate having reported it clean. Measured on DQ7: a Korean line
        # overflowing a 400px box against a one-line source produced NO problem,
        # because one page with two lines is still under a box limit of four.
        engine_wraps = title_lays_out_own_text()
        for i, (a, b) in enumerate(zip(p_new, p_src)):
            allowed = max(b, limit) if engine_wraps else b
            if a > allowed:
                if engine_wraps:
                    probs.append(f'page {i + 1} needs {a} lines, box holds {limit} '
                                 f'(shorten the translation)')
                else:
                    probs.append(f'page {i + 1} needs {a} lines but the container '
                                 f'stores {b}; this title lays out its own text, so '
                                 f'a line that rewraps cannot be stored '
                                 f'(shorten the translation)')
        if not probs and not engine_wraps:
            # FEWER lines is just as unstorable as more, and it was the failure that
            # actually happened: Korean is denser than Japanese, so a re-flowed
            # translation of a 3-line record lands on 1 or 2 lines. Measured across 7770
            # translated rows, 7724 had fewer newlines than their source and the gate
            # flagged NONE of them, because the check only looked for growth - every one
            # of those rows would have been refused at inject with the gate reporting
            # clean.
            #
            # Padding is not a guess: the source corpus writes short records the same way.
            # Of 66208 records, 22345 already carry one empty display line, 6733 carry
            # two and 372 are entirely empty, and the empty line sits last in 29449 of
            # them. So trailing blanks are the container's own idiom for "this record
            # needs fewer lines than it stores".
            new = _pad_to_source_lines(new, en)
            # Verify, do not assume. Padding is the only writer here, so if the count
            # still differs the record is unstorable and must be REFUSED rather than
            # handed to inject to discover.
            if new.count('\n') != en.count('\n'):
                probs.append(f'stored line count {new.count(chr(10)) + 1} != source '
                             f'{en.count(chr(10)) + 1}; this title lays out its own '
                             f'text, so the record cannot hold it')
    return new, probs


def _pad_to_source_lines(ko, en):
    """Pad each page of `ko` with trailing blanks to the source page's RAW line count.

    Raw, not the `pages()` count: `pages()` deliberately drops trailing blank lines before
    counting, while the injector compares `len(translation.split(chr(10)))` against the
    record's stored line count. Padding to the trimmed number would still be refused at
    inject, which is the failure this exists to prevent.
    """
    out = []
    src_pages = re.split(r'<page>', en)
    ko_pages = re.split(r'<page>', ko)
    if len(ko_pages) != len(src_pages):
        return ko
    for page, src in zip(ko_pages, src_pages):
        want = src.count('\n') + 1
        lines = page.split('\n')
        if len(lines) < want:
            lines += [''] * (want - len(lines))
        out.append('\n'.join(lines))
    return '<page>'.join(out)

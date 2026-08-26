"""Korean particle (josa) agreement: deterministic repair and runtime resolution.

Two different problems live here, and only the first one was ever handled.

**Fixed text.** Machine translation reliably gets josa wrong right after
transliterated proper nouns ("지오크은", "서고으로"). The correct form is computable
from the preceding syllable, so those are repaired instead of being sent back to
the model.

**Runtime substitutions.** Where the row carries a substitution token the engine
fills in at run time (`{HERO}`, `{ACTOR}`, `{I_NAME}`), the syllable the particle
has to agree with does not exist until the game draws the line, so NO single form
is correct. Measured on the shipped DQ7 corpus before this module resolved them:
2416 rows carried a particle immediately after a substitution token and every one
of them had been guessed by the model (1084 `는` against 500 `은`, 364 `가` against
121 `이`), while a further 506 rows carried a hand-written both-forms rendering in
four different shapes. A guess is wrong for roughly half of the values the token
can take; the reader sees "아루스은".

Three outcomes, in falling order of quality:

1. the token has a title-declared FIXED rendering (`substitution_values`, e.g. a
   party member whose Korean name never changes) — the form is computed from that
   name, exactly as for ordinary text;
2. the token is runtime-variable — the only rendering correct for every value is
   the both-forms one (`은(는)`), written in ONE canonical shape;
3. the particle has no readable both-forms rendering (`이었다`/`였다`) — refuse, and
   let the row be reworded. Inventing `(이)었다` would ship "아이라었다".

`resolve()` is the single seam where outcome 2 is decided. An engine-side runtime
resolver - a code hook that picks the form from the substituted name at draw time -
replaces that one branch and nothing else. It is deliberately NOT pretended to
exist here: this build has no such hook, so the canonical both-forms rendering is
what actually ships.
"""
import re

from hanpatch import config

# (form after a final consonant, form after a vowel, both-forms rendering).
# The third column is a Korean orthographic convention, not a mechanical join:
# `으로` brackets its own first syllable (`(으)로`) while `은` brackets the other
# form (`은(는)`), and two pairs have no readable bracketed form at all.
PAIRS = [
    ('은', '는', '은(는)'),
    ('이', '가', '이(가)'),
    ('을', '를', '을(를)'),
    ('과', '와', '과(와)'),
    ('으로', '로', '(으)로'),
    ('이라', '라', '(이)라'),
    ('이란', '란', '(이)란'),
    ('이랑', '랑', '(이)랑'),
    ('이며', '며', '(이)며'),
    ('아', '야', '아(야)'),
    ('이에요', '예요', None),
    ('이었다', '였다', None),
]
# forms where the "consonant" variant is also correct after a final ㄹ
RIEUL_TAKES_VOWEL_FORM = {'으로'}

_SINGLE = {}
for _c, _v, _d in PAIRS:
    _SINGLE[_c] = (_c, _v, _d)
    _SINGLE[_v] = (_c, _v, _d)


def duals():
    """Every both-forms rendering this module writes, longest first."""
    return sorted((d for _c, _v, d in PAIRS if d), key=len, reverse=True)


def _dual_variants(cons, vowel):
    """Shapes a human or a model writes a both-forms particle in.

    All of them mean the same thing, so they are folded into the one canonical
    shape rather than being counted as five different renderings: the shipped
    corpus carried `은(는)`, `(은)는`, `은/는` and `은 (는)` in the same build.
    """
    return [f'{cons}({vowel})', f'({cons}){vowel}', f'{cons}/{vowel}',
            f'{cons} ({vowel})', f'{vowel}({cons})', f'({vowel}){cons}',
            f'{vowel}/{cons}']


def jong(ch):
    """Final-consonant index of a Hangul syllable (0 = none); None if not Hangul."""
    o = ord(ch)
    if not (0xAC00 <= o <= 0xD7A3):
        return None
    return (o - 0xAC00) % 28


def pick(prev, cons_form, vowel_form):
    j = jong(prev)
    if j is None:
        return None
    if j == 0:
        return vowel_form
    if j == 8 and cons_form in RIEUL_TAKES_VOWEL_FORM:   # ㄹ
        return vowel_form
    return cons_form


def resolve(value, cons, vowel, dual):
    """The particle to write after `value`, or None when nothing can be written.

    `value` is what the reader will actually see in front of the particle: a real
    Korean word for fixed text, or None where the engine substitutes a name that
    is not known until run time. The runtime case is the whole reason this
    function exists as a seam - an engine-side josa hook changes this branch and
    leaves every caller untouched.
    """
    if value is not None:
        return pick(value[-1], cons, vowel)
    return dual


def fixed_values():
    """Substitution tokens whose rendered Korean text never changes.

    Declared per title, never inferred. `placeholder_text` looks like the same
    fact and is not: it holds ONE example rendering so the script book can be
    searched by what a player saw, and its `{HERO}` entry is a sample of a name
    the player types in. Reading it here would compute a particle for the hero's
    name from a name the hero does not have.
    """
    values = config.prof('substitution_values') or {}
    return {t: v for t, v in values.items() if v}


# Particle forms, including the ones with no consonant/vowel alternation. Used
# only in the ONE context where a token starting with one of these cannot be a
# word of its own: directly after a substitution token, i.e. after a name.
_PARTICLES = tuple(sorted(
    {f for _c, _v, _d in PAIRS for f in (_c, _v)}
    | {'의', '에', '에게', '에게서', '에서', '께', '께서', '한테', '한테서',
       '부터', '까지', '도', '만', '조차', '마재', '밖에', '처럼', '같이', '보다',
       '으로서', '로서', '으로써', '로써', '마다', '뿐', '이나', '나'},
    key=len, reverse=True))


def weld_after_tags(text):
    """Rejoin a particle the old wrapper pushed onto the next line.

    A line break between a substitution token and a particle is not a word gap.
    Korean writes no space before a particle, so what the reader sees is `아루스`
    followed by a line that starts `에게` - a word broken in half. 58 shipped rows
    were in that state, and they could not be repaired by the particle pass either,
    because the guessed particle was no longer adjacent to the token it agreed with.

    Only a NEWLINE is welded, never a plain space. The old wrapper collapsed the
    space it broke at, so a newline in this position is its own signature; a plain
    space there is a wording decision and stays one. A handful of the particle
    forms (`이`, `가`, `와`, `도`) are also words, and after a NAME they are not -
    which is why this is scoped to the token context and to nothing else.
    """
    alt = _tag_alternation()
    if not alt:
        return text
    pat = re.compile(rf'({alt})[ \u3000]*\n[ \u3000]*'
                     rf'({"|".join(map(re.escape, _PARTICLES))})(?![가-힣])')
    return pat.sub(lambda m: m.group(1) + m.group(2), text)


def _tag_alternation():
    """Every token a row can carry whose VALUE is not known until run time.

    `movable_tags` alone is not that set. It names the icon glyphs the injector
    may relocate, and a title whose only substitutions are printf conversions
    therefore had NO tags here at all - so `after_tags` compiled nothing, saw
    nothing, and every particle written after a `%s` shipped as the translator
    guessed it.

    Measured on Classic Dungeon X2, 2026-08-26: 21 rows carried a single-form
    particle straight after a placeholder and exactly ONE row carried a
    both-forms rendering. `%s를 생성했습니다!` renders `글렌를` for a name the
    PLAYER types, and the josa gate reported the corpus clean because `%s` was
    not a tag it knew.
    """
    tags = set(config.prof('movable_tags') or ())
    tags |= set(config.prof('runtime_tokens') or ())
    return '|'.join(re.escape(t) for t in
                    sorted(tags, key=len, reverse=True)) if tags else None


def fix_after(text, terms):
    """Correct the particle immediately following any term in `terms`."""
    if not terms:
        return text, []
    changed = []
    ordered = sorted(terms, key=len, reverse=True)
    alt = '|'.join(re.escape(t) for t in ordered)
    forms = sorted(_SINGLE, key=len, reverse=True)
    pat = re.compile(rf'({alt})({"|".join(map(re.escape, forms))})(?![가-힣])')

    def sub(m):
        term, part = m.group(1), m.group(2)
        cons, vowel, _dual = _SINGLE[part]
        want = pick(term[-1], cons, vowel)
        if want and want != part:
            changed.append(f'{term}{part} -> {term}{want}')
            return term + want
        return m.group(0)

    return pat.sub(sub, text), changed


EU_RO = re.compile(r'([가-힣])으로(?![가-힣])')


def check_eu_ro(text):
    bad = []
    for m in EU_RO.finditer(text):
        j = jong(m.group(1))
        if j == 0:
            bad.append(m.group(0))
    return bad


def fix_eu_ro(text):
    def sub(m):
        return m.group(1) + ('로' if jong(m.group(1)) == 0 else '으로')
    return EU_RO.sub(sub, text)


def collapse_duals(text):
    """Resolve a both-forms particle that follows ordinary Korean text.

    A bracketed particle after a real syllable is not a runtime case at all - the
    syllable is right there - so it is noise the reader has to parse. Written by
    hand in 506 shipped rows, including after party names whose spelling is fixed.
    """
    out = text
    for cons, vowel, dual in PAIRS:
        for form in ([dual] if dual else []) + _dual_variants(cons, vowel):
            pat = re.compile(r'([가-힣])' + re.escape(form) + r'(?![가-힣])')

            def sub(m, cons=cons, vowel=vowel):
                return m.group(1) + pick(m.group(1)[-1], cons, vowel)

            out = pat.sub(sub, out)
    return out


def after_tags(text):
    """Rewrite every particle that sits directly after a substitution token.

    Returns `(text, problems)`. A problem means the row cannot be rendered
    correctly for every value the token takes and has to be reworded - it is not
    a wording preference, and it is not repairable by retranslating the same
    sentence shape.
    """
    alt = _tag_alternation()
    if not alt:
        return text, []
    fixed = fixed_values()
    forms = sorted(_SINGLE, key=len, reverse=True)
    variants = duals() + [v for c, v_, d in PAIRS for v in _dual_variants(c, v_)]
    # Longest first so `은(는)` is matched before the bare `은` inside it.
    allforms = sorted(set(variants) | set(forms), key=len, reverse=True)
    pat = re.compile(rf'({alt})({"|".join(map(re.escape, allforms))})(?![가-힣])')
    problems = []

    def sub(m):
        tag, written = m.group(1), m.group(2)
        cons, vowel, dual = _particle_of(written)
        value = fixed.get(tag)
        want = resolve(value, cons, vowel, dual)
        if want is None:
            problems.append(
                f'josa after {tag}: "{written}" agrees with a name this row does '
                f'not know until run time and has no both-forms rendering; '
                f'reword the sentence')
            return m.group(0)
        return tag + want

    return pat.sub(sub, text), problems


def _particle_of(written):
    """The pair a written particle belongs to, single or both-forms."""
    if written in _SINGLE:
        return _SINGLE[written]
    for cons, vowel, dual in PAIRS:
        if written == dual or written in _dual_variants(cons, vowel):
            return cons, vowel, dual
    raise KeyError(written)


def auto(text, terms=()):
    """Every josa decision this build makes, in one pass over one string.

    Returns `(text, problems)`. Callers must not reach past this into the
    individual repairs: a particle after a substitution token is decided by
    `after_tags`, and a caller that ran only `fix_after` would leave exactly the
    2416 guessed rows this module exists to stop.
    """
    text = weld_after_tags(text)
    text = collapse_duals(text)
    text, _ = fix_after(text, set(terms))
    text = fix_eu_ro(text)
    return after_tags(text)

"""Korean particle (josa) agreement checks and deterministic repair.

Machine translation reliably gets josa wrong right after transliterated proper
nouns ("지오크은", "서고으로"). Those positions are exactly where the correct form
is computable from the preceding syllable, so they are auto-repaired instead of
being sent back to the model.
"""
import re

PAIRS = [
    ('은', '는'), ('이', '가'), ('을', '를'), ('과', '와'),
    ('으로', '로'), ('이라', '라'), ('이란', '란'), ('이랑', '랑'),
    ('이며', '며'), ('아', '야'), ('이에요', '예요'), ('이었다', '였다'),
]
# forms where the "consonant" variant is also correct after a final ㄹ
RIEUL_TAKES_VOWEL_FORM = {'으로'}


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


def fix_after(text, terms):
    """Correct the particle immediately following any term in `terms`."""
    if not terms:
        return text, []
    changed = []
    ordered = sorted(terms, key=len, reverse=True)
    alt = '|'.join(re.escape(t) for t in ordered)
    forms = sorted({f for p in PAIRS for f in p}, key=len, reverse=True)
    pat = re.compile(rf'({alt})({"|".join(map(re.escape, forms))})(?![가-힣])')

    def sub(m):
        term, part = m.group(1), m.group(2)
        for cons, vowel in PAIRS:
            if part in (cons, vowel):
                want = pick(term[-1], cons, vowel)
                if want and want != part:
                    changed.append(f'{term}{part} -> {term}{want}')
                    return term + want
                break
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

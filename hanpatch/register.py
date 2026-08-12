"""Which Korean speech level does one source string ask for?

This started as a policy question - "존대 or 평어?" - and the corpus answered it instead.
Measured over 65836 non-empty DQ7 records: 14.0% carry a polite marker (です/ます/ました/
ません/ございま/ください...), 23.1% carry a plain marker (だぞ/だな/だろう/のだ...), 2.5%
carry both, and 60.4% carry neither. Per family the picture is worse for a policy: of the
339 families with any marker at all, 178 sit between 0.2 and 0.8 polite share, so a
family is not a register either. Register belongs to the LINE, and the line usually says
so itself.

So there is nothing to choose for 39.6% of the corpus - it is derived. What remains is
the marker-less majority, and that is the one declared fact: `register_default`. It is
demanded, not defaulted, because guessing it silently would set the voice of two thirds
of a title's text with no record of the decision.

A record's lines share a speaker, and in this container a row IS a record - every one of
the 66208 extracted rows carries its display lines as newline-separated text - so
whole-record derivation happens for free: a marker on any line settles the row. There is
deliberately no per-line path and no `lines` parameter, because one would be an
unreachable branch pretending to do work the row shape already does.
"""
import re

from hanpatch import config

# Only markers that are unambiguous in a game script.
#
# `ます` also sits inside verbs like 済ます, so it is required at a sentence boundary. The
# boundary class must include the Japanese closing brackets: a quoted polite clause ends
# 「...ます」, and leaving 」 out made the string read as plain, which is the opposite of
# what it says.
#
# Bare よ/ぞ/ぜ were tried as plain enders and REMOVED after measurement: どうぞ。 ends in
# ぞ。 and is not a plain sentence ender at all, so a polite line reading 'こちらへどうぞ。
# ご案内します。' was scored as carrying both levels and therefore as declaring nothing.
# A marker that fires on a politeness formula is worse than a missing marker.
_END = r'[。！？\s」』）\)]'
_POLITE = re.compile(
    r'(です|ます' + _END + r'|ます$|ました|ません|ましょう|でしょう|ございま|ください'
    r'|ですか|であります|なさい)')
_PLAIN = re.compile(
    r'(だぞ|だな|だよ|だろう|だぜ|だわ|じゃん|のだ|んだ|だ' + _END + r'|だ$'
    # `した` must not be the tail of the POLITE past `ました`: 「わかりました」 matched both
    # patterns and therefore scored as declaring nothing, losing a plainly polite line.
    r'|する' + _END + r'|(?<!ま)した' + _END + r'|ない' + _END + r')')

POLITE, PLAIN = 'polite', 'plain'


def marker_of(text):
    """The register the string itself declares, or None when it declares nothing.

    A string carrying both is treated as declaring nothing rather than as polite: 2.5% of
    records do this, usually a polite quotation inside plain narration, and forcing one
    level on the whole string is how a translation ends up mixing levels mid-sentence -
    which the gate already refuses.
    """
    if not text:
        return None
    p = bool(_POLITE.search(text))
    q = bool(_PLAIN.search(text))
    if p and q:
        return None
    if p:
        return POLITE
    if q:
        return PLAIN
    return None


def declared_default():
    """The register for text that declares none. Demanded, never guessed."""
    v = config.prof('register_default')
    if v not in (POLITE, PLAIN):
        raise SystemExit(
            "REGISTER UNDECLARED: the profile must set register_default to 'plain' or "
            "'polite'. Measured on this corpus, 60.4% of records carry no register "
            "marker, so this one value sets the voice of most of the title; it is not "
            "something to default silently. Declare it and the other 39.6% is derived "
            "from the source line by line.")
    return v


INSTRUCTION = {
    POLITE: '이 문자열은 존댓말(~합니다/~하세요)로 번역한다. 평서형과 섞지 않는다.',
    PLAIN: '이 문자열은 평서형·구어체(~다/~해/~야)로 번역한다. 존댓말과 섞지 않는다.',
}


def instruction(text):
    """The register line to hand the translator for this string.

    Returns '' for a source language that carries no such marking, so an English-source
    title behaves exactly as before rather than being told a register it never asked for.
    """
    if config.source_lang() != 'ja':
        return ''
    reg = marker_of(text) or declared_default()
    return INSTRUCTION[reg]


# Korean speech-level endings, deliberately WIDE. A narrow set reports divergence where
# the translation is simply polite in a form the pattern did not list, and a false failure
# on fluent text is the expensive kind. Measured over 3395 rows whose source declares a
# register: the narrow set called 17.1% divergent, the wide set 14.3%, so 2.8 points were
# pattern gaps and 14.3 points are real.
_KO_POLITE = re.compile(
    # '니다' is NOT a bare member here, for the same reason '니까' is not: the polite
    # declarative is -ㅂ니다, and a bare '니다' also ends the PLAIN 아니다 / 매니다. Measured
    # after full-width stops stopped hiding sentence endings: 4 of the 8 rows the register
    # gate still rejected were '…올 곳은 아니다.' answering a plain source, scored polite
    # because '아니다' ends in '니다'. The polite reading is recognised by _polite_nida,
    # which requires the syllable before 니다 to carry the ㅂ the polite form always has.
    # '니까' is likewise excluded: measured on this corpus, all 24 rows where it fired on a
    # plain-marked source were 그러니까 / 없으니까 / 테니까 / 다니까 - "because", not a
    # question. _polite_nikka applies the same jamo test.
    r'(세요|셔요|십시오|시오|해요|예요|이에요'
    # -나요 / -ㄴ가요 / -ㄹ까요 / -군요 / -네요: polite endings that carry 요 as their own
    # syllable. Their absence read '마왕의 성은 이미 보셨나요?' as carrying no polite
    # marker at all, so a polite source scored as divergent against a polite translation.
    r'|나요|가요|까요|군요|네요'
    r'|어요|아요|지요|죠|시겠|드려요|주세요)')

# -ㅂ니다 / -습니다: same arithmetic as -ㅂ니까 below.
_NIDA = re.compile(r'([\uac00-\ud7a3])니다')


def _polite_nida(ko):
    for m in _NIDA.finditer(ko):
        if (ord(m.group(1)) - 0xAC00) % 28 == 17:
            return True
    return False


# -ㅂ니까 / -습니까: the syllable before 니까 ends with the final consonant ㅂ. Composed
# Hangul makes that arithmetic rather than a character class - (code - 0xAC00) % 28 is the
# final-jamo index, and 17 is ㅂ.
_NIKKA = re.compile(r'([\uac00-\ud7a3])니까')


def _polite_nikka(ko):
    for m in _NIKKA.finditer(ko):
        if (ord(m.group(1)) - 0xAC00) % 28 == 17:
            return True
    return False

# -ㅂ시다 / -읍시다: the polite propositive, and the plain pattern's bare `다` swallows it.
# Measured after full-width stops stopped hiding sentence endings: of 88 rows the register
# gate then rejected, 16 were 합시다 / 봅시다 / 놓읍시다 answering a ましょう source - polite
# exhortations scored as plain speech. Retranslation cannot fix those; the reading was
# wrong. Same arithmetic as -ㅂ니까: the syllable before 시다 carries final ㅂ (index 17).
_SIDA = re.compile(r'([\uac00-\ud7a3])시다')


def _polite_sida(ko):
    for m in _SIDA.finditer(ko):
        if (ord(m.group(1)) - 0xAC00) % 28 == 17:
            return True
    return False


def _polite_ending(text):
    """Whether the final sentence ending, rather than an embedded quote, is polite."""
    core = text.rstrip('.!?…"\'\u300d\u300f)] \t')
    if any(m.end() == len(core) for m in _KO_POLITE.finditer(core)):
        return True
    for pattern in (_NIKKA, _SIDA, _NIDA):
        for m in pattern.finditer(core):
            if m.end() == len(core) and (ord(m.group(1)) - 0xAC00) % 28 == 17:
                return True
    return False


# A Korean sentence ending that is unambiguously PLAIN. Needed because "no polite
# marker" is NOT the same as "plain": most records in a container that stores display
# lines end mid-sentence, and a noun fragment or an interjection carries no speech level
# at all.
_KO_PLAIN = re.compile(
    r'(다|야|어|아|자|지|네|군|구나|래|까|니|냐|마|라)[.!?\u2026"\'\s\u300d\u300f\)]*$')


def of_korean(ko):
    """The speech level a Korean string uses, or None when it carries none.

    Returning PLAIN for anything without a polite ending was a false-failure factory: of
    161 rows the gate rejected for register, 108 had NO sentence ending at all. Those are
    fragments - a line that continues into the next record, a name, an exclamation - and
    no amount of re-translation can put a speech level on them, so the gate demanded the
    impossible and a re-sweep recovered 7 of 34.

    Absence of evidence is not evidence of the opposite. This is the same shape as reading
    a missing glyph as an unrenderable one.
    """
    if not ko:
        return None
    polite = bool(_KO_POLITE.search(ko) or _polite_nikka(ko) or _polite_sida(ko)
                  or _polite_nida(ko))
    tail = re.sub(r'<[^>]*>|\{[^}]*\}', '', ko.strip().split('\n')[-1]).strip()
    tail_polite = _polite_ending(tail)
    plain = bool(_KO_PLAIN.search(tail)) and not tail_polite
    # A quoted polite clause inside plain narration (or the reverse) is mixed evidence,
    # not a reliable declaration for the whole record. The source-side marker_of() already
    # treats both markers as undeclared; doing the same here prevents a target with one
    # embedded polite phrase and a plain final line from being falsely rejected as wholly
    # polite. This was the sole remaining blocker in a 102-character DQ7 row after five
    # independent translators produced the same mixed shape.
    if polite and plain:
        return None
    if tail_polite:
        return POLITE
    return PLAIN if plain else None


def divergence(en, ko):
    """A problem string when the translation ignores the register the SOURCE declares.

    Silent when the source declares nothing: two thirds of this corpus carries no marker,
    and those rows follow the profile's declared fallback, which the translator was told
    but which cannot be verified against the source because the source says nothing.

    This is a check the prompt alone could not deliver. The per-string register
    instruction was already being sent, and an audit of forty gate-passing rows still
    found six register mismatches; measured corpus-wide, 486 of 3395 rows that declare a
    register were translated in the other one.
    """
    want = marker_of(en)
    if want is None:
        return None
    got = of_korean(ko)
    if got is None or got == want:
        return None
    return f'register: the source is {want} but the translation is {got}' ''.rstrip()

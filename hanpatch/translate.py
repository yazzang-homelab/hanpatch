"""Glossary-locked EN->KO batch translator with hard validation gates.

Consistency strategy (no human in the loop):
  1. Translation memory is keyed by the exact English string, so a given source
     line always resolves to one Korean line no matter how often it appears.
  2. Fixed terms live in an authoritative glossary; the relevant subset is
     injected into every prompt AND re-verified after generation.
  3. Structural invariants (markup tags, newline count, placeholder tokens) are
     compared as multisets and any mismatch fails the item.
  4. Failures are retried with explicit repair feedback, then rotated to the next
     provider; anything still failing is written to a review queue instead of
     being silently accepted.
"""
import itertools
import json
import os
import re
import sys
import unicodedata


from hanpatch import glossary
from hanpatch import register
from hanpatch import josa
from hanpatch import providers
from hanpatch import wrap
from hanpatch import tm
from hanpatch import config

TAG_RE = config.tag_re()
SOURCE_ONLY_RE = config.source_only_re()
HANGUL_RE = re.compile(r'[가-힣]')
# Fullwidth Latin letters were the reference title's inner-monologue device, so a
# survivor in the output meant untranslated or corrupt text. That is a per-title
# fact, not a universal one: `fullwidth_is_content` decides whether it holds.
FULLWIDTH_LATIN_RE = re.compile(r'[Ａ-Ｚａ-ｚ]')
LATIN_WORD_RE = re.compile(r'[A-Za-z]{2,}')
LATIN_ALLOW_ALWAYS = {'HP', 'MP', 'AI', 'II', 'III', 'IV', 'V', 'TRPG', 'SD', 'WT',
                      'ATK', 'DEF', 'EXP', 'LV', 'OK'}
LATIN_ALLOW = set(config.prof('latin_allow') or ()) | LATIN_ALLOW_ALWAYS
# tags that substitute a runtime value: Korean word order may move these.
# Defined in wrap.py because line wrapping needs the same distinction (a
# substitution tag renders glyphs, a control tag renders nothing).
MOVABLE_TAGS = wrap.SUBST_TAGS
# DQ7 delimiter validation is structural rather than dependent on how a profile
# spells its extraction regex. Its declared controls and substitutions remain the
# only engine tokens accepted at translation time.
_DQ7_BRACE_TOKEN = re.compile(r'\{[A-Z0-9_]+\}\Z')

# ---------------------------------------------------------------- source language
# The source script is a profile fact. `en` keeps every Latin-oriented heuristic
# exactly as it was; other values switch off the ones that are wrong for a script
# with no word separators, so nothing silently no-ops.
KANA_RE = re.compile(r'[\u3041-\u309f\u30a0-\u30ff\uff66-\uff9f]')
KANJI_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')


def source_lang():
    return config.source_lang()


def _fullwidth_is_device():
    """Whether fullwidth Latin is this title's authoring device (must be translated).

    `None` means AUTO. Today's code emits the rule unconditionally, so AUTO has to
    keep emitting it for a Latin source; otherwise every existing project that
    never declared the key would silently lose the rule.
    """
    declared = config.prof('fullwidth_is_content')
    if declared is None:
        return source_lang() == 'en'

    return bool(declared)


def residual_script_problems(ko):
    """Source script surviving in the target, per `residual_script_flag`.

    Kana can never be legitimate Korean output, so it always flags. Kanji flags
    by default because an untranslated Japanese row is otherwise invisible, with
    `kanji_allowlist` as the escape for characters a title deliberately keeps.
    """
    flag = config.prof('residual_script_flag')
    if flag == 'off':
        return []
    if flag != 'kana+kanji':
        raise SystemExit(
            f'unsupported residual_script_flag {flag!r}; accepted values: off, kana+kanji')

    stripped = TAG_RE.sub('', ko)
    out = []
    kana = sorted(set(KANA_RE.findall(stripped)))
    if kana:
        out.append(f'kana left in translation: {"".join(kana[:8])}')
    allow = set(config.prof('kanji_allowlist') or ())
    kanji = sorted({c for c in KANJI_RE.findall(stripped) if c not in allow})
    if kanji:
        out.append(f'kanji left in translation: {"".join(kanji[:8])}')
    return out


def fold(s):
    """NFKC fold so fullwidth Latin is detected like ASCII."""
    return unicodedata.normalize('NFKC', s)


def segments(s):
    """Text pieces between control tags (movable placeholders are not boundaries)."""
    parts = []
    buf = []
    for m in re.finditer(r'<[^>\n]*>|[^<]+', s):
        tok = m.group()
        if tok.startswith('<') and tok.endswith('>') and tok not in MOVABLE_TAGS:
            parts.append(''.join(buf))
            buf = []
        else:
            buf.append(tok if not tok.startswith('<') else '')
    parts.append(''.join(buf))
    return parts


def tag_skeleton(s):
    """Ordered control tags; movable substitution tags collapse to a wildcard.

    Source-only annotations are dropped for the same reason `tags` drops them: they are
    expected to vanish, so counting them turned every correct translation into a
    "control tag order changed" failure - 40445 of them on this corpus.
    """
    found = [match.group(0) for match in TAG_RE.finditer(s)]

    if SOURCE_ONLY_RE is not None:
        found = [t for t in found if not SOURCE_ONLY_RE.fullmatch(t)]
    return ['*' if t in MOVABLE_TAGS else t for t in found]


_WORD = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")


def _tokens(s):
    return _WORD.findall(fold(TAG_RE.sub(' ', s)).lower())


def copied_spans(en, ko, ngram=3):
    """Source word n-grams reproduced verbatim in the translation.

    Punctuation, case and character width are normalised first, so inserting a
    colon, an em dash, a newline or a digit cannot hide a copied span.
    """
    tokenizer = config.prof('copied_spans_tokenizer')
    if tokenizer != 'latin':
        raise ValueError(f'unimplemented copied_spans_tokenizer: {tokenizer!r}')
    et, kt = _tokens(en), _tokens(ko)
    if len(et) < ngram or len(kt) < ngram:
        return []
    kset = {tuple(kt[i:i + ngram]) for i in range(len(kt) - ngram + 1)}
    hits = []
    for i in range(len(et) - ngram + 1):
        g = tuple(et[i:i + ngram])
        if g in kset and not all(w.isdigit() or w.upper() in LATIN_ALLOW for w in g):
            hits.append(' '.join(g))
    return hits

STYLE_DEFAULT = {
    'dialogue': (
        '이 게임은 테이블톱 RPG(TRPG) 세션을 재현한 다크 판타지다. '
        '내레이션·지문은 반드시 소설체 평서형(~다/~였다) 문어체로 통일한다. '
        '등장인물 대사는 자연스러운 구어체로 하되 인물별 말투를 일관되게 유지한다. '
        '플레이어에게 조작을 안내하는 시스템 문장만 존댓말(~하세요/~합니다)을 쓴다. '
        '한 문자열 안에서 평서형과 존댓말을 섞지 않는다. '
        '주사위·판정 등 TRPG 용어의 어감을 살린다. 원문보다 길어지지 않게 간결하게 쓴다.'),
    'system': (
        '게임 시스템 도움말·메뉴 텍스트다. 설명문은 평서형(~한다)으로, '
        '플레이어에게 직접 지시·질문하는 문장은 존댓말(~하세요/~합니까)로 통일한다. '
        '버튼·메뉴 명칭은 짧게 유지한다.'),
    'arms_help': (
        '무기·방어구·장비의 도감 설명문이다. 간결한 설명체 평서형(~다)으로 번역한다.'),
    'default': '게임 내 텍스트다. 자연스러운 한국어로 번역한다.',
}

STYLE = dict(STYLE_DEFAULT)
STYLE.update(config.prof('register') or {})

# The string tables are not dialogue: they are menu labels, commands, and the item,
# monster, job and place names the dialogue interpolates. A sentence style produces
# '싸운다' where a Korean menu says '싸우기', and a name rendered as a sentence is worse
# still. Their families are the only ones prefixed '@', so the style follows the prefix
# rather than needing 28 profile entries that would drift from the file list.
TABLE_STYLE = (
    '게임 UI 문자열 표다. 대사가 아니라 메뉴 항목·명령·아이템/몬스터/직업/지명 이름이다. '
    '동작을 고르는 메뉴 항목은 한국어 메뉴 관례대로 명사형(~하기/~기)으로 옮긴다: '
    'たたかう→싸우기, にげる→도망치기, つかう→사용하기. '
    '이름은 문장이 아니라 이름으로 옮기고 서술어를 붙이지 않는다. '
    '대사에서 이미 쓰인 표기가 있으면 그 표기를 따른다. 화면 폭이 좁으니 짧게 유지한다.')


def style_for(kind):
    if kind and kind.startswith('@'):
        return TABLE_STYLE
    return STYLE.get(kind, STYLE['default'])


SOURCE_LANG_NAME = {'en': '영어', 'ja': '일본어'}

# Rule 5 forbids leaving SOURCE-language words untranslated. Which script that
# means is a per-title fact, so the rule text is parameterised rather than
# hardcoded to English.
_RULE_5 = {
    'en': '5. 영어 단어를 그대로 남기지 않는다. 단, HP/MP/AI 같은 약어와 숫자, 마크업 태그는 예외.',
    'ja': '5. 일본어(가나·한자)를 그대로 남기지 않는다. 단, HP/MP/AI 같은 약어와 숫자, 마크업 태그는 예외.',
}

# Rule 6-1 asserts fullwidth Latin is an authoring device that must be
# translated. That was true of the reference title and is false in general, so it
# is emitted only when the profile declares it.
_RULE_6_1 = """6-1. 전각 영문(Ｉ ｔｏｌｄ ｈｅｒ 처럼 넓은 영문자)은 등장인물의 속마음·귓속말을 나타내는 연출이다.
     반드시 한국어로 번역한다. 영문을 그대로 복사하면 실패로 처리된다.
     원문의 。 ゜ ， ． 같은 전각 기호는 그대로 유지한다."""


def _system_prompt():
    src = source_lang()
    rules = [
        '1. 출력은 JSON 객체 하나만. 키는 입력의 id 문자열, 값은 번역된 한국어 문자열.',
        '2. <...> 형태의 마크업 태그는 원문과 개수·순서·철자까지 완전히 동일하게 유지한다. 태그 내부를 번역하지 않는다.',
        '   (예: <player>, <enemy>, <item>, <br>, <page>, <key>, <color=default>, <wait=0.5>, <script=SE01>, <lineheight=8>)',
        '3. 줄바꿈(\\n)의 개수와 위치를 원문과 동일하게 유지한다.',
        '4. 용어집(GLOSSARY)에 있는 표현은 반드시 지정된 한국어 표기를 그대로 사용한다.',
        _RULE_5.get(src, _RULE_5['en']),
        '6. 원문에 없는 내용을 추가하거나, 문장을 생략하지 않는다.',
    ]
    if _fullwidth_is_device():
        rules.append(_RULE_6_1)
    # Rule 7 used to hard-code the KS X 1001 set. That was correct only while the fonts
    # carried 2350 syllables; a title that builds full coverage was still being told to
    # avoid 8822 of its own language's syllables, which forces stilted word choices for no
    # reason. The constraint now follows the DECLARED font charset.
    from hanpatch.platforms.threeds import fontbuild as _fb
    try:
        _wide = (config.prof('font_charset') or 'ksx1001') != 'ksx1001'
    except Exception:
        _wide = False
    rules.append('7. 한국어 음절은 자유롭게 쓴다. 특수 기호는 원문에 있는 것만 쓴다.'
                 if _wide else
                 '7. 한국어 완성형(KS X 1001) 범위의 음절만 사용한다. '
                 '특수 기호는 원문에 있는 것만 쓴다.')
    if config.source_lang() == 'ja':
        # Register is checked, so it belongs in the absolute rules rather than only in a
        # per-string hint. Measured: with the hint alone, 181 rows that the source marks
        # polite or plain came back in the other level, and a re-sweep recovered only half
        # of them - a rule the model sees once at the top carries more weight than a line
        # in a list it is also asked to translate.
        rules.append('8. 각 문자열의 화법(존댓말/평서형)은 [문자열별 화법] 지시를 '
                     '반드시 따른다. 원문이 です·ます체면 존댓말로, だ·である체면 '
                     '평서형으로 옮기고, 한 문자열 안에서 두 화법을 섞지 않는다.')
    head = '당신은 %s→%s 게임 로컬라이제이션 전문 번역가다. 게임 "%s"의 번역을 작업한다.' % (
        SOURCE_LANG_NAME.get(src, src), config.lang_name(), config.title())
    return '%s\n\n절대 규칙:\n%s\n' % (head, '\n'.join(rules))


SYSTEM_PROMPT = _system_prompt()

def reset():
    """Re-derive the profile-dependent constants (called by `config.set_root`).

    `wrap.reset()` runs first because `MOVABLE_TAGS` is an alias of the layout
    layer's substitution-tag set, and a stale alias would let a control tag be
    treated as movable under the new profile. Resetting `wrap` twice is harmless.
    """
    global TAG_RE, SOURCE_ONLY_RE, LATIN_ALLOW, MOVABLE_TAGS, STYLE, SYSTEM_PROMPT
    global _FONT_OK, _FONT_KEY
    wrap.reset()
    TAG_RE = config.tag_re()
    SOURCE_ONLY_RE = config.source_only_re()
    LATIN_ALLOW = set(config.prof('latin_allow') or ()) | LATIN_ALLOW_ALWAYS
    MOVABLE_TAGS = wrap.SUBST_TAGS
    STYLE = dict(STYLE_DEFAULT)
    STYLE.update(config.prof('register') or {})
    SYSTEM_PROMPT = _system_prompt()
    # The glyph authority belongs to the fonts of the project we just left.
    _FONT_OK = None
    _FONT_KEY = None


def tags(s):
    """Engine tokens whose presence the translation must preserve.

    Source-only annotations are EXCLUDED: they are supposed to disappear, so counting them
    here would make every correct translation look like it lost a tag. Their absence is
    checked separately, in the one direction that matters.
    """
    found = [match.group(0) for match in TAG_RE.finditer(s)]

    if SOURCE_ONLY_RE is not None:
        found = [t for t in found if not SOURCE_ONLY_RE.fullmatch(t)]
    return sorted(found)

def strip_source_only(text):
    """Remove source-language annotations before validating model output.

    A source-only token is a reading aid, not translated content. Models sometimes remove
    it, sometimes translate its body and leave the wrapper, and sometimes preserve it
    verbatim. All three cases have the same deterministic answer for the target: remove the
    wrapper and its body. The public `check()` call still rejects an unnormalised target so
    callers that bypass `Translator.batch()` cannot silently ship it; the batch path applies
    this lossless-for-the-target cleanup before that gate, which prevents safe cleanup from
    consuming a retry.
    """
    if SOURCE_ONLY_RE is None or not text:
        return text
    return SOURCE_ONLY_RE.sub('', text)


def nl(s):
    return s.count('\n')


def dq7_delimiter_problems(text):
    """Reject malformed or undeclared DQ7 delimiters without using ``tag_pattern``.

    A measured title may declare a literal delimiter when the container stores one next to
    a real token (DQ7 has one source row with an extra closing brace after a substitution).
    The declaration is explicit and applies to source and target; all other raw delimiters
    remain errors.
    """
    if config.cfg().get('adapter') != 'dq7':
        return []
    literal = set(config.prof('literal_delimiters') or ())
    declared = MOVABLE_TAGS | set(config.prof('control_tags') or ())
    position = 0
    while position < len(text):
        char = text[position]
        if char in literal:
            position += 1
            continue
        if char not in '<>{}':
            position += 1
            continue
        if char in '<{':
            source_only = (SOURCE_ONLY_RE.match(text, position)
                           if SOURCE_ONLY_RE is not None else None)
            if source_only is not None and source_only.end() > position:
                token = source_only.group(0)
                body = token[1:-1] if token.startswith('{') and token.endswith('}') else None
                if body is None or any(delimiter in body for delimiter in '<>{}'):
                    return ['invalid delimiter inside source-only markup']
                position = source_only.end()
                continue

        if char == '<':
            end = text.find('>', position + 1)
            if end < 0 or '\n' in text[position:end]:
                return [f'unconsumed delimiter {char!r}']
            token = text[position:end + 1]
            if token not in declared:
                return [f'unknown delimiter form {token!r}']
            position = end + 1
            continue
        if char == '{':
            end = text.find('}', position + 1)
            if end < 0:
                return [f'unconsumed delimiter {char!r}']
            token = text[position:end + 1]
            if not _DQ7_BRACE_TOKEN.fullmatch(token) or token not in declared:
                return [f'unknown delimiter form {token!r}']
            position = end + 1
            continue
        return [f'unconsumed delimiter {char!r}']
    return []


# Japanese lays a record out with ideographic spaces: a continuation line opens with
# `　　`, and a clause break inside a line is padded rather than punctuated. A translator
# copies that padding because it is part of the source string, and then `wrap.fits`
# re-flows the Korean - so the padding stops being an indent and lands mid-sentence.
# Measured on this corpus before this ran: 58,347 of 65,836 shipped rows (88.6%) carried a
# run of three or more spaces, which is what a player sees as holes in the dialogue.
#
# The fix belongs here rather than in the prompt because a prompt cannot be enforced: the
# value this function returns is the one the manifest seals, the panel judges and the
# injector writes, so all three see the same string.
_WS_RUN = re.compile(r'[ \u3000\t]{2,}')
_FW_PUNCT = {'。': '.', '、': ',', '！': '!', '？': '?', '：': ':', '；': ';',
             '～': '~', '〜': '~'}
_FW_PUNCT_RE = re.compile('[' + ''.join(_FW_PUNCT) + ']')
# A Korean sentence does not end twice. The kuten (`。`) becomes a full stop and
# the touten (`、`) a comma, and where the sentence already ended in an ellipsis,
# a wave dash or a bang the converted stop is left standing next to it. Measured
# on the shipped DQ7 corpus: 7795 rows carried `….` and 470 `~.`, 7905 rows in
# total, every one of them produced by this pipeline's own conversion of `……。`
# and `～。` - the Japanese source writes both, the Korean rendering keeps only
# the stronger mark.
_TERMINAL = '…‥!?~'
_REDUNDANT_STOP = re.compile('([' + re.escape(_TERMINAL) + r'])[.,]+')
# Whitespace in front of a character that may not open a display line is never a
# word gap: it is what lets `wrap` put a lone `」` or `?` at the head of the next
# line. The set of such characters belongs to the layout module, which enforces it.
_ORPHAN_SPACE = re.compile(r'[ \u3000\t\n]+(?=['
                           + re.escape(''.join(wrap.NO_LINE_START)) + r'])')


def _tag_pieces(text):
    """`text` as [(is_tag, piece)], safe for a title pattern that captures.

    `re.split` on a wrapped pattern returns one element per group, so a profile whose
    `tag_pattern` spells its alternatives as `(<...>)|(\\{...\\})` - which is a normal way
    to write it - yielded each tag three times and rejoining the pieces DUPLICATED every
    tag in the string. The passes below then returned that as the repaired value, and
    nothing noticed, because the tag-multiset check runs before them. It took running the
    rules twice over their own output to surface it: on the second pass the multiset had
    quadrupled. Walking matches is immune to how the title chose to group its pattern.
    """
    out = []
    last = 0
    for match in TAG_RE.finditer(text):
        if match.start() > last:
            out.append((False, text[last:match.start()]))
        out.append((True, match.group()))
        last = match.end()
    if last < len(text):
        out.append((False, text[last:]))
    return out


def normalise_punctuation(ko):
    """Korean punctuation for a Japanese source, with no doubled sentence end.

    Scoped to a Japanese source because the kuten and the touten are that
    source's punctuation. The reference title authors fullwidth punctuation
    deliberately - `。Ｆｉｅｎｄｓ！。` is its inner-monologue device - so rewriting it
    there would replace an authored glyph with a guess and change bytes the
    identity rebuild is pinned against.

    Whitespace before a closing mark goes with it. Deleting it is not cosmetic:
    `wrap` breaks lines at spaces, so ` ……` and ` 」` are exactly how 116 shipped
    rows ended up with a line that opens on punctuation.
    """
    if source_lang() != 'ja' or not ko:
        return ko
    out = []
    for is_tag, piece in _tag_pieces(ko):
        if is_tag:
            out.append(piece)
            continue
        piece = _FW_PUNCT_RE.sub(lambda m: _FW_PUNCT[m.group()], piece)
        piece = _REDUNDANT_STOP.sub(lambda m: m.group(1), piece)
        out.append(piece)
    return _ORPHAN_SPACE.sub('', ''.join(out))


def normalise_ja_layout(en, ko):
    """Strip the source's layout padding out of a Japanese-sourced translation.

    Only the ja path: an English source has no ideographic padding convention, and the
    reference title must keep producing identical bytes.

    Whitespace INSIDE a tag is left alone - `{2とくてん}` and friends are markup, and
    collapsing a run there would rewrite a delimiter rather than a layout artefact.
    """
    if source_lang() != 'ja' or not ko:
        return ko
    out = []
    for is_tag, piece in _tag_pieces(ko):
        if is_tag:
            out.append(piece)
            continue
        # A run of padding is one word gap, never a hole.
        piece = _WS_RUN.sub(' ', piece.replace('\u3000', ' '))
        out.append(piece)
    ko = ''.join(out)
    # Per display line: no leading or trailing padding. `wrap.fits` re-flows the text
    # afterwards, so an indent written here would not survive as an indent anyway.
    return '\n'.join(line.strip(' \t') for line in ko.split('\n'))


# How many times the normalising passes may run before the value must be stable.
# Two is what convergence needs in every measured case (one pass to normalise, one
# to prove nothing moved); the extra headroom exists so a rule that only becomes
# reachable after the wrapper joined two lines still converges instead of being
# reported as a defect. A value that is still moving at the cap is a rule conflict
# and is reported rather than shipped - picking whichever side the loop happened to
# stop on is how a build ships text its own rules disagree with.
_NORMALISE_PASSES = 4


def check(en, ko, gl, kind='default', group=None):
    """Return (normalised_ko, problems) at a FIXED POINT of the rules.

    `_check_once` both validates and repairs, and some repairs only become
    reachable after an earlier one moved the text: `normalise_punctuation` runs
    before the wrapper, so a `…` ending one display line and the `.` opening the
    next are not adjacent until `soften` has joined them, and `josa.weld_after_tags`
    is the same story for a particle the previous wrap pushed onto its own line.
    A single pass therefore returns a value that the NEXT pass would still change.

    That is not cosmetic. The audit gate demands `check(sealed) == sealed`, because
    a rule added after a seal was written is otherwise invisible - `check` quietly
    repairs the string and audit throws the repair away. A non-idempotent `check`
    makes that demand unsatisfiable, so the corpus can never go green and the gate
    stops meaning anything. Iterating here is what makes the sealed value and the
    rules agree.
    """
    value = ko
    for _ in range(_NORMALISE_PASSES):
        result, problems = _check_once(en, value, gl, kind, group)
        if problems:
            return result, problems
        if result == value:
            return result, []
        value = result
    return value, [f'normalisation did not settle in {_NORMALISE_PASSES} passes; '
                   f'two rules are undoing each other on this row']


def _check_once(en, ko, gl, kind='default', group=None):
    """One pass of validate-and-repair. Callers want `check`, which iterates."""
    problems = []
    if not ko or not ko.strip():
        return ko, ['empty']
    for side, value in (('source', en), ('target', ko)):
        delimiter_problems = dq7_delimiter_problems(value)
        if delimiter_problems:
            return ko, [f'{side} delimiter integrity: {delimiter_problems[0]}']
    # A source-only annotation is a reading aid for the source language. Keeping it puts
    # source script inside the translation, and keeping SOME of them is worse than keeping
    # all - it reads as corruption. Measured on DQ7 before this check existed: of 56824
    # records carrying furigana, 38274 translations dropped them correctly, 1 kept them
    # all, and 2171 kept a subset with no gate signal whatsoever, because the tag multiset
    # comparison never saw these tokens.
    if SOURCE_ONLY_RE is not None:
        left = [match.group(0) for match in SOURCE_ONLY_RE.finditer(ko)]
        if left:
            problems.append(f'source-only markup left in the translation: '
                            f'{sorted(set(left))[:4]}')
            return ko, problems
    # control tags must keep their order and position; only runtime-substitution
    # placeholders may move to fit Korean word order
    if tags(en) != tags(ko):
        problems.append(f'tag multiset mismatch: expected {tags(en)} got {tags(ko)}')
        return ko, problems
    if tag_skeleton(en) != tag_skeleton(ko):
        problems.append(f'control tag order changed: expected {tag_skeleton(en)} '
                        f'got {tag_skeleton(ko)}')
        return ko, problems
    # each stretch of text between two control tags must stay non-empty (or stay
    # empty) so translated text cannot migrate out of a coloured/lineheight span
    es, ks = segments(en), segments(ko)
    for i, (a, b) in enumerate(zip(es, ks)):
        if bool(a.strip()) != bool(b.strip()):
            problems.append(f'text moved across control tag boundary at segment {i}')
            break
    # A soft-break marker is layout the CONTAINER owns: the engine breaks the name there
    # when the box is too narrow, so dropping it turns a name that wrapped cleanly into
    # one that overflows or clips. Nothing else in this checker sees it, because it is not
    # a tag - it is one character inside a word. Measured on the string tables: of 1348
    # rows carrying it, 328 translations had silently dropped it.
    mark = config.prof('soft_break')
    if mark and en.count(mark) != ko.count(mark):
        problems.append(f'soft-break marker {mark!r}: source has {en.count(mark)}, '
                        f'translation has {ko.count(mark)}')
        return ko, problems
    ko = normalise_punctuation(ko)
    ko = normalise_ja_layout(en, ko)
    # Every josa decision in one pass: fixed proper nouns, `으로`, and the particles
    # that follow a runtime substitution token, which no single form can satisfy.
    ko, jprobs = josa.auto(ko, glossary.hard().values())
    problems += jprobs
    ko, wprobs = wrap.fits(en, ko, kind, group)
    problems += wprobs
    if not HANGUL_RE.search(re.sub(TAG_RE, '', ko)):
        stripped = re.sub(TAG_RE, '', en)
        # Japanese source text has no Latin run, so its no-Hangul guard requires
        # actual kana or kanji rather than treating tag-only and numeric rows as text.
        if ((source_lang() == 'ja' and (KANA_RE.search(stripped) or KANJI_RE.search(stripped)))
                or re.search(r'[A-Za-z]{3,}', stripped)):
            problems.append('no hangul produced')
    problems += residual_script_problems(ko)
    # Tags and row breaks carry no source glyph, so they cannot hide a hard term
    # from enforcement. The per-language join rule lives in `glossary` because the
    # relevance prefilter has to use the SAME normalisation: a term the prefilter
    # cannot see never reaches this loop at all.
    term_source = glossary.enforcement_blob([en])
    flat = ko.replace('\n', ' ')
    hardgl = glossary.hard()
    for term, koterm in gl.items():
        if term not in hardgl:
            continue
        # Whole-term matching is the source language's business: `\b` never
        # matches a term embedded in spaceless Japanese, which silently retired
        # this enforcement for every JP row.
        if (glossary.matches_term(term, term_source, fold_case=False)
                and not glossary.mandate_present(koterm, flat)):
            problems.append(f'glossary: "{term}" must render as "{koterm}"')
    # Latin survives only for the explicit acronym allowlist. Source
    # capitalisation is NOT a free pass: an English spell name left in the
    # Korean text contradicts its localised form elsewhere in the corpus.
    leftovers = [w for w in LATIN_WORD_RE.findall(fold(re.sub(TAG_RE, '', ko)))
                 if w.upper() not in LATIN_ALLOW]
    if leftovers:
        problems.append(f'untranslated latin: {sorted(set(leftovers))[:6]}')
    # isolated Latin letters fused into Hangul are corruption ("ｙ원로 ｏ이")
    fko = fold(TAG_RE.sub(' ', ko))
    fw = FULLWIDTH_LATIN_RE.findall(TAG_RE.sub(' ', ko))
    if fw and _fullwidth_is_device():
        problems.append(f'fullwidth latin left in translation: {"".join(fw[:8])}')
    fen = fold(TAG_RE.sub(' ', en))
    single_src = set(re.findall(r'(?<![A-Za-z])[A-Za-z](?![A-Za-z])', fen))
    fused = [m.group() for m in
             re.finditer(r'(?<![A-Za-z])[A-Za-z](?![A-Za-z])', fko)
             if m.group() not in single_src
             and (re.match(r'[가-힣]', fko[m.end():m.end() + 1] or ' ')
                  or re.match(r'[가-힣]', fko[m.start() - 1:m.start()] or ' '))]
    if fused:
        problems.append(f'latin fused into hangul: {sorted(set(fused))[:6]}')
    cp = copied_spans(en, ko)
    if cp:
        problems.append(f'source text copied verbatim: {cp[:2]}')
    reg = register.divergence(en, ko)
    if reg:
        problems.append(reg)
    bad = [c for c in ko if not _in_font(c)]
    if bad:
        problems.append(f'unsupported glyphs: {sorted(set(bad))[:8]}')
    return ko, problems[:6]


_FONT_OK = None
_FONT_KEY = None
def _target_fonts():
    return [config.p(x) for x in config.prof('font_out')]
def _source_fonts():
    return [config.p(x) for x in config.prof('font_src')]


def _in_font(ch):
    """Coverage authority is the intersection of the CMAPs of the fonts we ship.

    A built target font is the only glyph authority, so validation fails closed
    until every target font has been built.
    """
    global _FONT_OK, _FONT_KEY
    targets = _target_fonts()
    key = tuple((p, os.path.getmtime(p), os.path.getsize(p))
                for p in targets if os.path.exists(p))
    if _FONT_OK is None or key != _FONT_KEY:
        paths = [p for p in targets if os.path.exists(p)]
        if not paths:
            raise SystemExit(
                'no built target fonts: the built font is the only glyph authority. '
                'Run hanpatch fonts.')
        if len(paths) != len(targets):
            raise SystemExit(
                f'glyph authority incomplete: found {paths}, need {targets}. '
                'Run hanpatch fonts.')
        from hanpatch import adapter as _adapter
        supplied = None
        try:
            supplied = _adapter.project_adapter().font_coverage(paths)
        except Exception:
            supplied = None
        if supplied is not None:
            # set the cache and fall through to the membership test below.
            # Returning the set here instead made `_in_font` answer with a
            # non-empty set, which is truthy, so every character passed and the
            # glyph gate reported clean while rows shipped syllables the built
            # font does not hold.
            ok = set(supplied)
            ok.add('\n')
            _FONT_OK = ok
            _FONT_KEY = key
            return ch in _FONT_OK

        from hanpatch.platforms.threeds.bcfnt import Bcfnt
        sets = []
        for p in paths:
            f = Bcfnt(open(p, 'rb').read())
            cov = set()
            for s0, e0, mt, payload in f.cmap:
                if mt == 0:
                    cov |= {chr(c) for c in range(s0, e0 + 1)}
                elif mt == 1:
                    cov |= {chr(s0 + i) for i, v in enumerate(payload)
                            if v != 0xFFFF}
                else:
                    cov |= {chr(c) for c, _ in payload}
            sets.append(cov)
        ok = set.intersection(*sets)
        ok.add('\n')
        _FONT_OK = ok
        _FONT_KEY = key
    return ch in _FONT_OK


def build_prompt(items, gl_subset, kind, context):
    """The user message, ordered so the STABLE part is a prefix.

    Prompt caching bills a repeated leading span at a fraction of a fresh one - on the paid
    DeepSeek lane a cache-hit prefix costs 1/50 of a miss, which is the difference between
    about $1.54 and about $0.69 for this corpus. Caching keys on the PREFIX, so anything
    that changes per batch truncates the cached span at the point it appears.

    So the order is: family style (same for every batch of a family), then the enforced
    terms (the same 21 for the whole title), then the per-batch glossary subset, the
    per-string register lines, and finally the payload. Putting the varying glossary subset
    first, as this did, meant almost nothing after the system prompt could ever be reused.
    """
    parts = []
    parts.append(f'[문맥/문체 지침] {style_for(kind)}')
    # Enforced terms are title-wide and identical for every batch: they belong in the
    # cacheable prefix, not in the per-batch block, even though they also appear there.
    hard = {t: k for t, k in sorted(glossary.enforced_contract().items())}
    if hard:
        parts.append('[고정 표기 - 예외 없이 이 표기를 쓴다]\n' +
                     '\n'.join(f'- {t} => {k}' for t, k in hard.items()))
    gl_subset = {t: k for t, k in gl_subset.items() if t not in hard}
    if gl_subset:
        parts.append('[GLOSSARY] 아래 표기를 반드시 그대로 사용:\n' +
                     '\n'.join(f'- {en} => {ko}' for en, ko in gl_subset.items()))
    if context:
        parts.append('[직전 번역 예시 - 문체/호칭 일관성 참고용, 번역 대상 아님]\n' +
                     '\n'.join(f'EN: {strip_source_only(e)}\nKO: {k}'
                               for e, k in context))
    # Furigana and similar source-only annotations are removed from the prompt as well as
    # from accepted output. Sending `{N<reading>}` to a model asks it to translate an
    # annotation that the target engine never renders; on the last unresolved rows it also
    # obscured the short hard term the gate required. The raw source remains authoritative
    # for validation and manifest identity.
    payload = {str(i): strip_source_only(it['en']) for i, it in enumerate(items)}
    # Register is per STRING, not per family: measured on this corpus only 39.6% of
    # records carry a marker at all and 178 of 339 families are internally mixed, so a
    # single family-level instruction would impose one voice on both sides of a
    # conversation. Each row that declares a register carries its own line.
    regs = {str(i): register.instruction(it['en'])
            for i, it in enumerate(items)}
    regs = {k: v for k, v in regs.items() if v}
    if regs:
        parts.append('[문자열별 화법 - 원문이 지정한 것이므로 반드시 지킨다]\n' +
                     '\n'.join(f'- {k}: {v}' for k, v in regs.items()))
    parts.append('[번역 대상] 아래 JSON의 각 값을 한국어로 번역해 같은 키로 반환:\n' +
                 json.dumps(payload, ensure_ascii=False, indent=1))
    return '\n\n'.join(parts)


def _balanced_objects(text):
    r"""Every balanced {...} span in `text`, outermost only, string-aware.

    A single greedy r'\{.*\}' was WRONG and it was the main source of the
    'returned non-JSON' failures: a reasoning model narrates before it answers, so the
    span ran from the first brace in the prose to the last brace anywhere and parsed as
    nothing. Measured on opencode:nemotron-3-ultra-free, which emitted 10754 characters
    for an 8-string batch. Braces inside JSON strings must not open a level either,
    because the game's own markup uses {PLACEHOLDER} tokens.
    """
    spans = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for n, c in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == '{':
            if depth == 0:
                start = n
            depth += 1
        elif c == '}':
            if depth:
                depth -= 1
                if depth == 0:
                    spans.append(text[start:n + 1])
    return spans


def parse_json(text, want=()):
    """The reply as a dict, or None.

    `want` is the set of keys the caller asked for. When a reply carries several
    objects - a worked example, then the answer - the one that actually answers is
    chosen by key coverage rather than by position.
    """
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
        text = re.sub(r'\n?```$', '', text).strip()
    cands = []
    try:
        cands.append(json.loads(text))
    except json.JSONDecodeError:
        pass
    for span in _balanced_objects(text):
        try:
            cands.append(json.loads(span))
        except json.JSONDecodeError:
            continue
    want = set(want)
    best = None
    best_score = -1
    for obj in cands:
        if not isinstance(obj, dict):
            continue
        strs = {k: v for k, v in obj.items() if isinstance(v, str)}
        score = len(set(strs) & want) if want else len(strs)
        if score > best_score:
            best, best_score = obj, score
    return best


class Translator:
    def __init__(self, pool=None, kind='default', verbose=True):
        self.pool = pool or providers.build_pool()
        self.gl = glossary.load()
        self.kind = kind
        self.verbose = verbose
        self.pi = 0
        self._seat = itertools.count()
        self.stats = {'ok': 0, 'failed': 0, 'calls': 0, 'retries': 0}
        self.last_provider = {}

    def seat(self):
        """A starting endpoint for one batch, handed out round-robin.

        The first version pinned every batch to pool[0] so a batch's retries would not
        drift between models. With N workers that meant N concurrent calls to the SAME
        endpoint, which is how a run ended up reporting
        'all 4 Groq key(s) are parked; retry in 34s' - the rotator was being asked for
        more concurrency than one endpoint has keys, while the rest of the pool sat idle.

        Consistency WITHIN a batch is what mattered, and that is preserved: a batch keeps
        its own seat for its first tries. Consistency BETWEEN batches is no longer the
        prompt's problem either, because register is now derived per string and the
        glossary is enforced, so two batches on two endpoints do not drift apart.
        """
        return next(self._seat)

    def _next_provider(self, attempt=0, seat=0, exclude=()):
        # Skip endpoints whose rotator has told us its keys are parked. Asking anyway
        # spends an attempt on a certain 429 - measured, 86 of 211 calls in one run.
        #
        # The fallback when everything is parked is the LIVE pool, not the whole
        # pool: a parked lane is busy and worth waiting for, but a retired one has
        # failed LANE_ERROR_LIMIT times in a row and asking it again spends a full
        # timeout to learn nothing. Falling back to `self.pool` put dead lanes
        # back into rotation at exactly the moment the surviving ones were
        # saturated, which is the worst time to waste an attempt.
        pool = (providers.available(self.pool)
                or providers.live(self.pool)
                or self.pool)
        # `exclude` is the set of lanes that ALREADY failed this batch. Without it a
        # batch re-picks a lane it just watched fail: with three of five lanes
        # broken, a 12-row run lost 4 rows because one batch spent all five attempts
        # on two dead endpoints while two healthy ones sat idle. Preferring untried
        # lanes is what makes "any two of these may die" true rather than likely.
        untried = [p for p in pool if p.id not in exclude]
        live = untried or pool
        n = len(live)
        if attempt < 2:
            return live[seat % n]
        # repair attempts walk away from this batch's own seat rather than from index 0,
        # so two batches retrying at the same moment do not converge on one endpoint
        self.pi += 1
        return live[(seat + self.pi) % n]

    def batch(self, items, context=(), attempts=None):
        """items: list of {'en': str}. Returns (results dict idx->ko, failures list)."""
        # Attempts are counted against the lanes that can still answer, not the
        # nominal pool size. With lanes retiring mid-run, `len(self.pool)` promises
        # attempts that no longer exist; with a big healthy pool it is also the
        # number of DISTINCT lanes worth trying before giving up on a batch.
        attempts = attempts or max(3, len(providers.live(self.pool)) + 1)
        seat = self.seat()
        # Lanes that already failed THIS batch. Reset per batch, not per run: a lane
        # that could not do one batch is often fine on the next one, and retiring is
        # handled by the provider's own consecutive-failure count.
        tried = set()
        gl_subset = glossary.relevant(self.gl, [it['en'] for it in items], self.kind)
        pending = list(range(len(items)))
        results = {}
        feedback = ''
        for attempt in range(attempts):
            if not pending:
                break
            sub = [items[i] for i in pending]
            prov = self._next_provider(attempt, seat, exclude=tried)
            prompt = build_prompt(sub, gl_subset, self.kind, context)
            qa = [f"id {i}: {s['qa']}" for i, s in enumerate(sub) if s.get('qa')]
            if qa:
                prompt += ('\n\n[검수자 지적 - 이 문제를 반드시 고쳐서 번역]\n' +
                           '\n'.join(qa))
            if feedback:
                prompt += ('\n\n[이전 시도의 오류 - 반드시 수정해서 다시 번역]\n' + feedback)
            try:
                self.stats['calls'] += 1
                # Hold the endpoint's own gate, not a global one: a free rotator saturates
                # at one request in flight while a Codex account takes several, and a
                # single global worker count cannot express both.
                with providers.gate_for(prov.id):
                    raw = prov.chat(SYSTEM_PROMPT, prompt,
                                    max_tokens=min(16000, 900 + 6 * sum(len(s['en']) for s in sub)))
            except RuntimeError as e:
                if self.verbose:
                    print(f'    ! {e}'[:200], flush=True)
                tried.add(prov.id)
                feedback = ''
                continue
            obj = parse_json(raw, want=[str(i) for i in range(len(sub))])
            if not isinstance(obj, dict):
                if self.verbose:
                    print(f'    ! {prov.id} returned non-JSON', flush=True)
                tried.add(prov.id)
                continue
            fails = []
            still = []
            for local, gi in enumerate(pending):
                ko = obj.get(str(local))
                if isinstance(ko, str):
                    ko = strip_source_only(ko.replace('\r\n', '\n'))
                    ko, probs = check(items[gi]['en'], ko, gl_subset, self.kind,
                                      items[gi].get('group'))
                    if not probs:
                        results[gi] = ko
                        self.last_provider[gi] = prov.id
                        continue
                    fails.append(f'id {local}: ' + '; '.join(probs))
                else:
                    fails.append(f'id {local}: 누락됨')
                still.append(gi)
            if still and self.verbose:
                print(f'    {prov.id}: {len(pending) - len(still)}/{len(pending)} ok, '
                      f'{len(still)} retry', flush=True)
            if still:
                self.stats['retries'] += 1
            # re-index feedback ids for the next (smaller) batch
            remap = {gi: n for n, gi in enumerate(still)}
            feedback = '\n'.join(
                re.sub(r'^id (\d+)', lambda m: f'id {remap.get(pending[int(m.group(1))], "?")}', f)
                for f in fails)[:2000]
            pending = still
        self.stats['ok'] += len(results)
        self.stats['failed'] += len(pending)
        return results, pending

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
import json
import os
import re
import sys
import unicodedata


from hanpatch import glossary
from hanpatch import josa
from hanpatch import providers
from hanpatch import wrap
from hanpatch import tm
from hanpatch import config

TAG_RE = config.tag_re()
HANGUL_RE = re.compile(r'[가-힣]')
# Fullwidth Latin letters are the source's inner-monologue device. They must be
# translated, so any survivor in the output is untranslated or corrupt text.
FULLWIDTH_LATIN_RE = re.compile(r'[Ａ-Ｚａ-ｚ]')
LATIN_WORD_RE = re.compile(r'[A-Za-z]{2,}')
LATIN_ALLOW = set(config.prof('latin_allow') or ()) | {'HP', 'MP', 'AI', 'II', 'III', 'IV', 'V', 'TRPG', 'SD', 'WT',
               'ATK', 'DEF', 'EXP', 'LV', 'OK'}
# tags that substitute a runtime value: Korean word order may move these
MOVABLE_TAGS = set(config.prof('movable_tags'))


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
    """Ordered control tags; movable substitution tags collapse to a wildcard."""
    return ['*' if t in MOVABLE_TAGS else t for t in TAG_RE.findall(s)]


_WORD = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")


def _tokens(s):
    return _WORD.findall(fold(TAG_RE.sub(' ', s)).lower())


def copied_spans(en, ko, ngram=3):
    """Source word n-grams reproduced verbatim in the translation.

    Punctuation, case and character width are normalised first, so inserting a
    colon, an em dash, a newline or a digit cannot hide a copied span.
    """
    et, kt = _tokens(en), _tokens(ko)
    if len(et) < ngram or len(kt) < ngram:
        return []
    kset = {tuple(kt[i:i + ngram]) for i in range(len(kt) - ngram + 1)}
    hits = []
    for i in range(len(et) - ngram + 1):
        g = tuple(et[i:i + ngram])
        if g in kset and not all(w.upper() in LATIN_ALLOW for w in g):
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

SYSTEM_PROMPT = """당신은 영어→%s 게임 로컬라이제이션 전문 번역가다. 게임 "%s"의 번역을 작업한다.

절대 규칙:
1. 출력은 JSON 객체 하나만. 키는 입력의 id 문자열, 값은 번역된 한국어 문자열.
2. <...> 형태의 마크업 태그는 원문과 개수·순서·철자까지 완전히 동일하게 유지한다. 태그 내부를 번역하지 않는다.
   (예: <player>, <enemy>, <item>, <br>, <page>, <key>, <color=default>, <wait=0.5>, <script=SE01>, <lineheight=8>)
3. 줄바꿈(\\n)의 개수와 위치를 원문과 동일하게 유지한다.
4. 용어집(GLOSSARY)에 있는 표현은 반드시 지정된 한국어 표기를 그대로 사용한다.
5. 영어 단어를 그대로 남기지 않는다. 단, HP/MP/AI 같은 약어와 숫자, 마크업 태그는 예외.
6. 원문에 없는 내용을 추가하거나, 문장을 생략하지 않는다.
6-1. 전각 영문(Ｉ ｔｏｌｄ ｈｅｒ 처럼 넓은 영문자)은 등장인물의 속마음·귓속말을 나타내는 연출이다.
     반드시 한국어로 번역한다. 영문을 그대로 복사하면 실패로 처리된다.
     원문의 。 ゜ ， ． 같은 전각 기호는 그대로 유지한다.
7. 한국어 완성형(KS X 1001) 범위의 음절만 사용한다. 특수 기호는 원문에 있는 것만 쓴다.
""" % (config.lang_name(), config.title())


def tags(s):
    return sorted(TAG_RE.findall(s))


def nl(s):
    return s.count('\n')


def check(en, ko, gl, kind='default', group=None):
    """Return (rewrapped_ko, problems). problems == [] means valid."""
    problems = []
    if not ko or not ko.strip():
        return ko, ['empty']
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
    # deterministic josa repair right after fixed proper nouns
    ko, _ = josa.fix_after(ko, set(glossary.hard().values()))
    ko = josa.fix_eu_ro(ko)
    ko, wprobs = wrap.fits(en, ko, kind, group)
    problems += wprobs
    if not HANGUL_RE.search(re.sub(TAG_RE, '', ko)) and HANGUL_RE.search('가'):
        stripped = re.sub(TAG_RE, '', en)
        if re.search(r'[A-Za-z]{3,}', stripped):
            problems.append('no hangul produced')
    # compare against a newline-flattened copy: line wrapping may split a term
    # across two rendered lines, which is still the correct term
    flat = ko.replace('\n', ' ')
    hardgl = glossary.hard()
    for term, koterm in gl.items():
        if term not in hardgl:
            continue
        if re.search(r'\b' + re.escape(term) + r'\b', en) and koterm not in flat:
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
    if fw:
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

    Falls back to the KS X 1001 repertoire only when no built font exists yet
    (first bootstrap run), so the check can never be laxer than the real font.
    """
    global _FONT_OK, _FONT_KEY
    key = tuple((p, os.path.getmtime(p), os.path.getsize(p))
                for p in _target_fonts() if os.path.exists(p))
    if _FONT_OK is None or key != _FONT_KEY:
        from hanpatch.platforms.threeds.bcfnt import Bcfnt
        paths = [p for p in _target_fonts() if os.path.exists(p)]
        if paths and len(paths) != len(_target_fonts()) and os.environ.get('MTL_BOOTSTRAP') != '1':
            raise SystemExit(
                f'glyph authority incomplete: found {paths}, need {list(_target_fonts())}. '
                'Run font_build.py, or set MTL_BOOTSTRAP=1 for the first pass.')
        if len(paths) == len(_target_fonts()):
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
        else:
            ok = set(chr(c) for c in range(0x20, 0x7F))
            for cp in range(0xAC00, 0xD7A4):
                try:
                    b = chr(cp).encode('euc-kr')
                except UnicodeEncodeError:
                    continue
                if len(b) == 2 and 0xB0 <= b[0] <= 0xC8 and 0xA1 <= b[1] <= 0xFE:
                    ok.add(chr(cp))
            ok |= set('　·…‘’“”―－∼※①②③④⑤⑥⑦⑧⑨。、？！「」『』〈〉《》【】')
            ok |= set(chr(c) for c in range(0x3131, 0x3164))
            ok |= set(chr(c) for c in range(0xFF01, 0xFF5F))
            ok |= set('—∈∋⊆⊇▼▽〒゜Éé')
        ok.add('\n')
        _FONT_OK = ok
        _FONT_KEY = key
    return ch in _FONT_OK


def build_prompt(items, gl_subset, kind, context):
    parts = []
    parts.append(f'[문맥/문체 지침] {STYLE.get(kind, STYLE["default"])}')
    if gl_subset:
        parts.append('[GLOSSARY] 아래 표기를 반드시 그대로 사용:\n' +
                     '\n'.join(f'- {en} => {ko}' for en, ko in gl_subset.items()))
    if context:
        parts.append('[직전 번역 예시 - 문체/호칭 일관성 참고용, 번역 대상 아님]\n' +
                     '\n'.join(f'EN: {e}\nKO: {k}' for e, k in context))
    payload = {str(i): it['en'] for i, it in enumerate(items)}
    parts.append('[번역 대상] 아래 JSON의 각 값을 한국어로 번역해 같은 키로 반환:\n' +
                 json.dumps(payload, ensure_ascii=False, indent=1))
    return '\n\n'.join(parts)


def parse_json(text):
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
        text = re.sub(r'\n?```$', '', text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{.*\}', text, re.S)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None
    return None


class Translator:
    def __init__(self, pool=None, kind='default', verbose=True):
        self.pool = pool or providers.build_pool()
        self.gl = glossary.load()
        self.kind = kind
        self.verbose = verbose
        self.pi = 0
        self.stats = {'ok': 0, 'failed': 0, 'calls': 0, 'retries': 0}
        self.last_provider = {}

    def _next_provider(self, attempt=0):
        # stay on the primary model for the first two tries so the register
        # never drifts between batches; only then rotate to repair fallbacks
        if attempt < 2:
            return self.pool[0]
        idx = 1 + (self.pi % max(1, len(self.pool) - 1))
        self.pi += 1
        return self.pool[idx]

    def batch(self, items, context=(), attempts=None):
        """items: list of {'en': str}. Returns (results dict idx->ko, failures list)."""
        attempts = attempts or max(3, len(self.pool))
        gl_subset = glossary.relevant(self.gl, [it['en'] for it in items], self.kind)
        pending = list(range(len(items)))
        results = {}
        feedback = ''
        for attempt in range(attempts):
            if not pending:
                break
            sub = [items[i] for i in pending]
            prov = self._next_provider(attempt)
            prompt = build_prompt(sub, gl_subset, self.kind, context)
            qa = [f"id {i}: {s['qa']}" for i, s in enumerate(sub) if s.get('qa')]
            if qa:
                prompt += ('\n\n[검수자 지적 - 이 문제를 반드시 고쳐서 번역]\n' +
                           '\n'.join(qa))
            if feedback:
                prompt += ('\n\n[이전 시도의 오류 - 반드시 수정해서 다시 번역]\n' + feedback)
            try:
                self.stats['calls'] += 1
                raw = prov.chat(SYSTEM_PROMPT, prompt,
                                max_tokens=min(16000, 900 + 6 * sum(len(s['en']) for s in sub)))
            except RuntimeError as e:
                if self.verbose:
                    print(f'    ! {e}'[:200], flush=True)
                feedback = ''
                continue
            obj = parse_json(raw)
            if not isinstance(obj, dict):
                if self.verbose:
                    print(f'    ! {prov.id} returned non-JSON', flush=True)
                continue
            fails = []
            still = []
            for local, gi in enumerate(pending):
                ko = obj.get(str(local))
                if isinstance(ko, str):
                    ko = ko.replace('\r\n', '\n')
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

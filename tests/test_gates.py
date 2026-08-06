"""Adversarial regression tests for the validation gates.

Run:  python3 tests/test_gates.py
      HANPATCH_PROJECT=/path/to/project python3 tests/test_gates.py

Every case is a concrete attack that once slipped through.  Cases that only
exercise validation logic run anywhere; cases that audit a real corpus are
skipped with a printed notice when no project with translated data is given.
"""
import glob
import json
import os
import subprocess
import sys
import shutil
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hanpatch import config  # noqa: E402

# Without a real project, run against a throwaway one using the bundled
# reference profile, so the logic cases still have a markup grammar to check.
if not os.environ.get('HANPATCH_PROJECT'):
    _tmp = tempfile.mkdtemp(prefix='hanpatch-test-')
    json.dump({'title': 'Crimson Shroud', 'platform': 'threeds',
               'adapter': 'crimson_shroud', 'target': 'ko',
               'profile': 'profiles/crimson_shroud.json'},
              open(os.path.join(_tmp, config.PROJECT_FILE), 'w'), indent=1)
    os.environ['HANPATCH_PROJECT'] = _tmp

from hanpatch import capacity as capmod  # noqa: E402
from hanpatch import glossary  # noqa: E402
from hanpatch import josa  # noqa: E402
from hanpatch import manifest as manmod  # noqa: E402
from hanpatch import qa as qamod  # noqa: E402
from hanpatch import qagate  # noqa: E402
from hanpatch import qarelabel  # noqa: E402
from hanpatch import tm  # noqa: E402
from hanpatch import wrap as _wrap  # noqa: E402
from hanpatch import audit as _ad_audit  # noqa: E402
from hanpatch import translate as tr  # noqa: E402
from hanpatch import wrap  # noqa: E402

HAVE_CORPUS = (os.path.exists(config.src_path())
               and os.path.exists(config.out('manifest.json'))
               and os.path.exists(config.out('qa.json')))
# line measurement needs a real font; without one the layout cases cannot run
HAVE_FONT = any(os.path.exists(config.p(x))
                for x in (list(config.prof('font_out'))
                          + list(config.prof('font_src'))))

# The glyph gate's authority is the BUILT target font, and it now fails closed
# instead of guessing coverage from euc-kr. A logic-only run has no built font,
# so the coverage check is replaced by a declared test double and reported as
# NOT EXERCISED further down — never as a pass.
HAVE_TARGET_FONT = (bool(config.prof('font_out'))
                    and all(os.path.exists(config.p(x))
                            for x in config.prof('font_out')))
_real_in_font = tr._in_font
if not HAVE_TARGET_FONT:
    tr._in_font = lambda ch: True

GL = glossary.load() if os.path.exists(glossary.GLOSSARY_PATH()) else {}
PASS, FAIL, SKIP = [], [], []


def skip(name):
    SKIP.append(name)
    print('  skip ' + name)


def case(name, ok):
    (PASS if ok else FAIL).append(name)
    print(('  ok   ' if ok else '  FAIL ') + name)


def rejects(en, ko, kind='dialogue', group=None, needle=None):
    _, probs = tr.check(en, ko, glossary.relevant(GL, [en], kind), kind, group)
    if needle:
        return any(needle in p for p in probs)
    return bool(probs)


def accepts(en, ko, kind='dialogue', group=None):
    _, probs = tr.check(en, ko, glossary.relevant(GL, [en], kind), kind, group)
    if probs:
        print(f'        unexpected: {probs}')
    return not probs


print('== language / copy gates ==')
case('fullwidth English copied verbatim is rejected',
     rejects('Ｎｏ ｐｏｉｎｔ ｌｉｎｇｅｒｉｎｇ ｈｅｒｅ ａｎｙ ｌｏｎｇｅｒ．',
             'Ｎｏ ｐｏｉｎｔ ｌｉｎｇｅｒｉｎｇ ｈｅｒｅ ａｎｙ ｌｏｎｇｅｒ．'))
case('ASCII English copied verbatim is rejected',
     rejects('The danger is past and the dead rest.',
             '위험은 지나갔다. the dead rest 라고 생각했다.'))
case('copy fragmented by punctuation is still rejected',
     rejects('Humility may not come easy to a mage, Frea, but stay back.',
             '겸손은: may not; come easy: to a mage 뒤로 물러나.',
             needle='copied'))
case('copy containing digits is rejected',
     rejects('Go over 255 and I will not be able to pick up any more.',
             '이건 go over 255 and 라서 안 된다.', needle='copied'))
case('isolated fullwidth Latin letters are rejected',
     rejects('Ｉｓｎ゜ｔ ｔｈａｔ ｗｈｙ？', '그러니까 ｙ원로 ｏ이 아니야？',
             needle='fullwidth'))
case('a single surviving fullwidth letter is rejected',
     rejects('Ｂｅｓｔ ｎｏｔ， you think.', 'Ｂ안 되겠지, 라고 생각한다.',
             needle='fullwidth'))
case('fullwidth punctuation from the source is kept',
     accepts('。Ｆｉｅｎｄｓ！。 he signals.', '。적이다！。 그가 신호를 보낸다.'))
case('a single latin letter that is meaningful in the source is kept',
     accepts('X rarely marks the spot.', 'X가 진짜 자리를 가리키는 일은 드물다.'))
case('lowercase English leftover is rejected',
     rejects('The knight raised his blade.', '기사가 his blade 를 들었다.'))
case('acronyms and source-capitalised titles are allowed',
     accepts('Restores 50 MP to a SINGLE TARGET.', 'MP를 50 회복한다. 단일 대상.',
             kind='item'))
case('clean Korean is accepted',
     accepts('The danger is past.', '위험은 지나갔다.'))

print('== markup gates ==')
case('missing tag rejected',
     rejects('<color=red>피해<color=default>', '<color=red>피해'))
case('reordered control tags rejected',
     rejects('<color=otherline2>A<color=default>B',
             '<color=default>가<color=otherline2>나', needle='order'))
case('text moved out of a control span rejected',
     rejects('<color=red>A<color=default>B', '<color=red><color=default>가나',
             needle='boundary'))
case('runtime placeholders may be reordered',
     accepts('<player> swings <item> at <enemy>.',
             '<player>가 <enemy>에게 <item>을 내리쳤다.', kind='battle'))

print('== layout gates ==')
if not HAVE_FONT:
    skip('layout gates (no measurement font in this project)')
else:
    long_en = ('You look around but fail to spot anything\nresembling a passage out.'
               '<key> A dead end.<br><page>')
    case('overlong translation rejected for a 2-line-capacity group',
         rejects('Saving game. Do not turn off the system\nor remove the SD Card.',
                 '게임을 저장하는 중입니다. ' * 8,
                 kind='system', group='system/option', needle='box holds'))
    _ST_EN = ('<lineheight=8>Of course, all began with one, single gift.<br><key>\n'
              "<lineheight=0>But you already knew that, didn't you?<br><page>")
    _ST_KO = ('<lineheight=8>물론, 모든 것은 단 하나의 기프트에서 시작되었다.<br><key>'
              '<lineheight=0>하지만 그건 이미 알고 있었겠지?<br><page>')
    _st_new, _st_probs = wrap.fits(_ST_EN, _ST_KO, 'dialogue', 'dialogue/mes_ch#_#')
    case('a line break the script attaches to a control tag is reproduced',
         not _st_probs and _st_new.count('\n') == 1
         and _st_new.split('\n')[1].startswith('<lineheight=0>하지만'))
    case('a tag the profile does not call a break is not one',
         wrap.pages('가<br>나')[0] == 1 and wrap.rewrap('가<br>나', 392) == '가<br>나')
    case('freeform (engine-wrapped) source is not line-limited',
         accepts('The chest is empty.', '상자는 텅 비어 있다. ' * 12, kind='system',
                 group='system/treasure'))
    case('normal length accepted for dialogue capacity',
         accepts(long_en, '주변을 둘러보지만 밖으로 이어질 통로는 보이지 않는다.<key> 막다른 길이다.<br><page>',
                 group='dialogue/mes#_FM'))
    case('engine-wrapped source: stray newline is folded, not rejected',
         accepts('A terrace fronting on a small pond.', '작은 연못을\n마주한 테라스다.',
                 kind='system', group='system/help'))

print('== glossary gates ==')
# these assert against the project's own term table, so they need a built glossary
if GL:
    case('hard proper noun must use the fixed form',
         rejects('Frea calls out to Lippi.', '프로우가 리피를 부른다.',
                 needle='glossary'))
    case('fixed form split by wrapping is still accepted',
         accepts('Touch the Circle Pad.', '슬라이드\n패드를 조작한다.',
                 group='dialogue/mes_tutorial'))
    case('UI status label is not forced into narrative prose',
         'Dead' not in glossary.relevant(GL, ["You're profaning the dead!"],
                                         'dialogue'))
    case('UI status label is enforced in battle text',
         'Dead' in glossary.relevant(GL, ['Dead'], 'battle'))
else:
    for _n in ('hard proper noun must use the fixed form',
               'fixed form split by wrapping is still accepted',
               'UI status label is not forced into narrative prose',
               'UI status label is enforced in battle text'):
        skip(_n)
# scoping logic itself is testable without any project data
_synthetic = {'Frea': {'ko': '프레아', 'hard': True, 'families': []},
              'Dead': {'ko': '전투 불능', 'hard': False,
                       'families': ['battle']}}
case('a UI-scoped term is offered only inside its families',
     'Dead' in glossary.relevant(_synthetic, ['Dead'], 'battle')
     and 'Dead' not in glossary.relevant(_synthetic, ['Dead'], 'dialogue'))

print('== josa repair ==')
case('particle after a fixed proper noun is corrected',
     josa.fix_after('지오크은 검을 들었다', {'지오크'})[0] == '지오크는 검을 들었다')
case('eu-ro after a vowel-final syllable is corrected',
     josa.fix_eu_ro('서고으로 돌아왔다') == '서고로 돌아왔다')

print('== manifest gates ==')
case('the digest changes when any value changes',
     manmod.digest({'a/b': 'x'}) != manmod.digest({'a/b': 'y'}))
case('the digest changes when a key is added',
     manmod.digest({'a/b': 'x'}) != manmod.digest({'a/b': 'x', 'a/c': 'x'}))
case('the digest is order independent',
     manmod.digest({'a/b': 'x', 'a/c': 'y'})
     == manmod.digest({'a/c': 'y', 'a/b': 'x'}))
if HAVE_CORPUS:
    doc = manmod.load()
    case('manifest holds a full corpus', len(doc['entries']) > 3000)
    tampered = dict(doc['entries'])
    first = sorted(tampered)[0]
    tampered[first] = tampered[first] + '!'
    case('tampered manifest is detected',
         manmod.digest(tampered) != doc['digest'])
    case('every shippable source key is in the manifest',
         all(f'{fam}/{it["key"]}' in doc['entries']
             for fam, items in json.load(open(config.src_path())).items()
             for it in items
             if not (tm.is_skip(it['en'], it['key']) or not it['en'].strip())))
    # The load-bearing claim of the language-neutralisation work is that a
    # space-separated source still seals byte-for-byte. Pinning the digest here
    # means a future change cannot alter it quietly: re-baselining becomes an
    # explicit, reviewed edit to this line.
    _BASELINE_DIGEST = ('0915bba6ffecb3678b734f022d917da84e84ea5b41edc511'
                        'fa4b7e54834dbeee')
    if len(doc['entries']) == 3262:
        case('the reference corpus still seals to its recorded digest',
             doc['digest'] == _BASELINE_DIGEST)
    else:
        skip('the recorded digest belongs to the 3262-entry reference corpus')
else:
    for _n in ('manifest holds a full corpus', 'tampered manifest is detected',
               'every shippable source key is in the manifest'):
        skip(_n)

print('== semantic QA gate binding ==')
man = (json.load(open(config.out('manifest.json'))) if HAVE_CORPUS
       else {'entries': {}, 'digest': ''})
qa = json.load(open(config.out('qa.json'))) if HAVE_CORPUS else {}
src_all = json.load(open(config.src_path())) if HAVE_CORPUS else {}
bykey = {f'{f}/{i["key"]}': i for f, items in src_all.items() for i in items}
if HAVE_CORPUS:
    case('every manifest value has a verdict keyed to that exact pair',
         all(qamod.pair_key(bykey[k]['jp']
                            if (tm.is_skip(bykey[k]['en'], bykey[k]['key'])
                                or not bykey[k]['en'].strip())
                            else bykey[k]['en'], v) in qa
             for k, v in man['entries'].items() if k in bykey))
else:
    skip('every manifest value has a verdict keyed to that exact pair')
case('an edited manifest value invalidates its verdict',
     qamod.pair_key('The danger is past.', '위험은 지나갔다.') !=
     qamod.pair_key('The danger is past.', '위험은 지나갔다!'))
case('a defect disposition blocks release',
     qagate.verdict_problem({'a': 4, 'f': 5, 'd': 'defect', 'judge': 'j',
                             'en': 'en', 'ko': 'ko', 'r': 'fishbone 오역'},
                            'en', 'ko', '') is not None)
case('a policy disposition blocks release without a waiver',
     qagate.verdict_problem({'a': 5, 'f': 5, 'd': 'policy', 'judge': 'j',
                             'en': 'en', 'ko': 'ko', 'r': '표기 선호 차이'},
                            'en', 'ko', '') is not None)
case('an unknown judge id is rejected',
     qagate.verdict_problem({'a': 5, 'f': 5, 'd': 'pass', 'judge': 'forged',
                             'en': 'en', 'ko': 'ko', 'r': ''},
                            'en', 'ko', '') is not None)
_qamod = qamod
case('a clean pass verdict from a configured judge is accepted',
     qagate.verdict_problem({'a': 5, 'f': 5, 'd': 'pass',
                             'judge': _qamod.JUDGES[0],
                             'en': 'en', 'ko': 'ko', 'r': ''},
                            'en', 'ko', 'other') is None)
case('the packer revalidates QA in-process, not just the token',
     'qagate.validate(' in open(os.path.join(ROOT, 'hanpatch/pipeline.py')).read())
if HAVE_CORPUS:
    case('a coordinated manifest+token edit is rejected by revalidation',
         bool(qagate.validate({sorted(man['entries'])[0]: '조작된 값'})[0]))
else:
    skip('a coordinated manifest+token edit is rejected by revalidation')
case('the judge reader never synthesises a disposition',
     "d = 'pass' if" not in open(os.path.join(ROOT, 'hanpatch/qa.py')).read())
def _src_of(it):
    en = it['en']
    if tm.is_skip(en, it['key']) or not en.strip():
        return it.get('jp', en)
    return en


_wp = config.out('qa_waivers.json')
_waivers = json.load(open(_wp)) if os.path.exists(_wp) else {}
case('every waiver hash equals pair_key(source, shipped value)',
     all(qamod.pair_key(_src_of(bykey[w['key']]), man['entries'][w['key']]) == pk
         for pk, w in _waivers.items()
         if w['key'] in man['entries'] and w['key'] in bykey))
case('a waiver with a foreign key is rejected',
     qagate.waiver_problem({'key': 'x/y', 'category': 'JP_NAMING',
                            'reason': '충분히 긴 이유 문장'}, 'a/b') is not None)
case('an empty waiver object is rejected',
     qagate.waiver_problem({}, 'a/b') is not None)
case('an unknown waiver category is rejected',
     qagate.waiver_problem({'key': 'a/b', 'category': 'WHATEVER',
                            'reason': '충분히 긴 이유 문장'}, 'a/b') is not None)
case('a forged 5/5 verdict without judge metadata is rejected',
     qagate.verdict_problem({'a': 5, 'f': 5, 'd': 'pass'}, 'en', 'ko', '')
     is not None)
case('a verdict carrying a different pair is rejected',
     qagate.verdict_problem({'a': 5, 'f': 5, 'd': 'pass', 'judge': 'x',
                             'en': 'other', 'ko': 'ko'}, 'en', 'ko', '')
     is not None)
case('a defect disposition blocks even at score 4',
     qagate.verdict_problem({'a': 4, 'f': 4, 'd': 'defect',
                             'judge': _qamod.JUDGES[0],
                             'en': 'en', 'ko': 'ko', 'r': '조사 오류'},
                            'en', 'ko', '') is not None)
case('a producer may not judge its own output',
     qagate.verdict_problem({'a': 5, 'f': 5, 'd': 'pass',
                             'judge': _qamod.JUDGES[0], 'en': 'en',
                             'ko': 'ko'}, 'en', 'ko', _qamod.JUDGES[0])
     is not None)
# The panel must separate "this verdict is not evidence" from "this verdict found a
# defect". Conflating them deadlocked 25152 DQ7 pairs: the judging pass refuses to count
# a verdict from the producing model toward coverage, so it reported nothing left to
# judge, while the gate treated that same record as a permanent block that no repair
# could clear (verdicts are kept forever by design).
_P1 = 'agy:gemini-3-pro'
_P2 = 'agy:claude-sonnet-4.6'
_PROD = 'codex1:gpt-5.6-luna'
_SELF = 'codex2:gpt-5.6-luna'          # a sibling ACCOUNT of the producing model


def _v(judge, d='pass', a=5, f=5):
    return {'a': a, 'f': f, 'd': d, 'judge': judge, 'en': 'en', 'ko': 'ko', 'r': ''}


case('a self-serving pass does not veto an otherwise independent panel',
     qagate.panel_problem([_v(_SELF), _v(_P1), _v(_P2)], 'en', 'ko', _PROD) is None)
case('a self-serving pass is not counted toward the panel',
     qagate.panel_problem([_v(_SELF), _v(_P1)], 'en', 'ko', _PROD) is not None)
case('a pair judged only by its own model never passes',
     qagate.panel_problem([_v(_SELF), _v(_PROD)], 'en', 'ko', _PROD) is not None)
case('a defect from the producing model still blocks - it is against interest',
     qagate.panel_problem([_v(_SELF, d='defect', a=2), _v(_P1), _v(_P2)],
                          'en', 'ko', _PROD) is not None)
case('a corrupt score blocks even when the judge is disqualified',
     qagate.panel_problem([_v(_SELF, a=70), _v(_P1), _v(_P2)],
                          'en', 'ko', _PROD) is not None)
# The agy-c gateway answered every request as Gemini 3.6 Flash under three lane names.
# `qarelabel` renames those verdicts to the model that actually answered, so that name has
# to be a known judge identity - otherwise 39236 relabelled verdicts read as forgeries and
# blocked 13002 shipped pairs - while the lanes themselves must never be callable again.
case('the relabelled true identity is a known judge',
     qarelabel.TRUE_IDENTITY in qamod.JUDGES)
case('the retired identity is never dialled by a panel run',
     qarelabel.TRUE_IDENTITY not in qamod.active_judges())
case('a revoked gateway lane is never dialled by a panel run',
     not [j for j in qamod.active_judges() if j in qamod.REVOKED_GATEWAY_LANES])
case('every revoked lane is still a valid recorded identity',
     all(j in qamod.JUDGES for j in qamod.REVOKED_GATEWAY_LANES))
_pipe = open(os.path.join(ROOT, 'hanpatch/pipeline.py')).read()
case('the packer requires the approved manifest digest',
     'approve(' in _pipe and 'SKIP_GATE' not in _pipe)
import ast as _ast  # noqa: E402
# The gate sequence lives in the inner runner; the public `gates` is the wrapper
# that revokes the approval token when a gate fails, so the order analysis has to
# read the runner and the wrapper has to be proven to delegate to it.
_pipefns = {n.name: n for n in _ast.parse(_pipe).body
            if isinstance(n, _ast.FunctionDef)}
_gatefn = _pipefns.get('_gates') or _pipefns['gates']
_refs = []
for _n in _ast.walk(_gatefn):
    if isinstance(_n, _ast.Attribute) and isinstance(_n.value, _ast.Name):
        _refs.append(((_n.lineno, _n.col_offset), f'{_n.value.id}.{_n.attr}'))
_seq = [r for _, r in sorted(_refs)]
_want = ['glossary.build', 'capacity.build', 'materialize.main', 'audit.main',
         'manifest.build', 'qagate.validate']
case('the gates run in the order glossary->capacity->materialize->audit'
     '->manifest->qagate',
     [c for c in _seq if c in _want] == _want)
case('approval happens only after the QA panel validates',
     _seq.index('qagate.validate') < _seq.index('qagate.approve'))
# The floor is enforced through `note`, so approving before the last `note` call
# would hand a failed run a release-ready token.
case('the qagate floor is enforced before the approval token is written',
     _seq.index('qagate.approve') > max(
         i for i, c in enumerate(_seq) if c == 'qagate.LAST_EXAMINED'))
# The pipeline is the ONLY authority for the approval token. `qagate.main` runs
# the panel in isolation, without the five gates before it, without the input
# floors and without re-deriving the digest, so re-adding an `approve()` call
# there would restore a second, weaker authority for the same artifact with a
# green suite.
_qagate_src = open(os.path.join(ROOT, 'hanpatch/qagate.py')).read()
_qamain = next((n for n in _ast.parse(_qagate_src).body
                if isinstance(n, _ast.FunctionDef) and n.name == 'main'), None)
_main_calls = [] if _qamain is None else [
    getattr(n.func, 'id', getattr(n.func, 'attr', ''))
    for n in _ast.walk(_qamain) if isinstance(n, _ast.Call)]
case('the report-only qa panel never writes an approval token',
     _qamain is not None and 'approve' not in _main_calls
     and 'revoke' in _main_calls)
_wrapper = _pipefns['gates']
_wrapper_calls = [f'{n.value.id}.{n.attr}' for n in _ast.walk(_wrapper)
                  if isinstance(n, _ast.Attribute) and isinstance(n.value, _ast.Name)]
case('a failed gate run revokes the approval token',
     'qagate.revoke' in _wrapper_calls
     and any(isinstance(n, _ast.Call) and getattr(n.func, 'id', '') == '_gates'
             for n in _ast.walk(_wrapper)))
_adapter_srcs = [os.path.join(ROOT, 'hanpatch/adapter.py')] + [
    os.path.join(ROOT, 'hanpatch/adapters', f)
    for f in os.listdir(os.path.join(ROOT, 'hanpatch/adapters'))
    if f.endswith('.py')]
_leak = [(p, m) for p in _adapter_srcs
         for m in ('translate', 'glossary', 'josa', 'providers', 'wrap')
         if f'import {m}' in open(p).read()]
case('the container layer never imports the wording layer', not _leak)

if HAVE_CORPUS:
    print('== corrected-string regressions ==')
    _man = json.load(open(config.out('manifest.json')))['entries']
    case('the alchemical stone is not a tombstone',
         '비석' not in _man['item/item_001_help'])
    case('무얼 하겠는가 is spaced correctly',
         '무얼하겠는가' not in _man['dialogue/mes21_FM_001'])
    case('the ring band is a ring, not a belt',
         '띠를 장식' not in _man['arms_help/arms_238_help'])
    for _k, _bad in [('dialogue/mes14_FM_001', '없다, 라고'),
                     ('dialogue/mes14_FM_002', '정면으로 꽂힌다'),
                     ('dialogue/mes14_FM_004', '공격을 포효한다'),
                     ('dialogue/mes24_FM_001', '보물상자가 둔 곳')]:
        case(f'{_k} no longer contains {_bad!r}', _bad not in _man[_k])


else:
    skip('corrected-string regressions (needs a translated corpus)')

print('== glyph authority ==')
if HAVE_TARGET_FONT:
    case('glyph coverage comes from the built fonts',
         tr._in_font('가') and not tr._in_font('\u4e00'))
else:
    try:
        _real_in_font('가')
        _closed = False
    except SystemExit as e:
        _closed = 'glyph authority' in str(e) or 'target font' in str(e)
    case('the glyph gate fails closed when no target font is built', _closed)
    skip('glyph coverage comes from the built fonts (needs built fonts)')

# ---------------------------------------------------------------- source language
# Every case below proves a heuristic that was silently Latin-only now fires on a
# spaceless source, and is inert for `source_lang == 'en'`. The module-level
# constants of translate/glossary/wrap are built from the profile at import time,
# so a Japanese profile has to be exercised in its own interpreter.
print('== japanese source (M0b) ==')
_JP_PROFILE = {
    'source_lang': 'ja',
    'fullwidth_is_content': False,
    # residual_script_flag is deliberately NOT declared here: the AUTO rule must
    # supply kana+kanji for a Japanese source. A second fixture below declares it
    # explicitly, so removing the AUTO resolution fails this project's cases.
    'kanji_allowlist': [],
    # A non-Latin source must declare its own measured width: the pipeline now
    # refuses to inherit the reference title's 384px, because a budget nobody
    # measured lets the capacity gate pass while text runs off the screen.
    'budget': {'default': 256},
    # Whether the engine lays out a row that carries no break is a measured
    # title fact with no default, for the same reason the width has none: a row
    # assumed engine-laid-out is never measured at all. This fixture declares
    # the reference behaviour so its subject stays the Japanese-source rules.
    'engine_wraps': True,
    'terms': {'勇者': '용사'},
    'hard_terms': ['勇者'],
    'judge_policy': '[정책] 원문 표기를 기준으로 판단한다.',
}
_jp_root = tempfile.mkdtemp(prefix='hanpatch-test-ja-')
json.dump(_JP_PROFILE, open(os.path.join(_jp_root, 'profile.json'), 'w'))
json.dump({'title': 'JP fixture', 'platform': 'threeds', 'adapter': 'crimson_shroud',
           'target': 'ko', 'profile': 'profile.json'},
          open(os.path.join(_jp_root, config.PROJECT_FILE), 'w'))


def jp_probe(body):
    """Run `body` in a fresh interpreter bound to the Japanese fixture project.

    `body` prints one JSON value. The glyph authority is stubbed exactly as it is
    for this harness's own logic cases, because no font is built here.
    """
    code = ('import json,sys\n'
            f'sys.path.insert(0, {ROOT!r})\n'
            'from hanpatch import config, glossary, qa, translate as tr, wrap\n'
            'tr._in_font = lambda ch: True\n' + body)
    env = dict(os.environ, HANPATCH_PROJECT=_jp_root)
    out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                         text=True, env=env)
    if out.returncode != 0:
        print('        probe failed: ' + (out.stderr.strip().splitlines() or [''])[-1])
        return None
    return json.loads(out.stdout.strip().splitlines()[-1])


_jp = jp_probe(
    "gl = {'勇者': '용사'}\n"
    "subset = glossary.relevant(gl, ['むかし勇者がいた。'])\n"
    "_, omitted = tr.check('むかし勇者がいた。', '옛날에 영웅이 있었다.', subset)\n"
    "_, kept = tr.check('むかし勇者がいた。', '옛날에 용사가 있었다.', subset)\n"
    "_, raw = tr.check('こんにちは世界', 'こんにちは世界', {})\n"
    "_, tagonly = tr.check('<player>', '<player>', {})\n"
    "_, digits = tr.check('255', '255', {})\n"
    # The production path builds the prompt subset with relevant() first, so a
    # term the prefilter cannot see never reaches enforcement at all. Both halves
    # must therefore look through markup and row breaks for a spaceless source.
    "split_tag = 'むかし勇<color=red>者<color=default>がいた。'\n"
    "split_nl = 'むかし勇\\n者がいた。'\n"
    "sub_tag = glossary.relevant(gl, [split_tag])\n"
    "sub_nl = glossary.relevant(gl, [split_nl])\n"
    # Layout measurement needs a built font this fixture has no reason to carry,
    # and a wrapped row would fail on measurement before enforcement is reached.
    # These two cases are about term matching only, so measurement is stubbed for
    # exactly their duration; the real layout gates run in corpus mode.
    "_fits = wrap.fits\n"
    "wrap.fits = lambda en, ko, kind='default', group=None: (ko, [])\n"
    "_, ptag = tr.check(split_tag, '옛날에 영웅이<color=red>가<color=default> 있었다.', sub_tag)\n"
    "_, pnl = tr.check(split_nl, '옛날에 영웅이\\n있었다.', sub_nl)\n"
    "wrap.fits = _fits\n"
    "print(json.dumps({'subset': subset, 'hard': '勇者' in glossary.hard(),\n"
    "                  'omitted': omitted, 'kept': kept, 'raw': raw,\n"
    "                  'tagonly': tagonly, 'digits': digits,\n"
    "                  'sub_tag': sub_tag, 'sub_nl': sub_nl,\n"
    "                  'ptag': ptag, 'pnl': pnl,\n"
    "                  'residual': config.prof('residual_script_flag'),\n"
    "                  'prompt': tr.SYSTEM_PROMPT, 'judge': qa.system_prompt()}))")

# A probe that cannot run is a FAILURE, not a skip: every Japanese-source
# assertion lives inside it, so treating a crashed fixture as "skipped" would let
# the whole M0b surface disappear while the suite still exits 0.
case('the japanese-source fixture runs at all', _jp is not None)
if _jp is not None:
    case('a spaceless Japanese term is found by the glossary subset',
         _jp['subset'] == {'勇者': '용사'})
    case('a declared Japanese hard term is promoted without orthography',
         _jp['hard'] is True)
    case('a Japanese hard term omitted from the translation is flagged',
         any('용사' in p for p in _jp['omitted']))
    # Discriminating form: the correct rendering must produce NO problem at all,
    # so the case also fails if enforcement starts rejecting valid rows.
    case('the same row using the fixed form is accepted', _jp['kept'] == [])
    # A spaceless source hides a term behind markup and row breaks unless BOTH
    # the relevance prefilter and enforcement look through them.
    case('a Japanese term split by markup still reaches the prompt subset',
         _jp['sub_tag'] == {'勇者': '용사'})
    case('a Japanese term split by a row break still reaches the prompt subset',
         _jp['sub_nl'] == {'勇者': '용사'})
    case('a Japanese term split by markup is still enforced',
         any('용사' in p for p in _jp['ptag']))
    case('a Japanese term split by a row break is still enforced',
         any('용사' in p for p in _jp['pnl']))
    case('an untranslated Japanese row is flagged, not passed',
         any('no hangul' in p for p in _jp['raw'])
         and any('kana' in p for p in _jp['raw'])
         and any('kanji' in p for p in _jp['raw']))
    # The guard must demand Hangul only where the source carries Japanese: a
    # placeholder-only or numeric row has nothing to translate.
    case('a tag-only row is not demanded to produce hangul',
         not any('no hangul' in p for p in _jp['tagonly']))
    case('a digits-only row is not demanded to produce hangul',
         not any('no hangul' in p for p in _jp['digits']))
    case('a japanese profile gets residual detection without declaring it',
         _jp['residual'] == 'kana+kanji')
    case('the translator prompt declares the Japanese source',
         '일본어→' in _jp['prompt'])
    case('the translator rule 5 forbids leaving Japanese, not English',
         '일본어(가나·한자)를 그대로 남기지 않는다' in _jp['prompt']
         and '영어 단어를 그대로 남기지 않는다' not in _jp['prompt'])
    case('the fullwidth-Latin authoring rule is dropped when not declared',
         '전각 영문' not in _jp['prompt'])
    case('the judge prompt declares the Japanese source and the title policy',
         '일본어' in _jp['judge'] and '[정책]' in _jp['judge'])
    # The core judge template must not carry another title's naming policy, its
    # battle-log conventions or an English-edition precedence rule.
    case('the core judge prompt carries no foreign title policy',
         not any(n in _jp['judge']
                 for n in ('큐어 리프', '전투 로그', 'jp_original', '영어판'))
         and 'adequacy' in _jp['judge'])

# Whole-term matching is one matcher with two contracts: OFFERING a term to a
# prompt folds case, ENFORCING a fixed rendering does not. Collapsing them
# breaks the reference build, so the asymmetry is pinned here.
case('offering a term folds case but enforcing it does not',
     glossary.matches_term('paling', 'Behind the Paling.', fold_case=True)
     and not glossary.matches_term('paling', 'Behind the Paling.'))

print('== source language declaration (M0b) ==')
_unknown = subprocess.run(
    [sys.executable, '-c',
     'import json,sys,os,tempfile\n'
     f'sys.path.insert(0, {ROOT!r})\n'
     'root = tempfile.mkdtemp()\n'
     "json.dump({'source_lang': 'kl'}, open(os.path.join(root, 'profile.json'), 'w'))\n"
     "json.dump({'title': 'x', 'platform': 'threeds', 'adapter': 'crimson_shroud',\n"
     "           'target': 'ko', 'profile': 'profile.json'},\n"
     "          open(os.path.join(root, 'hanpatch.json'), 'w'))\n"
     "os.environ['HANPATCH_PROJECT'] = root\n"
     'from hanpatch import config\n'
     "print('ACCEPTED ' + repr(config.prof('source_lang')))\n"],
    capture_output=True, text=True)
# The pipeline refuses an unimplemented language by exiting, so the evidence is
# the non-zero status plus the offending value named in the diagnostic.
_refused = (_unknown.stdout + _unknown.stderr).strip()
case('an undeclared source language is refused, not guessed',
     _unknown.returncode != 0 and 'ACCEPTED' not in _unknown.stdout
     and 'kl' in _refused and 'en, ja' in _refused)

# `config.set_root` promises to repoint the WHOLE pipeline. Modules cache
# profile facts at import time, so a switch that leaves those caches behind keeps
# translating under the previous title while the accessor already reports the new
# one. Run in its own interpreter so the switch cannot contaminate this suite.
_reload = subprocess.run(
    [sys.executable, '-c',
     'import json,os,sys,tempfile\n'
     f'sys.path.insert(0, {ROOT!r})\n'
     'def mk(prof, title):\n'
     '    d = tempfile.mkdtemp()\n'
     "    prof = dict({'budget': {'default': 320}}, **prof)\n"
     "    json.dump(prof, open(os.path.join(d, 'profile.json'), 'w'))\n"
     "    json.dump({'title': title, 'platform': 'threeds',\n"
     "               'adapter': 'crimson_shroud', 'target': 'ko',\n"
     "               'profile': 'profile.json'},\n"
     "              open(os.path.join(d, 'hanpatch.json'), 'w'))\n"
     '    return d\n'
     "en = mk({'source_lang': 'en', 'terms': {'Paling': '결계'},\n"
     "         'hard_terms': ['Paling']}, 'EN')\n"
     "ja = mk({'source_lang': 'ja', 'terms': {'勇者': '용사'},\n"
     "         'budget': {'default': 256},\n"
     "         'hard_terms': ['勇者']}, 'JA')\n"
     "os.environ['HANPATCH_PROJECT'] = en\n"
     'from hanpatch import config, glossary, translate as tr\n'
     "assert '영어→' in tr.SYSTEM_PROMPT and 'Paling' in glossary.hard()\n"
     'config.set_root(ja)\n'
     "assert config.source_lang() == 'ja'\n"
     "assert '일본어→' in tr.SYSTEM_PROMPT, 'stale translator prompt'\n"
     "assert '勇者' in glossary.hard(), 'stale hard terms'\n"
     'config.set_root(en)\n'
     "assert '영어→' in tr.SYSTEM_PROMPT and 'Paling' in glossary.hard()\n"
     "print('RELOADED')\n"],
    capture_output=True, text=True)
case('switching project repoints the cached profile-derived state',
     _reload.returncode == 0 and 'RELOADED' in _reload.stdout)
if _reload.returncode != 0:
    print('        ' + (_reload.stderr.strip().splitlines() or [''])[-1])

# A switch that cannot resolve its new profile must not half-apply: the pipeline
# has to stay on the project it was already serving, caches included.
_atomic = subprocess.run(
    [sys.executable, '-c',
     'import json,os,sys,tempfile\n'
     f'sys.path.insert(0, {ROOT!r})\n'
     'def mk(prof, title):\n'
     '    d = tempfile.mkdtemp()\n'
     "    prof = dict({'budget': {'default': 320}}, **prof)\n"
     "    json.dump(prof, open(os.path.join(d, 'profile.json'), 'w'))\n"
     "    json.dump({'title': title, 'platform': 'threeds',\n"
     "               'adapter': 'crimson_shroud', 'target': 'ko',\n"
     "               'profile': 'profile.json'},\n"
     "              open(os.path.join(d, 'hanpatch.json'), 'w'))\n"
     '    return d\n'
     "good = mk({'source_lang': 'en', 'terms': {'Paling': '결계'},\n"
     "           'hard_terms': ['Paling']}, 'EN')\n"
     "bad = mk({'source_lang': 'kl'}, 'BROKEN')\n"
     "os.environ['HANPATCH_PROJECT'] = good\n"
     'from hanpatch import config, glossary, translate as tr\n'
     'before = config.root()\n'
     'raised = False\n'
     'try:\n'
     '    config.set_root(bad)\n'
     'except BaseException:\n'
     '    raised = True\n'
     "assert raised, 'a broken profile was accepted'\n"
     "assert config.root() == before, 'root moved to the broken project'\n"
     "assert config.source_lang() == 'en'\n"
     "assert '영어→' in tr.SYSTEM_PROMPT, 'prompt cache half-applied'\n"
     "assert 'Paling' in glossary.hard(), 'term cache half-applied'\n"
     "print('ATOMIC')\n"],
    capture_output=True, text=True)
case('a failed project switch leaves the previous project intact',
     _atomic.returncode == 0 and 'ATOMIC' in _atomic.stdout)
if _atomic.returncode != 0:
    print('        ' + (_atomic.stderr.strip().splitlines() or [''])[-1])

# Every rejection mode a switch can hit must be rejected AT switch time, not at
# first use, and must leave the previous project serving.
_MODES = {
    'a missing project file': "os.path.join(tempfile.mkdtemp(), 'nowhere')",
    'malformed project json': "mkraw('hanpatch.json', '{not json')",
    'a missing profile file': "mkraw('profile.json', None)",
    'malformed profile json': "mkbadprofile('{not json')",
    'a profile that is not an object': "mkbadprofile('[]')",
    'an unsupported residual mode':
        "mk({'source_lang': 'ja', 'residual_script_flag': 'kana+kanj'}, 'BAD')",
    'an uncompilable tag pattern': "mk({'tag_pattern': '<(unclosed'}, 'BAD')",
    'a budget of the wrong shape': "mk({'budget': []}, 'BAD')",
    'a non-positive budget width': "mk({'budget': {'default': 0}}, 'BAD')",
    # A missing measured width is deliberately NOT a profile-resolution error:
    # `info`, `keys` and `release inspect` must work before anything is measured.
    # The refusal is proven at the point that consumes a width instead, below.
}
for _name, _expr in _MODES.items():
    _probe = subprocess.run(
        [sys.executable, '-c',
         'import json,os,sys,tempfile\n'
         f'sys.path.insert(0, {ROOT!r})\n'
         'def mk(prof, title):\n'
         '    d = tempfile.mkdtemp()\n'
         "    prof = dict({'budget': {'default': 320}}, **prof)\n"
         "    json.dump(prof, open(os.path.join(d, 'profile.json'), 'w'))\n"
         "    json.dump({'title': title, 'platform': 'threeds',\n"
         "               'adapter': 'crimson_shroud', 'target': 'ko',\n"
         "               'profile': 'profile.json'},\n"
         "              open(os.path.join(d, 'hanpatch.json'), 'w'))\n"
         '    return d\n'
         'def mkbadprofile(body):\n'
         '    d = tempfile.mkdtemp()\n'
         "    open(os.path.join(d, 'profile.json'), 'w').write(body)\n"
         "    json.dump({'title': 'x', 'platform': 'threeds',\n"
         "               'adapter': 'crimson_shroud', 'target': 'ko',\n"
         "               'profile': 'profile.json'},\n"
         "              open(os.path.join(d, 'hanpatch.json'), 'w'))\n"
         '    return d\n'
         'def mkraw(which, body):\n'
         '    d = tempfile.mkdtemp()\n'
         "    if which == 'hanpatch.json':\n"
         "        open(os.path.join(d, 'hanpatch.json'), 'w').write(body)\n"
         '    else:\n'
         "        json.dump({'title': 'x', 'platform': 'threeds',\n"
         "                   'adapter': 'crimson_shroud', 'target': 'ko',\n"
         "                   'profile': 'absent-profile.json'},\n"
         "                  open(os.path.join(d, 'hanpatch.json'), 'w'))\n"
         '    return d\n'
         "good = mk({'source_lang': 'en', 'terms': {'Paling': '결계'},\n"
         "           'hard_terms': ['Paling']}, 'EN')\n"
         "os.environ['HANPATCH_PROJECT'] = good\n"
         'from hanpatch import config, glossary, translate as tr\n'
         'before = config.root()\n'
         f'target = {_expr}\n'
         'raised = False\n'
         'try:\n'
         '    config.set_root(target)\n'
         'except BaseException:\n'
         '    raised = True\n'
         "assert raised, 'the switch was accepted'\n"
         "assert config.root() == before, 'root moved'\n"
         "assert config.source_lang() == 'en'\n"
         "assert '영어→' in tr.SYSTEM_PROMPT, 'prompt cache half-applied'\n"
         "assert 'Paling' in glossary.hard(), 'term cache half-applied'\n"
         "print('REJECTED')\n"],
        capture_output=True, text=True)
    case(f'a switch with {_name} is rejected at switch time',
         _probe.returncode == 0 and 'REJECTED' in _probe.stdout)
    if _probe.returncode != 0:
        print('        ' + (_probe.stderr.strip().splitlines() or [''])[-1])

# The rejections above all run through `set_root`, where an already-imported
# layout module would also catch a bad tag pattern during its reset. This case
# pins the guard to profile RESOLUTION instead: a cold interpreter that only ever
# reads the profile must still refuse it.
_cold = subprocess.run(
    [sys.executable, '-c',
     'import json,os,sys,tempfile\n'
     f'sys.path.insert(0, {ROOT!r})\n'
     'd = tempfile.mkdtemp()\n'
     "json.dump({'tag_pattern': '<(unclosed'},\n"
     "          open(os.path.join(d, 'profile.json'), 'w'))\n"
     "json.dump({'title': 'x', 'platform': 'threeds',\n"
     "           'adapter': 'crimson_shroud', 'target': 'ko',\n"
     "           'profile': 'profile.json'},\n"
     "          open(os.path.join(d, 'hanpatch.json'), 'w'))\n"
     "os.environ['HANPATCH_PROJECT'] = d\n"
     'from hanpatch import config\n'
     'try:\n'
     '    config.profile()\n'
     "    print('ACCEPTED')\n"
     'except BaseException as e:\n'
     "    print('REFUSED ' + str(e))\n"],
    capture_output=True, text=True)
case('an invalid profile is refused when it is first resolved, not at first use',
     'REFUSED' in _cold.stdout and 'tag_pattern' in _cold.stdout)

print('== gate input floors (M0b) ==')
# Exercised against the REAL gate runner with the six emitters stubbed, so the
# case proves the runner's floor contract rather than any gate's internals. The
# fixture declares no name tables, so the glossary gate genuinely examines zero
# rows: exactly the "an empty gate must not read clean" hole the floor closes.
os.makedirs(os.path.join(_jp_root, 'work'), exist_ok=True)
json.dump({'dialogue': [{'key': 'k1', 'en': 'こんにちは', 'jp': ''}]},
          open(os.path.join(_jp_root, 'work', 'text_src.json'), 'w'))
_floor = jp_probe(
    "from hanpatch import audit, capacity, manifest, materialize, pipeline, qagate\n"
    "glossary.build = lambda *a, **k: {}\n"
    "capacity.build = lambda *a, **k: {}\n"
    "materialize.main = lambda *a, **k: 0\n"
    "audit.main = lambda *a, **k: 0\n"
    "manifest.build = lambda *a, **k: {'digest': 'd' * 64, 'entries': {}}\n"
    "qagate.validate = lambda entries: ([], [], [])\n"
    "qagate.approve = lambda digest, n: None\n"
    "res = {}\n"
    "for thresholds, label in (({}, 'absent'), ({'glossary': 5}, 'glossary'),\n"
    "                          ({'audit': 5}, 'audit')):\n"
    "    config._profile = dict(config._profile, gate_thresholds=thresholds)\n"
    "    try:\n"
    "        report = pipeline.gates(quiet=True)\n"
    "        res[label] = {'ok': True, 'inputs': report['inputs']}\n"
    "    except pipeline.GateFailed as e:\n"
    "        res[label] = {'ok': False, 'error': str(e)}\n"
    "    except BaseException as e:\n"
    "        res[label] = {'ok': False, 'error': type(e).__name__ + ': ' + str(e)[:80]}\n"
    "print(json.dumps(res, ensure_ascii=False))")
if _floor is None:
    case('the gate-floor fixture runs at all', False)
else:
    case('an absent gate threshold applies no floor', _floor['absent']['ok'])
    # Every gate must report a count, not just the one under test: a missing
    # counter would otherwise hide behind an unfloored gate forever.
    case('all six gates report an examined-input count',
         _floor['absent']['ok']
         and sorted(_floor['absent']['inputs']) == sorted(
             ['audit', 'capacity', 'glossary', 'manifest', 'materialize',
              'qagate']))
    for _g in ('glossary', 'audit'):
        _r = _floor[_g]
        case(f'a declared floor above the observed count fails {_g} closed',
             not _r['ok'] and _g in _r['error']
             and 'required minimum' in _r['error'])

# A failed floor must not leave a release-ready approval standing.
# `release.create` authorises on the token plus a digest match and never re-runs
# the gates, so a token surviving a failed run would make that run distributable.
_token = jp_probe(
    "import os\n"
    "from hanpatch import audit, capacity, manifest, materialize, pipeline, qagate\n"
    "glossary.build = lambda *a, **k: {}\n"
    "capacity.build = lambda *a, **k: {}\n"
    "materialize.main = lambda *a, **k: 0\n"
    "audit.main = lambda *a, **k: 0\n"
    "manifest.build = lambda *a, **k: {'digest': 'd' * 64, 'entries': {'a/b': 'c'}}\n"
    "qagate.validate = lambda entries: ([], [], [])\n"
    "config._profile = dict(config._profile, gate_thresholds={})\n"
    "pipeline.gates(quiet=True)\n"
    "had_token = os.path.exists(qagate.APPROVAL())\n"
    "config._profile = dict(config._profile, gate_thresholds={'qagate': 99})\n"
    "failed = False\n"
    "try:\n"
    "    pipeline.gates(quiet=True)\n"
    "except pipeline.GateFailed:\n"
    "    failed = True\n"
    "print(json.dumps({'had_token': had_token, 'failed': failed,\n"
    "                  'token_after': os.path.exists(qagate.APPROVAL())}))")
if _token is None:
    case('the approval-token fixture runs at all', False)
else:
    case('a passing gate run writes the approval token', _token['had_token'])
    case('a run that fails its floor leaves no approval token',
         _token['failed'] and not _token['token_after'])

# Revocation must cover EVERY failure path, not just a gate verdict: an unsealed
# manifest and a failing adapter injection both used to leave the previous run's
# approval standing, and `release.create` authorises on that token alone.
_revoke = jp_probe(
    "import os\n"
    "from hanpatch import audit, capacity, manifest, materialize, pipeline, qagate\n"
    "import hanpatch.adapter as adapter_mod\n"
    "glossary.build = lambda *a, **k: {}\n"
    "capacity.build = lambda *a, **k: {}\n"
    "materialize.main = lambda *a, **k: 0\n"
    "audit.main = lambda *a, **k: 0\n"
    "sealed = {'digest': 'd' * 64, 'entries': {'a/b': 'c'}}\n"
    "manifest.build = lambda *a, **k: sealed\n"
    "manifest.load = lambda *a, **k: sealed\n"
    "qagate.validate = lambda entries: ([], [], [])\n"
    "config._profile = dict(config._profile, gate_thresholds={})\n"
    "res = {}\n"
    "def token():\n"
    "    return os.path.exists(qagate.APPROVAL())\n"
    "pipeline.gates(quiet=True)\n"
    "res['token_after_pass'] = token()\n"
    "manifest.build = lambda *a, **k: None\n"
    "try:\n"
    "    pipeline.gates(quiet=True)\n"
    "    res['unsealed'] = 'accepted'\n"
    "except pipeline.GateFailed as e:\n"
    "    res['unsealed'] = 'gate-failed' if 'not sealed' in str(e) else str(e)[:40]\n"
    "except BaseException as e:\n"
    "    res['unsealed'] = 'unexpected ' + type(e).__name__\n"
    "res['token_after_unsealed'] = token()\n"
    "manifest.build = lambda *a, **k: sealed\n"
    "pipeline.gates(quiet=True)\n"
    "res['token_before_inject'] = token()\n"
    "class Boom:\n"
    "    def inject(self, entries, rom, out):\n"
    "        raise OSError('disk full')\n"
    "adapter_mod.project_adapter = lambda *a, **k: Boom()\n"
    "try:\n"
    "    pipeline.build(rom='r', out='o', quiet=True)\n"
    "    res['inject_raised'] = False\n"
    "except OSError:\n"
    "    res['inject_raised'] = True\n"
    "res['token_after_inject_failure'] = token()\n"
    "print(json.dumps(res))")
if _revoke is None:
    case('the revocation fixture runs at all', False)
else:
    case('an unsealed manifest is a gate failure, not a bare crash',
         _revoke['unsealed'] == 'gate-failed')
    case('an unsealed manifest leaves no approval token',
         _revoke['token_after_pass'] and not _revoke['token_after_unsealed'])
    case('a failing adapter injection leaves no approval token',
         _revoke['token_before_inject'] and _revoke['inject_raised']
         and not _revoke['token_after_inject_failure'])

# A threshold the runner cannot honour must fail before any gate runs, and must
# not leave an approval standing either.
_badthresh = jp_probe(
    "import os\n"
    "from hanpatch import audit, capacity, manifest, materialize, pipeline, qagate\n"
    "glossary.build = lambda *a, **k: {}\n"
    "capacity.build = lambda *a, **k: {}\n"
    "materialize.main = lambda *a, **k: 0\n"
    "audit.main = lambda *a, **k: 0\n"
    "manifest.build = lambda *a, **k: {'digest': 'd' * 64, 'entries': {'a/b': 'c'}}\n"
    "qagate.validate = lambda entries: ([], [], [])\n"
    "res = {}\n"
    "for label, value in (('unknown', {'imaginary': 1}), ('zero', {'glossary': 0}),\n"
    "                     ('negative', {'glossary': -1}),\n"
    "                     ('boolean', {'glossary': True}),\n"
    "                     ('nonmapping', [])):\n"
    "    config._profile = dict(config._profile, gate_thresholds={})\n"
    "    pipeline.gates(quiet=True)\n"
    "    config._profile = dict(config._profile, gate_thresholds=value)\n"
    "    try:\n"
    "        pipeline.gates(quiet=True)\n"
    "        res[label] = 'accepted'\n"
    # Either refusal point is correct: the gate runner rejects a threshold it
    # cannot honour, and profile validation rejects a wrongly shaped value even
    # earlier. What must hold in both cases is that no approval survives.
    "    except (pipeline.GateFailed, SystemExit):\n"
    "        res[label] = ('refused' if not os.path.exists(qagate.APPROVAL())\n"
    "                      else 'refused-but-token-left')\n"
    "    except BaseException as e:\n"
    "        res[label] = type(e).__name__\n"
    "print(json.dumps(res))")
if _badthresh is None:
    case('the invalid-threshold fixture runs at all', False)
else:
    for _label in ('unknown', 'zero', 'negative', 'boolean', 'nonmapping'):
        case(f'a {_label} gate threshold is refused and revokes the token',
             _badthresh[_label] == 'refused')

# `hanpatch all` chains build into verify, and `release.create` authorises on the
# token plus a digest match without re-running anything, so a ROM that failed its
# own round trip must not leave an approval standing.
_verify = jp_probe(
    "import os\n"
    "from hanpatch import audit, capacity, manifest, materialize, pipeline, qagate\n"
    "import hanpatch.adapter as adapter_mod\n"
    "sealed = {'digest': 'd' * 64, 'entries': {'a/b': 'c'}}\n"
    "glossary.build = lambda *a, **k: {}\n"
    "capacity.build = lambda *a, **k: {}\n"
    "materialize.main = lambda *a, **k: 0\n"
    "audit.main = lambda *a, **k: 0\n"
    "manifest.build = lambda *a, **k: sealed\n"
    "manifest.load = lambda *a, **k: sealed\n"
    "qagate.validate = lambda entries: ([], [], [])\n"
    "config._profile = dict(config._profile, gate_thresholds={})\n"
    "res = {}\n"
    "class Bad:\n"
    "    checked = 0\n"
    "    def verify(self, rom, entries):\n"
    "        return ['a/b lost in the rebuild']\n"
    "class Boom:\n"
    "    checked = 0\n"
    "    def verify(self, rom, entries):\n"
    "        raise OSError('unreadable rom')\n"
    "for label, ad in (('problems', Bad()), ('exception', Boom())):\n"
    "    pipeline.gates(quiet=True)\n"
    "    before = os.path.exists(qagate.APPROVAL())\n"
    "    adapter_mod.project_adapter = (lambda a: (lambda *x, **k: a))(ad)\n"
    "    raised = False\n"
    "    try:\n"
    "        pipeline.verify(rom='r', quiet=True)\n"
    "    except BaseException:\n"
    "        raised = True\n"
    "    res[label] = {'before': before, 'raised': raised,\n"
    "                  'after': os.path.exists(qagate.APPROVAL())}\n"
    "print(json.dumps(res))")
if _verify is None:
    case('the verify-revocation fixture runs at all', False)
else:
    for _label in ('problems', 'exception'):
        _r = _verify[_label]
        case(f'a verify failure by {_label} leaves no approval token',
             _r['before'] and _r['raised'] and not _r['after'])

# Both late release-integrity fixes shipped with no case of their own: reverting
# either left the suite green, which is exactly the gap that let the original
# ordering defect through. These two close it.
#
# 1) `verify` must revoke BEFORE it prints. The fixture above runs quiet, so it
#    cannot see the ordering; here stdout raises while the problems are printed.
_verify_order = jp_probe(
    "import os, sys\n"
    "from hanpatch import audit, capacity, manifest, materialize, pipeline, qagate\n"
    "import hanpatch.adapter as adapter_mod\n"
    "sealed = {'digest': 'd' * 64, 'entries': {'a/b': 'c'}}\n"
    "glossary.build = lambda *a, **k: {}\n"
    "capacity.build = lambda *a, **k: {}\n"
    "materialize.main = lambda *a, **k: 0\n"
    "audit.main = lambda *a, **k: 0\n"
    "manifest.build = lambda *a, **k: sealed\n"
    "manifest.load = lambda *a, **k: sealed\n"
    "qagate.validate = lambda entries: ([], [], [])\n"
    "config._profile = dict(config._profile, gate_thresholds={})\n"
    "pipeline.gates(quiet=True)\n"
    "before = os.path.exists(qagate.APPROVAL())\n"
    "class Bad:\n"
    "    checked = 0\n"
    "    def verify(self, rom, entries):\n"
    "        return ['a/b lost in the rebuild']\n"
    "adapter_mod.project_adapter = lambda *a, **k: Bad()\n"
    "class Dead:\n"
    "    def write(self, *a):\n"
    "        raise BrokenPipeError('closed')\n"
    "    def flush(self):\n"
    "        pass\n"
    "real_stdout = sys.stdout\n"
    "sys.stdout = Dead()\n"
    "raised = ''\n"
    "try:\n"
    "    pipeline.verify(rom='r', quiet=False)\n"
    "except BaseException as e:\n"
    "    raised = type(e).__name__\n"
    "finally:\n"
    "    sys.stdout = real_stdout\n"
    "print(json.dumps({'before': before, 'raised': raised,\n"
    "                  'after': os.path.exists(qagate.APPROVAL())}))")
if _verify_order is None:
    case('the verify-ordering fixture runs at all', False)
else:
    case('a verify failure revokes even when reporting it fails',
         _verify_order['before'] and not _verify_order['after']
         and _verify_order['raised'] == 'BrokenPipeError')

# 2) The direct `qagate` entry point must not delete a token on SUCCESS.
#    `sys.exit(main())` raises SystemExit(0), so a blanket `except BaseException`
#    revoked on the clean path and deleted the token `hanpatch gates` had written.
#
# The invariant is NOT "every revoke is code-guarded": the trailing
# `except BaseException: revoke()` is correct precisely because a clean
# `SystemExit(0)` never reaches it. What must hold is that a SystemExit handler
# comes FIRST and that ITS revoke is guarded by the exit code. Delete that clause,
# reorder the handlers, or drop the guard, and the blanket handler starts revoking
# on success again - all three mutations were run against this case and all three
# fail it. A live run also confirmed the behaviour (token present before and after
# a clean `python -m hanpatch.qagate`, exit 0).
_qa_src = open(os.path.join(ROOT, 'hanpatch/qagate.py')).read()
_main_block = _ast.parse('if True:' + _qa_src.split("if __name__ == '__main__':")[1])
_handlers = [n for n in _ast.walk(_main_block) if isinstance(n, _ast.ExceptHandler)]


def _catches(handler, name):
    t = handler.type
    if isinstance(t, _ast.Name):
        return t.id == name
    if isinstance(t, _ast.Tuple):
        return any(getattr(e, 'id', '') == name for e in t.elts)
    return False


_sysexit_at = [i for i, h in enumerate(_handlers) if _catches(h, 'SystemExit')]
_broad_at = [i for i, h in enumerate(_handlers) if _catches(h, 'BaseException')]
_first_sysexit = _sysexit_at[0] if _sysexit_at else None
_ordered = (_first_sysexit is not None
            and (not _broad_at or _first_sysexit < min(_broad_at)))
_code_guarded = False
if _first_sysexit is not None:
    _h = _handlers[_first_sysexit]
    _revokes = [n for n in _ast.walk(_h)
                if isinstance(n, _ast.Call) and getattr(n.func, 'id', '') == 'revoke']
    _guards = [n for n in _ast.walk(_h)
               if isinstance(n, _ast.If) and 'code' in _ast.dump(n.test)]
    # every revoke in that handler must live inside a code-testing branch
    _code_guarded = bool(_guards) and all(
        any(r in list(_ast.walk(g)) for g in _guards) for r in _revokes)
case('a clean exit from the direct qa panel cannot reach an unconditional revoke',
     _ordered and _code_guarded)

print('== document shape (M0b) ==')
# Every state document this pipeline reads is a mapping. `json.load` returns a
# list or a scalar just as happily, after which `dict.update` silently no-ops and
# `.items()` raises a bare AttributeError deep inside a gate. That produced 18
# red-team findings, including a waiver file of `[]` coerced into an empty waiver
# state while a release bundle was still emitted.
_docdir = tempfile.mkdtemp(prefix='hanpatch-test-doc-')
for _name, _body in (('array.json', '[]'), ('scalar.json', '1'),
                     ('broken.json', '{not json')):
    open(os.path.join(_docdir, _name), 'w').write(_body)
open(os.path.join(_docdir, 'object.json'), 'w').write('{"a": 1}')


def refuses(name, needle):
    try:
        config.load_object(os.path.join(_docdir, name), 'the test document')
        return False
    except SystemExit as exc:
        return needle in str(exc) and 'the test document' in str(exc)


case('a document that is a JSON array is refused',
     refuses('array.json', 'must be a JSON object'))
case('a document that is a JSON scalar is refused',
     refuses('scalar.json', 'must be a JSON object'))
case('a document that is not JSON at all is refused',
     refuses('broken.json', 'not valid JSON'))
case('a well-formed object still loads',
     config.load_object(os.path.join(_docdir, 'object.json'), 'x') == {'a': 1})

# The contract only matters if the readers use it: a bare `json.load(open(...))`
# in a reader is exactly how the silent coercion survived.
# The guard walks the package rather than a hand-maintained list, so a module
# nobody remembered to add cannot slip a bare load past it.
_READER_EXCLUDE = {
    # The validating loader itself lives in config.py and is skipped by body
    # below. `capacity.py` and `wrap.py` carry uncommitted operator work that is
    # out of scope for this change set; their remaining bare loads are recorded
    # as follow-up rather than edited here.
    'capacity.py',
    'wrap.py',
}
_bare_loads = []
for _path in sorted(glob.glob(os.path.join(ROOT, 'hanpatch', '*.py'))):
    _f = os.path.basename(_path)
    if _f in _READER_EXCLUDE:
        continue
    _src = open(_path).read()
    # The validating loader is the one place allowed to call `json.load`; skip its
    # own body so the guard checks its CALLERS.
    _skip = set()
    for _n in _ast.walk(_ast.parse(_src)):
        if isinstance(_n, _ast.FunctionDef) and _n.name == 'load_object':
            _skip = set(range(_n.lineno, (_n.end_lineno or _n.lineno) + 1))
    for _i, _line in enumerate(_src.splitlines(), 1):
        if _i in _skip:
            continue
        if 'json.load(' in _line and 'load_object' not in _line:
            _bare_loads.append(f'{_f}:{_i}')
case('no state-document reader loads JSON without shape validation',
     not _bare_loads)
if _bare_loads:
    print('        ' + ', '.join(_bare_loads[:8]))

# A wrongly shaped profile VALUE is the same defect one level down: `"terms": []`
# would silently empty the glossary the title declared.
for _key, _bad, _want in (('terms', '[]', 'must be a JSON object'),
                          ('hard_terms', '{}', 'must be a JSON list'),
                          # A mode this build does not implement must be refused
                          # at resolution: it used to surface as a bare
                          # ValueError inside translate.copied_spans, mid-gate.
                          ('copied_spans_tokenizer', '"mecab"', 'unsupported'),
                          ('fullwidth_is_content', '"yes"',
                           'must be true, false or absent'),
                          ('judge_policy', '[]', 'must be a string')):
    _shape = subprocess.run(
        [sys.executable, '-c',
         'import json,os,sys,tempfile\n'
         f'sys.path.insert(0, {ROOT!r})\n'
         'd = tempfile.mkdtemp()\n'
         f'json.dump(dict({{"budget": {{"default": 320}}}}, **{{{_key!r}: {_bad}}}),'
         ' open(os.path.join(d, "profile.json"), "w"))\n'
         "json.dump({'title': 'x', 'platform': 'threeds',\n"
         "           'adapter': 'crimson_shroud', 'target': 'ko',\n"
         "           'profile': 'profile.json'},\n"
         "          open(os.path.join(d, 'hanpatch.json'), 'w'))\n"
         "os.environ['HANPATCH_PROJECT'] = d\n"
         'from hanpatch import config\n'
         'try:\n'
         '    config.profile()\n'
         "    print('ACCEPTED')\n"
         'except BaseException as e:\n'
         "    print('REFUSED ' + str(e))\n"],
        capture_output=True, text=True)
    case(f'a profile whose {_key} has the wrong shape is refused',
         'REFUSED' in _shape.stdout and _want in _shape.stdout)

# A width nobody measured makes the capacity gate meaningless, so the demand is
# enforced where a width is CONSUMED rather than where a profile is resolved.
# There is no generic fallback and no per-language carve-out: source language
# cannot prove that THIS title rendered a given width.
_widths = jp_probe(
    "from hanpatch import wrap\n"
    "res = {}\n"
    "for label, budget in (('declared', {'default': 256}),\n"
    "                      ('empty', {}),\n"
    "                      ('family-only', {'dialogue': 392})):\n"
    "    config._profile = dict(config._profile, budget=budget)\n"
    "    wrap.reset()\n"
    "    try:\n"
    "        res[label] = {'width': wrap.budget_for('dialogue')}\n"
    "    except SystemExit as e:\n"
    "        res[label] = {'refused': 'measure the widest page' in str(e)}\n"
    "print(json.dumps(res))")
if _widths is None:
    case('the measured-width fixture runs at all', False)
else:
    case('a declared width is used as measured',
         _widths['declared'].get('width') == 256)
    case('an empty budget is refused where a width is consumed',
         _widths['empty'].get('refused') is True)
    case('a family budget with no default is refused the same way',
         _widths['family-only'].get('refused') is True)

# The resolver alone is not the contract: the gate that DERIVES capacity must
# also refuse an unmeasured width, or rerouting `capacity.build` back to a direct
# `wrap.BUDGET` read would silently restore the hole while the resolver test
# stayed green.
_capacity_width = jp_probe(
    "import json\n"
    "from hanpatch import capacity, wrap\n"
    "res = {}\n"
    "for label, budget in (('declared', {'default': 256}), ('empty', {})):\n"
    "    config._profile = dict(config._profile, budget=budget)\n"
    "    wrap.reset()\n"
    "    seen = []\n"
    "    real = wrap.budget_for\n"
    "    wrap.budget_for = lambda kind: seen.append(kind) or real(kind)\n"
    "    try:\n"
    "        capacity.build()\n"
    "        res[label] = {'ok': True, 'consulted': seen}\n"
    "    except SystemExit as e:\n"
    "        res[label] = {'refused': 'measure the widest page' in str(e),\n"
    "                      'consulted': seen}\n"
    "    finally:\n"
    "        wrap.budget_for = real\n"
    "print(json.dumps(res))")
if _capacity_width is None:
    case('the capacity-width fixture runs at all', False)
else:
    case('the capacity gate consults the measured-width resolver',
         _capacity_width['declared'].get('consulted'))
    case('the capacity gate refuses an unmeasured width',
         _capacity_width['empty'].get('refused') is True)

print('== waivers for the new categories (M0b) ==')
for _cat in ('JP_SOURCE_AMBIGUITY', 'DECLARED_REGISTER_CONFLICT'):
    case(f'a waiver in category {_cat} is accepted',
         _cat in qagate.WAIVER_CATEGORIES)
case('the dropped human-anchor waiver category is gone',
     'HUMAN_ANCHOR' not in qagate.WAIVER_CATEGORIES)
case('the release floor and judge count are untouched',
     qagate.FLOOR == 4 and qagate.REQUIRED_JUDGES == 2)

# A bare intra-package import resolves to a different module object when the
# project directory happens to shadow it, which is how two glossaries once
# existed at once.
_bare = []
for _f in sorted(os.listdir(os.path.join(ROOT, 'hanpatch'))):
    if not _f.endswith('.py'):
        continue
    for _i, _line in enumerate(open(os.path.join(ROOT, 'hanpatch', _f)), 1):
        _s = _line.strip()
        for _m in ('config', 'glossary', 'tm', 'translate', 'wrap', 'josa', 'qa',
                   'qagate', 'audit', 'capacity', 'manifest', 'materialize',
                   'pipeline', 'providers', 'release', 'scriptbook', 'tmpl'):
            if _s == f'import {_m}':
                _bare.append(f'{_f}:{_i}')
case('no module imports a sibling by bare name', not _bare)

print()
print('== free-provider rotation wiring ==')
# Every endpoint must be a LOCAL ROTATOR. A direct upstream base URL loses the
# key rotation, the per-account budget, the cooldowns and the vendor
# compatibility patches, and it burns quota shared with every other consumer.
# This is not hypothetical: `groq:openai/gpt-oss-120b` sat in this file recorded
# as permanently broken on HTTP 403, which was urllib's default User-Agent
# tripping Cloudflare on api.groq.com. Through the rotator the same spec answers.
from hanpatch import providers as _prov  # noqa: E402


def _prov_refuses(fn, needle):
    try:
        fn()
        return False
    except SystemExit as exc:
        return needle in str(exc)


# The rotator rule is narrowed, not dropped. Every FREE endpoint is still a loopback
# rotator that owns its own credentials - that is what keeps budgets, retries and vendor
# compatibility patches in one place. A PAID lane is the single exception and it is defined
# by carrying a key variable, so the two properties are checked as a partition: an endpoint
# either goes through a local rotator with no key here, or it is a declared paid lane. What
# must never happen is a keyed upstream that a registry role or a default pool can reach by
# accident, so that is asserted separately below.
_PAID = {n for n, (_b, k, _r) in _prov.ENDPOINTS.items() if k is not None}
case('every unkeyed provider endpoint is a loopback rotator, never an upstream',
     all(b.startswith('http://127.0.0.1:')
         for n, (b, k, _r) in _prov.ENDPOINTS.items() if k is None))
case('a keyed endpoint is the paid exception and is not a loopback rotator',
     all(not b.startswith('http://127.0.0.1:')
         for n, (b, k, _r) in _prov.ENDPOINTS.items() if k is not None))
case('a paid lane is never reachable from the pinned fallback specs',
     not any(s.split(':', 1)[0] in _PAID for s in _prov.DEFAULT_MODELS))
case('a paid lane declares its key variable rather than reading a hardcoded name',
     all(isinstance(k, str) and k.isupper()
         for _n, (_b, k, _r) in _prov.ENDPOINTS.items() if k is not None))
case('the Groq endpoint is the general rotator, not the subagent server',
     '18096' in _prov.ENDPOINTS['groq'][0]
     and not any('18091' in b for b, _k, _r in _prov.ENDPOINTS.values()))
case('every pinned fallback spec names a configured endpoint',
     all(s.split(':', 1)[0] in _prov.ENDPOINTS for s in _prov.DEFAULT_MODELS))
case('no pinned fallback spec is a known-dead or paid-slug model',
     not any(m in s for s in _prov.DEFAULT_MODELS for m in (
         'qwen3.5-397b', 'kimi-k2.6', 'glm-5.2', 'deepseek-v4-pro')))

# The registry is the SSOT. Absent means fall back; malformed must be loud,
# because a corrupt SSOT read as "no models" is indistinguishable from a
# deliberate empty configuration and would silently resurrect retired pins.
_rtmp = tempfile.mkdtemp(prefix='hanpatch-registry-')
_absent = os.path.join(_rtmp, 'missing.json')
case('an absent registry falls back rather than failing',
     _prov.registry_models(path=_absent) == [])
for _bad, _needle in (('[1, 2]', 'must be a JSON object'),
                      ('{"no_payload": 1}', 'no "payload" object'),
                      ('{ not json', 'not valid JSON')):
    _p = os.path.join(_rtmp, 'bad.json')
    open(_p, 'w').write(_bad)
    case(f'a malformed registry is refused loudly ({_needle})',
         _prov_refuses(lambda p=_p: _prov.registry_models(path=p), _needle))
_good = os.path.join(_rtmp, 'good.json')
json.dump({'payload': {'models': {
    'groq:openai/gpt-oss-120b': {'state': 'ok', 'roles_allowed': ['batch_translation']},
    'groq:probe-model': {'state': 'probe', 'roles_allowed': ['batch_translation']},
    'groq:judge-only': {'state': 'ok', 'roles_allowed': ['qa_judging']},
    'unknownprov:x': {'state': 'ok', 'roles_allowed': ['batch_translation']},
    'groq:bad-meta': 'not-a-dict',
}}}, open(_good, 'w'))
case('the registry selects by role and admits only verified states',
     _prov.registry_models(path=_good) == ['groq:openai/gpt-oss-120b'])
case('the registry honours a different role',
     _prov.registry_models('qa_judging', path=_good) == ['groq:judge-only'])
case('a registry spec for an unconfigured endpoint is dropped',
     'unknownprov:x' not in _prov.registry_models(path=_good, states=None))
case('an unverified probe-state model can be requested explicitly',
     'groq:probe-model' in _prov.registry_models(path=_good, states=('ok', 'probe')))

# build_pool is the entire point of the providers change and had no test. It
# needs no network: Provider construction only records a base URL and a model.
_penv = os.path.join(_rtmp, 'empty.env')
open(_penv, 'w').write('')
_prev_env = os.environ.get('HANPATCH_ENV')
_prev_reg = os.environ.get('HANPATCH_MODEL_REGISTRY')
os.environ['HANPATCH_ENV'] = _penv
os.environ['HANPATCH_MODEL_REGISTRY'] = _good
case('the pool is derived from the registry when one is present',
     [p.id for p in _prov.build_pool()] == ['groq:openai/gpt-oss-120b'])
case('the registry env override is honoured with no explicit path',
     _prov.registry_models() == ['groq:openai/gpt-oss-120b'])
case('the pool never merges the pins into a registry answer',
     not [p.id for p in _prov.build_pool()
          if p.id in _prov.DEFAULT_MODELS and p.id != 'groq:openai/gpt-oss-120b'])
os.environ['HANPATCH_MODEL_REGISTRY'] = _absent
# The pool is compared as a SET: build_pool now interleaves by endpoint so that
# consecutive seats hit different rotators, so order is no longer the contract. What must
# hold is that the pins are what gets used and nothing else leaks in.
case('an absent registry falls back to the pinned specs',
     sorted(p.id for p in _prov.build_pool()) == sorted(_prov.DEFAULT_MODELS))
os.environ['HANPATCH_MODEL_REGISTRY'] = _good
case('an explicit caller list beats the registry',
     [p.id for p in _prov.build_pool(['groq:llama-3.3-70b-versatile'])]
     == ['groq:llama-3.3-70b-versatile'])
case('a registry answer is used instead of the pins, not alongside them',
     sorted(p.id for p in _prov.build_pool()) != sorted(_prov.DEFAULT_MODELS))
case('a role nobody is seated for falls back rather than emptying the pool',
     sorted(p.id for p in _prov.build_pool(role='no_such_role'))
     == sorted(_prov.DEFAULT_MODELS))
for _k, _v in (('HANPATCH_ENV', _prev_env), ('HANPATCH_MODEL_REGISTRY', _prev_reg)):
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

print()
print('== engine layout authority ==')
# `wrap.fits` short-circuits on a row whose SOURCE has no line break. For Crimson
# Shroud that is right - the engine lays such rows out. For a container that
# stores one display line per row, such as DQ7's .txt records, the same
# short-circuit would discard the pixel budget and never consult the measured
# line capacity, turning the layout gate into a no-op for the whole title. So the
# fact is declared, demanded where it decides something, and has no default.
_ewroot = tempfile.mkdtemp(prefix='hanpatch-enginewrap-')
json.dump({'title': 'ew', 'platform': 'threeds', 'adapter': 'crimson_shroud',
           'target': 'ko', 'profile': 'p.json'},
          open(os.path.join(_ewroot, config.PROJECT_FILE), 'w'))


def _reread_cap():
    _prev = config.root()
    config.set_root(_ewroot)
    try:
        wrap.invalidate_capacity()
        return wrap.capacity('dialogue/x', 'dialogue')
    finally:
        if os.path.exists(os.path.join(_prev, config.PROJECT_FILE)):
            config.set_root(_prev)


#: a real measurement font, borrowed from the reference project when it is built.
#: The growth rules cannot be exercised without one - text_width needs advances.
_EW_FONT = '/mnt/ssd256/m0a-crimson/work/ko/font_text.bcfnt'
_HAVE_EW_FONT = os.path.exists(_EW_FONT)


def _ew(profile_extra, en, ko, kind='dialogue'):
    prof = {'budget': {'default': 64}, 'capacity': {'dialogue': 2}}
    if _HAVE_EW_FONT:
        prof['font_src'] = [_EW_FONT]
        prof['font_out'] = [_EW_FONT]
    prof.update(profile_extra)
    json.dump(prof, open(os.path.join(_ewroot, 'p.json'), 'w'))
    _prev = config.root()
    config.set_root(_ewroot)
    try:
        return wrap.fits(en, ko, kind)
    finally:
        if os.path.exists(os.path.join(_prev, config.PROJECT_FILE)):
            config.set_root(_prev)


case('an unbroken source row with no declared layout authority fails closed',
     _prov_refuses(lambda: _ew({}, 'a short line', '짧은 줄'), 'the profile does not say'))
case('a declared engine-laid-out row is passed through unmeasured',
     _ew({'engine_wraps': True}, 'a short line', '아주 긴 한국어 문장 ' * 6)[1] == [])
# Declaring the container per-line sends the row into measurement instead of past
# it. With a real font available the measurement itself is the observable: an
# overflowing Korean row must produce a problem. Without one, the font demand is.
if _HAVE_EW_FONT:
    case('a declared per-line container measures the unbroken row instead',
         _ew({'engine_wraps': False}, 'a short line', '\uac00' * 60)[1] != [])
else:
    case('a declared per-line container measures the unbroken row instead',
         _prov_refuses(lambda: _ew({'engine_wraps': False}, 'a short line',
                                   '아주 긴 한국어 문장 ' * 6),
                       'no font to measure against'))
# A row that carries its own break never asks who lays it out, so an UNDECLARED
# engine_wraps must not stop it.
if _HAVE_EW_FONT:
    case('a broken source row never consults the layout authority',
         _ew({}, 'two\nlines', '\uac00\uac00\n\uac00\uac00')[1] == [])
else:
    case('a broken source row never consults the layout authority',
         _prov_refuses(lambda: _ew({}, 'two\nlines', '두\n줄'),
                       'no font to measure against'))
case('a non-boolean engine_wraps is refused, not treated as truthy',
     _prov_refuses(lambda: _ew({'engine_wraps': 'false'}, 'x', 'x'),
                   'engine_wraps'))
case('engine_wraps is a registered profile key with no default',
     'engine_wraps' in config._BOOL_KEYS
     and config.DEFAULT_PROFILE['engine_wraps'] is None)
# The fact has to reach the DERIVATION too. Honouring it in the gate but not in
# capacity.build would leave a per-line title with an empty derived table, and
# the gate it feeds with nothing measured to enforce.
_capsrc = {'dialogue': [{'key': 'k1', 'en': 'one unbroken source line', 'jp': ''}]}


def _derive(engine_wraps):
    prof = {'budget': {'default': 64}, 'engine_wraps': engine_wraps}
    json.dump(prof, open(os.path.join(_ewroot, 'p.json'), 'w'))
    os.makedirs(os.path.join(_ewroot, 'work', 'ko'), exist_ok=True)
    _src = os.path.join(_ewroot, 'src.json')
    json.dump(_capsrc, open(_src, 'w'), ensure_ascii=False)
    _prev = config.root()
    config.set_root(_ewroot)
    try:
        return capmod.build(_src)
    finally:
        if os.path.exists(os.path.join(_prev, config.PROJECT_FILE)):
            config.set_root(_prev)


# A per-line container must take its unbroken rows INTO derivation; measurement
# then demands the font, which is the observable that the row was not skipped.
# An engine-laid-out title skips them and needs no font at all - so the two
# outcomes differ in kind, not merely in value.
# The predicate must be the ONLY authority: a call site that reverts to the shape
# test would honour the fact in one gate and ignore it in the next.
_wsrc = open(os.path.join(ROOT, 'hanpatch', 'wrap.py')).read()
_csrc = open(os.path.join(ROOT, 'hanpatch', 'capacity.py')).read()
_psrc = open(os.path.join(ROOT, 'hanpatch', 'pipeline.py')).read()
case('is_freeform has exactly one consumer and it is the predicate',
     sum(_wsrc.count(n) for n in ('is_freeform(en)',)) == 2
     and 'is_freeform' not in _csrc and 'is_freeform' not in _psrc)
case('every module that decides whether a row is measured calls the predicate',
     'wrap.engine_lays_out(' in _csrc and 'wrap.engine_lays_out(' in _psrc
     and 'if engine_lays_out(en):' in _wsrc)
case('engine_wraps has exactly one reader in the package',
     sum(open(os.path.join(ROOT, 'hanpatch', f)).read().count("prof('engine_wraps')")
         for f in os.listdir(os.path.join(ROOT, 'hanpatch'))
         if f.endswith('.py')) == 1)
case('both layout decisions go through the named accessor, not the profile',
     _wsrc.count('title_lays_out_own_text()') == 3)
case('a per-line container takes its unbroken rows into derivation',
     _prov_refuses(lambda: _derive(False), 'no font to measure against'))
case('an engine-laid-out title skips them and derives nothing',
     _derive(True) == {})
def _poisoned_cap():
    """Write a bad derived table, assert the refusal, then CLEAN UP.

    Leaving it behind poisoned every later case in this project: the next
    wrap.capacity() call loaded it and aborted the suite after the case had already
    reported ok.
    """
    path = os.path.join(_ewroot, 'work', 'ko', 'capacity.json')
    json.dump({'dialogue/x': 0}, open(path, 'w'))
    try:
        return _prov_refuses(_reread_cap, 'not positive line counts')
    finally:
        os.remove(path)
        wrap.invalidate_capacity()


case('the derived capacity table is validated, not trusted', _poisoned_cap())
case('a capacity value that is not a positive line count is refused',
     _prov_refuses(lambda: _ew({'engine_wraps': True, 'capacity': {'dialogue': 0}},
                               'x', 'x'),
                   'capacity values must be positive line counts'))

# How much a translation may GROW depends on who owns the layout. Where the engine
# wraps, a row may fill the box. Where the CONTAINER owns it, a line that rewraps to
# two cannot be stored at all, and the gate used to pass exactly that: measured on
# DQ7, an overflowing Korean line against a one-line source produced no problem,
# because one page holding two lines is still under a box limit of four - and inject
# would then refuse it, with the gate having reported clean.
if _HAVE_EW_FONT:
    case('a per-line container refuses a line that rewraps, even under the box limit',
         any('cannot be stored' in p for p in
             _ew({'engine_wraps': False, 'capacity': {'dialogue': 4}},
                 'a b', '\uac00' * 60)[1]))
    case('an engine-laid-out title may still grow a row up to the box limit',
         _ew({'engine_wraps': True, 'capacity': {'dialogue': 4}},
             'a b', '\uac00' * 60)[1] == [])
else:
    skip('layout growth rules (no built reference font to measure with)')

print()
print('== built artifact naming and the font gate ==')
# A cartridge project used to emit `<title> (ko).cia` - an NCSD image with a CIA
# extension - so the operator's first action was a CIA installer that rejects it
# for reasons unrelated to the real cause. Both closures below shipped without a
# test in the round that added them, which is the same shape as the defect they
# were fixing.
_bn_root = tempfile.mkdtemp(prefix='hanpatch-builtname-')
json.dump({'budget': {'default': 64}, 'engine_wraps': True},
          open(os.path.join(_bn_root, 'p.json'), 'w'))


def _built_for(rom):
    json.dump({'title': 'T', 'platform': 'threeds', 'adapter': 'crimson_shroud',
               'target': 'ko', 'profile': 'p.json', 'rom': rom},
              open(os.path.join(_bn_root, config.PROJECT_FILE), 'w'))
    _prev = config.root()
    config.set_root(_bn_root)
    try:
        return config.built_name()
    finally:
        if os.path.exists(os.path.join(_prev, config.PROJECT_FILE)):
            config.set_root(_prev)


case('a cartridge project builds a .3ds, not a .cia', _built_for('game.3ds') == 'T (ko).3ds')
case('a CIA project still builds a .cia', _built_for('game.cia') == 'T (ko).cia')
case('a CCI extension is carried through too', _built_for('game.cci') == 'T (ko).cci')
def _built_no_rom():
    json.dump({'title': 'T', 'platform': 'threeds', 'adapter': 'crimson_shroud',
               'target': 'ko', 'profile': 'p.json'},
              open(os.path.join(_bn_root, config.PROJECT_FILE), 'w'))
    _prev = config.root()
    config.set_root(_bn_root)
    try:
        return config.built_name()
    finally:
        if os.path.exists(os.path.join(_prev, config.PROJECT_FILE)):
            config.set_root(_prev)


# This case used to call the helper that WRITES `rom: game.cia` before reading, so
# it re-asserted the previous case and left both defaults in built_name untested.
case('a project with no declared rom falls back to .cia',
     _built_no_rom() == 'T (ko).cia')
case('an extensionless rom still yields a .cia name',
     _built_for('gamedata') == 'T (ko).cia')

# The font gate: a build that INTRODUCES characters the source never used cannot be
# verified while no target font exists. The predicate is deliberately not "the
# manifest has non-ASCII text" - a Japanese source is non-ASCII and the shipped
# font renders it, so that version refused an identity rebuild.
from hanpatch.adapters import dq7 as _dq7mod  # noqa: E402

_fg_root = tempfile.mkdtemp(prefix='hanpatch-fontgate-')
os.makedirs(os.path.join(_fg_root, 'work'), exist_ok=True)
json.dump({'budget': {'default': 64}, 'engine_wraps': False, 'source_lang': 'ja',
           'font_out': []},
          open(os.path.join(_fg_root, 'p.json'), 'w'))
json.dump({'title': 'DQ7', 'platform': 'threeds', 'adapter': 'dq7', 'target': 'ko',
           'profile': 'p.json', 'rom': 'game.3ds'},
          open(os.path.join(_fg_root, config.PROJECT_FILE), 'w'))
json.dump({'fam': [{'key': 'k.txt', 'en': '\u3053\u3093\u306b\u3061\u306f', 'jp': ''}]},
          open(os.path.join(_fg_root, 'work', 'text_src.json'), 'w'), ensure_ascii=False)
_prev_fg = config.root()
config.set_root(_fg_root)
try:
    _ad = _dq7mod.DragonQuest7()
    case('text identical to the source introduces no glyph',
         _ad._chars_absent_from_source({'fam/k.txt': '\u3053\u3093\u306b\u3061\u306f'}) == set())
    case('ASCII-only text introduces no glyph',
         _ad._chars_absent_from_source({'fam/k.txt': 'OK 123'}) == set())
    case('Hangul is counted as a glyph the source never used',
         len(_ad._chars_absent_from_source({'fam/k.txt': '\ud55c\uae00'})) == 2)
    case('a Japanese character the source never used still counts',
         _ad._chars_absent_from_source({'fam/k.txt': '\u4e00'}) == {'\u4e00'})
    # Absent evidence must REFUSE, not report "no new glyphs". The first version of
    # this predicate returned an empty set there, which handed a Korean build a
    # green verify precisely because the comparison basis was missing - and
    # `pipeline.verify` does not require the extracted source to exist.
    _src_file = os.path.join(_fg_root, 'work', 'text_src.json')
    os.rename(_src_file, _src_file + '.hidden')
    try:
        case('a missing extracted source is refused, not read as no-new-glyphs',
             _prov_refuses(
                 lambda: _ad._chars_absent_from_source({'fam/k.txt': '\ud55c'}),
                 'Run `hanpatch extract` first'))
        case('an empty manifest needs no comparison basis',
             _ad._chars_absent_from_source({}) == set())
    finally:
        os.rename(_src_file + '.hidden', _src_file)
finally:
    if os.path.exists(os.path.join(_prev_fg, config.PROJECT_FILE)):
        config.set_root(_prev_fg)


print('== anchor versioning and the drift report ==')
_av_root = tempfile.mkdtemp(prefix='hp-anchorver-')
_av_prev = config.root()
try:
    # the working directory is work/<target>, not work/ - writing to the wrong one
    # made the report examine nothing and call it clean, which is why it now refuses
    os.makedirs(os.path.join(_av_root, 'profiles'))
    # Two enforced names and one soft hint. The soft one is the point of the design:
    # it must not be part of the contract, so editing it must cost no revalidation.
    _av_prof = {'source_lang': 'ja', 'target_lang': 'ko', 'engine_wraps': False,
                'terms': {'アアア': '가가', 'イイイ': '나나', 'ウウウ': '다다'},
                'hard_terms': ['アアア', 'イイイ']}
    json.dump(_av_prof, open(os.path.join(_av_root, 'profiles', 'p.json'), 'w'),
              ensure_ascii=False)
    json.dump({'profile': 'profiles/p.json'},
              open(os.path.join(_av_root, config.PROJECT_FILE), 'w'))
    _av_work = os.path.join(_av_root, 'work', 'ko')
    os.makedirs(_av_work)
    _av_src = {'fam': [{'key': 'k1', 'en': 'アアアの話', 'jp': ''},
                       {'key': 'k2', 'en': 'イイイの話', 'jp': ''},
                       {'key': 'k3', 'en': 'ウウウの話', 'jp': ''}]}
    json.dump(_av_src, open(os.path.join(_av_root, 'work', 'text_src.json'), 'w'),
              ensure_ascii=False)
    json.dump({'アアアの話': '가가 이야기', 'イイイの話': '나나 이야기',
               'ウウウの話': '다다 이야기'},
              open(os.path.join(_av_work, 'tm.json'), 'w'), ensure_ascii=False)
    config.set_root(_av_root)
    _av_rows = ['アアアの話', 'イイイの話', 'ウウウの話']
    _v1 = glossary.record_version()
    _rec = {r: _v1 for r in _av_rows}
    case('the enforced contract holds only the hard terms, not the hints',
         set(glossary.enforced_contract()) == {'アアア', 'イイイ'})
    case('an unchanged contract re-sweeps nothing',
         glossary.resweep(_av_rows, _rec) == [])
    case('a row with no recorded version is re-swept fail-closed',
         len(glossary.resweep(_av_rows, {})) == 3)

    def _edit(term, ko):
        pr = json.load(open(os.path.join(_av_root, 'profiles', 'p.json')))
        pr['terms'][term] = ko
        json.dump(pr, open(os.path.join(_av_root, 'profiles', 'p.json'), 'w'),
                  ensure_ascii=False)
        config.set_root(_av_root)

    _edit('ウウウ', '다다다')
    case('editing a HINT does not move the version',
         glossary.anchor_version() == _v1)
    case('editing a hint re-sweeps nothing',
         glossary.resweep(_av_rows, _rec) == [])
    _edit('ウウウ', '다다')

    _edit('アアア', '가가가')
    case('editing an ENFORCED term moves the version',
         glossary.anchor_version() != _v1)
    case('the re-sweep selects only the rows carrying that term',
         glossary.resweep(_av_rows, _rec) == ['アアアの話'])
    _edit('アアア', '가가')
    case('restoring the enforced form restores the version',
         glossary.anchor_version() == _v1)

    # the ja drift report: reporting, never gating
    json.dump({'アアアの話': '가가 이야기', 'イイイの話': '나나 이야기',
               'ウウウの話': '이야기'},          # hint dropped: inconsistent, not a failure
              open(os.path.join(_av_work, 'tm.json'), 'w'), ensure_ascii=False)
    json.dump({'fam': [{'key': 'k3', 'en': 'ウウウの話', 'jp': ''},
                       {'key': 'k4', 'en': 'ウウウの村', 'jp': ''}]},
              open(os.path.join(_av_root, 'work', 'text_src.json'), 'w'),
              ensure_ascii=False)
    json.dump({'ウウウの話': '다다 이야기', 'ウウウの村': '이야기'},
              open(os.path.join(_av_work, 'tm.json'), 'w'), ensure_ascii=False)
    config.set_root(_av_root)
    _src2 = json.load(open(os.path.join(_av_root, 'work', 'text_src.json')))
    _in, _co, _ex = _ad_audit.term_rendering(_src2, tm.load())
    case('one run rendered two ways is reported',
         any(t == 'ウウウ' and have == 1 and miss == 1 for t, have, miss in _in))
    # a Korean form turning up where its source run is absent is a collision, but ONLY
    # for a proper noun: a common noun's Korean rendering legitimately occurs anywhere
    json.dump({'fam': [{'key': 'k1', 'en': 'アアアの話', 'jp': ''},
                       {'key': 'k5', 'en': 'ほかの話', 'jp': ''}]},
              open(os.path.join(_av_root, 'work', 'text_src.json'), 'w'),
              ensure_ascii=False)
    json.dump({'アアアの話': '가가 이야기', 'ほかの話': '가가 다른 이야기'},
              open(os.path.join(_av_work, 'tm.json'), 'w'), ensure_ascii=False)
    config.set_root(_av_root)
    _src3 = json.load(open(os.path.join(_av_root, 'work', 'text_src.json')))
    _in3, _co3, _ex3 = _ad_audit.term_rendering(_src3, tm.load())
    case('an enforced name used where its source run is absent is a collision',
         any(t == 'アアア' and n == 1 for t, n in _co3))
    _p3 = json.load(open(os.path.join(_av_root, 'profiles', 'p.json')))
    _p3['hard_terms'] = []
    json.dump(_p3, open(os.path.join(_av_root, 'profiles', 'p.json'), 'w'),
              ensure_ascii=False)
    config.set_root(_av_root)
    _in4, _co4, _ex4 = _ad_audit.term_rendering(_src3, tm.load())
    case('a hint is not collision-checked, because common Korean occurs everywhere',
         _co4 == [])
finally:
    config.set_root(_av_prev)
    shutil.rmtree(_av_root, ignore_errors=True)


print('== short enforced names need a word boundary ==')
_mp = glossary.mandate_present
for _kt, _flat, _want, _why in [
        ('마석', '족장의 마석', True, 'a mandate after a space is present'),
        ('마석', '수정 마석에서', True,
         'the right-hand side is NOT checked: Korean continues a name with 에서'),
        ('키슈', '키슈족의 전사', True, 'nor is a compound suffix like 족 a rejection'),
        ('매복', '매복당했다', True, 'nor a verbalising suffix'),
        ('결계', '방호 결계였다', True, 'nor the contracted copula'),
        ('장', '족장이 말했다', False,
         'but a mandate that is the TAIL of a longer word is not present'),
        ('마석', '흑마석을 들었다', False, 'nor when a modifier runs into it'),
        ('반즈', '반즈 왕', True, 'a name at the start of the line is present'),
        ('반즈', '왕 반즈', True, 'and after a space'),
        ('반즈', '그반즈가', False, 'and not inside another word'),
]:
    case(_why, _mp(_kt, _flat) is _want)
case('an empty mandate is never present', _mp('', '아무것') is False)

print('== an unenforceable mandate is refused at declaration ==')
case('a one-syllable enforced rendering is refused, not silently passed',
     _prov_refuses(lambda: glossary._refuse_unenforceable({'x': '얀'}, ['x']),
                   'one-syllable rendering is contained in ordinary Korean words'))
case('a two-syllable enforced rendering is accepted',
     glossary._refuse_unenforceable({'x': '반즈'}, ['x']) is None)


print('== register is derived from the source, not declared per family ==')
from hanpatch import register as _reg
case('a polite sentence-final marker is detected',
     _reg.marker_of('こちらへどうぞ。ご案内します。') == _reg.POLITE)
case('a plain sentence-final marker is detected',
     _reg.marker_of('もう行くのだ。') == _reg.PLAIN)
case('a politeness formula is NOT read as a plain ender',
     _reg.marker_of('こちらへどうぞ。') is None)
case('a bare sentence ender too weak to trust declares nothing',
     _reg.marker_of('もう行くぞ。') is None)
case('a polite clause closed by a japanese bracket is still polite',
     _reg.marker_of('「わかりました」') == _reg.POLITE)
case('a string with BOTH levels declares nothing rather than picking one',
     _reg.marker_of('「行きます」と言っただろう。') is None)
case('a string with no marker declares nothing',
     _reg.marker_of('アアア') is None)
case('an empty string declares nothing', _reg.marker_of('') is None)
case('ます inside a verb stem is not a polite marker',
     _reg.marker_of('用事を済ませておいた') is None)
_reg_prev = config.root()
_reg_root = tempfile.mkdtemp(prefix='hp-register-')
try:
    os.makedirs(os.path.join(_reg_root, 'profiles'))
    os.makedirs(os.path.join(_reg_root, 'work'))
    json.dump({'source_lang': 'ja', 'target_lang': 'ko'},
              open(os.path.join(_reg_root, 'profiles', 'p.json'), 'w'))
    json.dump({'profile': 'profiles/p.json'},
              open(os.path.join(_reg_root, config.PROJECT_FILE), 'w'))
    json.dump({'fam': []}, open(os.path.join(_reg_root, 'work', 'text_src.json'), 'w'))
    config.set_root(_reg_root)
    case('an undeclared fallback register is refused, not guessed',
         _prov_refuses(_reg.declared_default, 'REGISTER UNDECLARED'))
    case('a marker-less string refuses when the fallback is undeclared',
         _prov_refuses(lambda: _reg.instruction('アアア'), 'REGISTER UNDECLARED'))
    for _bad in ('Plain', '', 'formal', None):
        _pr = json.load(open(os.path.join(_reg_root, 'profiles', 'p.json')))
        _pr['register_default'] = _bad
        json.dump(_pr, open(os.path.join(_reg_root, 'profiles', 'p.json'), 'w'))
        config.set_root(_reg_root)
        case(f'register_default {_bad!r} is refused',
             _prov_refuses(_reg.declared_default, 'REGISTER UNDECLARED'))
    _pr = json.load(open(os.path.join(_reg_root, 'profiles', 'p.json')))
    _pr['register_default'] = 'plain'
    json.dump(_pr, open(os.path.join(_reg_root, 'profiles', 'p.json'), 'w'))
    config.set_root(_reg_root)
    case('a row is a record, so a marker on any of its lines settles it',
         _reg.marker_of('アアア\nもう行くのだ。') == _reg.PLAIN)
    case('a record carrying both levels declares nothing',
         _reg.marker_of('ご案内します。\nもう行くのだ。') is None)
    case('a derived polite string is instructed as polite',
         '존댓말' in _reg.instruction('ご案内します。'))
    case('a derived plain string is instructed as plain',
         '평서형' in _reg.instruction('もう行くのだ。'))
finally:
    config.set_root(_reg_prev)
    shutil.rmtree(_reg_root, ignore_errors=True)
case('an english-source title is told no register at all',
     _reg.instruction('Go now.') == '')


print('== pool seats spread across endpoints, not models ==')
class _FakeProv:
    def __init__(self, i): self.id = i
_il = _prov.interleave
case('two models on one endpoint are separated by the other endpoints',
     [p.id for p in _il([_FakeProv('groq:a'), _FakeProv('groq:b'),
                         _FakeProv('nim:c'), _FakeProv('zen:d')])]
     == ['groq:a', 'nim:c', 'zen:d', 'groq:b'])
case('an endpoint with more models keeps its extras at the tail',
     [p.id for p in _il([_FakeProv('groq:a'), _FakeProv('groq:b'),
                         _FakeProv('groq:c'), _FakeProv('nim:d')])]
     == ['groq:a', 'nim:d', 'groq:b', 'groq:c'])
case('a single-endpoint pool is unchanged',
     [p.id for p in _il([_FakeProv('groq:a'), _FakeProv('groq:b')])]
     == ['groq:a', 'groq:b'])
case('an empty pool interleaves to empty', _il([]) == [])
case('every input provider survives interleaving',
     len(_il([_FakeProv(f'e{i%3}:m{i}') for i in range(9)])) == 9)


print('== a parked endpoint is not scheduled again until it says so ==')
case('a rotator park message yields its own wait',
     _prov._retry_after(None, 'all 4 Groq key(s) are parked; retry in 34s') == 34.0)
case('a Retry-After header wins when present',
     _prov._retry_after({'Retry-After': '12'}, 'retry in 99s') == 12.0)
case('a 429 with no number still parks for the default',
     _prov._retry_after(None, 'too many requests') == 20.0)
case('a fractional wait is honoured',
     _prov._retry_after(None, 'retry in 8.8s') == 8.8)
class _PP:
    def __init__(self, i, until=0.0): self.id, self.parked_until = i, until
_now = 1000.0
case('a parked provider is filtered out of the live pool',
     [p.id for p in _prov.available([_PP('a', _now + 5), _PP('b')], now=_now)] == ['b'])
case('a provider whose park has expired comes back',
     [p.id for p in _prov.available([_PP('a', _now - 1), _PP('b')], now=_now)]
     == ['a', 'b'])
case('an all-parked pool reports empty rather than pretending',
     _prov.available([_PP('a', _now + 5)], now=_now) == [])


print('== every adapter that ships fonts reports them through the CLI ==')
from hanpatch import adapter as _ad_mod
import hanpatch.adapters.dq7            # noqa: F401  (registers the adapter)
import hanpatch.adapters.crimson_shroud  # noqa: F401
for _name in ('dq7', 'crimson_shroud'):
    _cls = type(_ad_mod.get(_name))
    case(f'{_name} overrides build_fonts rather than inheriting the no-op',
         _cls.build_fonts is not _ad_mod.Adapter.build_fonts)
    case(f'{_name} declares its font paths for width measurement',
         _cls.font_paths is not _ad_mod.Adapter.font_paths)


print('== a container that owns layout keeps the stored line count exactly ==')
_pad = _wrap._pad_to_source_lines
case('a shorter translation is padded with trailing blanks',
     _pad('한 줄', 'a\nb\nc') == '한 줄\n\n')
case('an equal-length translation is untouched',
     _pad('가\n나\n다', 'a\nb\nc') == '가\n나\n다')
case('a longer translation is left alone for the refusal to catch',
     _pad('가\n나\n다\n라', 'a\nb') == '가\n나\n다\n라')
case('padding is per page, not across the whole string',
     _pad('가<page>나', 'a\nb<page>c\nd\ne') == '가\n<page>나\n\n')
case('a page-count mismatch is not padded, it is returned for refusal',
     _pad('가<page>나<page>다', 'a\nb') == '가<page>나<page>다')
case('a single-line source needs no padding',
     _pad('가', 'a') == '가')


print('== the register the source declares is verified, not merely requested ==')
for _ja, _ko, _want, _why in [
        ('ご案内します。', '안내하겠습니다', None,
         'a polite source with a polite translation passes'),
        ('ご案内します。', '안내한다', 'plain',
         'a polite source translated plain is reported'),
        ('もう行くのだ。', '이제 간다', None,
         'a plain source with a plain translation passes'),
        ('もう行くのだ。', '이제 갑니다', 'polite',
         'a plain source translated polite is reported'),
        ('アアア', '아아아', None,
         'a source that declares nothing is never reported'),
        ('ご案内します。', '안내해요', None,
         'the informal polite -해요 counts as polite'),
        ('ご案内します。', '이쪽으로 오세요', None,
         'an imperative -세요 counts as polite'),
        ('もう行くのだ。', '이제 가자', None,
         'a propositive is plain'),
        ('ご案内します。', '이쪽', None,
         'a one-word fragment cannot carry a speech level and is not reported'),
]:
    _d = _reg.divergence(_ja, _ko)
    case(_why, (_d is None) if _want is None
         else (_d is not None and _d.endswith(_want)))
case('a -ㅂ니다 form on any stem reads as polite, not just 습니다',
     _reg.of_korean('갑니다') == _reg.POLITE
     and _reg.of_korean('봅니다') == _reg.POLITE)
case('a plain declarative reads as plain', _reg.of_korean('간다') == _reg.PLAIN)
# -니까 is the tail of the polite question -ㅂ니까 AND an everyday plain connective.
# Measured: every one of the 24 rows it wrongly marked polite was 그러니까 / 없으니까 /
# 테니까 / 다니까 - 'because', not a question. The polite reading is the one whose
# preceding syllable ends in ㅂ.
for _s, _want, _why in [
        ('그러니까 가자', _reg.PLAIN, 'the causal 그러니까 is not polite'),
        ('없다니까!', _reg.PLAIN, 'nor the emphatic -다니까'),
        ('좋습니까', _reg.POLITE, 'but -습니까 is'),
        ('정말입니까', _reg.POLITE, 'and -입니까 is'),
        ('가십니까', _reg.POLITE, 'and -십니까 is'),
        ('어디 갔죠', _reg.POLITE, 'the -죠 contraction is polite'),
]:
    case(_why, _reg.of_korean(_s) == _want)
case('an empty translation carries no speech level',
     _reg.of_korean('') is None)
case('a noun fragment carries no speech level, so it is never a violation',
     _reg.of_korean('왕의 검') is None
     and _reg.divergence('ご案内します。', '왕의 검') is None)
case('a line ending mid-sentence carries no speech level',
     _reg.of_korean('그리고 그때') is None)
case('a markup-only tail does not hide a real plain ending',
     _reg.of_korean('간다<wait=0.5>') == _reg.PLAIN)
case('a real plain ending against a polite source is still reported',
     _reg.divergence('ご案内します。', '안내한다') is not None)
case('mixed target evidence does not invent a whole-record register',
     _reg.of_korean('이쪽으로 오세요. 이제 간다') is None
     and _reg.divergence('もう行くのだ。', '이쪽으로 오세요. 이제 간다') is None)
case('numeric spans are not mistaken for copied Latin words',
     tr.copied_spans('10 10 1000', '10 10 1000') == [])
case('an embedded polite marker without a final ending is undecided',
     _reg.of_korean('이쪽으로 오세요. 그리고 그때') is None)

print('== the QA repair pass reads the verdict file it actually has ==')
from hanpatch import run as _runmod
_qf = {'aaaa': [{'d': 'defect', 'a': 2, 'f': 3, 'en': 'src1', 'ko': 'ko1', 'r': '오역'},
                {'d': 'pass', 'a': 5, 'f': 5, 'en': 'src1', 'ko': 'ko1', 'r': ''}],
       'bbbb': [{'d': 'defect', 'a': 2, 'f': 2, 'en': 'src2', 'ko': 'stale', 'r': '비문'}]}
_qr = _runmod.qa_reasons(_qf, {'src1': 'ko1', 'src2': 'ko2'}, 4)
case('a flagged pair is keyed by source, not by the pair hash',
     list(_qr) == ['src1'] and _qr['src1'] == ['오역'])
case('a verdict about a value no longer shipped is not repair work',
     'src2' not in _qr)
case('a low-scoring pass is still repair work',
     list(_runmod.qa_reasons({'c': [{'d': 'pass', 'a': 3, 'f': 5,
                                     'en': 'src1', 'ko': 'ko1', 'r': 'x'}]},
                             {'src1': 'ko1'}, 4)) == ['src1'])
# The judged value is the SEALED one, so freshness is decided against the manifest.
case('shipped values come from the manifest, not the raw memory',
     _runmod.shipped_values({'f': [{'en': 'src1', 'key': 'k1'}]},
                            {'f/k1': 'sealed'}) == {'src1': 'sealed'})
case('a source with no manifest entry is not shippable and carries no verdict',
     _runmod.shipped_values({'f': [{'en': 'src1', 'key': 'k1'}]}, {}) == {})

print('== the judge panel is scaled by identity, not by spend ==')
case('every Codex account on the machine is a judge identity',
     _qamod.codex_judges()
     == [f'codex{a}:{_qamod.CODEX_MODEL}' for a in _prov.codex_accounts()])
_LIVE_GATEWAY = [s for s in _qamod.GATEWAY_JUDGES
                 if s not in _qamod.REVOKED_GATEWAY_LANES]
case('the runtime panel leads with the flat-rate gateway models',
     _qamod.active_judges()[:len(_LIVE_GATEWAY)] == _LIVE_GATEWAY)
case('a metered lane is never in the preferred runtime panel',
     not any(s.startswith('deepseek:') for s in _qamod.active_judges()))
case('the preferred panel offers at least three independent models',
     len({_qamod.lane_model(s) for s in _qamod.active_judges()})
     >= qagate.REQUIRED_JUDGES + 1)
case('two accounts of one gateway model are one judge identity',
     _qamod.lane_model('agy:gemini-3-pro') == _qamod.lane_model('agy:gemini-3-pro-biz'))
case('a lane that recorded a verdict stays an accepted identity',
     set(_qamod.LEGACY_JUDGES) <= set(_qamod.JUDGES)
     and set(_qamod.codex_judges()) <= set(_qamod.JUDGES))
case('the panel needs one more lane than the release rule requires',
     qagate.REQUIRED_JUDGES + 1 <= len(_qamod.JUDGES))
class _Lane:
    def __init__(self, lane_id, live=True):
        self.id = lane_id
        self._live = live

    def chat(self, *a, **k):
        if not self._live:
            raise RuntimeError('usage limit')
        return '{"0":"ok"}'


case('a lane that refuses the probe is not counted as a judge',
     not _qamod.alive(_Lane('dead:m1', live=False))
     and _qamod.alive(_Lane('live:m1')))
# Judge identity is the MODEL. Two accounts of one model are one opinion, so a panel of
# three same-model accounts cannot satisfy a rule about independent models.
case('accounts and endpoints of one model collapse to one identity',
     _qamod.lane_model('codex1:gpt-5.6-luna') == _qamod.lane_model('codex2:gpt-5.6-luna')
     and (_qamod.lane_model('deepseek:deepseek-v4-pro')
          == _qamod.lane_model('nimproxy:deepseek-ai/deepseek-v4-pro'))
     and _qamod.lane_model('codex1:gpt-5.6-luna') != _qamod.lane_model('groq:gpt-oss-120b'))
_lp_made = {'acctA:luna': _Lane('acctA:luna'), 'acctB:luna': _Lane('acctB:luna'),
            'acctC:luna': _Lane('acctC:luna', live=False),
            'free:qwen3': _Lane('free:qwen3'), 'free:oss120b': _Lane('free:oss120b')}
_lp_prev = (_qamod.active_judges, _qamod.LEGACY_JUDGES, _prov.make)
try:
    _qamod.active_judges = lambda: ['acctA:luna', 'acctB:luna', 'acctC:luna']
    _qamod.LEGACY_JUDGES = ['free:qwen3', 'free:oss120b']
    _prov.make = lambda s, **k: _lp_made.get(s)
    _lp_pool = [p.id for p in _qamod.live_panel(3)]
    case('a same-model panel is widened until three distinct models answer',
         _lp_pool == ['acctA:luna', 'acctB:luna', 'free:qwen3', 'free:oss120b'])
    case('one model is one judge no matter how many accounts answer',
         [p.id for p in _qamod.live_panel(1)] == ['acctA:luna', 'acctB:luna'])
finally:
    _qamod.active_judges, _qamod.LEGACY_JUDGES, _prov.make = _lp_prev
# Fallback order is a cost order: a metered lane must be the last resort, not the first.
case('a metered lane is the last resort, never the first fallback',
     _qamod.LEGACY_JUDGES[-1].startswith('deepseek:')
     and not _qamod.LEGACY_JUDGES[0].startswith('deepseek:'))

print('== the script book stays openable on a full-size corpus ==')
from hanpatch import scriptbook as _sb
case('a family-shaped section id is safe in a file name and a fragment',
     _sb.slug('#100000') == '100000' and _sb.slug('ch1_0') == 'ch1_0')
case('an id with nothing safe in it still yields a usable name',
     _sb.slug('###') == 'sec')
_sb_src = {'fam': [{'key': 'k1', 'en': 'hello'}, {'key': 'k2', 'en': '   '}]}
_sb_secs = _sb.family_sections(_sb_src, {'fam/k1': '안녕'})
case('a title without the reference scene grammar still gets sections',
     list(_sb_secs) == ['fam'] and _sb_secs['fam']['rows'] == [('k1', 'hello', '안녕')])
case('a row with no sealed value is not in the book',
     _sb.family_sections(_sb_src, {}) == {})
case('the book prints the declared title, never another game\'s',
     _sb.book_name() == (config.prof('book_title_ko')
                         or config.cfg().get('title') or '한글화'))


print('== the system prompt follows the declared facts, not a frozen assumption ==')
_sp_prev = config.root()
_sp_root = tempfile.mkdtemp(prefix='hp-sysprompt-')
try:
    os.makedirs(os.path.join(_sp_root, 'profiles'))
    os.makedirs(os.path.join(_sp_root, 'work'))
    json.dump({'fam': []}, open(os.path.join(_sp_root, 'work', 'text_src.json'), 'w'))
    json.dump({'profile': 'profiles/p.json'},
              open(os.path.join(_sp_root, config.PROJECT_FILE), 'w'))

    def _prompt(prof):
        json.dump(prof, open(os.path.join(_sp_root, 'profiles', 'p.json'), 'w'))
        config.set_root(_sp_root)
        return tr.SYSTEM_PROMPT

    _narrow = _prompt({'source_lang': 'ja', 'target_lang': 'ko',
                       'register_default': 'plain', 'font_charset': 'ksx1001'})
    case('a KS-X-1001 title is still told to stay inside that set',
         'KS X 1001' in _narrow)
    _wide = _prompt({'source_lang': 'ja', 'target_lang': 'ko',
                     'register_default': 'plain', 'font_charset': 'hangul-all'})
    case('a full-coverage title is NOT told to avoid its own syllables',
         'KS X 1001' not in _wide)
    case('a japanese source gets an absolute rule about register',
         any(l.strip().startswith('8.') for l in _wide.splitlines()))
    _en = _prompt({'source_lang': 'en', 'target_lang': 'ko'})
    case('an english source gets no register rule, having no register markers',
         not any(l.strip().startswith('8.') for l in _en.splitlines()))
finally:
    config.set_root(_sp_prev)
    shutil.rmtree(_sp_root, ignore_errors=True)


print('== source-only markup: recognised, excluded from comparison, refused in output ==')
_so_prev = config.root()
_so_root = tempfile.mkdtemp(prefix='hp-sourceonly-')
try:
    os.makedirs(os.path.join(_so_root, 'profiles'))
    os.makedirs(os.path.join(_so_root, 'work', 'ko'))
    json.dump({'fam': []}, open(os.path.join(_so_root, 'work', 'text_src.json'), 'w'))
    json.dump({'profile': 'profiles/p.json'},
              open(os.path.join(_so_root, config.PROJECT_FILE), 'w'))
    _so_prof = {'adapter': 'dq7', 'source_lang': 'ja', 'target_lang': 'ko',
                'engine_wraps': False, 'register_default': 'plain',
                'tag_pattern': r'<[^>\n]*>|\{[A-Z0-9_]+\}',
                'source_only_pattern': r'\{[0-9]+[^}\n]*\}',
                'literal_delimiters': ['}'],
                'movable_tags': ['{HERO}'], 'control_tags': ['<NOTICE>']}

    json.dump(_so_prof, open(os.path.join(_so_root, 'profiles', 'p.json'), 'w'))
    config.set_root(_so_root)
    _en = '\u77f3{1\u3044\u3057}\u3092{HERO}\u304c\u62bc\u3059'
    case('the recogniser matches a source-only token, so it is not a stray delimiter',
         tr.dq7_delimiter_problems(_en) == [])
    case('a measured literal delimiter is accepted',
         tr.dq7_delimiter_problems('{HERO}}') == [])

    case('a source-only token is excluded from the tag multiset',
         tr.tags(_en) == ['{HERO}'])
    case('a source-only token is excluded from the tag skeleton',
         tr.tag_skeleton(_en) == ['*'])
    case('the batch normalizer removes a source-only wrapper before validation',
         tr.strip_source_only('\ub3cc{2\uc871\uc7a5}') == '\ub3cc')
    case('source-only wrappers are removed from the model prompt',
         '{1' not in tr.build_prompt([{'en': _en}], {}, 'default', ()))
    case('a declared movable tag still counts',
         tr.tags('{HERO}\uac00') == ['{HERO}'])
    _, _so_probs = tr.check(_en, '\ub3cc{1\ub3cc}\ub97c {HERO}\uac00 \ubc00\ub2e4', {}, 'plain')
    case('a source-only token surviving in the target is rejected',
         any('source-only markup' in p for p in _so_probs))

    _so = config.source_only_re()
    case('the source-only pattern matches source and translated wrapper shapes',
         all(_so.fullmatch(t) for t in ('{1\u3044}', '{2\u307e\u3082\u306e}',
                                        '{7\u304b\u307f}', '{2\uc871\uc7a5}')))

    case('it does not match a declared substitution tag',
         not _so.fullmatch('{HERO}'))
    case('a declared title gets source-only checking',
         config.source_only_re() is not None)
    _p2 = dict(_so_prof); _p2.pop('source_only_pattern')
    json.dump(_p2, open(os.path.join(_so_root, 'profiles', 'p.json'), 'w'))
    config.set_root(_so_root)
    case('an absent declaration means none, not match-everything',
         config.source_only_re() is None)
finally:
    config.set_root(_so_prev)
    shutil.rmtree(_so_root, ignore_errors=True)

print()
print(f'{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped')
if SKIP and not HAVE_CORPUS:
    print('  (set HANPATCH_PROJECT to a project with translated data to run '
          'the corpus cases)')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
sys.exit(1 if FAIL else 0)

"""Adversarial regression tests for the validation gates.

Run:  python3 tests/test_gates.py
      HANPATCH_PROJECT=/path/to/project python3 tests/test_gates.py

Every case is a concrete attack that once slipped through.  Cases that only
exercise validation logic run anywhere; cases that audit a real corpus are
skipped with a printed notice when no project with translated data is given.
"""
import json
import os
import subprocess
import sys
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
from hanpatch import tm  # noqa: E402
from hanpatch import translate as tr  # noqa: E402
from hanpatch import wrap  # noqa: E402

HAVE_CORPUS = (os.path.exists(config.src_path())
               and os.path.exists(config.out('manifest.json'))
               and os.path.exists(config.out('qa.json')))
# line measurement needs a real font; without one the layout cases cannot run
HAVE_FONT = any(os.path.exists(config.p(x))
                for x in (list(config.prof('font_out'))
                          + list(config.prof('font_src'))))

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
_pipe = open(os.path.join(ROOT, 'hanpatch/pipeline.py')).read()
case('the packer requires the approved manifest digest',
     'approve(' in _pipe and 'SKIP_GATE' not in _pipe)
import ast as _ast  # noqa: E402
_gatefn = next(n for n in _ast.parse(_pipe).body
               if isinstance(n, _ast.FunctionDef) and n.name == 'gates')
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
if HAVE_CORPUS:
    case('glyph coverage comes from the built fonts',
         tr._in_font('가') and not tr._in_font('\u4e00'))
else:
    skip('glyph coverage comes from the built fonts (needs built fonts)')

print()
print(f'{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped')
if SKIP and not HAVE_CORPUS:
    print('  (set HANPATCH_PROJECT to a project with translated data to run '
          'the corpus cases)')
if FAIL:
    for f in FAIL:
        print('  FAILED:', f)
sys.exit(1 if FAIL else 0)

"""A particle after a run-time placeholder cannot be guessed.

`josa.after_tags` exists to stop exactly one defect: a particle written to agree
with a value the row does not know until it is drawn. It reads its token list
from `_tag_alternation()`, and that read used to consult only `movable_tags` -
the icon glyphs the injector may relocate. A title whose substitutions are printf
conversions therefore declared no tags at all, the alternation compiled to None,
and the check returned every string untouched while reporting the corpus clean.

Measured on Classic Dungeon X2, 2026-08-26: 21 rows shipped a guessed single-form
particle and exactly ONE row carried a both-forms rendering. On screen,
`%s를 생성했습니다!` drew `글렌를` for a name the player types.

Run: python3 tests/test_josa_runtime_tokens.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hanpatch import config, josa  # noqa: E402

FAIL = []


def case(name, ok):
    print(('  ok   ' if ok else '  FAIL ') + name)
    if not ok:
        FAIL.append(name)


def project(**profile):
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, 'profiles'), exist_ok=True)
    base = {'movable_tags': [], 'control_tags': []}
    base.update(profile)
    with open(os.path.join(root, 'profiles', 'p.json'), 'w', encoding='utf-8') as fh:
        json.dump(base, fh, ensure_ascii=False)
    with open(os.path.join(root, 'hanpatch.json'), 'w', encoding='utf-8') as fh:
        json.dump({'profile': 'profiles/p.json'}, fh)
    config.set_root(root)
    return root


print('a placeholder is a run-time token, so the particle after it must be both-forms')

# The defect: printf conversions declared nowhere, so the check sees no tags.
project(movable_tags=[])
case('with no tokens declared the alternation is empty',
     josa._tag_alternation() is None)
case('and a guessed particle passes through untouched - the defect',
     josa.after_tags('%s를 생성했습니다!')[0] == '%s를 생성했습니다!')

# The fix: the profile may declare tokens whose value is substituted at run time.
project(runtime_tokens=['%s', '%d'])
case('a declared run-time token reaches the alternation',
     '%s' in (josa._tag_alternation() or ''))

for written, want in (
    ('%s를 생성했습니다!', '%s을(를) 생성했습니다!'),
    ('%s는 쓰러졌다!!', '%s은(는) 쓰러졌다!!'),
    ('%s가 서브 캐릭터입니다', '%s이(가) 서브 캐릭터입니다'),
    ('적 LV가 %s로 내려갔다!!', '적 LV가 %s(으)로 내려갔다!!'),
):
    got, problems = josa.after_tags(written)
    case('%r -> %r' % (written, want), got == want and not problems)

# Two placeholders in one row are both decided, not just the first.
got, _ = josa.after_tags('%s는 레벨이 %d로 올랐다!')
case('every placeholder in the row is decided',
     got == '%s은(는) 레벨이 %d(으)로 올랐다!')

# Already-correct rows are a fixed point: a second pass must not double-wrap.
once, _ = josa.after_tags('%s를 생성했습니다!')
twice, _ = josa.after_tags(once)
case('the correction is idempotent', once == twice)

# movable_tags must keep working - the fix adds a source, it does not replace one.
project(movable_tags=['{HERO}'])
case('an icon/substitution tag still resolves from movable_tags',
     josa.after_tags('{HERO}는 갔다')[0] == '{HERO}은(는) 갔다')

project(movable_tags=['{HERO}'], runtime_tokens=['%s'])
case('both sources are read together',
     josa.after_tags('{HERO}는 %s를 들었다')[0] == '{HERO}은(는) %s을(를) 들었다')

# A particle with no readable both-forms shape must be REFUSED, not invented:
# `(이)었다` would ship "아이라었다". This is the module's documented third outcome.
got, problems = josa.after_tags('%s였다')
case('a particle with no both-forms rendering is refused, not invented',
     got == '%s였다' and any('reword' in p for p in problems))

if FAIL:
    print()
    for f in FAIL:
        print('  FAILED:', f)
sys.exit(1 if FAIL else 0)

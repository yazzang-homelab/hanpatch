"""Rule-based Korean generation for the templated ability/item description text."""
import re

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'mtl'))
from hanpatch import josa
from hanpatch import tm

ELEM = {'': '무속성', 'AIR ': '풍속성', 'EARTH ': '토속성', 'LIGHTNING ': '뇌속성',
        'WATER ': '수속성', 'FIRE ': '화속성', 'ICE ': '빙속성', 'LIGHT ': '광속성',
        'DARK ': '암속성'}
RANGE = {'': '', 'MELEE ': '직접 ', 'RANGED ': '원거리 '}
TARGET = {
    'a SINGLE TARGET': '단일 대상',
    'MULTIPLE TARGETS': '다수 대상',
    'the CASTER': '시전자',
    'a SINGLE SKELETON': '스켈레톤 단일 대상',
    'MULTIPLE UNDEAD': '언데드 다수 대상',
    'GOBLINS': '고블린',
    'MINOTAURS': '미노타우로스',
    'SKELETONS': '스켈레톤',
}
STATUS = {
    'SLEEP': '수면', 'ASLEEP': '수면', 'PARALYZED': '마비', 'POISON': '독',
    'POISONED': '독', 'ENVENOMED': '맹독', 'SILENCED': '침묵', 'SLOWED': '슬로우',
    'TERRIFIED': '공포', 'PETRIFIED': '석화', 'REVEALED': '분석 중',
    'UNRAISEABLE': '부활 금지', 'HASTENED': '퀵', 'REGENERATING HP': 'HP 회복',
    'DICE LURE': '획득 주사위 증가', 'DICE LURE II': '획득 주사위 증가 II',
    'ENHANCED ATTACK': '직접 공격력 상승', 'ENHANCED MAGIC': '마법 공격력 상승',
    'ENHANCED DEFENSE': '직접 방어력 상승', 'ENHANCED RESISTANCE': '마법 방어력 상승',
    'ENHANCED ACCURACY': '명중률 상승', 'ENHANCED AVOIDANCE': '회피율 상승',
    'IMPAIRED ATTACK': '직접 공격력 감소', 'IMPAIRED MAGIC': '마법 공격력 감소',
    'IMPAIRED DEFENSE': '직접 방어력 감소', 'IMPAIRED RESISTANCE': '마법 방어력 감소',
    'IMPAIRED ACCURACY': '명중률 감소', 'IMPAIRED AVOIDANCE': '회피율 감소',
    'PHYSICAL IMMUNITY': '직접 효과 무효', 'MAGIC IMMUNITY': '마법 효과 무효',
    'ALL BUFFS': '유익한 상태', 'ALL DEBUFFS': '해로운 상태',
}
STATUS_RE = re.compile(r'^(WEAPON|MAGIC) (ACCURACY 100%|DAMAGE \+\d+%)$')


def status(s):
    if s in STATUS:
        return STATUS[s]
    m = re.match(r'^WEAPON ACCURACY 100%$', s)
    if m:
        return '무기 공격 100% 명중'
    if s == 'MAGIC ACCURACY 100%':
        return '마법 100% 명중'
    m = re.match(r'^WEAPON DAMAGE \+(\d+)%$', s)
    if m:
        return f'무기 피해 {m.group(1)}% 상승'
    m = re.match(r'^MAGIC DAMAGE \+(\d+)%$', s)
    if m:
        return f'마법 피해 {m.group(1)}% 상승'
    m = re.match(r'^MP REGENERATION \+(\d+)%$', s)
    if m:
        return f'MP 회복 {m.group(1)}% 상승'
    return None


ST = r'(?:a SINGLE TARGET|MULTIPLE TARGETS|the CASTER|a SINGLE SKELETON|MULTIPLE UNDEAD|GOBLINS|MINOTAURS|SKELETONS)'
EL = r'(?:AIR |EARTH |LIGHTNING |WATER |FIRE |ICE |LIGHT |DARK |)'
RG = r'(?:MELEE |RANGED |)'
STAT = r'[A-Z][A-Z \+%0-9]*[A-Z%0-9]'


def _rules():
    return [
        (rf'^Deals {RG}({EL})DAMAGE to ({ST})$',
         lambda m: f'{RANGE[_rg(m.group(0))]}{ELEM[m.group(1)]} 피해를 준다. {TARGET[m.group(2)]}.'),
    ]


def _rg(s):
    for k in ('MELEE ', 'RANGED '):
        if f'Deals {k}' in s or f'deals {k}' in s:
            return k
    return ''


def tr_sentence(s):
    s = s.strip()
    if not s:
        return ''
    pats = [
        (rf'^Deals ({RG})({EL})DAMAGE to ({ST})$',
         lambda m: f'{RANGE[m.group(1)]}{ELEM[m.group(2)]} 피해를 준다. {TARGET[m.group(3)]}.'),
        (rf'^Deals ({RG})({EL})DAMAGE and STEALS HP from ({ST})$',
         lambda m: f'{RANGE[m.group(1)]}{ELEM[m.group(2)]} 피해를 주면서 대상의 HP를 흡수한다. {TARGET[m.group(3)]}.'),
        (rf'^Deals ({RG})({EL})DAMAGE and DRAINS MP from ({ST})$',
         lambda m: f'{RANGE[m.group(1)]}{ELEM[m.group(2)]} 피해를 주면서 대상의 MP를 흡수한다. {TARGET[m.group(3)]}.'),
        (rf'^Deals ({RG})({EL})DAMAGE and has a chance to DRAIN MP from ({ST})$',
         lambda m: f'{RANGE[m.group(1)]}{ELEM[m.group(2)]} 피해를 주고, 일정 확률로 대상의 MP를 흡수한다. {TARGET[m.group(3)]}.'),
        (rf'^Deals ({RG})({EL})DAMAGE and has a chance to inflict ({STAT}) on ({ST})$',
         lambda m: f'{RANGE[m.group(1)]}{ELEM[m.group(2)]} 피해를 주고, 일정 확률로 「{status(m.group(3))}」 상태로 만든다. {TARGET[m.group(4)]}.'),
        (rf'^Deals ({RG})({EL})DAMAGE to ({ST}) and grants ({STAT}) to ({ST})$',
         lambda m: f'{RANGE[m.group(1)]}{ELEM[m.group(2)]} 피해를 준다({TARGET[m.group(3)]}). 또한 {TARGET[m.group(5)]}를 「{status(m.group(4))}」 상태로 만든다.'),
        (rf'^Deals ({RG})({EL})DAMAGE and STEALS HP from ({ST})$',
         lambda m: f'{RANGE[m.group(1)]}{ELEM[m.group(2)]} 피해를 주면서 HP를 흡수한다. {TARGET[m.group(3)]}.'),
        (rf'^STEALS MP from ({ST})$',
         lambda m: f'대상의 MP를 흡수한다. {TARGET[m.group(1)]}.'),
        (rf'^DRAINS MP from ({ST})$',
         lambda m: f'대상의 MP를 흡수한다. {TARGET[m.group(1)]}.'),
        (rf'^Also has a chance to inflict ({STAT}) on ({ST})$',
         lambda m: f'또한 일정 확률로 {TARGET[m.group(2)]}를 「{status(m.group(1))}」 상태로 만든다.'),
        (rf'^Also has a chance to DRAIN MP from ({ST})$',
         lambda m: f'또한 일정 확률로 {TARGET[m.group(1)]}의 MP를 흡수한다.'),
        (rf'^Inflicts ({STAT}) on ({ST})$',
         lambda m: f'「{status(m.group(1))}」 상태로 만든다. {TARGET[m.group(2)]}.'),
        (rf'^Grants ({STAT}) to the CASTER$',
         lambda m: f'시전자에게 「{status(m.group(1))}」 상태를 부여한다.'),
        (rf'^Grants ({STAT}) to ({ST})$',
         lambda m: f'「{status(m.group(1))}」 상태로 만든다. {TARGET[m.group(2)]}.'),
        (rf'^Removes ({STAT}) and ({STAT}) from ({ST})$',
         lambda m: f'「{status(m.group(1))}」「{status(m.group(2))}」 상태를 회복한다. {TARGET[m.group(3)]}.'),
        (rf'^Removes (ALL BUFFS|ALL DEBUFFS) from ({ST})$',
         lambda m: f'{status(m.group(1))}를 모두 해제한다. {TARGET[m.group(2)]}.'),
        (rf'^Removes ({STAT}) from ({ST})$',
         lambda m: f'「{status(m.group(1))}」 상태를 회복한다. {TARGET[m.group(2)]}.'),
        (rf'^Restores (\d+) (HP|MP) to ({ST})$',
         lambda m: f'{m.group(2)}를 {m.group(1)} 회복한다. {TARGET[m.group(3)]}.'),
        (rf'^Restores ALL (HP|MP) to ({ST})$',
         lambda m: f'{m.group(1)}를 완전히 회복한다. {TARGET[m.group(2)]}.'),
        (rf'^Restores (HP|MP) to ({ST})$',
         lambda m: f'{m.group(1)}를 회복한다. {TARGET[m.group(2)]}.'),
        (rf'^Restores (\d+) (HP|MP) \+ DICE ROLL to ({ST})$',
         lambda m: f'{m.group(2)}를 {m.group(1)} + 주사위 굴림만큼 회복한다. {TARGET[m.group(3)]}.'),
        (r'^Restores (\d+) (HP|MP) \+ DICE ROLL$',
         lambda m: f'{m.group(2)}를 {m.group(1)} + 주사위 굴림만큼 회복한다.'),
        (rf'^RAISES ({ST}) from the DEAD$',
         lambda m: f'「전투불능」 상태를 회복한다. {TARGET[m.group(1)]}.'),
        (rf'^Raises ({ST}) from the DEAD$',
         lambda m: f'「전투불능」 상태를 회복한다. {TARGET[m.group(1)]}.'),
        (r'^Amount determined by DICE ROLL$', lambda m: '수치는 주사위 굴림으로 결정된다.'),
        (r'^Consumed when used$', lambda m: '사용하면 소비된다.'),
        (r'^Weapon skills can only be used while the weapon is equipped$',
         lambda m: '무기 기술은 해당 무기를 장비한 동안에만 사용할 수 있다.'),
        (rf'^instantly KILLS ({ST})$', lambda m: f'즉사시킨다. {TARGET[m.group(1)]}.'),
        (r'^Raises MAX MP of the CASTER for the duration of battle$',
         lambda m: '전투가 끝날 때까지 시전자의 최대 MP를 올린다.'),
        (rf'^Converts the MP of ({ST}) into HP$',
         lambda m: f'{TARGET[m.group(1)]}의 MP를 HP로 전환한다.'),
        (r'^Removes the party from battle$', lambda m: '파티를 전투에서 이탈시킨다.'),
        (r'^Allows party to ESCAPE from battle$', lambda m: '전투에서 후퇴할 수 있다.'),
        (r'^Success determined by DICE ROLL$', lambda m: '성공 여부는 주사위 굴림으로 결정된다.'),
        (r'^Can only be used in random encounters$', lambda m: '조우 전투에서만 사용할 수 있다.'),
        (rf'^Grants ({STAT}) and ({STAT})$',
         lambda m: f'「{status(m.group(1))}」「{status(m.group(2))}」 상태로 만든다.'),
        (rf'^Grants ({STAT}), ({STAT}), and ({STAT})$',
         lambda m: f'「{status(m.group(1))}」「{status(m.group(2))}」「{status(m.group(3))}」 상태로 만든다.'),
        (rf'^Inflicts ({STAT}) and grants ({STAT}) and ({STAT})$',
         lambda m: f'「{status(m.group(1))}」 상태로 만들고 「{status(m.group(2))}」「{status(m.group(3))}」 상태를 부여한다.'),
        (r'^An alchemical stone used to TRANSMUTE WEAPONS$',
         lambda m: '무기 융합에 쓰이는 연금술의 신비한 돌.'),
        (r'^Only weapons of the same class can be transmuted$',
         lambda m: '융합에는 같은 종류의 무기만 사용할 수 있다.'),
    ]
    for rx, fn in pats:
        m = re.match(rx + r'$', s)
        if m:
            try:
                return fn(m)
            except (KeyError, TypeError):
                return None
    return None


def split_sentences(s):
    parts = re.split(r'(?<=\.)\s+', s.strip())
    return [p[:-1] if p.endswith('.') else p for p in parts if p.strip()]


def translate(s, tmdb):
    """Return korean string or None if any part is untranslatable."""
    s = s.strip()
    prefix = ''
    # "<SPELLNAME> spellbook." form
    m = re.match(r'^([A-Z][A-Z\'\- ]+?) spellbook\.\s*(.*)$', s)
    if m:
        name = m.group(1).strip()
        ko = _spell_ko(name, tmdb)
        if not ko:
            return None
        prefix = f'「{ko}」의 주문서. '
        s = m.group(2)
    else:
        # "<Spell Name> deals/inflicts/removes/grants/restores/STEALS ..."
        m = re.match(r"^([A-Z][A-Za-z'\- ]*?) (deals|inflicts|removes|grants|restores|STEALS|RAISES|instantly) (.*)$", s)
        if m:
            ko = _spell_ko(m.group(1).strip(), tmdb)
            if ko:
                prefix = f'「{ko}」 '
                verb = m.group(2)
                verb = {'deals': 'Deals', 'inflicts': 'Inflicts', 'removes': 'Removes',
                        'grants': 'Grants', 'restores': 'Restores'}.get(verb, verb)
                s = f'{verb} {m.group(3)}'
    out = []
    for sent in split_sentences(s):
        t = tr_sentence(sent)
        if t is None:
            return None
        out.append(t)
    res = (prefix + ' '.join(out)).strip()
    res, _ = josa.fix_after(res, set(TARGET.values()))
    return res


def _spell_ko(name, tmdb):
    if name in tmdb:
        return tmdb[name]
    v = tm.lookup(tmdb, name)
    if v:
        return v
    # uppercase form -> find case-insensitive match
    for k in tmdb:
        if k.upper() == name.upper():
            return tmdb[k]
    m = re.match(r'^(.*) (II|III)$', name)
    if m:
        base = _spell_ko(m.group(1), tmdb)
        if base:
            return base + ' ' + m.group(2)
    return None

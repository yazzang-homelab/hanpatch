"""Independent semantic QA: a different model judges every prose translation.

Structural gates cannot see meaning. This pass sends each (source, translation)
pair to a judge model that never produced the translation, and records adequacy
and fluency scores plus a reason. Anything below the threshold is queued for
retranslation, so "green structural audit" is backed by an actual meaning check.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor


from hanpatch import glossary
from hanpatch import providers
from hanpatch import tm
from hanpatch import translate

from hanpatch import config

def QA_PATH():
    return config.out('qa.json')
def MANIFEST():
    return config.out('manifest.json')


def pair_key(en, ko):
    """Verdict identity: the exact source/translation pair being shipped."""
    return hashlib.sha1((en + '\x00' + ko).encode()).hexdigest()[:16]


def producers():
    """en -> provider id that produced the translation, when recorded."""
    import glob as _glob
    out = {}
    for p in _glob.glob(config.out('prov_*.json')):
        try:
            out.update(json.load(open(p)))
        except (OSError, ValueError):
            continue
    return out
JUDGES = ['nimproxy:deepseek-ai/deepseek-v4-pro',
          'opencode:nemotron-3-ultra-free',
          'nimproxy:qwen/qwen3-next-80b-a3b-instruct',
          'groq:openai/gpt-oss-120b',
          'nimproxy:nvidia/nemotron-3-super-120b-a12b',
          'nimproxy:meta/llama-3.3-70b-instruct',
          'opencode:mimo-v2.5-free',
          'openrouter:nvidia/nemotron-3-ultra-550b-a55b:free']

SYSTEM = """당신은 영어→한국어 게임 로컬라이제이션 품질 심사관이다. 번역가가 아니라 검수자다.
각 항목의 영어 원문과 한국어 번역을 비교해 평가한다.

[이 프로젝트의 확정된 표기 정책 - 위반이 아니므로 감점하지 말 것]
0. 산문(내레이션·대사)의 기준 원문은 영어판이다. 영어판이 일본어 원판과 내용·화자가 다를 때는
   영어판을 따른다. 반면 아래 1번의 고유명 표기는 일본어 원판을 따른다.
1. 이 게임은 일본 원작이다. 무기·방어구·아이템·마법·기술의 고유명은 영어판 명칭이 아니라
   일본어 원판 표기를 기준으로 음역한다. 예: Vigor Leaf(キュアリーフ)→"큐어 리프",
   Occator Spike(ヴァルカンパイク)→"발칸 파이크", Cudgel(ワンド)→"완드",
   Heaven Strike(ヘヴンリーブロウ)→"헤븐리 블로우", Mass Heal(ラージヒール)→"라지 힐".
   영어 명칭과 달라 보여도 오역이 아니다.
2. 능력·아이템 설명문은 의도적으로 짧은 고정 문형을 쓴다.
   "SINGLE TARGET"→"단일 대상", "MULTIPLE TARGETS"→"다수 대상",
   "Deals X DAMAGE"→"X속성 피해를 준다", 무속성/풍속성/토속성/뇌속성/수속성/화속성/빙속성/광속성/암속성.
   원문의 대문자 강조를 한국어에서 재현하지 않는 것은 정상이다.
3. 상태이상·버프 명칭은 아래 GLOSSARY의 표기를 사용한다. GLOSSARY와 일치하면 정확한 것이다.
4. 원문이 빈 문자열이거나 공백뿐인 항목, 개발용 더미 문자열은 a=5, f=5로 처리한다.
5. <player> <enemy> <item> <magic> <tech> <status> <damage> <gain> <num1> 등 꺾쇠 안의 토큰은
   실행 중에 이름·수치로 치환되는 자리표시자다. 번역하지 않고 그대로 두는 것이 정상이며,
   한국어 어순에 맞게 위치가 바뀌는 것도 정상이다. <br> <page> <key> <color=...> <lineheight=...>
   <wait=...> <script=...>는 서식·연출 태그이므로 내용이 없다고 감점하지 말 것.
5-1. jp_original 필드는 일본어 원판이다. 고유명 표기와 어감의 근거로 삼되, 산문의 내용·화자가
   영어판과 다르면 0번 정책에 따라 영어판을 기준으로 판단한다.
   영어판에 없고 jp_original에 있는 정보를 한국어가 담고 있어도 감점하지 말 것.
6. 전투 로그는 일본어 원판 관례를 따른다. 적이 피해를 받는 문장은 "<enemy>에게 N의 피해를 주었다!",
   아군이 받는 문장은 "<player>는 N의 피해를 받았다!"로 시점을 바꿔 쓴다. MP는 "축적되었다",
   턴 알림은 "공격 턴"이라고 쓴다. 이는 확정된 표기이므로 감점하지 말 것.

평가 기준:
- adequacy(정확성) 1~5: 원문의 의미가 빠짐/왜곡/오역 없이 전달되었는가. 다의어를 문맥에 맞게 옮겼는가.
- fluency(자연스러움) 1~5: 어색한 직역, 비문, 오타, 깨진 단어, 잘못된 조사·띄어쓰기가 없는가.
- reason: 문제가 있으면 한국어로 40자 이내로 구체적으로 적는다. 문제가 없으면 빈 문자열.
- d(판정) 세 가지 중 하나를 반드시 고른다:
  * "pass"   : 그대로 출시해도 되는 번역. 사소한 문체 취향 차이만 있는 경우도 pass다.
  * "defect" : 오타, 비문, 조사·어미 오류, 띄어쓰기 오류, 의미 왜곡, 누락, 문맥 불일치 등
               반드시 고쳐야 하는 결함이 하나라도 있는 경우.
  * "policy" : 번역 자체는 정확하지만 위 [표기 정책]과 심사관의 선호가 다른 경우.
  결함을 발견했다면 점수가 4점이어도 반드시 "defect"로 판정한다.

출력은 JSON 객체 하나만. 형식:
{"<id>": {"a": <adequacy>, "f": <fluency>, "d": "pass|defect|policy", "r": "<reason>"}}
마크업 태그(<...>)와 줄바꿈은 평가 대상이 아니므로 무시한다. 번역문을 다시 쓰지 말고 점수만 매긴다."""


def load():
    """pair_key -> list of verdict records (one per judge)."""
    if not os.path.exists(QA_PATH()):
        return {}
    doc = json.load(open(QA_PATH()))
    out = {}
    for k, v in doc.items():
        out[k] = v if isinstance(v, list) else [v]
    return out


def save(doc, lock):
    with lock:
        tmp = f'{QA_PATH()}.{os.getpid()}.tmp'
        json.dump(doc, open(tmp, 'w'), ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, QA_PATH())


def prompt(rows, gl=None):
    payload = {}
    for i, row in enumerate(rows):
        en, ko = row[0], row[1]
        jp = row[2] if len(row) > 2 else ''
        item = {'en': en.replace('\n', ' ').strip(),
                'ko': ko.replace('\n', ' ').strip()}
        if jp.strip():
            item['jp_original'] = jp.replace('\n', ' ').strip()
        payload[str(i)] = item
    head = ''
    if gl:
        head = ('[GLOSSARY - 이 표기가 사용되었다면 정확한 것이다]\n' +
                '\n'.join(f'- {k} => {v}' for k, v in gl.items()) + '\n\n')
    return (head + '아래 항목들을 평가해 같은 키로 결과를 반환하라:\n' +
            json.dumps(payload, ensure_ascii=False, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--families', default='all')
    ap.add_argument('--batch', type=int, default=8)
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--threshold', type=int, default=4)
    ap.add_argument('--judges', type=int, default=1,
                    help='number of distinct judges required per pair')
    args = ap.parse_args(argv)

    providers.load_dotenv()
    pool = [p for p in (providers.make(s) for s in JUDGES) if p]
    src = json.load(open(config.src_path()))
    doc = load()
    prov_of = producers()
    if not os.path.exists(MANIFEST()):
        raise SystemExit('run mtl/manifest.py first: QA judges the sealed values')
    man = json.load(open(MANIFEST()))['entries']
    by_key = {}
    for fam, items in src.items():
        for it in items:
            by_key[f'{fam}/{it["key"]}'] = (fam, it)
    rows = []
    seen = set()
    fams = sorted(src) if args.families == 'all' else args.families.split(',')
    for mkey, ko in sorted(man.items()):
        fam, it = by_key.get(mkey, (None, None))
        if it is None or fam not in fams:
            continue
        en = it['en']
        if tm.is_skip(en, it['key']) or not en.strip():
            en = it.get('jp', en)      # placeholder EN row: JP is the real source
        pk = pair_key(en, ko)
        have = {r.get('judge') for r in doc.get(pk, [])}
        if len(have) >= args.judges or pk in seen:
            continue
        seen.add(pk)
        rows.append((en, ko, it.get('jp', ''), pk,
                     prov_of.get(en, ''), tuple(have)))
    rows.sort(key=lambda r: (tuple(sorted(r[5])), r[4]))
    if args.limit:
        rows = rows[:args.limit]
    print(f'QA: {len(rows)} pairs to judge, pool={[p.id for p in pool]}', flush=True)

    batches = [rows[i:i + args.batch] for i in range(0, len(rows), args.batch)]
    lock = threading.Lock()
    counter = [0, 0]
    t0 = time.time()

    def work(idx_batch):
        i, batch = idx_batch
        pending = list(batch)
        for attempt in range(len(pool) + 1):
            batch = pending
            if not batch:
                return
            prov = pool[(i + attempt) % len(pool)]
            # never let the model that produced a translation judge it, and
            # never let the same judge score one pair twice
            if any(r[4] and r[4] == prov.id for r in batch):
                continue
            if any(prov.id in r[5] for r in batch):
                continue
            try:
                sub = glossary.relevant(glossary.load(), [r[0] for r in batch])
                raw = prov.chat(SYSTEM, prompt(batch, sub), temperature=0.0,
                                max_tokens=min(4000, 400 + 40 * len(batch)))
            except RuntimeError as e:
                print(f'    ! {e}'[:160], flush=True)
                continue
            obj = translate.parse_json(raw)
            if not isinstance(obj, dict):
                continue
            out = {}
            for k, (en, ko, _jp, pk, _pr, _have) in enumerate(batch):
                v = obj.get(str(k))
                if not isinstance(v, dict):
                    continue
                try:
                    a = int(v.get('a', 0))
                    f = int(v.get('f', 0))
                except (TypeError, ValueError):
                    continue
                d = str(v.get('d', '')).strip().lower()
                if d not in ('pass', 'defect', 'policy'):
                    # the structured contract is the whole point: never guess a
                    # disposition, leave the row unjudged so it is retried
                    continue
                out[pk] = {'a': a, 'f': f, 'd': d,
                           'r': str(v.get('r', ''))[:160],
                           'judge': prov.id, 'en': en, 'ko': ko}
            if out:
                judged = {r[3] for r in batch if r[3] in out}
                pending = [r for r in batch if r[3] not in judged]
                with lock:
                    for k, v in out.items():
                        prev = [r for r in doc.get(k, [])
                                if r.get('judge') != v['judge']]
                        doc[k] = prev + [v]
                    counter[0] += len(out)
                    counter[1] += sum(1 for v in out.values()
                                      if v['d'] != 'pass'
                                      or min(v['a'], v['f']) < args.threshold)
                save(doc, lock)
                print(f'  [{counter[0]}/{len(rows)}] flagged={counter[1]} '
                      f'{time.time() - t0:.0f}s', flush=True)
                if not pending:
                    return
                continue
        print(f'  batch {i} unjudged', flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, enumerate(batches)))

    flagged = {k: v for k, v in doc.items()
               if any(r['d'] != 'pass' or min(r['a'], r['f']) < args.threshold
                      for r in v)}
    json.dump(flagged, open(config.out('qa_flagged.json'), 'w'), ensure_ascii=False,
              indent=1, sort_keys=True)
    counts = {}
    for v in doc.values():
        counts[len({r.get('judge') for r in v})] = \
            counts.get(len({r.get('judge') for r in v}), 0) + 1
    print(f'pairs={len(doc)} judges-per-pair={dict(sorted(counts.items()))} '
          f'flagged={len(flagged)} -> work/ko/qa_flagged.json')


if __name__ == '__main__':
    main()

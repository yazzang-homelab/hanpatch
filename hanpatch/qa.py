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
            out.update(config.load_object(p, 'the provenance shard'))
        except (OSError, SystemExit):
            continue
    return out
# Panel identity set. A verdict recorded by any of these remains valid forever, so entries
# are never removed - only the RUNTIME pool below changes.
CODEX_MODEL = 'gpt-5.6-luna'
LEGACY_JUDGES = ['deepseek:deepseek-v4-pro',
                 'nimproxy:deepseek-ai/deepseek-v4-pro',
                 'opencode:nemotron-3-ultra-free',
                 'nimproxy:qwen/qwen3-next-80b-a3b-instruct',
                 'groq:openai/gpt-oss-120b',
                 'nimproxy:nvidia/nemotron-3-super-120b-a12b',
                 'nimproxy:meta/llama-3.3-70b-instruct',
                 'opencode:mimo-v2.5-free',
                 'openrouter:nvidia/nemotron-3-ultra-550b-a55b:free']


def codex_judges():
    return [f'codex{a}:{CODEX_MODEL}' for a in providers.codex_accounts()]


JUDGES = codex_judges() + LEGACY_JUDGES


def active_judges():
    """The lanes a panel run may call.

    Panel cost is corpus x panel size, not corpus: a per-token lane that is affordable for
    one translation pass is not affordable for judging every shipped pair at least twice.
    The Codex accounts are flat-rate and are independent identities, so the panel widens by
    adding an account rather than by spending more. Everything else stays in the identity
    set for verdicts already recorded, and is used only when no Codex account exists.
    """
    return codex_judges() or LEGACY_JUDGES

SYSTEM_TEMPLATE = """당신은 %(source_name)s→한국어 게임 로컬라이제이션 품질 심사관이다. 번역가가 아니라 검수자다.
각 항목의 %(source_name)s 원문과 한국어 번역을 비교해 평가한다.

%(judge_policy)s[판정 우선순위] 제목별 [표기 정책]은 표기와 관례 중 감점하지 않을 사항만 정하며, 실제 오역·누락·비문을 pass로 판정하도록 허용할 수 없고 반드시 defect로 판정한다.
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

NEUTRAL_POLICY = """[일반 심사 규칙 - 위반이 아니므로 감점하지 말 것]
원문이 빈 문자열이거나 공백뿐인 항목, 개발용 더미 문자열은 a=5, f=5로 처리한다.
<player> <enemy> <item> <magic> <tech> <status> <damage> <gain> <num1> 등 꺾쇠 안의 토큰은
실행 중에 이름·수치로 치환되는 자리표시자다. 번역하지 않고 그대로 두는 것이 정상이며,
한국어 어순에 맞게 위치가 바뀌는 것도 정상이다. <br> <page> <key> <color=...> <lineheight=...>
<wait=...> <script=...>는 서식·연출 태그이므로 내용이 없다고 감점하지 말 것."""


def system_prompt():
    """Render the source-aware judge instructions for the active title profile."""
    source_lang = config.source_lang()
    source_name = {'en': '영어', 'ja': '일본어'}.get(source_lang, source_lang)
    judge_policy = config.prof('judge_policy') or NEUTRAL_POLICY
    return SYSTEM_TEMPLATE % {
        'source_name': source_name,
        'judge_policy': f'{judge_policy}\n\n',
    }


def load():
    """pair_key -> list of verdict records (one per judge)."""
    if not os.path.exists(QA_PATH()):
        return {}
    doc = config.load_object(QA_PATH(), 'the QA verdict file')
    out = {}
    for k, v in doc.items():
        out[k] = v if isinstance(v, list) else [v]
    return out


def save(doc, lock):
    """Replace the verdict file atomically and durably.

    A panel run is hours of paid work held in one document, and it is rewritten after every
    batch. Renaming without flushing leaves the window where a crash loses verdicts that the
    log already reported as recorded.
    """
    with lock:
        tmp = f'{QA_PATH()}.{os.getpid()}.tmp'
        with open(tmp, 'w') as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, QA_PATH())

def hold_panel_lock():
    """Refuse to run a second panel against the same verdict file.

    Every batch rewrites the whole document from the process's own copy, so two panels do
    not merge - the later writer silently discards the other's verdicts, which are hours of
    paid work that the log already reported as recorded.
    """
    import fcntl
    path = QA_PATH() + '.lock'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, 'a+')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(f'another QA panel is already running (lock {path}); '
                         f'two panels overwrite each other\'s verdicts')
    return fh                                   # held for the process lifetime


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
    _panel_lock = hold_panel_lock()              # noqa: F841 - held until exit
    pool = [p for p in (providers.make(s) for s in active_judges()) if p]
    # A judge may not score its own output, so a panel of exactly REQUIRED_JUDGES starves
    # every pair whose producer is one of them. Refuse to run a pool that cannot satisfy
    # the release rule for such a pair instead of discovering it as unjudged batches hours
    # later.
    from hanpatch import qagate
    if len(pool) < qagate.REQUIRED_JUDGES + 1:
        raise SystemExit(
            f'judge pool too small: {len(pool)} lane(s) available, '
            f'{qagate.REQUIRED_JUDGES + 1} needed so a pair whose producer is a judge can '
            f'still reach {qagate.REQUIRED_JUDGES} distinct verdicts')
    src = config.load_object(config.src_path(), 'the extracted source')
    doc = load()
    prov_of = producers()
    if not os.path.exists(MANIFEST()):
        raise SystemExit('run mtl/manifest.py first: QA judges the sealed values')
    man = config.load_object(MANIFEST(), 'the sealed manifest')['entries']
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
            en = it.get('jp') or en      # placeholder EN row: JP is the real source
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
            # Never let the model that produced a translation judge it, and never let the
            # same judge score one pair twice. Both exclusions are per ROW: dropping the
            # whole batch when any one row is ineligible starves a small pool, because a
            # mixed batch then excludes every lane and the stragglers never reach the
            # required panel size no matter how many passes run.
            batch = [r for r in pending
                     if not (r[4] and r[4] == prov.id) and prov.id not in r[5]]
            if not batch:
                continue
            try:
                sub = glossary.relevant(glossary.load(), [r[0] for r in batch])
                with providers.gate_for(prov.id):
                    raw = prov.chat(system_prompt(), prompt(batch, sub), temperature=0.0,
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
                # Keep the rows this lane was not allowed to judge: `batch` is now the
                # eligible subset, so filtering it would silently drop them from the run.
                judged = {r[3] for r in batch if r[3] in out}
                pending = [r for r in pending if r[3] not in judged]
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

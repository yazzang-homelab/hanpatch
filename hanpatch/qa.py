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
# The gateway panel: DIFFERENT model families on the personal account. Measured round-trip on
# this box: 3-12s per call, versus 40-170s for the free rotators, and every lane here is
# flat-rate.
GATEWAY_JUDGES = ['agy:gemini-3-pro',
                  'agy:claude-sonnet-4.6',
                  'agy:gpt-oss-120b',
                  'agy:gemini-3-flash',
                  'agy:claude-opus-4.6']
# DELETED, not merely disabled. The `agy-c` (company) gateway served "Gemini 3.6 Flash (High)"
# for every request regardless of the model asked for, so `agy:gemini-3-pro-biz`,
# `agy:gpt-oss-120b-biz` and `agy:gemini-3-flash-biz` were one model wearing three names -
# the exact opposite of what a panel exists to provide - and they are what produced the
# 2026-08-03 metered bill. Deletion is safe rather than merely convenient because zero
# verdicts carry a `-biz` judge name: `qarelabel` had already rewritten all 39,236 of them to
# the model that actually answered. The names stay listed here as a denylist so they cannot be
# re-added by a copy-paste, and `test_gates` asserts they appear in no runtime or identity set.
REVOKED_GATEWAY_LANES = ['agy:gemini-3-pro-biz',
                         'agy:gpt-oss-120b-biz',
                         'agy:gemini-3-flash-biz']
# The model those relabelled verdicts name. It is a JUDGE IDENTITY, not a runtime lane:
# 39,236 verdicts carry it, and a name the gate does not know is rejected as `unknown judge`,
# which silently blocked 13,002 shipped pairs after the rename. It never enters
# `active_judges()` - the account it lived on is retired.
RETIRED_JUDGES = ['agy:gemini-3.6-flash']
# Fallback order is a COST order: free rotators first, metered last. When all three Codex
# accounts were momentarily parked, a paid-first list admitted `deepseek:deepseek-v4-pro`
# for an entire run - about 4900k tokens of judging that the free lanes would have done.
LEGACY_JUDGES = ['opencode:nemotron-3-ultra-free',
                 'nimproxy:deepseek-ai/deepseek-v4-pro',
                 'nimproxy:qwen/qwen3-next-80b-a3b-instruct',
                 'groq:openai/gpt-oss-120b',
                 'nimproxy:nvidia/nemotron-3-super-120b-a12b',
                 'nimproxy:meta/llama-3.3-70b-instruct',
                 'opencode:mimo-v2.5-free',
                 'openrouter:nvidia/nemotron-3-ultra-550b-a55b:free',
                 'deepseek:deepseek-v4-pro']


def codex_judges():
    return [f'codex{a}:{CODEX_MODEL}' for a in providers.codex_accounts()]


def lane_model(lane_id):
    """The MODEL behind a lane id: accounts and endpoints collapse, models do not.

    `codex1:gpt-5.6-luna` and `codex2:gpt-5.6-luna` are one model on two accounts;
    `deepseek:deepseek-v4-pro` and `nimproxy:deepseek-ai/deepseek-v4-pro` are one model on
    two endpoints. Independence is a property of the model, so every rule that says "a
    different judge" resolves to this, not to the lane.
    """
    if not lane_id:
        return ''
    lane, _, model = str(lane_id).partition(':')
    name = (model.split('/')[-1] or lane).strip()
    # A gateway that exposes the same model twice for two billing accounts spells the second
    # one with a suffix (`gemini-3-pro` / `gemini-3-pro-biz`). Those are one model on two
    # accounts: useful for throughput, worthless for independence, so the suffix collapses
    # here rather than reintroducing the account-as-judge bug under a new spelling.
    for suffix in ('-biz',):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


# The revoked lanes are subtracted from the identity set as well as from the runtime pool. That
# is only sound because no verdict names them; a lane that had recorded one would have to stay
# an accepted identity, or the gate would reject its own history as `unknown judge`.
JUDGES = [s for s in GATEWAY_JUDGES + codex_judges() + LEGACY_JUDGES + RETIRED_JUDGES
          if s not in REVOKED_GATEWAY_LANES]


def active_judges():
    """The lanes a panel run may call, in preference order.

    Panel cost is corpus x panel size, not corpus, so every preferred lane is flat-rate. The
    gateway lanes come first because they are the only set that offers several DIFFERENT
    models at a few seconds per call: a Codex-only panel is one model however many accounts
    answer, and the free rotators take 40-170s per call, which is hours per pass on a corpus
    this size. Codex follows as another model, and the rotators remain the last free resort.

    The `-biz` lanes are gone entirely, not filtered here: they answered as one model under
    three names, which is the opposite of what a panel is for, and they were metered. The
    subtraction below is a belt on the braces in case a name is re-added upstream.
    """
    return [s for s in GATEWAY_JUDGES if s not in REVOKED_GATEWAY_LANES] + codex_judges() + [
        s for s in LEGACY_JUDGES if not s.startswith('deepseek:')]

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


def alive(prov):
    """Whether the lane answers right now, not merely whether it is configured.

    A configured-but-exhausted account is the worst kind of judge: `make()` succeeds, the
    panel counts it, and the release rule then cannot be met for any pair the remaining
    lanes produced. The failure is silent - the run records no verdicts and the queue stops
    moving. Here that cost twelve hours: one Codex account hit its usage limit, two lanes
    stayed live, and every pair those two produced was structurally unreachable.
    """
    # Two attempts: a rotator that is parked for a few seconds is not an exhausted account,
    # and demoting a flat-rate lane on one transient refusal is what admitted a metered lane
    # for a whole run.
    for attempt in range(2):
        try:
            prov.chat('JSON만 반환한다.', '{"0":"ok"} 를 그대로 반환하라.',
                      temperature=0.0, max_tokens=64)
            return True
        except Exception:                        # noqa: BLE001 - any failure is "not live"
            if attempt == 0:
                time.sleep(5)
    return False


def live_panel(required):
    """Live lanes for a panel, widened past the declared preference only when forced.

    Flat-rate accounts come first because a panel scores the whole corpus at least
    `required` times. When too few of them answer, a metered lane is admitted rather than
    letting the queue stall, and the admission is printed - paying per token is a decision
    the operator must see, not one to discover in a bill.
    """
    pool = []
    have = set()
    for spec in active_judges():
        # Stop once the panel is independent AND has spare lanes for throughput. Probing the
        # whole preference list would spend a slow-rotator round trip on every run just to
        # discover lanes the panel does not need.
        if len(have) >= required and len(pool) >= 2 * required:
            break
        p = providers.make(spec)
        if p is None or not alive(p):
            continue
        pool.append(p)
        have.add(lane_model(p.id))
    if len(have) >= required:
        return pool
    # Counted in MODELS: several accounts of one model are one opinion, so a same-model panel
    # can never satisfy a rule about independent models however many accounts answer.
    for spec in LEGACY_JUDGES:
        if len(have) >= required:
            break
        p = providers.make(spec)
        if p is None or lane_model(p.id) in have or not alive(p):
            continue
        print(f'  + admitting {p.id}: only {len(have)} preferred model(s) answered, '
              f'{required} needed', flush=True)
        pool.append(p)
        have.add(lane_model(p.id))
    return pool


def main(argv=None):
    # `--batch` exists HERE and nowhere else. Copying this invocation to
    # `hanpatch translate` used to prefix-match it onto `--batch-chars`, capping
    # that run at eight source characters. Abbreviations are off so the mistake
    # is an error instead of a silent 57,621-call bill.
    ap = argparse.ArgumentParser(prog='hanpatch qa', allow_abbrev=False)
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
    from hanpatch import qagate
    # A model may not score its own output, so a panel of exactly REQUIRED_JUDGES models
    # starves every pair its own models produced. Liveness is part of that count: a lane
    # that is configured but out of quota cannot judge anything.
    need = qagate.REQUIRED_JUDGES + 1
    pool = live_panel(need)
    models = {lane_model(p.id) for p in pool}
    if len(models) < need:
        raise SystemExit(
            f'judge pool too small: {len(models)} live model(s) {sorted(models)}, '
            f'{need} needed so a pair produced by one of them can still reach '
            f'{qagate.REQUIRED_JUDGES} independent verdicts')
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
        # Coverage is counted in MODELS, and a verdict from the producing model does not
        # count at all - the gate will reject it, so treating it as progress would leave the
        # pair permanently short while the run reported it done.
        pm = lane_model(prov_of.get(en, ''))
        have = {lane_model(r.get('judge')) for r in doc.get(pk, [])}
        have.discard(pm)
        have.discard('')
        if len(have) >= args.judges or pk in seen:
            continue
        seen.add(pk)
        rows.append((en, ko, it.get('jp', ''), pk,
                     prov_of.get(en, ''), tuple(sorted(have))))
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
            pmodel = lane_model(prov.id)
            batch = [r for r in pending
                     if lane_model(r[4]) != pmodel and pmodel not in r[5]]
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

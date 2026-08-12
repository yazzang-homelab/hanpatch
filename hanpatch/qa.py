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


_WS_ONLY = re.compile(r'[\s\u3000]+')


def wording_key(en, ko):
    """Identify unchanged wording across a layout-only rewrap.

    The exact pair key remains the storage identity. This comparison removes
    whitespace only because DQ7's wrapper may replace a source word-boundary
    space with a newline or insert a newline between Korean syllables. Every
    non-whitespace character must remain byte-for-byte identical. A carried
    record must still store ``carried_from`` so the inheritance is auditable.
    """
    return pair_key(en, _WS_ONLY.sub('', ko))


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
CLAUDE_MODELS = ('sonnet', 'opus')
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
                 # Google is the only vendor left on this box that is neither OpenAI nor
                 # a DeepSeek/Qwen relay, and vendor diversity is the thing the panel is
                 # short of: `codex1/2/3:gpt-5.6-luna` are three ACCOUNTS of one model and
                 # supply about half of all verdicts. Measured in a blinded 3-arm trial
                 # (90 rows, 3 judges, 720 verdicts, /root/tmp/gemmaqa/result90.json):
                 # gemma-4-31b defect rate 9.4% against the shipped control's 7.3%,
                 # McNemar p=1.00 - indistinguishable - with 0/90 mechanical failures,
                 # while codex luna scored 15.8% and failed 3/90 mechanically. It is 6.3x
                 # slower per call, which is why it belongs in the panel and NOT at the
                 # front of the translation pool.
                 'nimproxy:google/gemma-4-31b-it',
                 'opencode:mimo-v2.5-free',
                 'openrouter:nvidia/nemotron-3-ultra-550b-a55b:free',
                 'deepseek:deepseek-v4-pro',
                 # Metered, and named here only so its verdicts are ACCEPTED - identity and
                 # runtime are separate lists, and a lane outside this set has its verdicts
                 # rejected as an unknown judge after the calls were already paid for.
                 # It is never admitted implicitly: `active_judges()` reaches it only when
                 # an operator names it, because it is the one lane on this box that bills
                 # per token. Measured for judging: 20 pairs in 7.0s, full parse, about 110
                 # tokens per pair.
                 'a6:deepseek-v4-flash',
                 # The a6 catalogue was screened end to end on 2026-08-11: all 8 models,
                 # translation contract (16 real DQ7 rows) and judge contract (12 pairs),
                 # then detection against 6 SEEDED defects at two independent seeds.
                 # Artefacts: judge-contract-all8.json, judge-sensitivity-all8.json,
                 # judge-sensitivity-all8-seed29.json under /root/tmp/gemmaqa.
                 #
                 #   model                 contract  recall s17/s29   FP    tok/pair
                 #   qwen3.8-max           12/12     6/6  4/6         1,0   156-160
                 #   minimax-m3            12/12     6/6  4/6         0,0   292-303
                 #   minimax-m2.7          12/12     2/6  2/6         2,0   237-426  OUT
                 #   minimax-m2.5           0/12 (2 of 3 runs)              223-251  OUT
                 #
                 # `a6:minimax-m2.7` was admitted on 2026-08-10 on a SINGLE 6/6 reading and
                 # is removed here: at n=2 it caught 2 of 6 planted defects both times and
                 # missed every dropped syllable. A judge that misses two thirds of the
                 # damage still satisfies REQUIRED_JUDGES, which is the failure mode that
                 # makes a panel worse than no panel.
                 #
                 # `a6:gpt-5.6-luna` scores as well as anything here (6/6, 4/6, 136-145
                 # tok/pair) and is still NOT admitted: it collapses to the identity
                 # `gpt-5.6-luna`, which `codex1/2/3` already supply 165,744 verdicts of.
                 # It would add throughput and zero independence.
                 # `a6:qwen3.8-max-preview` and `a6:DeepSeek-V4-Flash-0731` are likewise
                 # aliases - `lane_model` now folds them - and cost 3x more per pair.
                 #
                 # Recall is measured against MECHANICAL damage. On real corpus pairs these
                 # models passed the two the recorded panel had failed for semantic
                 # mistranslation, so the panel detects damage, not subtle 오역. That limit
                 # is why `defect_corroboration` is not relaxed for them.
                 'a6:qwen3.8-max',
                 'a6:minimax-m3']

# Lanes that bill per token. They stay in JUDGES so their verdicts are accepted when an
# operator names them, and they are refused by the automatic widening below: reaching for
# a metered lane to rescue a panel is how a QA budget disappears without anyone deciding
# to spend it. Being LAST in LEGACY_JUDGES is the cost order the gate test asserts.
METERED_LANES = ('a6:deepseek-v4-flash', 'a6:qwen3.8-max', 'a6:minimax-m3')

# Judges that spend most of their completion budget reasoning before they answer. The
# default cap is sized for a model that replies immediately, and a reasoning model hits it
# mid-scratchpad: measured on `a6:minimax-m3`, 12 pairs at the default 880-token cap
# returned finish_reason=length with 2,998 of 3,000 completion tokens spent on reasoning
# and ZERO usable verdicts. Raising the cap to 12,000 produced 12/12 at 2,483 tokens.
#
# This is a floor, not an allocation - the model stops when it is done, and `m3` settles
# around 4,000. Getting it wrong is expensive in the silent direction: a truncated verdict
# is billed in full and counts for nothing.
REASONING_JUDGE_TOKENS = {'minimax-m3': 12000}


def judge_max_tokens(lane, batch_size):
    """Completion cap for one judging call, sized to the MODEL rather than the batch."""
    return max(min(4000, 400 + 40 * batch_size),
               REASONING_JUDGE_TOKENS.get(lane_model(lane), 0))

# Lanes that must never be CALLED again, while their recorded verdicts stay valid. The two
# lists have to be separate: this box holds 49,807 verdicts from `deepseek:deepseek-v4-pro`,
# so dropping it from JUDGES to stop the spending would have the gate reject all 49,807 as
# an unknown judge and collapse coverage that was already paid for. Retirement is about the
# next call, not about the evidence already on disk.
# The direct DeepSeek account is retired by operator decision (2026-08-10): it is prepaid
# and will not be topped up again. The same model remains reachable through `a6` and
# `nimproxy`, which bill differently, so no capability is lost by refusing this endpoint.
RETIRED_LANES = ('deepseek:deepseek-v4-pro',)

# Claude accounts are reserved for translation by operator decision (2026-08-10) and must
# not be spent on judging. This is a PREFIX rule rather than a list of lanes because the
# same accounts reach Claude three ways - directly, through the `agy` gateway, and through
# the `a6` relay - and a list would have to be extended every time a new path appears,
# which is exactly the kind of omission that spends an account nobody meant to spend.
# Their 13,000-odd recorded verdicts stay valid for the same reason retirement does not
# touch identity: dropping them from JUDGES would have the gate reject coverage that was
# already paid for.
RESERVED_JUDGE_PREFIXES = ('claude',)


def reserved(lane):
    """True when a lane belongs to an account reserved for work other than judging.

    The model half is matched on its BASENAME, not on the whole string. A relay that
    namespaces by vendor spells the same model `nimproxy:anthropic/claude-haiku-4-5`, and a
    `startswith` on the full spec let that through while `a6:claude-haiku-4-5` was refused -
    one spelling away from spending a reserved account on judging.
    """
    text = str(lane)
    account, _, model = text.partition(':')
    if any(account.startswith(p) for p in RESERVED_JUDGE_PREFIXES):
        return True
    return bool(model) and model.split('/')[-1].startswith('claude')

# Runtime-only parking: this lane has 6,256 historical verdicts, so it remains in JUDGES.
# It is not safe to feed it new work while the local rotator reports Worker local total
# request limit 16/16 and intermittent 300-second timeouts (observed in the current pass).
# Re-enable only after a bounded liveness probe is clean; identity and runtime are separate.
PARKED_RUNTIME_LANES = ['nimproxy:meta/llama-3.3-70b-instruct']

# Cheap QA tier: free rotators only. These three were live in the bounded probe just now and
# are three distinct model identities. Paid DeepSeek, subscription Claude/Codex, and Agy are
# deliberately not runtime judges in this tier; their historical verdict identities remain in
# JUDGES below. A6 (짱쫀쿠) stays out of the CHEAP tier because it bills per token, not
# because it cannot judge - `a6:qwen3.8-max` and `a6:minimax-m3` were measured against the
# real judge contract on 2026-08-11 and both returned 12/12 verdicts, recall 6/6 and 4/6
# on seeded defects at two seeds. They are METERED, so an operator names them explicitly.
CHEAP_QA_JUDGES = ['nimproxy:nvidia/nemotron-3-super-120b-a12b',
                   'opencode:mimo-v2.5-free',
                   'openrouter:nvidia/nemotron-3-ultra-550b-a55b:free']

# Set by `--models`. Module-level because `active_judges()` is called from the panel
# builder rather than threaded through it, and an env var alone would make a CLI flag
# invisible to a nested call.
_POOL_OVERRIDE = ''


def codex_judges():
    return [f'codex{a}:{CODEX_MODEL}' for a in providers.codex_accounts()]


def claude_judges():
    """One lane per Claude account per model, accounts outermost.

    Ordering accounts outermost matters: the panel takes the first lanes that answer, and a
    per-model-first order would spend the whole first model on one account's quota before
    touching the others. Two models across N accounts is 2 identities and 2N lanes - the
    identities are what the release rule counts, the lanes are what carries throughput.
    """
    return [f'claude{a}:{m}' for a in providers.claude_accounts() for m in CLAUDE_MODELS]


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
    name = (model.split('/')[-1] or lane).strip().lower()
    # A gateway that exposes the same model twice for two billing accounts spells the second
    # one with a suffix (`gemini-3-pro` / `gemini-3-pro-biz`). Those are one model on two
    # accounts: useful for throughput, worthless for independence, so the suffix collapses
    # here rather than reintroducing the account-as-judge bug under a new spelling.
    #
    # A reseller catalogue spells the same model three more ways, and every one of them
    # would have passed as an independent judge. Measured on the a6 catalogue 2026-08-11:
    #   DeepSeek-V4-Flash-0731  vs  deepseek-v4-flash    dated snapshot + casing
    #   qwen3.8-max-preview     vs  qwen3.8-max          preview channel
    # `judge_identity` called those distinct, so a panel of `qwen3.8-max` plus
    # `qwen3.8-max-preview` would have satisfied REQUIRED_JUDGES=2 with ONE model - the
    # same defect that made the `-biz` lanes worthless. Casing is folded for the same
    # reason: a catalogue that renames `deepseek-v4-flash` to `DeepSeek-V4-Flash` has not
    # produced a second opinion.
    #
    # Safe to apply to history: every one of the 28 judge lanes recorded in the 374,807
    # verdicts on this box already normalizes to itself under this rule, so no pair loses
    # coverage and nothing becomes an `unknown judge`. Verified before landing, not assumed.
    for suffix in ('-biz', '-preview'):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    name = re.sub(r'-\d{4,8}$', '', name)
    return name


# The revoked lanes are subtracted from the identity set as well as from the runtime pool. That
# is only sound because no verdict names them; a lane that had recorded one would have to stay
# an accepted identity, or the gate would reject its own history as `unknown judge`.
JUDGES = [s for s in (claude_judges() + GATEWAY_JUDGES + codex_judges()
                      + LEGACY_JUDGES + RETIRED_JUDGES)
          if s not in REVOKED_GATEWAY_LANES]



def active_judges():
    """The lanes a panel run may call, in preference order.

    The default stays free-only: a panel that quietly reaches for a subscription or
    metered lane to keep moving is how a QA budget disappears without anyone deciding to
    spend it. What was never the point is refusing an operator who decides to spend it -
    silent admission is the hazard, not the lanes themselves. `--models`, or
    `HANPATCH_QA_MODELS`, widens the pool explicitly, prints what it selected, and refuses
    a lane that is not a recognised judge identity, because a verdict from an unknown
    judge is rejected by the gate anyway and would only be discovered after the calls were
    paid for.

    Independence is unaffected and is not negotiable here: `qagate.disqualified` drops a
    verdict from the model that produced the translation, whatever pool it came from.
    """
    spec = _POOL_OVERRIDE or os.environ.get('HANPATCH_QA_MODELS', '')
    if not spec.strip():
        return CHEAP_QA_JUDGES
    wanted = [s.strip() for s in spec.split(',') if s.strip()]
    unknown = [s for s in wanted if s not in JUDGES]
    if unknown:
        raise SystemExit(
            f'unknown judge lane(s) {unknown}: a verdict recorded by a lane outside the '
            f'panel identity set is refused by the gate as an unknown judge, so calling '
            f'it would spend the call and prove nothing. Known lanes: {sorted(JUDGES)}')
    # A retired lane is still a KNOWN judge, so the check above cannot catch it. Naming one
    # explicitly is the only way it could be called, and it is refused loudly rather than
    # dropped from the list: silently judging with fewer lanes than the operator asked for
    # is how a panel quietly shrinks below what the gate requires.
    retired = [s for s in wanted if s in RETIRED_LANES]
    if retired:
        raise SystemExit(
            f'retired judge lane(s) {retired}: their recorded verdicts remain valid, but '
            f'the account is closed and must not be called again. Remove them from the '
            f'pool. Reachable substitutes for the same model: a6, nimproxy.')
    held = [s for s in wanted if reserved(s)]
    if held:
        raise SystemExit(
            f'reserved lane(s) {held}: these accounts are kept for translation and must '
            f'not be spent on judging. Their existing verdicts stay valid; only new calls '
            f'are refused.')
    return wanted

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


_LAST_SAVE = [0.0]
# The verdict document is rewritten whole, so the cost of a save grows with the corpus.
# Measured on this run: 109 MB, dumped with indent+sort under a global lock after EVERY
# batch, which throttled 26 workers down to one serialized 109 MB write each. Throughput
# fell from 4.93 pairs/s at the start of the run to 0.29 - a 17x collapse that looked like
# dying lanes and was not.
#
# Time-based instead of per-batch. A crash now loses at most this window of verdicts, and
# losing a verdict costs one re-judge because judging is idempotent - the pair is simply
# judged again. Losing THROUGHPUT costs the whole run.
SAVE_INTERVAL_S = float(os.environ.get('HANPATCH_QA_SAVE_INTERVAL', '60'))


def save(doc, lock, force=False):
    """Replace the verdict file atomically and durably, at most once per interval.

    A panel run is hours of paid work held in one document, so the write stays atomic and
    fsynced: renaming without flushing leaves the window where a crash loses verdicts the
    log already reported as recorded.
    """
    with lock:
        now = time.time()
        if not force and now - _LAST_SAVE[0] < SAVE_INTERVAL_S:
            return
        _LAST_SAVE[0] = now
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


def _ident(lane_id):
    """Judge identity under the declared standard. Local import: `qagate` imports this
    module, so a top-level import would be circular."""
    from hanpatch import qagate
    return qagate.judge_identity(lane_id)


def live_panel(required):
    """Return the live lanes to judge with; never spend to rescue a cheap-tier panel."""
    pool = []
    have = set()
    explicit = bool(_POOL_OVERRIDE or os.environ.get('HANPATCH_QA_MODELS', '').strip())
    for spec in active_judges():
        # Stop once the panel is independent and has spare lanes for throughput. Any fallback
        # stays inside the free legacy set; paid/subscription lanes are never implicit.
        #
        # An EXPLICIT pool is not a default to be trimmed: an operator who names ten lanes
        # named them to be used, and stopping at four leaves the throughput they asked for
        # on the floor. Measured: the default break admitted 6 of 17 named lanes.
        if not explicit and len(have) >= required and len(pool) >= 2 * required:
            break
        p = providers.make(spec)
        if p is None or not alive(p):
            continue
        pool.append(p)
        have.add(_ident(p.id))
    if len(have) >= required:
        return pool
    # A configured free candidate may have died since the bounded probe. Widen only to other
    # free legacy candidates, excluding the parked lane and the explicitly metered endpoint.
    for spec in LEGACY_JUDGES:
        if len(have) >= required:
            break
        # Widening is the path that spends without anyone deciding to, so every "do not
        # call" rule has to be repeated here. A reserved or retired lane is still a KNOWN
        # judge, so the identity checks upstream cannot catch it, and this loop is reached
        # precisely when the panel is short - the moment a rescue looks justified.
        if (spec in METERED_LANES or spec in PARKED_RUNTIME_LANES
                or spec in RETIRED_LANES or reserved(spec)):
            continue
        p = providers.make(spec)
        if p is None or _ident(p.id) in have or not alive(p):
            continue
        print(f'  + admitting cheap lane {p.id}: only {len(have)} free model(s) answered, '
              f'{required} needed', flush=True)
        pool.append(p)
        have.add(_ident(p.id))
    return pool


def main(argv=None):
    # `--batch` exists HERE and nowhere else. Copying this invocation to
    # `hanpatch translate` used to prefix-match it onto `--batch-chars`, capping
    # that run at eight source characters. Abbreviations are off so the mistake
    # is an error instead of a silent 57,621-call bill.
    ap = argparse.ArgumentParser(prog='hanpatch qa', allow_abbrev=False)
    ap.add_argument('--families', default='all')
    ap.add_argument('--pairs', default='',
                    help='JSON file of pair keys to judge, and only those. Use when a '
                         'newly added lane would otherwise re-sweep the whole corpus')
    ap.add_argument('--batch', type=int, default=8)
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--threshold', type=int, default=4)
    ap.add_argument('--judges', type=int, default=1,
                    help='number of distinct judges required per pair')
    ap.add_argument('--models', default='',
                    help='explicit comma-separated judge pool; every lane must be a '
                         'known judge identity. Default is the free-only rotation - '
                         'this is how an operator deliberately spends a faster lane, '
                         'and it never changes the independence rule')
    args = ap.parse_args(argv)
    want_pairs = None
    if args.pairs:
        # The file holds the pair keys to judge. It is REQUIRED to be non-empty: an empty
        # list would otherwise mean "judge nothing" and report success, which is the same
        # silent no-op the translate side had to be taught to refuse.
        want = config.load_object(args.pairs, 'the targeted pair list')
        want_pairs = set(want if isinstance(want, list) else want.keys())
        if not want_pairs:
            raise SystemExit(f'{args.pairs} names no pairs: a targeted run with an empty '
                             f'list would judge nothing and report success')
        print(f'targeted pass: {len(want_pairs)} pair(s) from {args.pairs}', flush=True)
    global _POOL_OVERRIDE
    if args.models.strip():
        _POOL_OVERRIDE = args.models

    providers.load_dotenv()
    _panel_lock = hold_panel_lock()              # noqa: F841 - held until exit
    from hanpatch import qagate
    # A model may not score its own output, so a panel of exactly REQUIRED_JUDGES models
    # starves every pair its own models produced. Liveness is part of that count: a lane
    # that is configured but out of quota cannot judge anything.
    need = qagate.REQUIRED_JUDGES + 1
    pool = live_panel(need)
    identities = {qagate.judge_identity(p.id) for p in pool}
    if len(identities) < need:
        unit = 'lane' if qagate.independence() == 'lane' else 'model'
        raise SystemExit(
            f'judge pool too small: {len(identities)} live {unit}(s) '
            f'{sorted(identities)}, {need} needed so a pair produced by one of them can '
            f'still reach {qagate.REQUIRED_JUDGES} independent verdicts')
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
        # A targeted pass judges a NAMED set of pairs and nothing else. Without this, adding
        # a lane that has zero verdicts means every pair is short one judge, so a run meant
        # to settle 721 disputed rows would sweep all 63,054 - at 6.3x the per-call latency
        # for the lane in question. Same idea as `translate --qa-list`.
        if want_pairs is not None and pk not in want_pairs:
            continue
        # Coverage is counted the way the GATE counts it, or the run reports progress the
        # release rule does not accept. Under the default standard that is the model;
        # under a declared `lane` standard it is the lane. A verdict from the producer does
        # not count at all - the gate rejects it, so treating it as progress would leave
        # the pair permanently short while the run reported it done.
        ident = qagate.judge_identity
        pm = ident(prov_of.get(en, '')) if prov_of.get(en, '') else ''
        have = {ident(r.get('judge')) for r in doc.get(pk, []) if r.get('judge')}
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
            pident = qagate.judge_identity(prov.id)
            batch = [r for r in pending
                     if (not r[4] or qagate.judge_identity(r[4]) != pident)
                     and pident not in r[5]]
            if not batch:
                continue
            try:
                sub = glossary.relevant(glossary.load(), [r[0] for r in batch])
                with providers.gate_for(prov.id):
                    raw = prov.chat(system_prompt(), prompt(batch, sub), temperature=0.0,
                                     max_tokens=judge_max_tokens(prov.id, len(batch)))
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
    # The interval save may be holding the last minute of verdicts in memory. This is the
    # one place that must not skip: the process is about to exit.
    save(doc, lock, force=True)

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

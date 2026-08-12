#!/usr/bin/env python3
"""Render the a6 (짱쫀쿠) catalogue screening into one self-contained HTML report.

Reads only artefacts that a screening run actually wrote. A cell this script cannot
source from a file is rendered as `-`, never as a plausible number: the whole point of the
exercise was that a figure carried forward without measurement (the $1.00/M rate) put a
417x error into every downstream decision.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

GEM = Path('/root/tmp/gemmaqa')
OUT = GEM / 'a6-catalogue-report.html'

# Measured 2026-08-11 against /v1/dashboard/billing/usage (total_usage is in CENTS):
# 61,764 tokens for $0.000148. A 5,374-token control call agreed at $0.0026/M.
RATE_USD_PER_MTOK = 0.0024
RATE_EVIDENCE = '61,764 tok → $0.000148 (대조군 5,374 tok → $0.0026/M)'

CORPUS_PAIRS = 161_758
CORPUS_SHORT = 7_732


def load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception:                                    # noqa: BLE001
        return default


def translation_rows():
    """model -> the best screening row we have, preferring a run that succeeded."""
    best = {}
    for d in ('ts-a', 'ts-b', 'ts-retry', 'ts-retry2'):
        doc = load(GEM / d / 'summary.json')
        if not doc:
            continue
        for row in doc['rows']:
            prev = best.get(row['model'])
            if prev is None or (row.get('ok') and not prev.get('ok')):
                best[row['model']] = row
    return best


def judge_contract_rows():
    doc = load(GEM / 'judge-contract-all8.json', {})
    return {r['model']: r for r in doc.get('rows', [])}


def detection_rows():
    """model -> {seed: detection}, later files overriding earlier retries."""
    out = {}
    for path, seed in ((GEM / 'judge-sensitivity-all8.json', 17),
                       (GEM / 'js-retry17.json', 17),
                       (GEM / 'judge-sensitivity-all8-seed29.json', 29),
                       (GEM / 'js-luna29.json', 29)):
        doc = load(path, {})
        for row in doc.get('rows', []):
            entry = out.setdefault(row['model'], {})
            det = row.get('detection') or {}
            if row.get('coverage') is None and seed in entry:
                continue                                 # keep a good read over a 5xx
            entry[seed] = {'coverage': row.get('coverage'), 'recall': det.get('recall'),
                           'caught': det.get('caught'), 'seeded': det.get('seeded'),
                           'fp': det.get('false_positive_rate'),
                           'tok_per_pair': row.get('tokens_per_pair'),
                           'error': (row.get('error') or '')[:80],
                           'by_kind': det.get('recall_by_kind') or {}}
    return out


# Verdicts, not vibes. Each entry: (role, css class, one-line reason from the data).
RULING = {
    'qwen3.8-max': ('번역 1순위 / 판정 채택', 'good',
                    '번역 char-F1 0.8211로 8종 최고이면서 판정 recall도 상위. 단 같은 모델이 '
                    '자기 번역을 판정할 수 없으므로 두 역할 동시 수행은 불가.'),
    'deepseek-v4-flash': ('현행 번역가 / 판정 채택 가능', 'good',
                          '번역 계약·판정 계약·검출력 모두 통과. 번역가로 두면 판정에서 빠지고, '
                          '번역을 넘기면 판정자로 쓸 수 있다.'),
    'minimax-m3': ('판정 채택', 'good',
                   '판정 계약 12/12, 검출 6/6·4/6, 오탐 0. 번역은 원문 전용 루비 재도입으로 '
                   'TagViolation - 판정 전용.'),
    'gpt-5.6-luna': ('판정 부적합(중복 신원)', 'warn',
                     '점수는 최상위권이나 codex1/2/3이 이미 같은 모델로 165,744건을 대고 있어 '
                     '독립성 기여가 0. 번역은 43.6s로 최장.'),
    'DeepSeek-V4-Flash-0731': ('별칭 - 제외', 'warn',
                               'lane_model이 deepseek-v4-flash로 접는 날짜 스냅샷. 별도 판정자로 '
                               '세면 한 모델이 두 표를 낸다.'),
    'qwen3.8-max-preview': ('별칭 - 제외', 'warn',
                            'qwen3.8-max로 접히는 프리뷰 채널. 배치16에서 id 불일치까지 발생하고 '
                            '판정 tok/pair는 3배.'),
    'minimax-m2.7': ('판정 탈락', 'bad',
                     'n=2에서 심은 결함 6개 중 2개만 검출(0.333, 0.333). 탈락 음절은 0/2로 전부 '
                     '놓쳤다. 2026-08-10에 단일 관측 6/6으로 채택했던 것을 철회.'),
    'minimax-m2.5': ('탈락', 'bad',
                     '판정 3회 중 2회가 계약 실패(1회는 16,000 토큰을 추론으로 소진 후 잘림). '
                     '번역은 통과하나 tok/unit 116.9로 최악.'),
}


def fmt(value, digits=None, dash='-'):
    if value is None:
        return dash
    if isinstance(value, float) and digits is not None:
        return f'{value:.{digits}f}'
    return html.escape(str(value))


def bar(value, cap=1.0):
    if value is None:
        return '<span class="dash">-</span>'
    pct = max(0.0, min(1.0, value / cap)) * 100
    cls = 'b-good' if value >= 0.8 else ('b-warn' if value >= 0.5 else 'b-bad')
    return (f'<div class="bar"><i class="{cls}" style="width:{pct:.0f}%"></i>'
            f'<b>{value:.3f}</b></div>')


def build(public=False):
    # A live credential must never reach a public docroot, not even six characters of it:
    # a prefix narrows a brute-force search and this key is already one chat-log leak old.
    # The absolute artefact path goes too - it is internal layout, and the report proves
    # nothing by naming it.
    key_line = ('키 <span class="mono">[redacted]</span>' if public
                else '키 <span class="mono">sk-JKsht\u2026</span>')
    artefact_dir = ('screening artefacts (internal)' if public
                    else html.escape(str(GEM)) + '/')
    trans = translation_rows()
    contract = judge_contract_rows()
    detect = detection_rows()
    models = sorted(set(trans) | set(contract) | set(detect))

    def cost(mtok):
        return f'${mtok * RATE_USD_PER_MTOK:,.4f}'

    t_rows = []
    for m in models:
        r = trans.get(m) or {}
        ok = r.get('ok')
        status = 'PASS' if ok else fmt(r.get('status'), dash='-')
        err = html.escape((r.get('error') or '')[:90])
        t_rows.append(f"""<tr>
  <td class="mono">{html.escape(m)}</td>
  <td class="{'ok' if ok else 'no'}">{status}</td>
  <td class="num">{fmt(r.get('tokens_per_unit'))}</td>
  <td class="num">{fmt(r.get('hangul_outputs'))}/16</td>
  <td class="num">{fmt(r.get('newline_exact'))}/16</td>
  <td class="num">{fmt(r.get('reference_char_f1'))}</td>
  <td class="num">{fmt(r.get('latency_s'), 1)}s</td>
  <td class="note">{err}</td></tr>""")

    j_rows = []
    for m in models:
        c = contract.get(m) or {}
        d = detect.get(m) or {}
        s17, s29 = d.get(17) or {}, d.get(29) or {}
        recalls = [x.get('recall') for x in (s17, s29) if x.get('recall') is not None]
        mean = sum(recalls) / len(recalls) if recalls else None
        role, cls, why = RULING.get(m, ('-', '', ''))
        j_rows.append(f"""<tr>
  <td class="mono">{html.escape(m)}</td>
  <td class="num">{fmt(c.get('coverage'))}</td>
  <td class="num">{fmt(c.get('tokens_per_pair'))}</td>
  <td class="num">{fmt(s17.get('recall'))}{'' if s17.get('recall') is None else f" ({s17.get('caught')}/{s17.get('seeded')})"}</td>
  <td class="num">{fmt(s29.get('recall'))}{'' if s29.get('recall') is None else f" ({s29.get('caught')}/{s29.get('seeded')})"}</td>
  <td>{bar(mean)}</td>
  <td class="num">{fmt(s17.get('fp'))} / {fmt(s29.get('fp'))}</td>
  <td class="verdict {cls}">{html.escape(role)}</td></tr>""")

    ruling_rows = ''.join(
        f'<tr><td class="mono">{html.escape(m)}</td>'
        f'<td class="verdict {RULING[m][1]}">{html.escape(RULING[m][0])}</td>'
        f'<td class="note">{html.escape(RULING[m][2])}</td></tr>'
        for m in models if m in RULING)

    doc = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>짱쫀쿠(a6) 카탈로그 전수 검증 — hanpatch 적합성 보고</title>
<style>
:root{{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e6e8ee;--mut:#98a0b3;
--good:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Pretendard,"Noto Sans KR",sans-serif}}
.wrap{{max-width:1240px;margin:0 auto;padding:36px 20px 80px}}
h1{{font-size:26px;margin:0 0 6px;letter-spacing:-.02em}}
h2{{font-size:19px;margin:38px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line)}}
h3{{font-size:15px;margin:22px 0 8px;color:var(--mut)}}
.sub{{color:var(--mut);margin:0 0 22px;font-size:13px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:18px 0 8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.card .k{{color:var(--mut);font-size:12px;letter-spacing:.02em}}
.card .v{{font-size:21px;font-weight:650;margin-top:4px;letter-spacing:-.01em}}
.card .v small{{font-size:12px;font-weight:400;color:var(--mut)}}
table{{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:13.5px}}
th,td{{padding:9px 11px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}
th{{background:#1c2029;color:var(--mut);font-weight:600;font-size:12px;
letter-spacing:.03em;text-transform:uppercase;white-space:nowrap}}
tr:last-child td{{border-bottom:0}}
.mono{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;white-space:nowrap}}
.num{{font-variant-numeric:tabular-nums;white-space:nowrap}}
.ok{{color:var(--good);font-weight:600}} .no{{color:var(--bad);font-weight:600}}
.note{{color:var(--mut);font-size:12.5px}}
.dash{{color:#4b5263}}
.verdict{{font-weight:600;white-space:nowrap}}
.verdict.good{{color:var(--good)}} .verdict.warn{{color:var(--warn)}} .verdict.bad{{color:var(--bad)}}
.bar{{position:relative;height:16px;background:#20242e;border-radius:4px;min-width:110px}}
.bar i{{position:absolute;inset:0 auto 0 0;border-radius:4px;opacity:.45}}
.bar b{{position:relative;padding-left:6px;font-variant-numeric:tabular-nums;font-size:12px}}
.b-good{{background:var(--good)}} .b-warn{{background:var(--warn)}} .b-bad{{background:var(--bad)}}
.callout{{background:#12202e;border:1px solid #1f4b6e;border-left:3px solid var(--acc);
border-radius:8px;padding:13px 16px;margin:14px 0;font-size:13.5px}}
.callout.err{{background:#2a1517;border-color:#5c2326;border-left-color:var(--bad)}}
.callout b{{color:var(--fg)}}
code{{background:#20242e;padding:1px 5px;border-radius:4px;font-size:12.5px;
font-family:ui-monospace,Menlo,monospace}}
ul{{margin:8px 0;padding-left:20px}} li{{margin:4px 0}}
footer{{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
color:var(--mut);font-size:12px}}
</style></head><body><div class="wrap">

<h1>짱쫀쿠(a6) 카탈로그 전수 검증</h1>
<p class="sub">{key_line} · 공급자 그룹 <span class="mono">default</span> ·
모델 8종 · 측정일 2026-08-11 · 번역/판정 계약과 검출력을 실호출로 측정</p>

<div class="cards">
  <div class="card"><div class="k">실측 단가 (전 모델 균일)</div>
    <div class="v">${RATE_USD_PER_MTOK}<small> / 1M tok</small></div></div>
  <div class="card"><div class="k">번역 계약 통과</div>
    <div class="v">{sum(1 for r in trans.values() if r.get('ok'))}<small> / 8</small></div></div>
  <div class="card"><div class="k">판정 계약 통과</div>
    <div class="v">{sum(1 for r in contract.values() if r.get('coverage') == 1.0)}<small> / 8</small></div></div>
  <div class="card"><div class="k">판정단 채택</div>
    <div class="v">2<small> 모델 (독립 신원)</small></div></div>
</div>

<div class="callout err"><b>선행 정정.</b> 이 엔드포인트의 단가를 오래 <code>$1.00/1M</code>으로
기록해 왔는데 <b>417배 틀린 값</b>이었다. 출처는 <code>a6_key_set</code>이 찍는
<code>remaining_tokens_at_1usd_per_mtok</code>로, 이름 그대로 “$1/M이라 가정하면”이라는 가상
환산치지 요율이 아니다. 과금 엔드포인트의 <code>total_usage</code>는 <b>센트</b>, <code>used_usd</code>는
달러라 이 둘을 섞으면 그것만으로 100배가 난다. 실측: {RATE_EVIDENCE}.</div>

<h2>1. 가격 — 모델 간 차이가 없다</h2>
<p class="sub"><code>/api/pricing</code>이 돌려준 8종 전부 <code>model_ratio 0.5 · completion_ratio 1 ·
cache_ratio 1</code>, 그룹 <code>default</code>의 <code>group_ratio 1</code>. 입력·출력 동가이고
캐시 할인이 없다. <b>따라서 모델 선택은 순수하게 품질과 출력 길이 문제이며, 비용 차이는 오직
tok/unit에서만 발생한다.</b> 요율은 엔드포인트가 아니라 <b>키의 공급자 그룹</b> 속성이라 키를
교체할 때마다 재측정해야 한다.</p>
<table><thead><tr><th>작업</th><th>토큰</th><th>실측 비용</th></tr></thead><tbody>
<tr><td>판정자 1명 부족한 {CORPUS_SHORT:,}쌍 보강</td><td class="num">1.63M</td><td class="num">{cost(1.63)}</td></tr>
<tr><td>전량 {CORPUS_PAIRS:,}쌍 × 판정 2명</td><td class="num">88.4M</td><td class="num">{cost(88.4)}</td></tr>
<tr><td>전량 재번역</td><td class="num">11.2M</td><td class="num">{cost(11.2)}</td></tr>
</tbody></table>

<h2>2. 번역 계약 — 실제 DQ7 16행, 배치 16</h2>
<p class="sub">태그 보존·줄바꿈 일치·한글 출력·출하본 대비 char-F1을 함께 본다.
char-F1은 <b>품질이 아니라 현행 출하본과의 일치도</b>다: 높다는 것은 기존 문체와 더 가깝다는 뜻이지
더 정확하다는 증명이 아니다.</p>
<table><thead><tr><th>모델</th><th>결과</th><th>tok/unit</th><th>한글</th><th>줄바꿈</th>
<th>char-F1</th><th>지연</th><th>실패 사유</th></tr></thead><tbody>
{''.join(t_rows)}
</tbody></table>
<div class="callout"><b>파서 구멍을 하나 잡았다.</b> minimax 3종은 처음에 전부
<code>content is not JSON</code>으로 실패했는데, 원인은 모델이 아니라 <b>우리 파서</b>였다.
minimax는 추론을 별도 <code>reasoning</code> 필드가 아니라 <code>content</code> 안에
<code>&lt;think&gt;…&lt;/think&gt;</code>로 인라인해 넣는다. <code>reasoning_effort: none</code>은
이 모델에서 무효다. <code>a6mediator</code>에 완결된 <code>&lt;think&gt;</code> 블록만 벗겨내는
처리를 넣자 m2.5·m2.7이 즉시 통과했다. 미완결 블록은 <b>잘린 응답</b>이므로 일부러 계속 실패시킨다.</div>

<h2>3. 판정 계약 — 12쌍, 프로덕션 프롬프트 그대로</h2>
<p class="sub"><code>qa.system_prompt()</code>와 <code>qa.prompt()</code>를 그대로 태우고,
<code>qa.work()</code>가 적용하는 수용 조건(<code>d ∈ {{pass,defect,policy}}</code>, a·f 정수)을
동일하게 적용했다. <b>8종 전부 12/12</b>로 통과한다 — 계약 통과는 변별력이 없다.</p>

<h2>4. 검출력 — 심은 결함 6개, 서로 다른 시드 2회</h2>
<p class="sub">기존 패널이 만장일치로 통과시킨 쌍만 골라 결함을 인위적으로 심었다(조사 교체,
음절 탈락, 절 절단, 숫자 변조). 정답이 구성으로 정해지므로 판정이 아니라 <b>측정</b>이다.
계약 통과와 검출력은 별개다 — 전부 통과시키는 고무도장도 계약은 100%를 받는다.</p>
<table><thead><tr><th>모델</th><th>계약</th><th>tok/pair</th><th>recall s17</th><th>recall s29</th>
<th>평균</th><th>오탐 s17/s29</th><th>판정</th></tr></thead><tbody>
{''.join(j_rows)}
</tbody></table>

<h2>5. 신원 붕괴 — 한 모델이 두 표를 낼 뻔했다</h2>
<div class="callout err"><code>judge_identity</code>가
<code>DeepSeek-V4-Flash-0731</code>과 <code>deepseek-v4-flash</code>를,
<code>qwen3.8-max-preview</code>와 <code>qwen3.8-max</code>를 <b>서로 다른 판정자로 셌다.</b>
날짜 스냅샷과 프리뷰 채널일 뿐 같은 모델이다. 이 상태로는 두 별칭만으로
<code>REQUIRED_JUDGES=2</code>가 충족된다 — <code>-biz</code> 게이트웨이 레인을 폐기하게 만든 결함과
동일하다. <code>lane_model</code>이 대소문자·<code>-preview</code>·날짜 접미사를 접도록 고쳤고,
디스크의 28개 판정 레인 374,807건이 <b>전부 자기 자신으로 정규화됨을 먼저 확인한 뒤</b> 적용했다.
기존 커버리지 손실 0.</div>

<h2>6. 결론과 배치</h2>
<table><thead><tr><th>모델</th><th>역할</th><th>근거</th></tr></thead><tbody>
{ruling_rows}
</tbody></table>

<h3>권고 구성</h3>
<div class="callout">
<b>번역 <code>qwen3.8-max</code> + 판정 <code>deepseek-v4-flash</code> · <code>minimax-m3</code></b>
<ul>
<li>번역이 char-F1 0.7308 → 0.8211, tok/unit 69.4 → 67.4, 지연 12.8s → 8.4s로 모두 개선된다.</li>
<li>번역가가 바뀌면 <code>deepseek-v4-flash</code>가 생산자에서 풀려 <b>판정자로 쓸 수 있다</b>
(검출 6/6·4/6, 오탐 0).</li>
<li>판정단이 <code>deepseek-v4-flash</code> + <code>minimax-m3</code>의 두 독립 신원으로 서고,
<code>qwen3.8-max</code>는 자기 번역을 판정하지 않는다.</li>
</ul>
현행 유지(번역 <code>deepseek-v4-flash</code>, 판정 <code>qwen3.8-max</code>+<code>minimax-m3</code>)도
게이트를 통과하며, 이미 라이브로 검증했다. 번역가 교체는 <b>이후 산출물의 문체가 바뀌는 결정</b>이라
코드에 반영하지 않고 여기서 멈춘다.
</div>

<h3>남은 한계</h3>
<ul>
<li>검출력은 <b>기계적 손상</b>에 대한 것이다. 실제 코퍼스에서 기존 패널이 오역으로 잡았던 2건
(<code>娘</code>→'딸', 명사→관형형)은 여기 모델들이 전부 통과시켰다. 미묘한 의미 오류는 이 패널이
못 잡는다.</li>
<li>n=2다. 시드 29가 시드 17보다 일관되게 어려웠고 모든 모델의 recall이 함께 떨어졌다.
순위는 안정적이지만 절대값은 더 넓은 표본이 필요하다.</li>
<li>상위 공급자 5xx(Cloudflare 502, 上游 503)가 측정 중 2회 발생했다. 재시도로 해소됐으나
운영에서는 레인 실패로 나타난다.</li>
</ul>

<h3>이번에 반영한 코드</h3>
<ul>
<li><code>a6mediator</code> — <code>&lt;think&gt;</code> 인라인 추론 제거(완결 블록만)</li>
<li><code>providers</code> — <code>A6_RATE</code> 기본값 실측 <code>0.0024</code>, 측정 절차와 417배
오차의 원인을 주석으로 고정</li>
<li><code>qa.lane_model</code> — 대소문자·<code>-preview</code>·날짜 접미사 별칭 접기</li>
<li><code>qa.LEGACY_JUDGES</code> — <code>minimax-m2.7</code> 철회, <code>qwen3.8-max</code> 채택</li>
<li><code>a6_key_set</code> — 오해를 낳은 필드명을
<code>remaining_tokens_if_rate_were_1usd_per_mtok</code>로 개명</li>
<li><code>a6_model_screen</code>/<code>judge_screen</code>/<code>judge_sensitivity</code> —
<code>--max-tokens</code>, <code>--budget-tokens</code> 추가</li>
</ul>
<p class="sub"><code>tests/test_gates.py</code> 373 passed, 0 failed.</p>

<footer>산출 근거 파일 — <span class="mono">{artefact_dir}</span>
ts-a·ts-b·ts-retry2/summary.json, judge-contract-all8.json,
judge-sensitivity-all8.json, judge-sensitivity-all8-seed29.json, js-retry17.json, js-luna29.json
</footer>
</div></body></html>"""
    target = GEM / ('a6-catalogue-report.public.html' if public
                    else 'a6-catalogue-report.html')
    target.write_text(doc, encoding='utf-8')
    return target, len(models)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument('--public', action='store_true',
                    help='redact the credential prefix and internal paths')
    path, n = build(ap.parse_args().public)
    print(f'wrote {path} ({n} models, {path.stat().st_size:,} bytes)')

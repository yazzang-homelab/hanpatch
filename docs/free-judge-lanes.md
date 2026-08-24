# Free judge lanes — official limits and what passed screening (2026-08-23)

The judge panel needs DISTINCT models that are not the translator, and every paid lane
spends money to disagree. Free endpoints are therefore panel capacity, not a convenience.
This file records the vendor documentation the lanes are sized against and the screening
that decided admission. Lane membership itself lives in `hanpatch/qa.py`
(`LEGACY_JUDGES`), with the same numbers repeated at the point of decision.

## Official limits

### OpenRouter — free-priced models
Source: <https://openrouter.ai/docs/api-reference/limits> (constants read from the doc
page itself, 2026-08-23).

| Limit | Value |
|---|---|
| Requests/min on a free model | 20 |
| Requests/day, account with < $10 lifetime credit | **50** |
| Requests/day, account with >= $10 credit | 1000 |
| Enforcement scope | per account, globally — extra keys on one account buy nothing |

This box holds 3 OpenRouter keys across 2 accounts, all three `is_free_tier: true`, so the
ceiling is **100 requests/day**, not 150. `openrouter-budget-proxy.service` on
`127.0.0.1:18094` is the only path lanes may use: it owns the keys, counts per account
against 50/day in `/root/free-ai-registry/openrouter-rotator-counters.json`, and refuses
anything the catalogue does not price at zero.

The proxy's guard is **price-based, not name-based** (changed 2026-08-23). It used to
forward only slugs ending in `:free`, which both missed zero-priced models that carry no
suffix — `stealth/ox-alpha` — and trusted a name to decide whether a request can bill.
It now admits a model when OpenRouter's own catalogue reports
`pricing.prompt == pricing.completion == 0`, so a slug that starts billing drops out on
the next refresh (TTL) and is refused. If the catalogue refresh fails, it falls back to
the `:free` suffix rule and refuses unverifiable slugs.

### Groq
Source: <https://console.groq.com/docs/rate-limits> (2026-08-23).

| Model | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `openai/gpt-oss-120b`, `gpt-oss-20b`, `gpt-oss-safeguard-20b`, `qwen/qwen3.6-27b` | 30 | 1K | 8K | 200K |
| `groq/compound`, `groq/compound-mini` | 30 | 250 | 70K | — |
| `meta-llama/llama-prompt-guard-2-22m/86m` | 30 | 14.4K | 15K | 500K |

Judging fits inside 8K TPM — the panel spends about 110-200 tokens per pair — which is why
`groq:openai/gpt-oss-120b` is a working judge lane. The same 8K ceiling makes these models
useless as a general agent backend: a GJC session's system prompt alone measured 69,143
tokens and every request came back 413. `groq/compound*` has the 70K headroom but refuses
`tools` (HTTP 400 "`tool calling` is not supported with this model"), so it cannot run an
agent loop either. Cached tokens do not count toward the limits.

Do not diagnose Groq auth from a 403: `api.groq.com` answers a default python/curl
User-Agent with Cloudflare `403 error code: 1010`, which is indistinguishable from an auth
failure and once had a live key recorded here as permanently dead. Probe with a browser UA
(the rotator on `:18096` already sends one).

## Screening (production prompts, 12 pairs, seeds 17 and 29)

Run with `tools/judge_screen.py` (contract: does it return usable verdicts on real corpus
pairs?) and `tools/judge_sensitivity.py` (detection: does it catch planted mechanical
damage without flagging clean rows?). Artefacts under `/root/tmp/gemmaqa`:
`judge-screen-free-20260823-{a,b,c}.json`,
`judge-sens-free-20260823-{s17,s17b,s17c,s29a,s29b}.json`.

| model | contract | recall s17 / s29 | FP s17 / s29 | tok/pair | latency | verdict |
|---|---|---|---|---|---|---|
| `stealth/ox-alpha` | 12/12 | 6/6 · 4/6 | 0/6 · 0/6 | 183-196 | 11-32s | **admitted** |
| `poolside/laguna-xs-2.1:free` | 12/12 | 5/6 · 4/6 | 0/6 · 1/6 | 196 | 5-12s | **admitted** |
| `poolside/laguna-s-2.1:free` | 12/12 | 4/6 · 1/6 | 2/6 · 0/6 | 181-208 | 6-10s | rejected — unstable |
| `cohere/north-mini-code:free` | 12/12 | 6/6 | **6/6** | 157 | 19s | rejected — flags every clean row |
| `nvidia/nemotron-3.5-lightning:free` | 12/12 | 1/6 | 0/6 | 151 | 8s | rejected — misses 5 of 6 |
| `dots-studio/dots-3-note-preview:free` | 12/12 | — | — | 194 | 8s | rejected — disagreed with the recorded panel on 4/12 |
| `thinkingmachines/inkling:free` | — | — | — | — | — | unusable: 403 "only available on agentic harnesses" |
| `z-ai/glm-5.2:free` | — | — | — | — | — | unusable: upstream 429 (Decart) on every attempt |

For scale, the lanes already admitted measured 6/6 · 4/6 with 1 and 0 false positives
(`a6:qwen3.8-max`) and 6/6 · 4/6 with none (`a6:minimax-m3`), and `a6:minimax-m2.7` was
removed for catching 2 of 6 twice. The two admissions sit inside that band; the four
rejections do not.

## Lane-specific requirements

- **`openrouter:stealth/ox-alpha`** — reasoning is mandatory on this endpoint
  (`reasoning={"enabled": false}` returns HTTP 400) and the default effort is `max`, which
  spends the completion budget thinking and can return an empty 200. `providers.chat()`
  therefore sends `reasoning={"effort": "low"}` for this model, measured at zero reasoning
  tokens and 12/12 usable verdicts. It is also a **stealth preview**: an anonymous third
  party operates it and retains prompts (<https://openrouter.ai/terms/stealth>), so it
  judges the public DQ7 corpus only — never anything under NDA. Being a preview, the slug
  can become paid without notice; the proxy's price check is what stops that from
  silently billing.
- **`openrouter:poolside/laguna-xs-2.1:free`** — no special parameters. Fastest of the
  candidates at 5-12s per batch.

Both are free-tier lanes drawing on the shared 100 requests/day, so they widen the panel's
model diversity and must not be used as a translation pool.

## Re-running the screens

```sh
cd /root/tmp/hanpatch
export OPENROUTER_KEY_FALLBACK=$(grep -oP '(?<=^OPENROUTER_API_KEY=)\S+' ~/.gjc/agent/.env)

python3 tools/judge_screen.py --models 'stealth/ox-alpha' --pairs 12 --seed 17 \
  --reasoning '{"effort":"low"}' --max-tokens 4000 --output /tmp/screen.json

python3 tools/judge_sensitivity.py --models 'poolside/laguna-xs-2.1:free' \
  --url https://openrouter.ai/api/v1/chat/completions \
  --key-env OPENROUTER_KEY_FALLBACK --key-file /dev/null \
  --effort '' --reasoning '{"enabled":false}' --seed 29 --output /tmp/sens.json
```

The screens call `openrouter.ai` directly and therefore bypass the budget proxy's daily
counters — keep a screening pass to a handful of calls, or the production panel finds the
account already spent.

## Per-org Groq lanes in the GJC model picker (2026-08-23)

`groq-1` … `groq-4` are four selectable lanes in `/model`, one per key. The four keys on
this host belong to four DISTINCT Groq organizations, verified by reading the org id out of
a forced 413:

| lane | org | env |
|---|---|---|
| `groq-1` | `org_01kpa497…q021` | `GROQ_API_KEY` |
| `groq-2` | `org_01kr8xvc…nnkw` | `GROQ_API_KEY_2` |
| `groq-3` | `org_01kr91jn…htr5` | `GROQ_API_KEY_3` |
| `groq-4` | `org_01krakcp…hchh` | `GROQ_API_KEY_4` |

Rate limits apply per organization, so these are four independent 30 RPM / 8K TPM
allowances rather than one shared pool. That is the whole reason four lanes are worth four
lanes.

### They are review lanes, not agent lanes — and the flags are load-bearing

Two walls, both measured:

- Groq answers a request body over roughly **20-25 KB** with a bare HTTP 413 (bisected on
  `compound-mini`: 6,531 Korean characters passed, 8,375 returned 413).
- `groq/compound*` is an agentic **wrapper**, not a model with its own budget. Exceeding it
  answers ``Rate limit reached for model `openai/gpt-oss-120b` … Limit 8000``, so the
  documented 70K TPM is not what a call spends.

An ordinary `gjc -p` request is far above the first wall, and the excess is entirely
context GJC injects by default:

| request | body | result |
|---|---|---|
| default cwd (`~/tmp`), full config | 37.2 KB | 413 |
| empty cwd, `--no-tools --no-rules --no-mcp` | 30.9 KB | 413 |
| + `--append-system-prompt "-"` | **6.0 KB** | **200** |

`APPEND_SYSTEM.md` is 14.6 KB and GJC only discovers it when the CLI passes no append text
of its own, so supplying `-` suppresses it. `--no-tools` alone is NOT enough: goal mode
still injects the `goal` tool, and `compound` rejects any `tools` array outright
(HTTP 400 ``tool calling` is not supported with this model``). models.yml
`requestTransform` can add body fields but not remove them, so the strip happens in
`groq-lane-proxy.service` (`127.0.0.1:18101/k<n>/v1`), which drops
`tools`/`tool_choice`/`reasoning_effort`/`reasoning`/`parallel_tool_calls` per lane.

`/usr/local/bin/gjc-review` bakes the working recipe — use it instead of re-deriving:

```sh
gjc-review --lanes                       # list lanes and their constraints
gjc-review --rotate 'is this rendering right?'          # random org, spreads the TPM
gjc-review -m openrouter/stealth/ox-alpha -r 'You grade KO translations.' < pairs.txt
```

`ox-alpha` needs none of this (1M context, tools supported) and is accepted by the same
wrapper only so one command shape covers every free review lane.

`groq/compound` (the large system) is exposed too but reserves about 6.7K of the 8K TPM per
call, so it manages roughly one call per minute per org, and it prepends reasoning prose to
`content` — which is why `compound-mini` is the lane the names point at.

### compound-mini screens well and is still NOT an independent judge

Production harness, same procedure as the OpenRouter candidates:

| | contract | recall s17 / s29 | FP s17 / s29 | tok/pair | latency |
|---|---|---|---|---|---|
| `groq/compound-mini` | 12/12 (agree 10/12) | 6/6 · 4/6 | 0/6 · 1/6 | 486-529 | 5-6s |

That is the admitted band. It is nevertheless folded onto `gpt-oss-120b` by
`qa.lane_model`, because Groq's own rate-limit accounting says that is what it runs. Left
unfolded, a panel of `groq:openai/gpt-oss-120b` plus `groq-1:groq/compound-mini` would
satisfy `REQUIRED_JUDGES` with one model — the `-preview` alias bug under a new spelling.
It costs 4x throughput on that identity, not a new opinion. Artefacts:
`judge-screen-groqmini-20260823.json`, `judge-sens-groqmini-s{17,29}.json`.

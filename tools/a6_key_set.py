#!/usr/bin/env python3
"""Swap the A6 (짱쫀쿠) upstream credential, and record enough to compare keys.

A6 is METERED. A key is not interchangeable with another key the way a free-tier
key is: each carries its own quota, its own spend so far and its own access
expiry, and the reseller exposes all three. So this does not just write the new
secret - it snapshots the OUTGOING key's numbers first, because after the swap
they are unrecoverable and "is the new key better than the old one" becomes
unanswerable.

It also fixes a wiring gap. hanpatch reads A6_API_KEY from its own dotenvs
(~/.hanpatch/env, ~/.env), and the key only ever lived in /etc/a6dq7.env, which
is where the isolated DQ7 worker and tools/a6_*.py read it from. `make('a6:...')`
therefore returned None and the a6 lane could not be built by hanpatch at all.
Both files are written here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = "https://a6.a6api.com"
ETC = "/etc/a6dq7.env"
HANPATCH_ENV = os.path.expanduser("~/.hanpatch/env")
HISTORY = "/root/.a6-key-history.json"
# Cloudflare fronts this host and answers urllib's default User-Agent with an
# empty 403, so the UA is not cosmetic.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PROBE_MODEL = "deepseek-v4-flash"


def get(path: str, key: str, timeout: float = 20.0):
    request = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {key}", "User-Agent": UA},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def snapshot(key: str) -> dict:
    """What this key is worth right now: limit, spend, expiry, models.

    Every field is read from the reseller rather than assumed. `total_usage` is
    reported in CENTS, following the OpenAI dashboard shape this API copies, so
    it is converted here once instead of at each print site.
    """
    out: dict = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        sub = get("/dashboard/billing/subscription", key)
        out["hard_limit_usd"] = sub.get("hard_limit_usd")
        out["access_until"] = sub.get("access_until")
        if sub.get("access_until"):
            out["access_until_local"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(sub["access_until"]))
    except Exception as exc:                                  # noqa: BLE001
        out["subscription_error"] = f"{type(exc).__name__}: {exc}"[:160]
    try:
        start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 90 * 86400))
        end = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 86400))
        usage = get(f"/dashboard/billing/usage?start_date={start}&end_date={end}", key)
        cents = usage.get("total_usage")
        if isinstance(cents, (int, float)):
            out["used_usd"] = round(cents / 100.0, 4)
    except Exception as exc:                                  # noqa: BLE001
        out["usage_error"] = f"{type(exc).__name__}: {exc}"[:160]
    if isinstance(out.get("hard_limit_usd"), (int, float)) and "used_usd" in out:
        out["remaining_usd"] = round(out["hard_limit_usd"] - out["used_usd"], 4)
        # A projection, NOT a rate this reseller charges. It answers "how many tokens
        # would this balance buy IF the rate were $1/M", which is a readable yardstick
        # for comparing keys and nothing more. The measured rate on the `default`
        # supplier group is $0.0024/M (2026-08-11), so the real token capacity is about
        # 417x this number. The name says `at_1usd` for exactly that reason - it was
        # read as a measurement once and put a 417x error into every cost estimate
        # downstream, so do not rename it to anything that sounds like a fact.
        out["remaining_tokens_if_rate_were_1usd_per_mtok"] = int(
            out["remaining_usd"] * 1_000_000)
    try:
        out["models"] = sorted(m["id"] for m in get("/v1/models", key).get("data", []))
    except Exception as exc:                                  # noqa: BLE001
        out["models_error"] = f"{type(exc).__name__}: {exc}"[:160]
    return out


def completion_probe(key: str) -> dict:
    """One real translation call, because /v1/models answers for a dead key too.

    `reasoning_effort: none` is mandatory on this route and is not a tuning
    preference: without it a short line spent all 1,024 completion tokens on
    hidden reasoning and returned empty content, billed at the full output rate
    (1307 tokens vs 281, a 4.65x difference).
    """
    body = json.dumps({
        "model": PROBE_MODEL,
        "messages": [
            {"role": "system", "content": "JSON만 반환한다."},
            {"role": "user",
             "content": '{"0":"はい"} 의 값을 한국어로 번역해 같은 키로 JSON 반환.'},
        ],
        "temperature": 0.0,
        "max_tokens": 256,
        "reasoning_effort": "none",
    }).encode()
    request = urllib.request.Request(
        f"{BASE}/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "User-Agent": UA,
                 "Content-Type": "application/json"})
    began = time.time()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        return {"ok": False,
                "error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    message = (payload.get("choices") or [{}])[0].get("message") or {}
    content = (message.get("content") or "").strip()
    usage = payload.get("usage") or {}
    return {"ok": bool(content), "seconds": round(time.time() - began, 2),
            "content": content[:120], "usage": usage,
            "model_answered": payload.get("model")}


def read_key(path: str) -> str | None:
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if line.startswith("A6_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def write_env(path: str, key: str, mode: int = 0o600) -> None:
    """Set A6_API_KEY in a KEY=VALUE file, leaving every other line alone."""
    lines = []
    if os.path.exists(path):
        lines = open(path, encoding="utf-8").read().splitlines()
    replaced = False
    for i, line in enumerate(lines):
        bare = line[7:] if line.strip().startswith("export ") else line
        if bare.strip().startswith("A6_API_KEY="):
            lines[i] = f"A6_API_KEY={key}"
            replaced = True
            break
    if not replaced:
        lines.append(f"A6_API_KEY={key}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".a6env-")
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def record(entry: dict) -> None:
    history = []
    if os.path.exists(HISTORY):
        try:
            history = json.load(open(HISTORY, encoding="utf-8"))
        except ValueError:
            history = []
    history.append(entry)
    handle, tmp = tempfile.mkstemp(dir=os.path.dirname(HISTORY), prefix=".a6hist-")
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        json.dump(history, fh, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, HISTORY)


def show(title: str, snap: dict) -> None:
    print(f"  {title}")
    for field in ("hard_limit_usd", "used_usd", "remaining_usd",
                  "remaining_tokens_if_rate_were_1usd_per_mtok", "access_until_local"):
        if field in snap:
            print(f"    {field:34} {snap[field]}")
    for field in ("subscription_error", "usage_error", "models_error"):
        if field in snap:
            print(f"    {field:34} {snap[field]}")
    if "models" in snap:
        print(f"    {'models':34} {len(snap['models'])}: {', '.join(snap['models'][:6])}")


def main() -> int:
    parser = argparse.ArgumentParser(description="swap the A6 (짱쫀쿠) API key")
    parser.add_argument("key", nargs="?", help="the new sk-... key")
    parser.add_argument("--show", action="store_true",
                        help="report the CURRENT key's quota and exit, changing nothing")
    args = parser.parse_args()

    current = read_key(ETC)

    if args.show or not args.key:
        if not current:
            print(f"no A6_API_KEY in {ETC}", file=sys.stderr)
            return 1
        print(f"current key {current[:8]}... in {ETC}")
        show("quota", snapshot(current))
        probe = completion_probe(current)
        print(f"  completion probe: {'OK' if probe['ok'] else 'FAILED'} "
              f"{probe.get('error') or probe.get('content')!r} "
              f"usage={probe.get('usage')}")
        return 0 if probe["ok"] else 1

    new = args.key.strip().strip('"').strip("'")
    if not new.startswith("sk-") or len(new) < 16 or any(c.isspace() for c in new):
        print(f"refusing: {new[:10]}... is not shaped like an A6 key (sk-...)",
              file=sys.stderr)
        return 2
    if new == current:
        print("refusing: that is already the current key", file=sys.stderr)
        return 2

    print("snapshotting the OUTGOING key before it is overwritten "
          "(these numbers cannot be read back afterwards)")
    old_snap = snapshot(current) if current else {"note": "no previous key"}
    show(f"old {current[:8] + '...' if current else 'none'}", old_snap)

    print(f"\nprobing the NEW key before installing it")
    new_snap = snapshot(new)
    show(f"new {new[:8]}...", new_snap)
    new_probe = completion_probe(new)
    print(f"    {'completion probe':34} "
          f"{'OK' if new_probe['ok'] else 'FAILED ' + str(new_probe.get('error'))}"
          f" {new_probe.get('usage') or ''}")
    if not new_probe["ok"]:
        print("refusing to install a key that does not answer; nothing was changed",
              file=sys.stderr)
        record({"event": "rejected", "old": old_snap, "new": new_snap,
                "new_probe": new_probe, "new_key_prefix": new[:10]})
        return 1

    write_env(ETC, new, 0o600)
    write_env(HANPATCH_ENV, new, 0o600)
    record({"event": "swapped", "old_key_prefix": (current or "")[:10],
            "new_key_prefix": new[:10], "old": old_snap, "new": new_snap,
            "new_probe": new_probe})
    print(f"\ninstalled into {ETC} and {HANPATCH_ENV} (0600)")
    print(f"history appended to {HISTORY}")

    print("\ncomparison")
    for field in ("hard_limit_usd", "used_usd", "remaining_usd",
                  "remaining_tokens_if_rate_were_1usd_per_mtok", "access_until_local"):
        if field in old_snap or field in new_snap:
            print(f"  {field:34} old={old_snap.get(field)}  new={new_snap.get(field)}")
    old_models = set(old_snap.get("models") or [])
    new_models = set(new_snap.get("models") or [])
    if old_models or new_models:
        print(f"  {'models gained':34} {sorted(new_models - old_models) or 'none'}")
        print(f"  {'models lost':34} {sorted(old_models - new_models) or 'none'}")
    print("\nNOTE: the guest VM 101 keeps its own copy of /etc/a6dq7.env "
          "(0640 root:a6worker). Update it there too if that lane will be used.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)

"""Batch translation runner: fills work/ko/tm.json for one message family."""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor


from hanpatch import glossary
from hanpatch import providers
from hanpatch import tm
from hanpatch import capacity as capmod  # noqa: E402
from hanpatch import translate

from hanpatch import config

def REVIEW_FMT():
    return config.out('review_%s.json')
# primary model per family keeps the register stable; the rest are repair fallbacks
# There is deliberately no model table here any more. This one WAS the second
# authority on model selection: it bypassed providers.build_pool() entirely, so the
# registry-derived, liveness-verified pool was not what the translator actually
# called. It still listed nimproxy:z-ai/glm-5.2, which was measured as producing no
# first byte in 90 seconds and dominated the retries of a real run, and
# nimproxy:deepseek-ai/deepseek-v4-pro, whose slug is the PAID identity on
# api.deepseek.com and which the registry grants no role. Selection order is now:
# --models (an explicit caller list), then the profile's own `models` if the title
# declares one, then providers.build_pool(), which resolves the registry by role and
# falls back to the pinned specs.


def load_review(path):
    if os.path.exists(path):
        return config.load_object(path, 'the translation review shard')
    return {}



def save_json_locked(path, updates, removals=(), what='the translation state shard'):
    """Merge `updates` into the on-disk JSON under an exclusive inter-process
    lock, then replace it durably. Safe for several runners on one shard."""
    import fcntl
    lockpath = path + '.lock'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(lockpath, 'a+') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            cur = {}
            if os.path.exists(path):
                cur = config.load_object(path, what)
            cur.update(updates)
            for k in removals:
                cur.pop(k, None)
            tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
            with open(tmp, 'w') as fh:
                json.dump(cur, fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            dfd = os.open(os.path.dirname(path) or '.', os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            return cur
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def qa_reasons(flagged, merged, floor):
    """source -> actionable judge complaints, from the `pair_key`-keyed QA review.

    A pair is listed when ANY of its records is flagged, so the passing records in the
    same list are not work. A complaint is dropped once the value it judged is no longer
    the shipped one, because a later repair already answered it.
    """
    reasons = {}
    for recs in flagged.values():
        for rec in recs if isinstance(recs, list) else ():
            if not isinstance(rec, dict):
                continue
            if (rec.get('d') == 'pass'
                    and min(rec.get('a', 0), rec.get('f', 0)) >= floor):
                continue
            en = rec.get('en')
            if not en or tm.lookup(merged, en) != rec.get('ko'):
                continue
            reasons.setdefault(en, []).append(str(rec.get('r', '')).strip())
    return reasons

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--family', required=True)
    ap.add_argument('--batch-chars', type=int, default=2600)
    ap.add_argument('--max-items', type=int, default=14)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--models', default='')
    ap.add_argument('--workers', type=int, default=1)
    ap.add_argument('--refail', action='store_true',
                    help='re-translate entries whose current value fails validation')
    ap.add_argument('--qafail', action='store_true',
                    help='re-translate entries the semantic QA judge flagged')
    args = ap.parse_args(argv)

    providers.load_dotenv()
    if args.models:
        pool = providers.build_pool(args.models.split(','))
    else:
        declared = config.prof('models') or []
        pool = providers.build_pool(declared or None)
    src = config.load_object(config.src_path(), 'the extracted source')
    if args.qafail:
        from hanpatch import qagate
        flagged = config.load_object(config.out('qa_flagged.json'),
                                     'the QA flagged review')
        reasons = qa_reasons(flagged, tm.load(), qagate.FLOOR)
        todo = []
        seen = set()
        for it in src[args.family]:
            en = it['en']
            if en in reasons and en not in seen:
                seen.add(en)
                todo.append({'en': en, 'jp': it.get('jp', ''),
                             'group': capmod.group(args.family, it['key']),
                             'qa': '; '.join(r for r in reasons[en] if r)[:300],
                             'refs': [f"{args.family}:{it['key']}"]})
    elif args.refail:
        merged = tm.load()
        gl = glossary.load()
        todo = []
        seen = set()
        for it in src[args.family]:
            en = it['en']
            if tm.is_skip(en, it['key']) or not en.strip() or en in seen:
                continue
            ko = tm.lookup(merged, en)
            if ko is None:
                continue
            _, probs = translate.check(en, ko, glossary.relevant(gl, [en]), args.family)
            if probs:
                seen.add(en)
                todo.append({'en': en, 'jp': it.get('jp', ''),
                             'group': capmod.group(args.family, it['key']),
                             'refs': [f"{args.family}:{it['key']}"]})
    else:
        todo = [x for x in tm.untranslated(src)
                if x['refs'][0].split(':')[0] == args.family]
    if args.limit:
        todo = todo[:args.limit]
    print(f'{args.family}: {len(todo)} strings, {sum(len(x["en"]) for x in todo)} chars, '
          f'pool={[p.id for p in pool]}', flush=True)

    # split into batches up front so workers never race over ordering
    batches = []
    i = 0
    while i < len(todo):
        batch = []
        chars = 0
        while i < len(todo) and len(batch) < args.max_items:
            n = len(todo[i]['en'])
            if batch and chars + n > args.batch_chars:
                break
            batch.append(todo[i])
            chars += n
            i += 1
        batches.append(batch)

    tr = translate.Translator(pool=pool, kind=args.family)
    shard = config.out(f'tm_{args.family}.json')
    review_path = REVIEW_FMT() % args.family
    review = load_review(review_path)
    tmdb = (config.load_object(shard, 'the translation memory shard')
            if os.path.exists(shard) else {})
    # fixed style anchor: already-approved pairs from this family keep the
    # register identical across parallel workers
    anchor = []
    merged = tm.load()
    for it in config.load_object(config.src_path(), 'the extracted source')[args.family]:
        en = it['en']
        if en in merged and len(anchor) < 3 and len(en) > 60:
            anchor.append((en, merged[en]))
    # Snapshot the enforced contract before any row is written, so every row this run
    # produces carries the version it was validated against.
    anchor_ver = glossary.record_version()
    print(f'  anchor version {anchor_ver} '
          f'({len(glossary.enforced_contract())} enforced terms)', flush=True)
    lock = threading.Lock()
    t0 = time.time()
    done = [0]

    def work(batch):
        res, failed = tr.batch(batch, context=anchor)
        with lock:
            got = {batch[k]['en']: ko for k, ko in res.items()}
            prov = {batch[k]['en']: tr.last_provider.get(k, '')
                    for k in res}
            bad = {batch[k]['en']: {'refs': batch[k]['refs'],
                                    'reason': 'validation failed'} for k in failed}
            tmdb.update(got)
            save_json_locked(shard, got, what='the translation memory shard')
            save_json_locked(config.out(f'prov_{args.family}.json'), prov,
                             what='the provenance shard')
            # Which enforced contract did this row pass under? Without it an anchor
            # edit can only invalidate everything, because nothing records what was
            # actually validated. With it, glossary.resweep() selects just the rows
            # whose own obligations moved - soft hints are excluded from the contract
            # by construction, so renaming a hint costs no revalidation at all.
            save_json_locked(config.out(f'anchorver_{args.family}.json'),
                             {en: anchor_ver for en in got},
                             what='the anchor version shard')
            save_json_locked(review_path, bad, removals=list(got),
                             what='the translation review shard')
            done[0] += len(batch)
            print(f'  [{done[0]}/{len(todo)}] ok={tr.stats["ok"]} '
                  f'fail={tr.stats["failed"]} calls={tr.stats["calls"]} '
                  f'{time.time() - t0:.0f}s', flush=True)

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            list(ex.map(work, batches))
    else:
        for b in batches:
            work(b)
    print(f'done: ok={tr.stats["ok"]} failed={tr.stats["failed"]} '
          f'calls={tr.stats["calls"]} in {time.time() - t0:.0f}s')


if __name__ == '__main__':
    main()

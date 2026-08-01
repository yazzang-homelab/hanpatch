"""Derive proven per-layout text-box capacities from the shipped English text.

Capacity is keyed by `family/key-shape` (digits folded to `#`) instead of by
family, so a group like `system/treasure` is bounded at the single line it
actually renders rather than borrowing the whole family maximum.
"""
import json
import os
import re
import sys


from hanpatch import wrap

from hanpatch import config

def OUT():
    return config.out('capacity.json')


def group(family, key):
    g = re.sub(r'\d+', '#', key)
    g = re.sub(r'_#+$', '', g)
    return f'{family}/{g}'


def build(src_path=None):
    src_path = src_path or config.src_path()
    src = json.load(open(src_path))
    cap = {}
    for family, items in src.items():
        budget = wrap.budget_for(family)
        for it in items:
            en = it['en']
            # Same predicate as the layout gate: a title whose container stores
            # one display line per row must still DERIVE a capacity, or the gate
            # it feeds has nothing measured to enforce.
            if not en.strip() or wrap.engine_lays_out(en):
                continue
            g = group(family, it['key'])
            pages = wrap.pages(wrap.rewrap(en, budget))
            n = max(pages) if pages else 0
            cap[g] = max(cap.get(g, 0), n)
    json.dump(cap, open(OUT(), 'w'), indent=1, sort_keys=True)
    # A process that already read the old table keeps it cached, and the new
    # precedence would silently degrade to the profile value. Gate order hides
    # this today and the reference title hides it further, because its profile
    # equals its derived maxima - it will bite the first title where they differ.
    wrap.invalidate_capacity()
    return cap


if __name__ == '__main__':
    c = build()
    print(f'{len(c)} layout groups -> {OUT()}')

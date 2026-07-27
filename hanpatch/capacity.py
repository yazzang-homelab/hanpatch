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
        budget = wrap.BUDGET.get(family, wrap.BUDGET['default'])
        for it in items:
            en = it['en']
            if not en.strip() or wrap.is_freeform(en):
                continue
            pages = wrap.pages(wrap.rewrap(en, budget))
            n = max(pages) if pages else 0
            g = group(family, it['key'])
            cap[g] = max(cap.get(g, 0), n)
    json.dump(cap, open(OUT(), 'w'), indent=1, sort_keys=True)
    return cap


if __name__ == '__main__':
    c = build()
    print(f'{len(c)} layout groups -> {OUT()}')

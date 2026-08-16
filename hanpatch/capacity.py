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


def _per_family_evidence(src):
    """family -> (widest measured source line in px, number of measured lines).

    Measured means the rows this derivation actually walks: a row the engine lays
    out is not a display line, so counting it would compare a budget against text
    nobody stores line by line.
    """
    evidence = {}
    for family, items in src.items():
        widest = 0.0
        lines = 0
        for it in items:
            en = it['en'] or ''
            if not en.strip() or wrap.engine_lays_out(en):
                continue
            for line in en.split('\n'):
                if not line.strip():
                    continue
                lines += 1
                widest = max(widest, wrap.text_width(line))
        if lines:
            evidence[family] = (widest, lines)
    return evidence


def build(src_path=None):
    src_path = src_path or config.src_path()
    src = json.load(open(src_path))
    cap = {}
    # The layout DECLARATIONS get checked here, before a single width is
    # consumed, because this is the gate that measures with them:
    #   * a substitution tag with no declared width would otherwise raise a bare
    #     ValueError from inside the rewrap below, halfway through a gate run.
    #   * a budget equal to the widest SOURCE line is the signature of a number
    #     copied out of the text. DQ7 declared 321px that way and 287px lines
    #     still spilled outside the frame on a device, so the title has to say
    #     where its number came from.
    # The source maximum is taken over the rows this derivation actually
    # measures, and only when a measurement font exists: comparing against rows
    # nobody measures would invent evidence, and `text_width` needs the shipped
    # font. Without evidence the budget check is skipped rather than guessed -
    # the tag check above needs no font and always runs.
    measured = [line
                for items in src.values() for it in items
                if (it['en'] or '').strip() and not wrap.engine_lays_out(it['en'])
                for line in it['en'].split('\n')]
    source_max = (max((wrap.text_width(line) for line in measured), default=0)
                  if measured and wrap.have_font() else None)
    wrap.assert_layout_declared(source_max)
    # The same evidence, kept per family. A title-wide maximum cannot see a family
    # that was handed another box's width, because the widest line in the corpus
    # belongs to the widest box in the game.
    if wrap.have_font():
        wrap.assert_budget_matches_evidence(_per_family_evidence(src))
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

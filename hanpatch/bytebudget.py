"""Byte budgets for text stored INSIDE fixed-stride records.

A line the engine reaches through an offset table may be any length, because the
offset is rewritten with it. A line stored inside a record cannot move and cannot
grow: it occupies a field of fixed width, and the byte that lands past the field
is the next column - a numeric stat that no gate downstream reads back. The
symptom is not a clipped name, it is a monster with the wrong ATK.

So this constraint is measured in BYTES, and the layout gate cannot express it: a
two-syllable name is comfortably inside any text box while being two bytes too
long for the field it lives in.

It is enforced in `translate.check`, with every other target-side rule, because
that is the last point where a lane can still be told to shorten the line. At
inject the corpus is already sealed and the only options left are truncating the
text or aborting the build.

One Korean string serves every slot that shares its Japanese source, so the
budget a source carries is the MINIMUM over those slots.
"""
import json
import os

from hanpatch import config

FILE = 'db_budget.json'

_CACHE = None
_KEY = None


def load():
    """{family: {source: bytes}}; empty when the title has no such fields."""
    global _CACHE, _KEY
    path = config.work(FILE)
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        _CACHE, _KEY = {}, None
        return _CACHE
    if _CACHE is None or key != _KEY:
        # Shape-validated, not bare `json.load`. A budget document that is a list or
        # a scalar would otherwise become the cache, and every `of()` lookup would
        # then raise mid-gate or - worse - return None and report every row
        # unconstrained, which is how an over-budget name reaches inject.
        _CACHE = config.load_object(path, 'the byte-budget map')
        _KEY = key
    return _CACHE


def of(family, source):
    """The byte budget for one source line, or None when unconstrained."""
    return load().get(family, {}).get(source)


def _encoded_length(text):
    """Bytes `text` occupies once encoded, asked of the title's adapter.

    The adapter owns the encoding, so it owns the count: this title spends two
    bytes on a Hangul syllable because the syllable travels as the Shift-JIS code
    of the font cell it was baked into, and only the adapter knows that map.
    """
    from hanpatch import adapter as _adapter
    fn = getattr(_adapter.project_adapter(), 'encoded_length', None)
    if fn is None:
        return None
    return fn(text)


def check(en, ko, family):
    """[problem] when `ko` does not fit the field `en` was read out of."""
    budget = of(family, en)
    if budget is None:
        return []
    n = _encoded_length(ko)
    if n is None or n <= budget:
        return []
    return ['%d bytes exceeds the %d this field stores '
            '(shorten the translation)' % (n, budget)]

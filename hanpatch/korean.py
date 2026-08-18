"""Korean spacing, checked structurally rather than asked of a model.

Korean writes spaces between eojeol; Japanese writes none. A translator that
carries the source's spacing across produces a line that is legible to a
machine, scores well on meaning, and is simply wrong to read. That failure is
mechanical, so it belongs in a gate and not in a judge's opinion.

WHY A JUDGE IS NOT ENOUGH, measured on this title: a single-lane screen over
3624 pairs flagged 240 defects, of which 79 were spacing. The same corpus holds
319 rows whose source is multi-token and whose translation has no space at all,
so the screen agreed with about a quarter of them and passed the rest. It found
the one family where the fault was at 91% and missed it where it sat at 5-10%.
That is what a correlated blind spot looks like, and it is the argument for
checking this in code.

THE THRESHOLD IS MEASURED, NOT CHOSEN. Across every translated line on this
disc that DOES carry spaces, 6780 eojeol were observed:

    length   1     2     3     4     5     6     7     8
    share  15.3  31.9  30.4  14.6   5.4   1.9   0.5   0.1

p99 is 6 characters and the longest observed is 8. A run of 9 or more Hangul
characters with no space has therefore never occurred as a single eojeol in this
corpus, which is what makes it a defect rather than a preference. Recalibrate
against a title's own text rather than importing this number.
"""
import re

#: longest Hangul run observed as one eojeol in a corpus that spaces correctly
MAX_EOJEOL = 8

_RUN = re.compile(r'[\uac00-\ud7a3]+')


def runs(text):
    """Every maximal Hangul run, longest first."""
    return sorted(_RUN.findall(text or ''), key=len, reverse=True)


def unspaced(text, limit=MAX_EOJEOL):
    """Hangul runs too long to be one word, i.e. a missing space.

    Punctuation, Latin and digits break a run, so `가나다,라마바` is two runs and
    not one long one - a comma is a boundary a reader sees even without a space.
    """
    return [r for r in _RUN.findall(text or '') if len(r) > limit]


def check(text, limit=MAX_EOJEOL):
    """A problem string, or None. Shaped for `translate.check`'s problem list."""
    bad = unspaced(text, limit)
    if not bad:
        return None
    return ('korean spacing: %d-character run with no space (longest eojeol '
            'measured is %d)' % (len(bad[0]), limit))

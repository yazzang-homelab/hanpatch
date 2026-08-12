"""Consistency / integrity audit over the whole translation memory."""
import json
import os
import re
import sys
import difflib
from collections import Counter, defaultdict


from hanpatch import glossary
from hanpatch import manifest as manmod
from hanpatch import tm
from hanpatch import capacity as capmod  # noqa: E402
from hanpatch import translate
from hanpatch import wrap

from hanpatch import config

LAST_EXAMINED = 0


TAG = re.compile(r'<[^>\n]*>')
POLITE = re.compile(r'(니다|세요|십시오|습니까|나요)[.!?"\'”’)\]]*$')
PLAIN = re.compile(r'(다|였다|한다|된다|이다)[.!?"\'”’)\]]*$')


# A sentence ends at final punctuation, not at a line break. Newlines are joined with a
# space so a word split across a wrap does not fuse into a different word.
_SENT_END = re.compile(r'(?<=[.!?…。？！])\s+')


def sentences(text):
    flat = ' '.join(l.strip() for l in text.split('\n'))
    return [p for p in _SENT_END.split(flat) if p.strip()]


def main():
    global LAST_EXAMINED
    src = config.load_object(config.src_path(), 'the extracted source')
    # Audit what SHIPS. `inject` is handed the sealed manifest, so that is the artifact a
    # release claim is about. Reading the merged TM instead was measured wrong on DQ7
    # 2026-08-11: `tm.lookup` matches with the soft-break marker stripped, so for
    # `はがねの;つるぎ` it returned the `;`-less '강철검' while the manifest - and the ROM -
    # carried '강철;검'. That produced 76 soft-break failures plus 58 register/normalisation
    # failures against text no player would ever see, and blocked a build whose actual
    # output was clean. The manifest also holds the normalisation `translate.check` applies
    # on its return path, which the raw TM predates.
    man_entries = {}
    try:
        man_entries = config.load_object(manmod.PATH(), 'the sealed manifest')['entries']
    except SystemExit:
        # No seal yet: fall back to the TM so `audit` still works before the first
        # `manifest build`, and say so rather than silently checking something else.
        print('  note: no sealed manifest; auditing the translation memory instead')
    tmdb = tm.load()
    gl = glossary.load()
    fails = defaultdict(list)
    stats = Counter()
    register = defaultdict(Counter)

    for family, items in src.items():
        for it in items:
            # The same source resolution the gate and the injector use. `it['en']` alone is
            # empty on placeholder rows, where the Japanese column is the real source.
            en = it['en']
            if tm.is_skip(en, it['key']) or not en.strip():
                continue
            stats['total'] += 1
            ko = man_entries.get(f'{family}/{it["key"]}')
            if ko is None:
                ko = tm.lookup(tmdb, en)
            if ko is None:
                stats['untranslated'] += 1
                fails['untranslated'].append(f'{family}:{it["key"]}')
                continue
            stats['translated'] += 1
            sub = glossary.relevant(gl, [en], family)
            fixed, probs = translate.check(en, ko, sub, family,
                                           capmod.group(family, it['key']))
            for p in probs:
                fails[p.split(':')[0]].append(f'{family}:{it["key"]} :: {p}')
            # A gate that only normalises cannot fail, so a rule added after the
            # seal was written is invisible here: `check` quietly returns the
            # repaired string and audit throws it away. Measured when the
            # punctuation and josa passes landed - 7905 sealed rows carried `….`
            # and 2416 a guessed particle after a runtime name, and audit reported
            # the corpus clean. The sealed value must equal what today's rules
            # produce from it, or the ROM ships text this build disagrees with.
            if not probs and fixed != ko:
                fails['normalisation-drift'].append(
                    f'{family}:{it["key"]} :: sealed {ko[:40]!r} != normalised '
                    f'{fixed[:40]!r}')
            # register mixing inside one string
            # Register belongs to a SENTENCE, not to a line. A dialogue box wraps mid
            # sentence, so classifying each line makes any wrap look like a sentence ending.
            # Measured on DQ7 2026-08-11: all 8 remaining "mixed register" reports were this
            # - '숨기셔도 저에게는 다' scored plain because the line ends in the ADVERB 다, and
            # '이전보다' because it ends in the particle 보다, while both strings were wholly
            # polite. Splitting on sentence-final punctuation instead reports 0.
            lines = [l.strip() for l in sentences(TAG.sub('', ko)) if l.strip()]
            pol = sum(1 for l in lines if POLITE.search(l))
            pla = sum(1 for l in lines if PLAIN.search(l) and not POLITE.search(l))
            if pol and pla:
                register[family]['mixed'] += 1
                fails['register-mixed'].append(f'{family}:{it["key"]}')
            elif pol:
                register[family]['polite'] += 1
            elif pla:
                register[family]['plain'] += 1

    print('=== coverage ===')
    for k in ('total', 'translated', 'untranslated'):
        print(f'  {k:14} {stats[k]}')
    print('=== integrity ===')
    if not fails:
        print('  no problems')
    for k, v in sorted(fails.items(), key=lambda kv: -len(kv[1])):
        print(f'  {k:22} {len(v)}')
        for s in v[:4]:
            print(f'      {s[:150]}')
    print('=== register per family (plain / polite / mixed) ===')
    for f, c in sorted(register.items()):
        print(f'  {f:11} {c["plain"]:5} {c["polite"]:5} {c["mixed"]:5}')
    print('=== dedup ===')
    print(f'  unique EN keys: {len(tmdb)}, distinct KO values: '
          f'{len(set(tmdb.values()))}')

    # unresolved review-queue entries must be zero before a build
    import glob
    pending = {}
    for p in glob.glob(config.out('review_*.json')):
        try:
            pending.update(config.load_object(p, 'the pending review shard'))
        except (OSError, SystemExit):
            continue
    # A row the profile says NOT to translate is not pending review. Without this the queue
    # can never be emptied and the release bar is held shut by a rule stating the work should
    # not be done: measured on DQ7 2026-08-11, 9 markup-only sources (`<BLANK>`, `<JA_HP>`,
    # `<JA_EQUIP>`, ...) were recorded before `skip_tag_only` existed, have no translation by
    # design, and so survived the has-a-translation filter forever.
    pending = {k: v for k, v in pending.items()
               if tm.lookup(tmdb, k) is None and not tm.is_skip(k)}
    print(f'=== review queue ===\n  unresolved: {len(pending)}')
    for k in list(pending)[:5]:
        print(f'      {k[:90]!r}')

    print('=== term rendering consistency ===')
    incons, collide, examined = term_rendering(src, tmdb)
    print(f'  glossary runs present in translated source: {examined}')
    print(f'  runs whose Korean form is missing from some renderings: {len(incons)}')
    for term, have, miss in incons[:6]:
        print(f'      {term!r}: present in {have}, absent in {miss} translated strings')
    print(f'  Korean forms appearing where their source run is absent: {len(collide)}')
    for term, n in collide[:6]:
        print(f'      form mandated for {term!r} used in {n} unrelated strings')

    decided_bad, decided_rows = decided_terms(src, man_entries or tmdb,
                                              from_manifest=bool(man_entries))
    if config.prof('decided_terms'):
        print('=== reader decisions ===')
        print(f'  decided runs: {len(config.prof("decided_terms"))}, '
              f'rows carrying one: {decided_rows}')
        print(f'  rows contradicting a decision: {len(decided_bad)}')
        for row in decided_bad[:10]:
            print(f'      {row}')

    print('=== terminology drift ===')
    drift = terminology_drift(src, tmdb)
    print(f'  near-identical source pairs with divergent Korean: {len(drift)}')
    for a_, b_, r in drift[:6]:
        print(f'      ratio={r:.2f} {a_[:70]!r}')
        print(f'                 {b_[:70]!r}')

    # The release bar is not "the gate passed": a gate sees one string at a time, so a
    # corpus can pass every string and still ship two Korean names for one thing. Both
    # counts below are therefore REPORTED - and both are advisory, which is a measured
    # decision, not caution. Making collisions fatal was tried and it broke the pinned
    # reference project: 34 collisions on a fully released corpus, every one from a
    # common-noun hard term whose Korean form legitimately occurs in strings that do not
    # contain the exact English word, because English inflects and paraphrases
    # ('Spellbook' mandated, its Korean rendering present in 102 other strings).
    # `hard_terms` only means "classified proper nouns" where a title took the trouble
    # to classify them; it is not a portable proxy for that. A title that wants these
    # fatal must say so, not inherit it silently.
    hard_fail = (stats['untranslated'] + len(pending) + len(decided_bad) +
                 sum(len(v) for k, v in fails.items() if k != 'register-mixed'))
    print(f'  release bar: 0 untranslated, 0 unresolved reviews | advisory: '
          f'{len(incons)} inconsistent renderings, {len(collide)} name collisions')
    print(f'\nHARD FAILURES: {hard_fail}')
    LAST_EXAMINED = stats['total']
    return 1 if hard_fail else 0


def decided_terms(src, values, from_manifest=True):
    """Rows that contradict a wording a reader asked for and an operator approved.

    General term consistency is advisory here for reasons the release-bar comment gives:
    a mandated Korean form legitimately turns up in strings that do not carry the source
    run, and making that fatal broke a released corpus. A DECIDED term is the opposite
    case and is fatal, because the evidence is different in kind:

      * it names one source run and one Korean form, both written down with the order
        that decided them, so there is nothing to infer;
      * it exists because a player reported the old wording from real hardware, which is
        the one place none of the machine checks can see;
      * and the failure it prevents is the one readers actually file - the name table
        says one thing and the dialogue that names the item says another.

    Measured on DQ7: three decisions (城下町, コック長, アミット漁) reversed an earlier
    order, and applying them only to the 138 rows the order named would have left 196
    other rows contradicting them. Nothing catches that but this.
    """
    decided = config.prof('decided_terms') or {}
    if not decided:
        return [], 0
    bad, rows = [], 0
    ruby = re.compile(r'\{\d+[^}]*\}')
    for family, items in src.items():
        for it in items:
            en = it['en']
            if tm.is_skip(en, it['key']) or not en.strip():
                continue
            ko = (values.get(f'{family}/{it["key"]}') if from_manifest
                  else tm.lookup(values, en))
            if not ko:
                continue
            flat = ruby.sub('', en)
            for run, spec in decided.items():
                if run not in flat:
                    continue
                rows += 1
                want = spec['ko'] if isinstance(spec, dict) else spec
                order = spec.get('order', '?') if isinstance(spec, dict) else '?'
                # The stored value carries the container's own breaks, so a form that
                # wrapped is still the form.
                loose = re.compile(r'[\s\u3000;]*'.join(
                    re.escape(c) for c in want if not c.isspace()))
                if not loose.search(ko):
                    bad.append(f'{family}/{it["key"]}: source has {run!r}, '
                               f'{order} decided {want!r}, shipped {ko.strip()[:40]!r}')
    return bad, rows


def signature(en):
    """Template signature: digits folded, proper-noun runs collapsed."""
    s = re.sub(r'\d+', '#', en)
    # only fold mid-sentence capitalised runs: a sentence-initial word carries
    # real meaning ("Increases ..." vs "Decreases ...")
    s = re.sub(r"(?<=[^.!?\n] )[A-Z][a-z]+(?:'[a-z]+)?(?: [A-Z][a-z]+)*", 'X', s)
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s


def term_rendering(src, tmdb):
    """Report, do not gate: where does one source run render two ways?

    `terminology_drift` below cannot see this on a Japanese source. It groups by
    `signature(en)`, and signature folds digits and LATIN capitalised runs - a Japanese
    string has no Latin capitals, so the proper-noun folding is a no-op and the grouping
    degenerates to near-exact template matching. Paraphrases and 'the same rare name
    rendered two ways in two unrelated sentences' pass it silently.

    This is the safety net for the part the glossary gate does not cover. Only the terms
    declared `hard_terms` are enforced at translation time, deliberately - enforcing a
    substring on a common noun false-fails fluent Korean, which is pro-drop and
    rephrases. That leaves every SOFT term with no signal at all. Counting is the right
    instrument for those: a report cannot false-fail a correct translation, and it
    cannot let drift through unseen either.

    Two findings over the translated pairs:
      - inconsistency, for EVERY glossary run: the run appears in N translated source
        strings and its declared Korean form appears in only some of their translations.
      - collision, for PROPER NOUNS ONLY: the Korean form declared for one run turns up
        in translations whose source does not contain that run, i.e. one Korean name
        serving two things. Scoped deliberately - measured on 126 translated pairs the
        unscoped version reported 34 collisions and every one was a common noun, because
        the Korean rendering of a word like 'work' legitimately occurs everywhere. On a
        proper noun the same signal is real. `hard_terms` is the classified proper-noun
        set, so it is the scope; widening it turns the report into noise nobody reads.
    """
    gl = glossary.load()
    pairs = []
    if not gl:
        raise SystemExit('TERM REPORT REFUSED: the glossary is empty, so this would '
                         'examine nothing and report clean')
    for family, items in src.items():
        for it in items:
            en = it['en']
            if not en.strip():
                continue
            ko = tm.lookup(tmdb, en)
            if ko:
                pairs.append((en, ko.replace('\n', ' ')))
    present = defaultdict(lambda: [0, 0])       # term -> [rendered with, rendered without]
    collide = defaultdict(int)
    proper = set(glossary.hard())
    for term, koterm in gl.items():
        if not term or not koterm:
            continue
        for en, ko in pairs:
            in_src = term in en
            in_ko = koterm in ko
            if in_src:
                present[term][0 if in_ko else 1] += 1
            elif in_ko and term in proper:
                collide[term] += 1
    incons = sorted(((t, v[0], v[1]) for t, v in present.items() if v[0] and v[1]),
                    key=lambda r: -r[2])
    examined = sum(1 for v in present.values() if v[0] or v[1])
    if pairs and not examined:
        # Reporting clean after examining zero runs is the same 'verified nothing
        # therefore fine' shape the superblock check refuses. Say so instead.
        print('  note: no glossary run occurs in any translated source string')
    return incons, sorted(collide.items(), key=lambda r: -r[1]), examined


def terminology_drift(src, tmdb, ko_sim=0.60):
    """Same-template English whose Korean diverges. Covers the whole corpus."""
    groups = defaultdict(list)
    for family, items in src.items():
        for it in items:
            en = it['en']
            if tm.is_skip(en, it['key']) or not en.strip() or len(en) < 30:
                continue
            ko = tm.lookup(tmdb, en)
            if ko:
                groups[(family, signature(en))].append((en, ko))
    out = []
    for _, rows in groups.items():
        if len(rows) < 2:
            continue
        base_en, base_ko = rows[0]
        for en, ko in rows[1:]:
            r = difflib.SequenceMatcher(None, base_ko, ko).ratio()
            if r < ko_sim:
                out.append((base_en, en, r))
    return out


if __name__ == '__main__':
    sys.exit(main())

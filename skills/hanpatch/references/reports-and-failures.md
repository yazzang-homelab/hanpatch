# User reports and failed attempts

Two ledgers the gates cannot replace. Both exist to stop the same thing: solving
a problem twice, or solving it once and losing the proof.

## A report becomes a regression case or it did not get fixed

A defect found by a player is the only kind the gates already failed to catch.
Fixing the string and moving on guarantees the next rebuild can undo it silently.

For each report:

1. **Identify** release version, bundle hash, input ROM hash, platform
   (emulator/hardware), and the scene. Missing identity means it is a lead, not
   a defect — ask before analysing.
2. **Deduplicate** by symptom *and* root cause. Keep every reporter in the
   chain; count the cause once.
3. **Classify** before editing: translation wording, layout/capacity, control
   tag, font glyph, packer, container, emulator behaviour, or user misapplied
   the patch. Each has a different owner and a different fix.
4. **Smallest fix.** One string, one budget, one glyph — not a re-translation of
   the family.
5. **Add the case where a machine will re-check it.** A wording or tag defect
   becomes a test in `tests/`; a capacity defect becomes a measured budget in the
   profile; a glyph defect becomes a coverage assertion against the built font.
   If none of those can hold it, say so in Honest limitations instead of
   pretending it is closed.
6. **Rebuild from clean input** and verify the reported scene *and its
   neighbours* — the adjacent page is where a length fix reappears.
7. **Do not call it final on one passing path.** Keep unreproduced and minority
   reports open until their evidence is closed.

## Failed attempts stay searchable

Record every disproved hypothesis. Deleting it means re-running it in three
weeks.

- hypothesis, and the observation that would have confirmed it
- exact input hash, output hash, tool, and version
- command run and where the artifact landed
- the result, and **why it disproves the hypothesis** — or why it merely failed
  to prove it, which is a different outcome
- what is still unknown
- the explicit condition under which it may be revisited

Mark superseded conclusions as superseded. Do not delete them: the reason a path
was abandoned is the part that prevents its reuse.

## Do not retry an unchanged path

Retry only when at least one premise actually moved:

- new runtime evidence contradicts the old conclusion
- new instrumentation can observe the variable the old test could not
- the target build, region, or revision changed
- a hidden assumption was found to be false
- the earlier test never reached the required scene or checkpoint
- a genuinely equivalent, verified fix exists elsewhere — equivalent means same
  renderer and same container, not same symptom

"It might work this time" is not on the list.

## Do not transfer a fix on symptom similarity

Two titles clipping text at the same column may have nothing in common: one is a
width budget, the other a terminator. Port the *mechanism* only after proving the
same ownership in the target — and state the smallest proof you accepted.

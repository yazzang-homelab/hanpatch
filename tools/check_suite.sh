#!/bin/bash
# The check suite a delegated change to this package has to pass.
#
# These tests are standalone scripts, not pytest cases - pytest's assertion
# rewriting imports them at collection time and their module-level `sys.exit`
# aborts the whole run with an INTERNALERROR, so a `pytest -q tests` suite here
# reports "no tests ran" and exits non-zero while every test is in fact fine.
# Running them the way CONTRIBUTING.md documents is what actually checks them.
#
# The list is the corpus-free, project-free subset plus everything that governs
# Classic Dungeon X2's font, its operands and the runtime evidence envelope. A
# test that needs HANPATCH_PROJECT is deliberately absent: it would pass or fail
# on whichever title happened to be checked out.
set -u
cd "$(dirname "$0")/.." || exit 2

TESTS="
tests/test_cdx2_font.py
tests/test_cdx2_operand.py
tests/test_font.py
tests/test_eboot.py
tests/test_dsf.py
tests/test_containers.py
tests/test_runtime_evidence.py
tests/test_loop.py
tests/test_loop_seal.py
tests/test_layering.py
tests/test_stage_ledger.py
tests/test_josa_runtime_tokens.py
"

fail=0
for t in $TESTS; do
  [ -f "$t" ] || { echo "MISSING $t"; fail=1; continue; }
  if out=$(timeout 600 python3 "$t" 2>&1); then
    echo "PASS $t   $(printf '%s' "$out" | tail -1 | cut -c1-100)"
  else
    fail=1
    echo "FAIL $t"
    printf '%s\n' "$out" | tail -25
  fi
done

[ "$fail" -eq 0 ] && echo 'check suite: all tests passed'
exit $fail

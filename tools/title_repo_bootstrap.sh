#!/usr/bin/env bash
# Publish a title project's TEXT AND POLICY layer to its own private repository.
#
# Why this is not just `git add . && git push`:
#
#   * A title project directory holds the game. dq7-kr's existing history has a
#     1.4GB ROM archive committed in it, and the working directories hold ROMs,
#     ISOs, built patches, fonts and a multi-hundred-megabyte verdict ledger.
#     None of that may leave this machine, and GitHub would refuse it anyway.
#   * Those directories also hold the operator's in-progress work. This script
#     must not commit, revert, stash or stage any of it.
#
# So it builds an ORPHAN commit through a temporary index and a synthesised
# .gitignore blob: the working tree, the real index and every existing branch are
# left byte-identical. Nothing is added that is not on the allowlist below.
#
# Usage: title_repo_bootstrap.sh <project-dir> <github-repo> <branch>
set -euo pipefail

PROJECT="${1:?project directory}"
REPO="${2:?github repository, owner/name}"
BRANCH="${3:-main}"

cd "$PROJECT"

TARGET="$(python3 -c 'import json,sys; print(json.load(open("hanpatch.json"))["target"])')"
TITLE="$(python3 -c 'import json,sys; print(json.load(open("hanpatch.json"))["title"])')"

# An allowlist, not a blocklist. A blocklist that forgets one extension ships a
# ROM; an allowlist that forgets one file ships an incomplete PR surface, which
# is merely annoying. Directory exceptions are spelled out because git cannot
# re-include a file underneath an excluded directory.
IGNORE=$(cat <<'EOF'
# This repository tracks ONLY the translation text and the policy that governs
# it. Game data never enters it: not the ROM, not the extracted romfs, not the
# built patch, not the fonts, not the verdict ledger. Those live on the build
# machine and are reproduced from the operator's own dump.
#
# The rule is an allowlist. Everything is ignored, and each tracked path is
# re-included explicitly, so a new binary artefact in the project directory
# cannot become a tracked file by accident.
*

!.gitignore
!README.md
!hanpatch.json

!profiles/
!profiles/*.json
profiles/*.bak-*

# The repair loop's durable state and its gate reports. `loop/state.json`
# carries the seal (lastGate.textSha256) that the check suite verifies, so it
# has to be tracked for a pull request to be checkable at all.
!loop/
!loop/*.json

# The translation itself, and nothing else from work/: the sibling files are
# the extracted source, the shards and the verdict ledger.
!work/
work/*
EOF
)
IGNORE="${IGNORE}
!work/${TARGET}/
work/${TARGET}/*
!work/${TARGET}/text_ko.json
"

if [ ! -d .git ]; then
  git init -q
  echo "  git init"
fi

# Everything below runs against a scratch index. The operator's staged state is
# untouched, which is the whole point.
IDX="$(mktemp -t titlerepo-index-XXXXXX)"
rm -f "$IDX"
export GIT_INDEX_FILE="$IDX"
trap 'rm -f "$IDX"' EXIT

# The .gitignore is written straight into the object store. Writing it into the
# working tree would modify a file the operator is using.
IGNORE_BLOB="$(printf '%s' "$IGNORE" | git hash-object -w --stdin)"
git update-index --add --cacheinfo "100644,${IGNORE_BLOB},.gitignore"

# A project that had no .gitignore also gets the real file, because without one
# in the working tree `git status` lists the ROM and every build artefact as
# untracked - and the next `git add -A` anywhere near this directory would stage
# gigabytes of game data. A project that already has one keeps it: that file is
# the operator's, and this branch's copy governs the worktree the bridge checks
# out anyway.
if [ ! -e .gitignore ]; then
  printf '%s' "$IGNORE" > .gitignore
  echo "  wrote .gitignore (none existed)"
fi

added=0
for f in hanpatch.json README.md "work/${TARGET}/text_ko.json" loop/state.json; do
  if [ -f "$f" ]; then
    # --cacheinfo writes the entry directly and never consults any .gitignore,
    # so the project's own blocklist (which excludes work/ wholesale) cannot
    # drop the translation, and the allowlist above stays the only authority.
    git update-index --add --cacheinfo \
      "100644,$(git hash-object -w "$f"),$f"
    added=$((added + 1))
    printf '  + %s\n' "$f"
  else
    printf '  - %s (absent)\n' "$f"
  fi
done

for f in profiles/*.json; do
  case "$f" in *.bak-*) continue ;; esac
  [ -f "$f" ] || continue
  git update-index --add --cacheinfo "100644,$(git hash-object -w "$f"),$f"
  added=$((added + 1))
  printf '  + %s\n' "$f"
done

if [ "$added" -eq 0 ]; then
  echo "nothing to publish for $TITLE" >&2
  exit 1
fi

TREE="$(git write-tree)"
# No parent: this history deliberately shares nothing with the local branch that
# has the ROM archive in it.
COMMIT="$(printf '%s\n' \
  "$TITLE: 번역 텍스트와 정책 레이어" \
  "" \
  "자율 되먹임 루프(hanpatch loop)가 PR을 내는 표면이다. 게임 데이터는" \
  "추적하지 않는다 - ROM/ISO/추출물/빌드 산출물/폰트/판정 원장은 빌드" \
  "머신에 남고, 받는 쪽은 자기 덤프로 재빌드한다." \
  "" \
  "병합 게이트: tools/loop_seal_check.py 가 loop/state.json 의" \
  "lastGate.textSha256 과 커밋된 번역본의 sha256 이 일치하는지 검사한다." \
  "게이트를 통과하지 않은 번역은 병합될 수 없다." \
  | git commit-tree "$TREE")"

git update-ref "refs/heads/${BRANCH}" "$COMMIT"
echo "  ${BRANCH} -> ${COMMIT:0:12} (tree ${TREE:0:12}, ${added}+1 files)"

# The scratch index must not be inherited by the network commands below; git
# would otherwise write to it and, worse, read it as the real index.
unset GIT_INDEX_FILE

if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin "git@github.com:${REPO}.git"
  echo "  remote origin -> ${REPO}"
fi

# Only this commit's tree is measured. The repository may still hold enormous
# objects from the operator's local history - dq7-kr has a 1.4GB ROM archive in
# it - and those are precisely what this branch exists to leave behind.
echo "  published tree:"
git ls-tree -r -l "$BRANCH" | awk '{printf "    %8.1f KB  %s\n", $4/1024, $5}'
BIG=$(git ls-tree -r -l "$BRANCH" | awk '$4 > 26214400 {print $5}')
if [ -n "$BIG" ]; then
  echo "REFUSING: tracked file over 25MB, this layer must stay text-only:" >&2
  echo "$BIG" >&2
  git update-ref -d "refs/heads/${BRANCH}"
  exit 1
fi

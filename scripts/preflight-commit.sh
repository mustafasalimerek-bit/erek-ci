#!/usr/bin/env bash
# Run flutter-preflight.sh against a COMMIT, not a working tree.
#
# A working tree carries uncommitted edits, ignored generated files and
# whatever a session left behind; GitHub sees none of that. So the gate runs in
# a throwaway detached worktree of the commit, exactly as CI checks it out, and
# the worktree is removed afterwards. A pass is remembered per (commit, gate,
# Flutter version), so the pre-push hook does not run the suite a second time
# for a commit that was already checked by hand.
#
# usage: preflight-commit.sh <repo-dir> <commit> [app_dir]
#   app_dir defaults to the repo's entry in apps.json, then to whichever of
#   pubspec.yaml / app/pubspec.yaml the commit contains.
# exit:  0 passed (or the commit holds no Flutter app)
#        1 the gate failed — CI would fail the same way
#        2 usage or setup problem (bad commit, worktree could not be created)
set -uo pipefail

# Everything runs inside main(): bash parses a function whole before running
# it, so a `git pull` of erek-ci while this is mid-run cannot change it.
main() {
# The repository is passed explicitly, so inherited repo-local git variables
# (GIT_DIR from a hook running in a linked worktree, GIT_INDEX_FILE, ...)
# can only point somewhere else. Flutter's own version lookup is one victim.
unset $(git rev-parse --local-env-vars 2>/dev/null)

self=$(realpath "$0" 2>/dev/null || python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$0")
erek_ci=$(cd "$(dirname "$self")/.." && pwd)
gate="$erek_ci/scripts/flutter-preflight.sh"

[ $# -ge 2 ] || { echo "usage: $(basename "$0") <repo-dir> <commit> [app_dir]" >&2; exit 2; }
repo_in="$1"; ref="$2"; app_dir="${3:-}"

sha=$(git -C "$repo_in" rev-parse --verify --quiet "$ref^{commit}") ||
  { echo "preflight: '$ref' is not a commit in $repo_in" >&2; exit 2; }
common=$(git -C "$repo_in" rev-parse --path-format=absolute --git-common-dir)
repo_name=$(basename "$(dirname "$common")")

manifest() { # <field> — this repo's value in apps.json, empty if absent
  python3 - "$erek_ci/apps.json" "$repo_name" "$1" <<'PY' 2>/dev/null
import json, sys
apps = json.load(open(sys.argv[1]))
for a in apps.get("apps", []):
    if a.get("repo") == sys.argv[2]:
        v = a.get(sys.argv[3]) or apps.get(sys.argv[3]) or ""
        print(v)
        break
PY
}

has() { git -C "$repo_in" cat-file -e "$sha:$1" 2>/dev/null; }
[ -n "$app_dir" ] || app_dir=$(manifest app_dir)
if [ -n "$app_dir" ]; then
  [ "$app_dir" = "." ] && probe=pubspec.yaml || probe="$app_dir/pubspec.yaml"
  has "$probe" || app_dir=""
fi
if [ -z "$app_dir" ]; then
  if has pubspec.yaml; then app_dir=.
  elif has app/pubspec.yaml; then app_dir=app
  else
    echo "preflight: ${sha:0:7} holds no Flutter app — nothing to check"
    exit 0
  fi
fi

want_flutter=$(manifest flutter_version)
have_flutter=$(flutter --version 2>/dev/null | sed -n 's/^Flutter \([^ ]*\).*/\1/p' | head -1)
if ! echo "$have_flutter" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+' || [ "${have_flutter#0.0.0}" != "$have_flutter" ]; then
  echo "preflight: Flutter reports version '${have_flutter:-nothing}' — that is this Mac's environment, not the commit. Not judging ${sha:0:7}." >&2
  exit 2
fi
if [ -n "$want_flutter" ] && [ "$want_flutter" != "$have_flutter" ]; then
  echo "preflight: WARNING — CI uses Flutter $want_flutter, this Mac has ${have_flutter:-none}; results may differ" >&2
fi

cache="${XDG_CACHE_HOME:-$HOME/.cache}/erek-preflight"
mkdir -p "$cache/pass" "$cache/logs"
gate_src=$(cat "$gate") # one read: the cache key and the run use the same gate
gate_id=$( { printf '%s\n' "$gate_src"; echo "$have_flutter"; } | shasum | cut -c1-12)
marker="$cache/pass/${repo_name}-${sha}-${gate_id}-$(echo "$app_dir" | tr '/.' '__')"
if [ -f "$marker" ]; then
  echo "preflight: ${repo_name} ${sha:0:7} already passed this gate ($(cat "$marker"))"
  exit 0
fi

log="$cache/logs/${repo_name}-${sha:0:12}.log"
wt=$(mktemp -d "${TMPDIR:-/tmp}/erek-preflight.XXXXXX")
# Run a snapshot of the gate: bash reads a script as it executes it, so an
# edit to erek-ci during a long test run would otherwise change the gate
# halfway through.
gate_copy="$wt.gate.sh"
printf '%s\n' "$gate_src" > "$gate_copy"
cleanup() {
  git -C "$repo_in" worktree remove --force "$wt" >/dev/null 2>&1
  rm -rf "$wt" "$gate_copy"
}
trap cleanup EXIT
git -C "$repo_in" worktree add --detach --quiet "$wt" "$sha" ||
  { echo "preflight: could not create a worktree for ${sha:0:7}" >&2; exit 2; }

echo "preflight: ${repo_name} ${sha:0:7} (app_dir=$app_dir) — pub get, analyze, l10n, tests…"
start=$(date +%s)
(cd "$wt" && bash "$gate_copy" "$app_dir") > "$log" 2>&1 < /dev/null
rc=$?
secs=$(( $(date +%s) - start ))

if [ $rc -eq 0 ]; then
  tests=$(grep -Eo '\+[0-9]+: All tests passed' "$log" | tail -1)
  echo "$(date '+%Y-%m-%d %H:%M') ${tests:-passed} in ${secs}s" > "$marker"
  echo "preflight: PASS ${repo_name} ${sha:0:7} — ${tests:-all checks passed} (${secs}s)"
  exit 0
fi

echo "preflight: FAIL ${repo_name} ${sha:0:7} after ${secs}s — GitHub CI would fail the same way." >&2
grep -E '^== |^\s*(error|warning) •|\[E\]$|Expected:|Actual:|Which:|gen-l10n|not committed|Some tests failed|Error: ' "$log" |
  head -25 >&2
echo "--- last lines of $log" >&2
tail -8 "$log" >&2
exit 1
}

main "$@"

#!/usr/bin/env bash
# Link hooks/pre-push into every fleet repository on this Mac (apps.json), or
# into the repositories given as arguments.
#
# One link per repository is enough: worktrees share the hooks directory of
# the repository they belong to. An existing hook that is not ours is left
# alone and reported, as is a repository that sets core.hooksPath. Re-running
# is a no-op.
#
# usage: install-hooks.sh [--uninstall] [repo-dir ...]
set -uo pipefail

self=$(realpath "$0" 2>/dev/null || python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$0")
erek_ci=$(cd "$(dirname "$self")/.." && pwd)
hook="$erek_ci/hooks/pre-push"
uninstall=0
if [ "${1:-}" = "--uninstall" ]; then uninstall=1; shift; fi

if [ $# -gt 0 ]; then
  repos=("$@")
else
  repos=()
  while IFS= read -r name; do repos+=("$HOME/development/$name"); done < <(
    python3 -c 'import json,sys; print("\n".join(sorted({a["repo"] for a in json.load(open(sys.argv[1]))["apps"]})))' "$erek_ci/apps.json"
  )
fi

for dir in "${repos[@]}"; do
  name=$(basename "$dir")
  if ! common=$(git -C "$dir" rev-parse --path-format=absolute --git-common-dir 2>/dev/null); then
    echo "skip   $name: not a git repository here"
    continue
  fi
  if [ -n "$(git -C "$dir" config --get core.hooksPath)" ]; then
    echo "skip   $name: core.hooksPath is set — link $hook there by hand"
    continue
  fi
  target="$common/hooks/pre-push"
  current=$(readlink "$target" 2>/dev/null || true)
  if [ $uninstall -eq 1 ]; then
    if [ "$current" = "$hook" ]; then rm "$target" && echo "removed $name"; else echo "skip   $name: not ours"; fi
    continue
  fi
  if [ "$current" = "$hook" ]; then
    echo "ok     $name: already linked"
  elif [ -e "$target" ] || [ -L "$target" ]; then
    echo "skip   $name: $target exists and is not ours"
  else
    mkdir -p "$common/hooks" && ln -s "$hook" "$target" && echo "linked $name"
  fi
done

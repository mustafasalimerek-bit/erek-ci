#!/usr/bin/env bash
# The Flutter gate, in one place.
#
# flutter-ci.yml runs this on GitHub for every push to main and every pull
# request. hooks/pre-push runs it on this Mac (through preflight-commit.sh) on a
# clean checkout of the exact commit about to land on main. One file, so the
# two can never disagree about what "green" means: a "Run failed: CI" email
# should only ever arrive for something a local run would have caught first.
#
# usage: flutter-preflight.sh [app_dir]
#   CHECK_FORMAT=true   also fail on `dart format` drift (off by default, as in CI)
#   CHECK_L10N=false    skip the generated-localizations drift check
#   TEST_REPORTER=...   `flutter test` reporter (default: expanded)
#
# An app whose tests need files git does not carry (colophon's ~180 MB
# content.db) ships tool/gate-prepare.sh; it runs first, from app_dir. It runs
# here rather than in the workflow so CI and the pre-push hook prepare the same
# inputs. In CI it gets GH_TOKEN, enough to download this repo's release assets.
# It must pin what it fetches by hash and fail when it cannot: a prepare step
# that falls back to "no data" turns failing tests into skipped ones.
set -euo pipefail

cd "${1:-.}"

step() { echo "== $*"; }

if [ -f tool/gate-prepare.sh ]; then
  step "tool/gate-prepare.sh"
  bash tool/gate-prepare.sh
fi

step "flutter pub get"
flutter pub get

if [ "${CHECK_FORMAT:-false}" = true ]; then
  step "dart format (must be clean)"
  dart format --set-exit-if-changed lib test
fi

# Errors and warnings block; `info` lints do not. The fleet carries hundreds of
# prefer_const_constructors and withOpacity suggestions, and gating shipping on
# those would mean the gate gets turned off — at which point the real warnings
# stop being seen too.
step "flutter analyze --no-fatal-infos"
flutter analyze --no-fatal-infos

if [ "${CHECK_L10N:-true}" = true ]; then
  step "generated localizations are up to date"
  if [ ! -f l10n.yaml ]; then
    echo "no l10n.yaml — nothing to check"
  elif [ -z "$(git ls-files 'lib/l10n/*.dart' 'lib/l10n/**/*.dart')" ]; then
    echo "generated localizations are not tracked in git (gen_l10n writes"
    echo "to .dart_tool here) — this check cannot detect drift, skipping"
  else
    # gen_l10n output is derived, so an ARB edited without regenerating shows
    # up here as a diff rather than as missing text on a screen. Scoped to
    # lib/l10n on purpose: `flutter pub get` can rewrite pubspec.lock, and a
    # repo-wide diff would fail on that instead. `git diff` cannot see a file
    # that was never added, so untracked generated files are checked too.
    flutter gen-l10n
    untracked=$(git status --porcelain --untracked-files=all -- lib/l10n | grep '^??' || true)
    if [ -n "$untracked" ]; then
      echo "gen-l10n produced files that are not committed:" >&2
      echo "$untracked" >&2
      exit 1
    fi
    git diff --exit-code -- lib/l10n || {
      echo "gen-l10n changed the committed output — regenerate and commit" >&2
      exit 1
    }
  fi
fi

step "flutter test"
flutter test --reporter "${TEST_REPORTER:-expanded}"

#!/usr/bin/env bash
# Merge/PR gate: a UI-touching PR must carry a SCREENSHOT (an embedded image) as proof.
#
# Generalizes the "no visual change ships without a screenshot" rule. It (1) decides whether
# the PR touches UI by matching changed files against a configurable glob, and (2) if so,
# fails unless the PR body or its comments embed at least one image. Use it as a CI check
# at PR-open AND as a preflight at merge-time (the two enforcement points the rule wants).
#
# Requires: gh (authenticated). PR number from $1 or $PR_NUMBER.
#
# Knobs (env):
#   PR_NUMBER         the PR (or pass $1).
#   UI_PATH_REGEX     ERE matched against each changed file path. If ANY changed file
#                     matches, the PR is "UI-touching" and a screenshot is required.
#                     Default below covers common front-end locations + any *.tsx/*.jsx/
#                     *.vue/*.svelte. Override for your layout.
#   REQUIRE_ALWAYS    "1" = require a screenshot on EVERY PR, ignore UI_PATH_REGEX.
#   IMAGE_REGEX       ERE that counts as "an embedded image" in the PR text. Default
#                     matches Markdown image embeds and <img>/attachment URLs.
#   ALLOW_NO_SHOT     a non-empty override REASON; logs it and passes (escape hatch for
#                     genuinely non-visual UI-path changes, e.g. a CSS var rename).
#
# Usage:
#   PR_NUMBER=123 sh ci/screenshots/require-screenshots.sh
#   REQUIRE_ALWAYS=1 sh ci/screenshots/require-screenshots.sh 123
set -euo pipefail

PR="${1:-${PR_NUMBER:-}}"
[ -n "$PR" ] || { echo "Usage: $0 <pr-number>  (or set PR_NUMBER)" >&2; exit 2; }
command -v gh >/dev/null 2>&1 || { echo "gh CLI not found" >&2; exit 2; }

UI_PATH_REGEX="${UI_PATH_REGEX:-(^|/)(components|pages|views|ui|app|src/app)/|\.(tsx|jsx|vue|svelte)$}"
REQUIRE_ALWAYS="${REQUIRE_ALWAYS:-0}"
# Markdown image  ![alt](http...)  OR an <img ...> tag  OR a github user-attachment URL.
IMAGE_REGEX="${IMAGE_REGEX:-!\[[^]]*\]\(https?://|<img |user-images\.githubusercontent\.com|github\.com/user-attachments/}"
ALLOW_NO_SHOT="${ALLOW_NO_SHOT:-}"

# 1) UI-touching?
ui_touching=0
if [ "$REQUIRE_ALWAYS" = "1" ]; then
  ui_touching=1
else
  if ! files=$(gh pr diff "$PR" --name-only 2>/dev/null); then
    echo "::error::could not list changed files for PR #$PR — refusing rather than skip the visual-proof gate." >&2
    exit 1
  fi
  if printf '%s\n' "$files" | grep -qE "$UI_PATH_REGEX"; then
    ui_touching=1
  fi
fi

if [ "$ui_touching" = "0" ]; then
  echo "PASS: PR #$PR does not touch UI paths — screenshot not required."
  exit 0
fi

# 2) Does the PR carry an embedded image (body or any comment)?
pr_text=$(gh pr view "$PR" --json body,comments \
  -q '(.body // "") + "\n" + ([.comments[].body] | join("\n"))' 2>/dev/null || echo "")

if printf '%s' "$pr_text" | grep -qE "$IMAGE_REGEX"; then
  echo "PASS: PR #$PR is UI-touching and has an embedded screenshot."
  exit 0
fi

if [ -n "$ALLOW_NO_SHOT" ]; then
  echo "PASS (overridden): PR #$PR touches UI but the screenshot requirement was waived — reason: $ALLOW_NO_SHOT"
  exit 0
fi

echo "::error::PR #$PR touches UI but has NO embedded screenshot (no image in body or comments)." >&2
echo "FAIL: attach a before/after image to the PR. To override a genuine non-visual change," >&2
echo "      set ALLOW_NO_SHOT='<reason>' (logged)." >&2
exit 1

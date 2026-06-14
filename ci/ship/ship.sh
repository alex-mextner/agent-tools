#!/usr/bin/env bash
# ship — green-CI-gated PR merge with pre-merge safety checks, then branch/worktree cleanup.
#
# A portable generalization of a "gh ship <PR>" helper: before merging it verifies the PR is
# actually ready, then squash-merges and cleans up. It refuses to merge when any of these is
# true (each a real way a bad merge sneaks in):
#   • PR is not OPEN / is CONFLICTING / is BEHIND its base (ruleset wants up-to-date).
#   • Required status checks aren't all passing (green-CI gate).
#   • There are unresolved review threads (see ci/review-threads/).
#   • A UI-touching PR has no embedded screenshot (see ci/screenshots/) — unless overridden.
#   • The local branch has unpushed/diverged commits, or its worktree is dirty.
#
# All project-specific coupling is OPTIONAL and configured by env/flags — no issue-tracker,
# no path layout, no org is hard-coded.
#
# Requires: gh (authenticated), git. jq strongly recommended (required-checks-only gating).
#
# Usage:
#   ship.sh <PR-number> [--skip-ci] [--dry-run] [--no-screenshot-ok <reason>]
#           [--screenshot <path> [desc]]...
#
# Flags:
#   --skip-ci              admin-merge bypassing the green-CI gate (use only when CI is
#                          billing-blocked / stuck — it still runs the other preflights).
#   --dry-run              print what would happen; change nothing.
#   --no-screenshot-ok R   override the UI screenshot requirement with a logged reason R.
#   --screenshot P [D]     attach a local screenshot P (desc D) via SHIP_IMAGE_UPLOAD_CMD,
#                          then post it as a PR comment. Repeatable.
#
# Knobs (env):
#   SHIP_MAIN_CHECKOUT     path to the primary (main) checkout for the post-merge pull.
#                          Default: first worktree in `git worktree list`.
#   SHIP_DEFAULT_BRANCH    base branch name (default: main).
#   SHIP_UI_PATH_REGEX     ERE; changed files matching it => "UI-touching" (screenshot
#                          required). Default covers common front-end paths. Empty = never
#                          treat as UI (disables the screenshot gate).
#   SHIP_MERGE_METHOD      squash | merge | rebase (default: squash).
#   SHIP_IMAGE_UPLOAD_CMD  optional command to upload a screenshot and PRINT a public URL on
#                          stdout. Receives the image path as {FILE} (or as $1 if no token).
#                          Used only with --screenshot. If unset, --screenshot embeds a
#                          local-path note instead (does NOT satisfy the screenshot gate).
set -euo pipefail

ORIG_PWD=$(pwd -P)
PR=""; SKIP_CI=0; DRY_RUN=0; NO_SHOT_OK=""
SHOT_PATHS=(); SHOT_DESCS=()
USAGE='Usage: ship.sh <PR-number> [--skip-ci] [--dry-run] [--no-screenshot-ok <reason>] [--screenshot <path> [desc]]...'

# --- arg parse (PR number is the lone bare arg; --screenshot takes path + optional desc) --
args=("$@"); i=0; n=${#args[@]}
while [ "$i" -lt "$n" ]; do
  a=${args[$i]}
  case "$a" in
    --skip-ci) SKIP_CI=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --no-screenshot-ok)
      i=$((i+1)); { [ "$i" -lt "$n" ] && [ "${args[$i]:0:1}" != "-" ]; } || { echo "--no-screenshot-ok needs a <reason>." >&2; exit 1; }
      NO_SHOT_OK=${args[$i]}; [ -n "$NO_SHOT_OK" ] || { echo "--no-screenshot-ok reason empty." >&2; exit 1; } ;;
    --screenshot|--shot)
      i=$((i+1)); { [ "$i" -lt "$n" ] && [ "${args[$i]:0:1}" != "-" ]; } || { echo "$a needs a <path>." >&2; exit 1; }
      p=${args[$i]}; case "$p" in /*) : ;; *) p="$ORIG_PWD/$p" ;; esac
      SHOT_PATHS+=("$p")
      if [ "$((i+1))" -lt "$n" ] && [ "${args[$((i+1))]:0:1}" != "-" ]; then
        # The next bare token is this shot's desc UNLESS it's the (still unset) PR number.
        nxt=${args[$((i+1))]}; rest_bare=0; j=$((i+2))
        while [ "$j" -lt "$n" ]; do [ "${args[$j]:0:1}" != "-" ] && rest_bare=$((rest_bare+1)); j=$((j+1)); done
        if [ -z "$PR" ] && [ "$rest_bare" -eq 0 ]; then SHOT_DESCS+=(""); else SHOT_DESCS+=("$nxt"); i=$((i+1)); fi
      else SHOT_DESCS+=(""); fi ;;
    -*) echo "Unknown flag: $a"$'\n'"$USAGE" >&2; exit 1 ;;
    *) [ -n "$PR" ] && { echo "Multiple PR numbers ($PR, $a) — pass one." >&2; exit 1; }; PR="$a" ;;
  esac
  i=$((i+1))
done
[ -n "$PR" ] || { echo "$USAGE" >&2; exit 1; }

DEFAULT_BRANCH="${SHIP_DEFAULT_BRANCH:-main}"
MERGE_METHOD="${SHIP_MERGE_METHOD:-squash}"
UI_PATH_REGEX="${SHIP_UI_PATH_REGEX-(^|/)(components|pages|views|ui|app|src/app)/|\.(tsx|jsx|vue|svelte)$}"

run() { if [ "$DRY_RUN" = "1" ]; then echo "[dry-run] $*"; else "$@"; fi; }

ROOT=$(git rev-parse --show-toplevel); cd "$ROOT"
command -v gh >/dev/null 2>&1 || { echo "gh CLI not found" >&2; exit 1; }
MAIN_CHECKOUT="${SHIP_MAIN_CHECKOUT:-$(git worktree list --porcelain | awk '/^worktree /{print $2; exit}')}"

# --- resolve PR state -----------------------------------------------------------------
read -r BRANCH STATE MERGEABLE CROSS_REPO MERGE_STATE < <(gh pr view "$PR" \
  --json headRefName,state,mergeable,isCrossRepository,mergeStateStatus \
  -q '[.headRefName,.state,.mergeable,.isCrossRepository,.mergeStateStatus]|@tsv')
[ -n "$BRANCH" ] || { echo "Could not resolve PR #$PR" >&2; exit 1; }
[ "$STATE" = "OPEN" ] || { echo "Refusing: PR #$PR is $STATE, not OPEN." >&2; exit 1; }
[ "$MERGEABLE" = "CONFLICTING" ] && { echo "Refusing: PR #$PR is CONFLICTING — resolve the conflict first." >&2; exit 1; }
if [ "${MERGE_STATE:-}" = "BEHIND" ]; then
  echo "Refusing: PR #$PR head is BEHIND its base. Update it (gh pr update-branch $PR --rebase), wait for CI, re-run." >&2
  exit 1
fi

# --- green-CI gate (required checks only when jq + branch protection are available) ----
if [ "$SKIP_CI" = "0" ]; then
  SUCCESS_FILTER='((.conclusion=="SUCCESS" or .conclusion=="SKIPPED" or .conclusion=="NEUTRAL") or .state=="SUCCESS")'
  if command -v jq >/dev/null 2>&1; then
    ROLLUP=$(gh pr view "$PR" --json statusCheckRollup -q '.statusCheckRollup' 2>/dev/null || echo '[]')
    PROT=$(gh api "repos/{owner}/{repo}/branches/$DEFAULT_BRANCH/protection/required_status_checks" 2>/dev/null) || PROT=''
    REQUIRED=$(printf '%s' "$PROT" | jq -c '[.contexts[]?]' 2>/dev/null || echo '[]'); [ -n "$REQUIRED" ] || REQUIRED='[]'
    if [ "$REQUIRED" != "[]" ]; then
      FAILED=$(printf '%s' "$ROLLUP" | jq --argjson req "$REQUIRED" \
        "[.[]? | select($SUCCESS_FILTER) | (.name // .context)] as \$ok
          | [\$req[] | select((. as \$r | \$ok | index(\$r)) | not)] | length" 2>/dev/null || echo 1)
      DESC="required check(s) not yet passing"
    else
      FAILED=$(printf '%s' "$ROLLUP" | jq \
        "([.[]? | select($SUCCESS_FILTER | not)] | length) + (if (. | length) == 0 then 1 else 0 end)" 2>/dev/null || echo 1)
      DESC="check(s) not yet passing (no branch protection — gating on ALL checks)"
    fi
  else
    FAILED=$(gh pr view "$PR" --json statusCheckRollup -q \
      '([.statusCheckRollup[]? | select((.conclusion=="SUCCESS" or .conclusion=="SKIPPED" or .conclusion=="NEUTRAL" or .state=="SUCCESS")|not)] | length) + (if (.statusCheckRollup | length)==0 then 1 else 0 end)' 2>/dev/null || echo 1)
    DESC="check(s) not yet passing (jq missing — gating on ALL checks; install jq for required-only)"
  fi
  [ "${FAILED:-0}" = "0" ] || { echo "Refusing: PR #$PR has $FAILED $DESC. Wait for/fix CI (or --skip-ci if CI is billing-blocked)." >&2; exit 1; }
fi

# --- unresolved review threads (see ci/review-threads/) --------------------------------
# Fail-CLOSED: if the API call fails we must NOT merge (a silent 0 would let an unresolved
# PR through). So check the gh exit status, not just the parsed count.
THREAD_Q='query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){repository(owner:$owner,name:$name){pullRequest(number:$pr){reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{isResolved}}}}}'
if THREAD_RAW=$(gh api graphql --paginate -F owner='{owner}' -F name='{repo}' -F pr="$PR" -f query="$THREAD_Q" \
     --jq '[.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved | not)] | length' 2>/dev/null); then
  UNRESOLVED=$(printf '%s' "$THREAD_RAW" | awk '{s+=$1} END{print s+0}')
else
  echo "Refusing: could not query review threads for PR #$PR (gh api failed) — refusing to merge rather than fail open. Fix gh access and retry." >&2
  exit 1
fi
[ "${UNRESOLVED:-0}" = "0" ] || { echo "Refusing: PR #$PR has $UNRESOLVED unresolved review thread(s) — resolve them, then re-run." >&2; exit 1; }

# --- local branch sanity (no unpushed/diverged commits; clean worktree) ----------------
WT=$(git worktree list --porcelain | awk -v b="refs/heads/$BRANCH" '/^worktree /{w=$2} $0=="branch "b{print w}')
if [ -n "$WT" ] && [ -n "$(git -C "$WT" status --short)" ]; then
  echo "Refusing: worktree $WT has uncommitted changes. Commit or discard first." >&2; git -C "$WT" status --short >&2; exit 1
fi
git fetch -q origin "$BRANCH" 2>/dev/null || true
if git show-ref --verify --quiet "refs/heads/$BRANCH" && git show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
  AHEAD=$(git rev-list --count "origin/$BRANCH..$BRANCH" 2>/dev/null || echo 0)
  [ "$AHEAD" = "0" ] || { echo "Refusing: local $BRANCH has $AHEAD unpushed commit(s). Push first." >&2; exit 1; }
fi

# --- screenshot gate (optional uploader + UI-touching check) ---------------------------
upload_png() {
  local png="$1"
  [ -n "${SHIP_IMAGE_UPLOAD_CMD:-}" ] || return 1
  if [ "$DRY_RUN" = "1" ]; then printf 'https://example.invalid/dry-run/%s' "$(basename "$png")"; return 0; fi
  local out
  if printf '%s' "$SHIP_IMAGE_UPLOAD_CMD" | grep -q '{FILE}'; then
    out=$(eval "${SHIP_IMAGE_UPLOAD_CMD//\{FILE\}/$png}" 2>/dev/null)
  else
    out=$(eval "$SHIP_IMAGE_UPLOAD_CMD \"$png\"" 2>/dev/null)
  fi
  out=$(printf '%s' "$out" | grep -oE 'https?://[^ ]+' | tail -1)
  [ -n "$out" ] || return 1; printf '%s' "$out"
}

POSTED_IMAGE=0   # set only when a screenshot is ACTUALLY uploaded + embedded (a real image)
if [ "${#SHOT_PATHS[@]}" -gt 0 ]; then
  BODY="### Visual proof"; idx=0
  while [ "$idx" -lt "${#SHOT_PATHS[@]}" ]; do
    png=${SHOT_PATHS[$idx]}; desc=${SHOT_DESCS[$idx]}; [ -n "$desc" ] || desc=$(basename "$png")
    if [ ! -f "$png" ]; then
      echo "[ship] WARNING: screenshot not found: $png (does NOT satisfy the gate)." >&2
      BODY="$BODY"$'\n\n'"**$desc**"$'\n'"_Visual proof MISSING — file not found: \`$png\`_"
    elif url=$(upload_png "$png"); then
      echo "[ship] uploaded '$desc' -> $url"; BODY="$BODY"$'\n\n'"**$desc**"$'\n'"![$desc]($url)"; POSTED_IMAGE=1
    else
      # No uploader / upload failed: we post a path note, but a path note is NOT an embedded
      # image, so it must NOT satisfy the UI screenshot gate. Do not bump VALID_SHOTS here.
      echo "[ship] NOTE: no uploader (SHIP_IMAGE_UPLOAD_CMD) or upload failed for '$desc' — embedding a path note (does NOT satisfy the gate)." >&2
      BODY="$BODY"$'\n\n'"**$desc**"$'\n'"_Local path: \`$png\` — upload manually._"
    fi
    idx=$((idx+1))
  done
  echo "[ship] posting visual-proof comment to PR #${PR} ..."; run gh pr comment "$PR" --body "$BODY"
fi

UI_TOUCHING=0
if [ -n "$UI_PATH_REGEX" ]; then
  if PR_FILES=$(gh pr diff "$PR" --name-only 2>/dev/null); then
    printf '%s\n' "$PR_FILES" | grep -qE "$UI_PATH_REGEX" && UI_TOUCHING=1
  else
    echo "Refusing: could not list changed files for PR #$PR — cannot evaluate the screenshot gate." >&2; exit 1
  fi
fi
if [ "$UI_TOUCHING" = "1" ]; then
  HAS_IMAGE=0
  # Only a really-uploaded image, or an image already embedded in the PR, counts — a failed
  # upload's path note does NOT satisfy the gate.
  if [ "$POSTED_IMAGE" = "1" ]; then HAS_IMAGE=1
  else
    PR_TEXT=$(gh pr view "$PR" --json body,comments -q '(.body // "") + "\n" + ([.comments[].body] | join("\n"))' 2>/dev/null || echo "")
    printf '%s' "$PR_TEXT" | grep -qE '!\[[^]]*\]\(https?://|<img |user-attachments/' && HAS_IMAGE=1
  fi
  if [ "$HAS_IMAGE" = "0" ]; then
    if [ -n "$NO_SHOT_OK" ]; then echo "[ship] UI screenshot gate OVERRIDDEN — reason: $NO_SHOT_OK"
    else
      echo "Refusing: PR #$PR touches UI but has NO embedded screenshot. Pass --screenshot <path> [desc]" >&2
      echo "  (with SHIP_IMAGE_UPLOAD_CMD set), or --no-screenshot-ok <reason> to override." >&2
      exit 1
    fi
  fi
fi

# --- merge -----------------------------------------------------------------------------
if [ "$SKIP_CI" = "1" ]; then
  echo "[ship] --skip-ci: admin-merging PR #${PR} (${BRANCH}) --${MERGE_METHOD} --admin ..."
  run gh pr merge "$PR" "--$MERGE_METHOD" --admin
else
  echo "[ship] preflight clean — merging PR #${PR} (${BRANCH}) --${MERGE_METHOD} ..."
  run gh pr merge "$PR" "--$MERGE_METHOD"
fi

# --- cleanup: remote branch, local worktree+branch, refresh main -----------------------
if [ "${CROSS_REPO:-false}" = "true" ]; then
  echo "[ship] PR head is a fork — leaving its branch to the fork owner."
elif git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  echo "[ship] deleting remote branch origin/${BRANCH} ..."; run git push -q origin --delete "$BRANCH"
fi

# Don't delete the worktree out from under a session running inside it.
SELF=0
if [ -n "$WT" ]; then
  WT_REAL=$(cd "$WT" 2>/dev/null && pwd -P || echo "$WT")
  { [ "$ORIG_PWD" = "$WT_REAL" ] || [ "${ORIG_PWD#"$WT_REAL"/}" != "$ORIG_PWD" ]; } && SELF=1
fi
if [ "$SELF" = "1" ]; then
  echo "[ship] running inside the PR's worktree ($WT_REAL) — leaving it in place; remove later from $MAIN_CHECKOUT."
else
  if [ -n "$WT" ]; then
    echo "[ship] removing worktree $WT and local branch ${BRANCH} ..."
    [ "$(git -C "$WT" symbolic-ref --quiet --short HEAD 2>/dev/null || echo)" = "$BRANCH" ] && run git -C "$WT" checkout --detach --quiet
    run git worktree remove "$WT" || run git worktree remove --force "$WT"
  fi
  git show-ref --verify --quiet "refs/heads/$BRANCH" && run git branch -D "$BRANCH"
  run git worktree prune
fi

if [ -d "$MAIN_CHECKOUT" ]; then
  echo "[ship] refreshing main checkout at ${MAIN_CHECKOUT} ..."
  [ "$(git -C "$MAIN_CHECKOUT" symbolic-ref --quiet --short HEAD 2>/dev/null || echo)" = "$DEFAULT_BRANCH" ] || run git -C "$MAIN_CHECKOUT" checkout "$DEFAULT_BRANCH"
  run git -C "$MAIN_CHECKOUT" fetch origin
  [ "$DRY_RUN" = "1" ] || git -C "$MAIN_CHECKOUT" pull --ff-only origin "$DEFAULT_BRANCH" || echo "[ship] WARNING: could not fast-forward $DEFAULT_BRANCH — pull manually." >&2
fi

if [ "$DRY_RUN" = "1" ]; then echo "[ship] [dry-run] complete — nothing changed for #$PR."
else echo "[ship] shipped #$PR — merged + cleaned up."; fi

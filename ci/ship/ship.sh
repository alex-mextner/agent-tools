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

# --- green-CI gate: CI must EXIST + be green; pending CI is WATCHED to completion -------
# Design (CTO): "no CI" is itself a FAILED gate, not a free pass — refuse with guidance to
# set CI up. Existing-but-pending checks are WATCHED (polled to completion, up to
# SHIP_CI_WAIT) so you don't have to babysit; then the merge is gated on the final result of
# ALL checks. (gh has `gh run watch <run-id>` for a single run; we poll the PR-aggregate.)
if [ "$SKIP_CI" = "0" ]; then
  command -v jq >/dev/null 2>&1 || { echo "Refusing: jq is required for the CI gate (install jq — or --skip-ci only if CI is genuinely N/A)." >&2; exit 1; }
  SUCCESS_FILTER='((.conclusion=="SUCCESS" or .conclusion=="SKIPPED" or .conclusion=="NEUTRAL") or .state=="SUCCESS")'
  SETTLED_FILTER='(.status=="COMPLETED" or .state=="SUCCESS" or .state=="FAILURE" or .state=="ERROR")'
  CI_WAIT="${SHIP_CI_WAIT:-900}"; CI_POLL="${SHIP_CI_POLL:-20}"; CI_GRACE="${SHIP_CI_GRACE:-45}"
  START=$(date +%s); DEADLINE=$(( START + CI_WAIT )); GRACE_DEADLINE=$(( START + CI_GRACE ))
  while :; do
    ROLLUP=$(gh pr view "$PR" --json statusCheckRollup -q '.statusCheckRollup' 2>/dev/null || echo '[]')
    N=$(printf '%s' "$ROLLUP" | jq 'length' 2>/dev/null || echo 0)
    if [ "${N:-0}" = "0" ]; then
      # An empty rollup is ambiguous: either "no CI configured" OR "checks not registered yet"
      # (GitHub briefly returns [] on a freshly-opened PR before Actions enqueue). Give a grace
      # window for checks to appear; only after it lapses do we conclude there is genuinely no CI.
      NOW=$(date +%s)
      if [ "$NOW" -lt "$GRACE_DEADLINE" ]; then
        echo "[ship] no checks reported yet on PR #$PR — waiting ${CI_POLL}s for CI to register (grace $(( GRACE_DEADLINE - NOW ))s left) ..."
        sleep "$CI_POLL"; continue
      fi
      { echo "Refusing: PR #$PR has NO CI checks (none registered within ${CI_GRACE}s) — set up CI before merging (an ungated merge is not allowed; 'no CI' is a failed gate, not a pass)."
        echo "  Provision CI: enable rig's ci block — \`rig apply\` writes secret-scan / codeql / dependency-review into .github/workflows — or add your own workflows."
        echo "  Override ONLY if CI is genuinely N/A for this repo: --skip-ci."; } >&2
      exit 1
    fi
    PENDING=$(printf '%s' "$ROLLUP" | jq "[.[] | select($SETTLED_FILTER | not)] | length" 2>/dev/null || echo 0)
    [ "${PENDING:-0}" = "0" ] && break
    NOW=$(date +%s); [ "$NOW" -ge "$DEADLINE" ] && { echo "Refusing: PR #$PR still has $PENDING pending check(s) after ${CI_WAIT}s. Let CI settle, then re-run." >&2; exit 1; }
    echo "[ship] $PENDING check(s) still running on PR #$PR — watching (poll ${CI_POLL}s, $(( DEADLINE - NOW ))s left before giving up) ..."
    sleep "$CI_POLL"
  done
  FAILED=$(printf '%s' "$ROLLUP" | jq "[.[] | select($SUCCESS_FILTER | not)] | length" 2>/dev/null || echo 1)
  if [ "${FAILED:-0}" != "0" ]; then
    echo "Refusing: PR #$PR has $FAILED check(s) not passing:" >&2
    printf '%s' "$ROLLUP" | jq -r ".[] | select($SUCCESS_FILTER | not) | \"  x \(.name // .context) -> \(.conclusion // .state)\"" >&2 2>/dev/null || true
    echo "  Fix CI, then re-run (or --skip-ci if CI is billing-blocked)." >&2
    exit 1
  fi
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
# A branch can be checked out in MORE THAN ONE worktree (git permits it; a stray/leftover
# tree often lingers). Collect ALL of them: feeding a single $WT that had concatenated two
# paths to `git -C` / `git worktree remove` was the exit-128 bug this script had. Parse the
# stable `--porcelain` line form (`worktree <path>` then `branch <ref>`); the `-z` variant
# isn't available on older git (e.g. Apple git 2.39), so don't rely on it.
# (bash 3.2 compatible — `#!/usr/bin/env bash` is 3.2 on stock macOS; guard every empty-array
# expansion with the `${arr[@]+...}` idiom so `set -u` doesn't trip on an empty WTS.)
WTS=()
while IFS= read -r wpath; do
  [ -n "$wpath" ] && WTS+=("$wpath")
done < <(
  git worktree list --porcelain 2>/dev/null \
    | awk -v b="refs/heads/$BRANCH" '/^worktree /{w=substr($0,10)} $0=="branch "b{print w}'
)
for wt in ${WTS[@]+"${WTS[@]}"}; do
  if [ -n "$(git -C "$wt" status --short 2>/dev/null)" ]; then
    echo "Refusing: worktree $wt has uncommitted changes. Commit or discard first." >&2
    git -C "$wt" status --short >&2; exit 1
  fi
done
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

# --- cleanup --------------------------------------------------------------------------
# The merge ABOVE already succeeded. Cleanup (remote branch delete, worktree/branch
# removal, main refresh) is best-effort housekeeping: a failure here must NEVER mask the
# fact that #$PR is merged, nor abort the script with a non-zero exit (the exit-128 that
# made a clean squash-merge look like a failed ship). So the whole cleanup runs in a
# function whose result is REPORTED, not propagated — `set -e` is relaxed inside it and
# every step is allowed to fail with a warning.
cleanup() {
  set +e  # local to this function's subshell-free body: warn, don't abort
  if [ "${CROSS_REPO:-false}" = "true" ]; then
    echo "[ship] PR head is a fork — leaving its branch to the fork owner."
  elif git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    echo "[ship] deleting remote branch origin/${BRANCH} ..."
    run git push -q origin --delete "$BRANCH" || echo "[ship] WARNING: could not delete remote branch origin/${BRANCH} — delete it manually." >&2
  fi

  # Don't delete a worktree out from under the session running inside it. With multiple
  # worktrees for this branch, remove EACH one (the one we're inside is skipped, kept for
  # the user to remove later).
  for wt in ${WTS[@]+"${WTS[@]}"}; do
    [ -n "$wt" ] || continue
    wt_real=$(cd "$wt" 2>/dev/null && pwd -P || echo "$wt")
    if [ "$ORIG_PWD" = "$wt_real" ] || [ "${ORIG_PWD#"$wt_real"/}" != "$ORIG_PWD" ]; then
      echo "[ship] running inside the PR's worktree ($wt_real) — leaving it in place; remove later from $MAIN_CHECKOUT."
      continue
    fi
    echo "[ship] removing worktree $wt ..."
    [ "$(git -C "$wt" symbolic-ref --quiet --short HEAD 2>/dev/null || echo)" = "$BRANCH" ] && run git -C "$wt" checkout --detach --quiet
    run git worktree remove "$wt" 2>/dev/null \
      || run git worktree remove --force "$wt" \
      || echo "[ship] WARNING: could not remove worktree $wt — remove it manually (git worktree remove --force)." >&2
  done

  # Delete the local branch only once no worktree still has it checked out (git refuses
  # otherwise, and that refusal is fine — the merge is already done).
  if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    run git branch -D "$BRANCH" || echo "[ship] WARNING: could not delete local branch ${BRANCH} (still checked out somewhere?) — delete it manually." >&2
  fi
  run git worktree prune || true

  if [ -d "$MAIN_CHECKOUT" ]; then
    echo "[ship] refreshing main checkout at ${MAIN_CHECKOUT} ..."
    [ "$(git -C "$MAIN_CHECKOUT" symbolic-ref --quiet --short HEAD 2>/dev/null || echo)" = "$DEFAULT_BRANCH" ] || run git -C "$MAIN_CHECKOUT" checkout "$DEFAULT_BRANCH"
    run git -C "$MAIN_CHECKOUT" fetch origin
    [ "$DRY_RUN" = "1" ] || git -C "$MAIN_CHECKOUT" pull --ff-only origin "$DEFAULT_BRANCH" || echo "[ship] WARNING: could not fast-forward $DEFAULT_BRANCH — pull manually." >&2
  fi
  return 0
}

# Report the merge FIRST (it is the durable, already-applied result), then run cleanup
# in a way that can only warn. `|| true` belt-and-suspenders so even an unexpected
# non-zero from cleanup can't flip ship's exit code after a successful merge.
if [ "$DRY_RUN" = "1" ]; then
  echo "[ship] [dry-run] would merge #$PR, then clean up."
else
  echo "[ship] merged #$PR (${BRANCH}) — running best-effort cleanup ..."
fi
cleanup || echo "[ship] WARNING: post-merge cleanup hit an error — #$PR IS merged; finish cleanup manually." >&2

if [ "$DRY_RUN" = "1" ]; then echo "[ship] [dry-run] complete — nothing changed for #$PR."
else echo "[ship] shipped #$PR — merged + cleaned up."; fi

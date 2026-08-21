# ship — green-CI-gated PR merge + cleanup

> **Agents: the sanctioned interface is `gh ship <PR>`** (a gh alias → the repo's provisioned
> `pr-ship.sh`). Never invoke this `ship.sh`/`pr-ship.sh` by path and never hand-roll a merge
> script — `gh ship` is the single front door. This file documents the underlying behavior;
> `ship.sh` is the source/template that `rig apply` provisions as `pr-ship.sh`.

`ship.sh <PR>` is a portable "merge this PR safely, then clean up" helper. It is **not** a
CI workflow — it's a local/CI command you run to merge a ready PR. Before it merges, it
runs the same preflights the CI gates in this directory enforce, so you can't merge a PR
that isn't actually ready even with a one-liner.

## What it refuses to merge

| Refusal | Why |
| ------- | --- |
| PR not OPEN / CONFLICTING / BEHIND base | Not mergeable / ruleset wants up-to-date branch. |
| **No CI checks at all** | "No CI" is a *failed* gate, not a pass — ship refuses and tells you to set CI up first (`rig apply` provisions the gates, or add workflows). Override only with `--skip-ci`, which is **deny-by-default** (needs a one-time live Telegram approval — see below). **Exception — CI outage:** if the empty rollup is a genuine outage (a `pull_request`-triggered workflow exists on the PR's **pushed base branch** — `origin/<baseRefName>` — but registered no checks, i.e. billing suspended / Actions down), ship runs the local fallback gate below instead of refusing, and — only if it is fully green — falls through to the **normal (non-admin) merge** (the same posture as the checks-failed structural-outage path; a branch-protection ruleset still gates it, and if required checks are genuinely absent GitHub blocks the merge so you `--skip-ci` deliberately — ship never silently `--admin`-bypasses here). A failed/unreadable rollup query is NOT an empty rollup — an unknown remote state hard-refuses (never merges). A repo whose workflows never trigger on PRs (push/schedule only) is NOT an outage and still hard-refuses. The signal is read from the pushed `origin/<base>` ref, not the local worktree, so an untracked/unpushed workflow cannot trigger the exception. **Trigger detection is a heuristic** over a YAML subset: it recognises an unquoted top-level `on:` naming `pull_request`/`pull_request_target` inline, as a block key, or as a list item; a `pull_request` trigger **narrowed by `branches:`/`paths:`/`types:` filters is still treated as an outage** (the local gate then runs, so the merge is verified — it is only more permissive about substituting local gates for absent remote checks), and quoted-key (`"on":`) / flow-mapping (`on: { … }`) forms conservatively hard-refuse (the safe direction). |
| Any CI check's latest run failing | The green-CI gate (gates on ALL checks, by each check's LATEST run — the rollup is deduped to the newest run per logical check, so a stale FAILURE from a re-run that has since gone green does not block, matching GitHub's `mergeStateStatus`). **Exception — a confirmed pre-existing flake:** below the ~80% "CI is structurally down" threshold (see `ci_appears_structurally_down` above), the far more common shape is "one specific check is a flaky, already-broken-on-main test and everything else is green" — that never looks like an outage, so without help the only way through used to be a human escalation for something a quick check of the base branch's own recent CI history already answers. Pass `--known-flake <check-name>` (repeatable, once per currently-failing check) to name the claim; ship does **not** trust it — it independently queries the last `SHIP_FLAKE_LOOKBACK_RUNS` (default 5) completed runs of the SAME workflow on the PR's own base branch and requires a job of that exact name to have also FAILED in at least one of them. Every currently-failing check must be BOTH asserted AND confirmed this way, or the gate still hard-refuses (no partial credit) — an assertion that doesn't check out is refused identically to not passing the flag at all. On success it runs the SAME local fallback gate as the CI-down path and merges only if that is green too — a confirmed flake is not a free pass on testing. Logged to `SHIP_AUDIT_FILE` (`gate:"known-flake"`, `decision: confirmed`/`confirmed-local-gate-failed`/`refused` — `confirmed` is written only after an actual merge, never for a run that stopped short). Scope, stated plainly: this confirms the check NAME has also failed recently on the base branch — job-name granularity, not proof the SAME sub-test failed both times (a monorepo-wide test job could in principle fail for two different reasons); the refusal message that leads you here always prints exactly what failed, so look before asserting. **Known gap:** if the flaky check is also a *required* status check under branch protection, GitHub can still refuse the final `gh pr merge` even after this gate and the local tests pass (a required check FAILING still blocks, unlike the empty-rollup case above where nothing failed, it was merely absent) — you'd land at the `--skip-ci` hatch anyway. Not unsafe, just an unresolved UX gap. |
| CI still running | Ship **watches** pending checks to completion (polls every `SHIP_CI_POLL`s up to `SHIP_CI_WAIT`s) instead of refusing — you don't babysit. |
| Unresolved review threads | Same check as [`../review-threads/`](../review-threads/). Pass `--resolve-addressed-threads` to let ship first auto-close the threads that are safe without a human — unresolved + outdated (addressed by a later commit) + authored entirely by bots; human or unaddressed threads still block (see Flags, #268). |
| **PR younger than the review-dwell window** | Closes the premature-merge gap: the unresolved-threads check above only fails when threads *already exist*, so "0 unresolved threads" is **vacuously true** before any review has posted — a PR opened and shipped within seconds passes it without a single question forming. The dwell gate refuses until at least `SHIP_REVIEW_DWELL` seconds (default **600 / 10 min**) have elapsed since the PR's last code push, giving async review (multi-model / CI-AI / human) time to form its comments (which then become threads the check above forces resolved). Runs **independently of `--skip-ci`**. Window starts at `max(createdAt, head-commit committedDate)` so a new push restarts it. Disable with `SHIP_REVIEW_DWELL=0`; override one ship with `--no-review-dwell-ok <reason>` (logged). Fail-closed: unreadable/unparseable timestamps refuse. |
| UI-touching PR with no screenshot | Same check as [`../screenshots/`](../screenshots/); override with `--no-screenshot-ok`. |
| **Shippable source changed but the version is UNCHANGED** | A ship of source is a release; the declared version (`pyproject.toml` `version`/`package.json` `"version"`) must be bumped so `--version` stays a real freshness signal (skill: `bump-version-on-release`). Docs-only / pure test/CI PRs are exempt. Override a genuine no-release ship with `--no-version-bump-ok <reason>` (or `SHIP_SKIP_VERSION_BUMP=1`). |
| **Review-quorum bar not met** (Guard-B, self-merge-authority) | The gate that makes self-merge *"strictly controlled"*. Before merging, ship derives the PR's task code (`$REVIEW_TASK_CODE`, else a `HYP-<n>`/uppercase ticket token, else a purely descriptive ALL-CAPS/hyphenated code — `SME-ROADMAP-WORKTREE-NOTE`-shaped, 3+ segments, no digits — tried against the branch name, then the PR body) and asks review-cli whether that task has >= `SHIP_REVIEW_QUORUM_MIN_ITER` **PASSED** review iterations across >= `SHIP_REVIEW_QUORUM_MIN_MODELS` distinct models among those passed iterations (`review task <code> --check`, falling back to `--quorum-check` on an older review-cli — a failed/degraded review does not count toward the bar). Both floors are **clamped to a hard minimum of 3** (raise-only; a `0`/negative/below-3 value resolves to 3). Bar met -> ship **re-derives the verdict from the counts** (never trusts the subprocess's `passed` boolean alone), prints `AUTHORITY CONFIRMED`, and proceeds. Bar not met, a quorum reading 0 iterations / 0 distinct models, no task code, `review`/`jq` missing, or the store unreadable -> **fail-closed refuse**. There is **NO self-service override flag**: a one-time bypass is requested by setting `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>"`, which asks Alex **live on Telegram** (via the shared `agenttools_hatch_escalation` lib) and proceeds ONLY on his real-time approval — a blank/bare value is denied. Runs **independently of `--skip-ci`**. Every non-dry-run gated ship appends an audit line (`authorized` / `bypass:approved` / `bypass:denied` / `refused`) to `SHIP_AUDIT_FILE`; `--dry-run` prints the would-be audit instead. Disable the whole gate with `SHIP_REVIEW_QUORUM=0`. |
| Local branch has unpushed/diverged commits, or dirty worktree | Avoids merging stale/uncommitted local state. |

Then it squash-merges, deletes the remote branch, removes the local branch+worktree
(unless you're *inside* that worktree — then it's left so your session keeps a cwd), and
fast-forwards your main checkout.

When ship decides CI is structurally unavailable and runs its local fallback gates, the
test runner picks a command in this order (highest priority first):

1. **`SHIP_LOCAL_TEST_CMD` env var** — test-only escape hatch, never set in production.
2. **`.ship-config` file, committed at the repo root** — an audited, per-repo override for
   repos where auto-detection can't guess correctly (e.g. a monorepo-of-fixtures whose real
   suite lives in a subdirectory, like a project with no root-level manifest). Simple
   `KEY=value` lines, two whitelisted keys: `SHIP_LOCAL_TEST_DIR=<path>` (a repo-relative
   subdirectory to run the command from, or to scope auto-detection to) and
   `SHIP_LOCAL_TEST_CMD=<cmd>` (a command line, eval'd the same way as the env var of the
   same name — this is just a committed, per-repo source for it). The file is read from the
   last **committed** content at `HEAD` (`git show HEAD:.ship-config`), never the working
   tree, so an uncommitted/staged-only edit is not honored — the "audited" claim is an
   enforced property. `SHIP_LOCAL_TEST_DIR` is rejected (and invalidates the whole file) if
   it's absolute, contains a `..` path component, or resolves to the repo root itself.
3. **`dev run --repo-only test`** if the repo has a committed `rig.yaml`, that file declares
   top-level `scripts.test`, and `dev` is on PATH. Probe/runtime errors from
   `dev has-script --repo-only test` block instead of silently guessing a fallback runner.
   If `dev` is installed, install it with its runtime dependencies (`agenttools-config`/
   PyYAML); repos with `rig.yaml` fail closed on dependency or config errors so a declared
   script is not bypassed by auto-detection. A PATH `dev` must answer the hidden
   `dev --agenttools-dev-probe`; unrelated tools named `dev` are ignored and ship falls back
   to stack detection.
4. **Root-level auto-detection** — the portable pyproject/package/Cargo detection in
   `ship.sh`, at the repo root.
5. **`e2e/` subdirectory auto-detection** — the same three manifests, one level down, in
   `e2e/` ONLY. `test/` and `tests/` are deliberately never auto-probed (those directory
   names are also where repos most often keep fixture manifests unrelated to the real
   suite) — use `.ship-config` for any other/ambiguous subdirectory layout.

If nothing above matches, ship fails closed with "no recognized test runner found" rather
than merging unverified.

## Quick start

```bash
cp ci/ship/ship.sh ~/bin/ship && chmod +x ~/bin/ship    # or wherever your PATH points
ship 123                                                # merge PR #123 when green
ship 123 --dry-run                                      # show what it would do
ship 123 --screenshot ./after.png "new dialog"          # attach + post visual proof
```

Wire it as a `gh` alias if you like:

```bash
gh alias set ship '!bash ~/bin/ship'    # then: gh ship 123
```

> **Hatch bypass needs the sibling `lib/`.** The review-quorum gate's one-time Telegram bypass
> imports `agenttools_hatch_escalation` from `lib/` two levels above `ship.sh` in the checkout.
> The sanctioned `gh ship` → `pr-ship.sh` delegator runs the **canonical catalog** `ci/ship/ship.sh`
> (resolved from the repo checkout or `AGENT_TOOLS_ROOT`), so `lib/` is present and the bypass works.
> A bare `cp ci/ship/ship.sh ~/bin/ship` copy has no sibling `lib/`, so a bypass request from it
> **fails closed** (the gate still enforces; you just can't hatch-bypass) — run `ship` from the
> checkout, or point `AGENT_TOOLS_ROOT` at it, to use the bypass.

> **Trust model — run `gh ship` from a protected checkout.** Every gate here (CI, threads,
> version-bump, review-quorum) trusts the ship PROCESS's execution environment: the tools it
> resolves on `PATH` (`gh`, `git`, `jq`, `python3`, `review`, `tg-ctl`) and the `ci/ship/ship.sh`
> + `lib/` code it runs. A party who controls that environment — a hostile `PATH`, or a PR
> worktree whose own `ci/ship/ship.sh` is executed — can weaken any gate, not just the hatch.
> So `gh ship` is meant to run from the **orchestrator's canonical/main checkout**, never from an
> untrusted PR worktree. Within that trust model the review-quorum bypass is closed: nothing the
> PR contents or the `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM` value carries can forge approval — the
> lib loads by fixed file path under `python3 -I`, tg-ctl resolves off the OS-identity home, and
> approval requires the helper's explicit stdout sentinel (a benign/broken `python3` fails closed).

## All project coupling is OPTIONAL (env knobs)

Nothing org-/tracker-/layout-specific is hard-coded. Configure via env:

| Env | Default | Purpose |
| --- | ------- | ------- |
| `SHIP_DEFAULT_BRANCH` | `main` | Base branch. |
| `SHIP_MERGE_METHOD` | `squash` | `squash` / `merge` / `rebase`. |
| `SHIP_CI_WAIT` | `900` | Max seconds to WATCH pending CI to completion before giving up. |
| `SHIP_CI_POLL` | `20` | Poll interval (seconds) while watching pending CI. |
| `SHIP_CI_GRACE` | `45` | Grace window for checks to *register* on a fresh PR before concluding "no CI" (an empty rollup is briefly normal right after opening a PR). |
| `SHIP_FLAKE_LOOKBACK_RUNS` | `5` | For the `--known-flake` gate: how many recent COMPLETED runs of the same workflow on the base branch to search for a matching failure. A match in any of them is enough. |
| `SHIP_MAIN_CHECKOUT` | first worktree | Where to fast-forward after merge. |
| `SHIP_UI_PATH_REGEX` | common FE paths | What makes a PR "UI-touching". **Set empty to disable the screenshot gate.** |
| `SHIP_IMAGE_UPLOAD_CMD` | (unset) | Optional uploader for `--screenshot`: a command that takes the image (`{FILE}` token or `$1`) and prints a public URL. Without it, `--screenshot` just embeds a local-path note. |
| `SHIP_SKIP_VERSION_BUMP` | (unset) | `=1` overrides the version-bump gate (env equivalent of `--no-version-bump-ok`). |
| `SHIP_VERSION_FILES` | auto-detect | Space-separated version files to check (relative to repo root). Default: `pyproject.toml` then `package.json` at the root. Set for a non-standard layout. |
| `SHIP_REVIEW_DWELL` | `600` | Minimum seconds since the PR's last code push before a merge is allowed, so async review has time to **form** its comments. `0` disables the gate. |
| `SHIP_REVIEW_QUORUM` / `SHIP_REVIEW_QUORUM_ENABLED` | enabled | Set either to `0` to disable the review-quorum gate (Guard-B) entirely (ops off-switch). |
| `SHIP_REVIEW_QUORUM_MIN_ITER` | `3` | Quorum floor: PASSED review-cli iterations for the task. **Clamped to a hard minimum of 3** — this knob can only RAISE the bar, never lower it (an unset / `0` / negative / below-3 value resolves to 3, fail-closed #242). |
| `SHIP_REVIEW_QUORUM_MIN_MODELS` | `3` | Quorum floor: distinct models across those PASSED iterations. **Clamped to a hard minimum of 3** — raise-only, same as `MIN_ITER`. |
| `REVIEW_TASK_CODE` | (auto) | The PR's task code for the quorum gate. Unset → derived from a `HYP-<n>`/uppercase ticket token, then a purely descriptive ALL-CAPS/hyphenated code (`SME-ROADMAP-WORKTREE-NOTE`-shaped, 3+ segments, no digits), tried against the branch name then the PR body; none found → fail-closed refuse. |
| `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM` | (unset) | One-time bypass request for a not-met quorum bar: set to a written justification to ask Alex live on Telegram (shared `agenttools_hatch_escalation` lib); proceeds ONLY on his real-time approval. Under `--dry-run`, the helper reports the would-be request but does not contact `tg-ctl` or write a bypass audit line. Blank/bare values denied. **No self-service reason flag exists.** The lib is imported from a fixed path relative to `ship.sh`, and tg-ctl is resolved from a rig.yaml in the OS account's **real home** (`pwd.getpwuid` — **not** the `$HOME` env var, **not** the PR's repo) then a trusted-paths allowlist. So neither an env var (including a doctored `HOME`) nor a rig.yaml the PR commits can redirect approval to a stub. |
| `SHIP_AUDIT_FILE` | `~/.config/agent-tools/ship-audit.jsonl` | JSONL audit log; one line per non-dry-run gated ship. Review-quorum decisions: `authorized` / `bypass:approved` / `bypass:denied` / `refused` (with a `task_code`). `--skip-ci` decisions: `skipci:bypass:approved` / `skipci:bypass:denied` / `skipci:refused` (with `gate":"skip-ci"`). `--known-flake` decisions: `confirmed` / `confirmed-local-gate-failed` / `refused` (with `"gate":"known-flake"` and the asserted check names) — `confirmed` is written only after `gh pr merge` actually succeeds, never merely after the claim checks out. |

## Flags

- `--skip-ci` — admin-merge bypassing the green-CI gate (+ branch protection); the other
  preflights still run. **Deny-by-default**: it proceeds ONLY on a one-time live Telegram approval
  requested via `RIG_HATCH_REQUEST_SHIP_SKIP_CI="<justification>"` (same shared hatch lib as the
  review-quorum gate; a blank/bare value is denied; no ops off-switch). This is **not** the way to
  handle billing-blocked/stuck CI — for that, run **without** `--skip-ci`: the normal path
  auto-detects the outage, runs the local fallback gate, and does a normal (non-admin) merge.
- `--dry-run` — print, change nothing.
- `--no-screenshot-ok <reason>` — override the UI screenshot requirement, logged.
- `--no-version-bump-ok <reason>` — override the version-bump requirement for a genuine
  no-release ship (docs-only, pure test/CI, a revert), logged.
- `--no-review-dwell-ok <reason>` — fast-track past the review-dwell window for a genuine
  trivial/urgent merge, logged (the reason is printed to the ship log).
- `--resolve-addressed-threads` (or `SHIP_RESOLVE_ADDRESSED_THREADS=1`) — before the
  unresolved-threads gate, auto-resolve the review threads that are **safe to close without a
  human**: unresolved **and** `isOutdated` (the code the thread anchored to has changed — a later
  commit addressed it) **and** authored **entirely** by automated reviewers (login ends in
  `[bot]`, or `chatgpt-codex-connector` / `codex-review-bot`). A thread with **any** human comment,
  or one that is still current (not addressed), is **never** touched — it falls through and still
  blocks the merge. This lets a shipping agent close its own bot nits through `gh ship` instead of
  hand-running the `resolveReviewThread` mutation (which the `block-raw-pr-merge` agent-hook used to
  false-block, #268). Off by default; honours `--dry-run` (reports what it would resolve, mutates
  nothing). A bot thread with **more than 100 comments** is fail-closed (never auto-resolved, since a
  human reply could hide on an unfetched page) and must be resolved by hand. **Caveat:** `isOutdated`
  only means the anchored code *changed* since the comment (a heuristic that the nit was addressed) —
  **not** a verified fix. A still-valid bot finding on rewritten/rebased code can therefore be
  auto-closed; use the flag only when you trust the bot threads are genuinely addressed. A
  severity-aware gate (never auto-close a P0/P1/security bot thread) is a tracked follow-up.
- `--screenshot <path> [desc]` — upload (via `SHIP_IMAGE_UPLOAD_CMD`) and post a screenshot
  as a PR comment; repeatable.
- `--known-flake <check-name>` — assert that the FAILED check named `<check-name>` (exactly as
  printed by the green-CI gate's own `x <name> -> <conclusion>` refusal lines) is a pre-existing
  failure unrelated to this diff, not the "CI is structurally down" case above. Repeatable — one
  per currently-failing check. **Not a blind trust-me flag**: ship independently verifies the
  claim against the base branch's own recent CI history before doing anything with it (see the
  table row above); an unverifiable assertion is refused, same as not passing the flag. On
  success, runs the local fallback gate and merges only if it's green too. Logged to
  `SHIP_AUDIT_FILE`.

The **review-quorum gate (Guard-B) has no override flag** by design — a bypass goes through live
Telegram approval to Alex via `RIG_HATCH_REQUEST_SHIP_REVIEW_QUORUM="<justification>"` (see the env
table above), never a self-service reason. The hatch imports the shared `agenttools_hatch_escalation`
lib from `lib/` **relative to `ship.sh`'s location in the checkout**, so run `ship` from within the
agent-tools checkout (or via `gh ship` pointed at it) to use the hatch. A bare `cp ci/ship/ship.sh
~/bin/ship` copy can still *enforce* the gate but can't reach the lib, so a bypass request from a
detached copy **fails closed** (refuses) — that's intentional, not a bug.

## What was stripped vs an internal version

This is generalized from a real in-house ship script. Removed/parameterized: any issue-
tracker coupling (ticket-id extraction, attaching proof to a tracker — replaced by the
generic `SHIP_IMAGE_UPLOAD_CMD` hook), hard-coded front-end path layout (now
`SHIP_UI_PATH_REGEX`), and any org/branch assumptions (now `SHIP_DEFAULT_BRANCH`). If you
want tracker integration back, set `SHIP_IMAGE_UPLOAD_CMD` to your tracker's attach command.

## When to use

When you merge PRs from the CLI and want the merge to be impossible unless the PR is green,
its threads resolved, a review-dwell window elapsed (so review questions had time to form),
and (for UI) its screenshot present — without trusting yourself to remember. The CI workflows
in this directory are the server-side backstop; `ship` is the client-side gate.

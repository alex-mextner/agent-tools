# block-raw-pr-merge

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 10 (runs early)

Denies a shell command that merges a PR directly and bypasses the ship gate:

- `gh pr merge <PR>` (squash/merge/rebase, any flags)
- `gh pr merge --admin <PR>` (the admin bypass is the most dangerous case)

Lets the sanctioned merge path through:

- `gh ship <PR>` (and a `gh alias` that runs ship)
- `pr-ship.sh` / `ship.sh` (the script the ship alias points at)
- any non-merge `gh pr` subcommand (`view`, `list`, `checkout`, `create`, `comment`, …)

## Why an agent-hook (not a skill, not a git-hook)

"Use `gh ship`, never a raw merge" was for a long time **advice only** — a line in
`AGENTS.md`. Advice cannot stop an autonomous (auto-mode) agent from running `gh pr merge`:
the moment permission prompts are auto-accepted, a doc rule has zero enforcement and the
green-CI + mandatory-screenshot gates are silently skipped. A git-hook can't help either —
the merge happens server-side on GitHub, not at a local commit/push. The only place to stop
the bypass is **before the command runs**, which is what a `pre-bash` agent-hook does. This
is the enforcement counterpart of the `ci/ship` gate and the ship usage rule.

This guard is what makes auto-mode safe: the agent runs without babysitting, and the guard
catches the one irreversible action (a gate-skipping merge) before the side effect.

## Escape hatch (controllable, not a hard wall)

Mirrors the other configurable guards (`enforce-timeout-on-bash`, `block-raw-process-env`):
a deliberate raw merge is allowed **with an explicit, logged reason** —

```bash
# session-wide override (reason REQUIRED, or it still blocks):
ALLOW_RAW_PR_MERGE=1 ALLOW_RAW_PR_MERGE_REASON="ship gate down, manual verify done" \
  gh pr merge 123 --admin

# one-off, self-documenting in the command itself:
gh pr merge 123 --admin   # no-ship-guard: hotfix, CI provider outage, verified locally
```

A reasonless `ALLOW_RAW_PR_MERGE=1` is ignored and the merge stays blocked — a silent bypass
of the bypass-guard is the exact failure this hook prevents.

## Fail-closed

`on_error: "closed"`. If the hook can't inspect the command (a malformed event, a crash),
it **blocks** rather than allows — a raw merge slipping through a broken guard is the failure
this hook exists to stop.

## Install

```bash
chmod +x block_raw_pr_merge.py
# edit the descriptor's "cmd" to this file's absolute path, then drop the descriptor
# into your harness's pre-bash hook directory. (rig apply does this for you.)
```

## Test

```bash
echo '{"args":{"command":"gh pr merge 123 --admin"}}' | ./block_raw_pr_merge.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block",...}  exit=10

echo '{"args":{"command":"gh ship 123"}}' | ./block_raw_pr_merge.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

echo '{"args":{"command":"gh pr merge 123 # no-ship-guard: provider outage"}}' | ./block_raw_pr_merge.py; echo "exit=$?"
# → decision":"allow" (escape hatch with a reason)  exit=0
```

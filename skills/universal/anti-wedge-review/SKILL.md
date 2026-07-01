---
name: anti-wedge-review
description: Use when a subagent is about to run a code review before committing. Run review synchronously as a Bash call — NEVER via TaskCreate, Monitor, or any background mechanism. The review process is killed when the subagent's turn ends; backgrounding it means the marker never refreshes, git commit stays blocked, and the agent wedges forever.
---

# Anti-wedge: run review synchronously, never via Task/Monitor

A subagent that backgrounds its pre-commit review via the TaskCreate tool (or
Monitor, or any "wait for notification" mechanism) creates an **infinite wedge**:

1. The background review process is **killed** when the subagent's context/turn
   ends — background tasks do NOT outlive the turn that started them.
2. The review marker (`~/.cache/agent-tools/last-review`) is **never updated**.
3. The `pre-commit` hook checks for a fresh marker and **blocks the commit**.
4. The agent has nothing left to do — it can't commit, the review is gone — so
   it **gives up and signals "done"** with no PR.
5. The orchestrator gets a "done" notification with nothing shipped. Every agent
   sent to rescue the situation hits the same wall.

This wedge has burned 4+ consecutive task attempts on task #73. The `subagent-no-bg-longproc`
hook catches Bash-level backgrounding (`run_in_background: true`, `&`, `setsid`), but
**does not catch TaskCreate-based backgrounding** — that is the gap this skill closes.

## Rules (non-negotiable for any subagent)

### 1. NEVER use TaskCreate / Task / Monitor to run review

```
# WRONG — the review dies with your turn, marker never refreshes, commit blocked
TaskCreate("review diff -C /repo")   # TaskCreate — FORBIDDEN
Monitor("review diff -C /repo")      # Monitor — FORBIDDEN
```

The subagent-no-bg-longproc hook **does not intercept these** — they are agent tool
calls, not Bash calls. You will not be warned. The wedge will happen silently.

### 2. ALWAYS run review as a synchronous Bash call with an explicit timeout

```bash
# CORRECT — blocks until complete, marker is refreshed, commit can proceed
review diff -C /path/to/repo
```

Set `timeout_ms: 600000` (10 minutes) on the Bash tool call. Multi-model review
is slow; without a timeout you risk a silent hang from the other direction.

### 3. Review MUST COMPLETE before `git commit`

Do not start review and commit in parallel. The pre-commit hook checks the marker
at commit time; if the review is still running (or never started), the commit is
blocked. Sequence strictly:

```
1. git add <files>
2. review diff -C /repo          ← synchronous, wait for it
3. git commit -m "..."           ← only after review completes
```

### 4. "Review not fresh" at commit time means the review did not complete

If `git commit` is blocked with a message like "review marker not fresh" or
"review required before commit":

- Your review either did not run, ran via Task (and was killed), or ran in a
  different working directory.
- **Fix:** run `review diff -C <repo>` synchronously right now, wait for it to
  finish, then commit again. Do not attempt workarounds.

## Why the hook gap exists

`subagent-no-bg-longproc` fires on the `pre-bash` hook point — it sees Bash tool
calls and can inspect `run_in_background`, trailing `&`, and `setsid`. It cannot
see `TaskCreate` or `Monitor` tool calls, which fire on `pre-agent` (a different
hook point). The `pre-agent` point is not yet wired to the anti-wedge guard
(tracked: agent-tools#69). Until that wiring lands, **the skill is the only
enforcement** for Task-tool-based review backgrounding.

## Escape hatch

For a truly docs-only change where no review is needed: stage only `.md` files.
The docs-only classifier in the pre-commit hook bypasses the review-marker check
when ALL staged files are `.md` files. This is legitimate for pure documentation
changes; it is not a workaround for skipping review on code.

```bash
git diff --cached --name-only   # must show ONLY .md files for the bypass to apply
```

## Summary table

| Mechanism | Safe? | Why |
|-----------|-------|-----|
| `review diff -C /repo` (Bash, foreground) | YES | blocks, completes, marker refreshes |
| `review diff … &` (Bash, shell background) | NO | caught by `subagent-no-bg-longproc` hook |
| `run_in_background: true` on Bash | NO | caught by `subagent-no-bg-longproc` hook |
| TaskCreate / Task tool | NO | NOT caught by any hook; process dies with turn |
| Monitor tool | NO | NOT caught by any hook; process dies with turn |

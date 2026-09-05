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
Monitor("review diff -C /repo")      # Monitor — now hard-BLOCKED by subagent-no-monitor
```

Neither `subagent-no-bg-longproc` nor `subagent-no-monitor` intercept **TaskCreate** — it is
an agent tool call, not a Bash or Monitor call. You will not be warned; the wedge will happen
silently. **Monitor is different**: a dispatched subagent calling Monitor is now hard-blocked
by the `subagent-no-monitor` hook (`pre-monitor`, agent-tools#439 / HYP-1350) — but only once
the companion rig-cli `Monitor` matcher has shipped and `rig apply` has run on the machine (a
two-repo registration gap, same as `pre-agent`/`pre-skill`). Do not rely on the hook alone
until you've confirmed it is actually wired on your machine — the rule above (never call
Monitor from a subagent) still applies unconditionally regardless of whether the hook fires.

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
see `TaskCreate` or `Monitor` tool calls.

`Monitor` now has its own dedicated hook, `subagent-no-monitor`, on a new `pre-monitor`
point — it blocks **any** subagent call to Monitor unconditionally (no foreground mode
exists for Monitor to fall back to, so there's no backgrounded/foreground classification to
make). This closes the Monitor half of the gap this skill originally documented.

`TaskCreate` (and `Task`) still fire on `pre-agent`, which is not yet wired to an
anti-wedge guard for THIS wedge shape (tracked: agent-tools#69). Until that lands, **the
skill is the only enforcement** for TaskCreate-based review backgrounding — Monitor-based
backgrounding is now also hook-enforced, but keep following the rule above regardless, since
the hook depends on a machine having the companion rig-cli matcher applied.

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
| Monitor tool | NO | caught by `subagent-no-monitor` hook (once the companion rig-cli matcher is applied on this machine — see above); process dies with turn regardless |

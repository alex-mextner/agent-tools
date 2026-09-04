---
name: anti-wedge-review
description: Use when a subagent is about to run a code review before committing. Run review synchronously as a Bash call — NEVER via TaskCreate, Monitor, or any background mechanism. The review process is killed when the subagent's turn ends; backgrounding it means the marker never refreshes, git commit stays blocked, and the agent wedges forever.
---

# Anti-wedge: run review synchronously, never via Task/Monitor

A subagent that backgrounds its pre-commit review via the TaskCreate tool (or
Monitor, or any "wait for notification" mechanism) creates an **infinite wedge**:

1. The background review process is **killed** when the subagent's context/turn
   ends — background tasks do NOT outlive the turn that started them.
2. The review marker (`~/.cache/agent-tools/last-review`) is **never updated** — only a
   review that RUNS TO COMPLETION and passes writes it.
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

### 2. ALWAYS run review as a synchronous Bash call, with a GENEROUS explicit timeout

```bash
# CORRECT — blocks until complete, marker is refreshed, commit can proceed
review diff --staged --task <CODE> -C /path/to/repo
```

Neither flag is optional. review-cli writes the commit-gate marker for exactly one shape
of run: a COMPLETED `review diff --staged --task <CODE>` whose diff came from the real
index and was small enough to reach every reviewer in full. "Completed" is not "clean" — a review that
comes back with findings still writes the marker, because the gate enforces that a review
happened, not that it was happy. What withholds the marker is a run that could not be
trusted to have covered the change. An unstaged `review diff` reviews the working
tree, prints its findings, and deliberately leaves the marker alone — so a green unstaged
review followed by a blocked commit is the gate working as designed, not a wedge. Stage
what you are committing first. `--task <CODE>` files the run in review-cli's iteration
history and is REQUIRED: without it (or an exported `REVIEW_TASK_CODE`) `review diff`
exits 2 before reviewing anything, which looks like a failed review and leaves you exactly
as blocked.

If the staged diff is too large, review-cli truncates it for dispatch and then withholds
the marker on purpose — no reviewer saw the whole thing. Re-running does not help; split
the change into smaller staged commits and review each one.

ALWAYS set `timeout_ms` explicitly, to the LARGEST value your Bash tool accepts (in
Claude Code that ceiling is `600000` — ten minutes; other harnesses differ, so check
yours). Two different wedges live on either side of this number, and both have bitten:

- Naming nothing is not "no timeout". The Bash tool applies its own default — about two
  minutes in Claude Code — and kills a review that had barely started.
- Naming a small one is the same wedge with extra steps. The timeout lands on the run
  that did all the work and was about to write the marker, so you pay the full review
  cost and get nothing.

`review` itself wants no external deadline at all: it prints the expected duration for
your pool size at startup and carries its own internal backstop measured in hours. So if
your harness lets you omit the deadline entirely — no implicit default, no ceiling — omit
it, and this whole section is moot. The instruction above is for the harnesses that give
you no such choice (Claude Code among them): there a deadline applies whether the tool
likes it or not, and the only decision left is whether you pick it or the default picks
it for you. On those, if the printed estimate is anywhere near the ceiling, shrink the
WORK rather than hope: stage a smaller commit, or review with a smaller `--pool`. A review
that fits inside the ceiling is the only one guaranteed to reach the marker write.

### 3. Review MUST COMPLETE before `git commit`

Do not start review and commit in parallel. The pre-commit hook checks the marker
at commit time; if the review is still running (or never started), the commit is
blocked. Sequence strictly:

```
1. git add <the files you are committing>
2. review diff --staged --task <CODE> -C /repo   ← synchronous, wait for it
3. git commit -m "..."                           ← only after review completes
```

### 4. "Review not fresh" at commit time means the review did not complete

If `git commit` is blocked with a message like "review marker not fresh" or
"review required before commit":

- Your review either did not run, ran via Task (and was killed), ran WITHOUT
  `--staged` (the shape that writes no marker), or ran in a different working
  directory.
- **Fix:** stage the files you are committing and run
  `review diff --staged --task <CODE> -C <repo>` synchronously right now, wait for it to
  finish, then commit again.
- **Never** `touch`/`mkdir` the marker path by hand to get past this. It certifies a
  review nobody ran. It also does not work headless: the agent runner screens the commands
  YOU issue and rejects a write outside the project, while `review …` is allow-listed and
  writes the marker from inside its own process. Two detached agents died in one night
  trying the `touch`.

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
| `review diff --staged --task <CODE> -C /repo` (Bash, foreground) | YES | blocks, completes, and the passing staged run writes the marker |
| `review diff --task <CODE> -C /repo` (unstaged, foreground) | NOT for a commit | a fine review of the working tree, but it writes no marker, so the commit stays blocked |
| `review diff --staged -C /repo` (no `--task`) | NO | exits 2 before reviewing anything — no review, no marker |
| `review diff … &` (Bash, shell background) | NO | caught by `subagent-no-bg-longproc` hook |
| `run_in_background: true` on Bash | NO | caught by `subagent-no-bg-longproc` hook |
| TaskCreate / Task tool | NO | NOT caught by any hook; process dies with turn |
| Monitor tool | NO | NOT caught by any hook; process dies with turn |

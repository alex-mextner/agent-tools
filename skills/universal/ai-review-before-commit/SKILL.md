---
name: ai-review-before-commit
description: Use before committing a non-trivial or architectural change. Run an automated code review on the diff (ideally a model different from the one that wrote it) and address its findings before you commit, treating it as peer review rather than a rubber stamp.
---

# AI review before commit

For anything beyond a trivial fix, get a second pair of eyes on the diff before it
lands. An automated review on the uncommitted change catches the class of mistake the
author is blind to — exactly because they wrote it (see `gan-critic-loop`).

## Rule

- Before committing a non-trivial change, run a review tool over the **uncommitted
  diff** and read its findings.
- Prefer a **different model** as the reviewer than the one that produced the code —
  different blind spots catch different bugs. For architectural calls, a multi-model
  panel beats any single reviewer.
- Treat findings as **peer review, not gospel**: verify each one, fix the real
  problems, and push back (with reasoning) on the ones that are wrong. Performative
  agreement with a bad suggestion is as harmful as ignoring a good one.

## Recording that a review ran (the `require-review-before-commit` marker)

Some repos gate the commit on a marker file proving a review ran (see the
`require-review-before-commit` agent-hook). **Check the index first** (`git status`) —
`git add` only adds, it never clears what's already staged, so in a shared/dirty worktree
unstage anything unrelated (`git restore --staged <path>`) before you run this. **Then
stage the intended change and review staged — that's the recipe, no `touch` needed:**

```bash
git add -- 'path/to/changed-file.py' && review diff --staged --task 'TICKET-CODE'
```

Stage the SPECIFIC files you changed (repeatable), not `git add -A` — a broad add in a
shared or dirty worktree can pull in unrelated edits (or untracked secrets) that end up
committed alongside your change. Replace both placeholders with real values, and keep
BOTH quoted — `git add -- 'real/path.py'` (`--` first so a path starting with `-` isn't
read as a flag) and `--task 'CODE'`. Do NOT wrap either in `<...>`: that's shell
input-redirection syntax in bash/zsh, so `--task <TICKET-CODE>` is a parse error, not a
placeholder. Neither `git add` nor `--task` sanitizes its argument, so an unquoted real
value with shell metacharacters (a filename or task id copied from an untrusted source)
would execute as a second shell command if pasted bare — and a single-quoted template
isn't a full escape either (a value containing a literal `'` closes the quote early), so
never splice raw untrusted text into the command; use a real shell-quoting function
(`shlex.quote` in Python) if the value isn't one you chose yourself. `--task 'CODE'` (or
exporting `REVIEW_TASK_CODE='CODE'`, quoted the same way) is REQUIRED by review-cli —
without either the command exits nonzero before any review runs, and no marker gets
touched.

review-cli touches the marker itself, in Python, the instant a `--staged` review passes —
there is no shell command to construct, so there's nothing for a worktree-isolated Claude
Code session's guard to trip on. Reviewing WITHOUT `--staged` does not touch the marker, and
is the usual reason this gate still blocks right after a clean-looking review.

Only fall back to a manual `touch` when no reviewer actually ran (e.g. a human already
reviewed it) — never as a shortcut to skip running a real review. If you must, it's a **flat
command with no substitution** — no `$(...)`, no `${...}`, no bare `$VAR` — e.g.
`touch ~/.cache/agent-tools/last-review`; a worktree-isolated session's own guard refuses
`$()`/`${...}`/bare-`$VAR` shapes outright (unrelated to this hook, fires even though the
command touches no git — splitting into two Bash calls doesn't help if either one still
contains the expansion). If `REVIEW_MARKER` might be overridden, resolve it first with
`printenv REVIEW_MARKER` (safe — it passes the variable NAME as a plain argument, not an
expansion) and touch the literal path it prints — single-quote it if it has spaces.

## When to spend the call

Worth it: a non-obvious refactor, a new API surface, a security-relevant change, a
weird failure class, "should this ship?" at the end of a feature.

Skip it: a one-line typo fix, a comment tweak, a version bump. Don't burn a review on
trivia — it trains you to ignore the output.

## Why

Review is the cheapest place to catch a bug — before it's in history, before CI,
before a teammate builds on it. An independent reviewer imports a perspective the
author structurally lacks. Wiring it into your pre-commit habit (see
`pre-commit-gate`) makes "did anyone else look at this" the default, not the
exception. The `review` CLI (a skill, not an MCP) makes such a reviewer callable from any agent, and the
`ci/ai-review/` slot runs the same idea automatically on every PR (advisory — it posts
findings, it does not auto-block). See also the `ci-gate-suite` skill.

# Carrier decision guide: skill vs agent-hook vs git-hook

A rule can be carried three ways. They're not interchangeable — each enforces at a
different moment, with different power and different failure modes. Picking the wrong
carrier means a rule that either can't be enforced or fires too late to matter.

## The three carriers

| Carrier        | Fires when                          | Enforces by         | Can it block? |
| -------------- | ----------------------------------- | ------------------- | ------------- |
| **Skill**      | The agent/human reads it, any time  | Persuasion (advice) | No            |
| **Agent-hook** | An agent uses a tool, mid-session   | Tool-use interception (allow/block) | Yes — *before* the side effect |
| **Git-hook**   | A git event (commit / push / msg)   | Aborting the git op | Yes — at commit/push time |

## Decision tree

1. **Is the rule a matter of judgment** — naming, design, "investigate before deleting",
   tone, debugging discipline — with no mechanical check that captures it?
   → **Skill.** You can't regex "is this a good name". Advisory text is the only honest
   carrier; pretending otherwise produces false-positive gates people learn to bypass.

2. **Must it be enforced *before* a side effect that a git-hook would see too late** —
   blocking a `--no-verify` bypass, stopping a secret from being *written to a file*,
   wrapping a hangable command in `timeout`, prompting the completion self-check as the
   turn ends?
   → **Agent-hook.** Only a mid-session tool-use interception can catch these. A git-hook
   literally cannot block its own bypass, and can't see a secret until it's already on disk
   and staged.

3. **Is it a mechanical check that's meaningful at commit/push time** — format clean, types
   pass, tests green, conventional-commit message, no secret in the staged diff?
   → **Git-hook.** It runs for *every* committer (human or agent), needs no harness, and is
   the right gate for "this artifact is well-formed before it enters history".

4. **Both mid-session AND at commit?** Use **both**. Example: secrets get a `block-secrets-
   write` agent-hook (catches the write) *and* a `no-secrets-scan` git-hook (catches the
   commit). Defense in depth — the earliest carrier prevents, the later one backstops.

## Worked examples

| Rule                                   | Carrier(s)                          | Why |
| -------------------------------------- | ----------------------------------- | --- |
| Never `git commit --no-verify`         | agent-hook                          | A git-hook can't block the flag that skips git-hooks. |
| No secrets in source                   | agent-hook + git-hook               | Block the write *and* backstop the commit. |
| Conventional-commit message            | git-hook (`commit-msg`) + skill     | Mechanically checkable at commit; the skill explains the format. |
| Lint/typecheck/tests green             | git-hook (`pre-commit`) + skill     | The gate runs the checks; the skill says why and "never bypass". |
| Timeout every hangable command         | agent-hook + skill                  | The bound must be on the command as issued, mid-session. |
| AI review before commit                | agent-hook + MCP + skill            | The hook gates the commit on a review having run; the MCP makes the review callable. |
| Investigate before deleting dead code  | skill only                          | Pure judgment — no regex captures "is this orphaned-by-migration". |
| Naming / smallest-change / tone        | skill only                          | Judgment; a gate would be all false positives. |
| Completion self-check                  | agent-hook (`stop`) + skill         | The Stop hook is the only thing that fires at end-of-turn. |
| No raw `process.env` in feature dirs   | agent-hook (or git-hook grep) + skill | Catchable mechanically; the skill explains the loader pattern. |

## Fail-policy note (agent-hooks)

When an agent-hook *itself* errors, its `on_error` policy decides the outcome:

- **`closed`** (block on error) for **security** gates — "I couldn't check" must mean
  "don't do it" (block-no-verify, block-secrets-write).
- **`open`** (warn + proceed) for **discipline** nudges — a crash must never wedge the
  agent (require-review, enforce-timeout, block-raw-process-env, stop-selfcheck).

Getting this backwards is its own bug: a fail-open security gate is no gate, and a
fail-closed discipline nudge can trap the agent. Match the policy to the cost of being
wrong.

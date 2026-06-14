---
name: pre-commit-gate
description: Use before every commit. Lint, typecheck, and tests must all be green, with zero warnings, before you commit. Never bypass the gate with --no-verify or by disabling hooks.
---

# Pre-commit gate: green before you commit

A commit that fails lint, type checking, or tests pollutes history and blocks
everyone who pulls it. Run the gate locally before committing — don't outsource it
to CI and don't push red.

## The gate

Before each commit, all of these must pass with **zero warnings**:

1. **Lint / format** — formatter clean, linter clean. Warnings count as failures;
   a "zero-warnings" policy is the only one that stays at zero.
2. **Typecheck** — the type checker reports no errors.
3. **Tests** — the relevant test suite is green.

Wire this into a `pre-commit` git-hook (see `git-hooks/`) so it runs automatically,
and into CI as a backstop. The local hook gives fast feedback; CI catches anyone
whose local hook is missing.

## Never bypass the gate

- Never `git commit --no-verify` / `-n`.
- Never comment out, delete, or weaken a hook to get a commit through.
- Never `--skip` a hook to "fix it later".

If the gate is failing, the fix is to fix the code (or the test), not to skip the
gate. A bypassed gate is how a red commit reaches main. (The `block-no-verify`
agent-hook can enforce this for AI agents that might be tempted to take the
shortcut.)

## AI code review as part of the gate

For non-trivial or architectural changes, run an automated multi-model review on
the diff before committing, and treat a second independent model as a peer review,
not a rubber stamp. See `gan-critic-loop` and the `mcp/review` slot.

---
name: shell-exit-codes
description: Use when piping a command into tail/head/grep/tee and then gating on its exit code — lint, typecheck, test, build, or any pass/fail check. A naive pipe reports the wrong command's status and masks failures as success.
---

# Capture the exit code through a pipe correctly

`cmd | tail -50; echo "rc=$?"` is a trap: `$?` holds the exit status of `tail`,
not `cmd`. `tail` almost always succeeds, so a failing `cmd` is reported as `rc=0`.
This is especially dangerous when gating lint / typecheck / test / build — a real
failure is silently swallowed and the pipeline "passes".

## Three safe ways

1. **Run the command alone**, read `$?` immediately after it, *before* any other
   command runs:
   ```bash
   cmd > out.log 2>&1
   rc=$?
   tail -50 out.log
   [ "$rc" -eq 0 ] || exit "$rc"
   ```

2. **`set -o pipefail`** before the pipe — the whole pipeline then fails if any
   stage fails:
   ```bash
   set -o pipefail
   cmd | tail -50          # non-zero if cmd failed, even though tail succeeded
   ```

3. **Read the pipe-status array** after the pipe:
   ```bash
   cmd | tail -50
   rc=${PIPESTATUS[0]}     # bash: status of the first stage; zsh: ${pipestatus[1]}
   ```

## Why it matters

A masked failure in a CI gate is worse than no gate at all: it gives false
confidence. If you ever pipe a gating command into a pager or filter, you must
use one of the three forms above — never trust a bare `$?` after a pipe.

# block-no-verify

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 10 (runs early)

Denies a shell command that would bypass the pre-commit gate:

- `git commit --no-verify` / `git commit -n`
- `git push --no-verify`
- inline hook-disabling env vars (`HUSKY=0`, `SKIP=...`, `LEFTHOOK=0`, ...)

## Why an agent-hook (not a git-hook)

A git hook *cannot* enforce this — `--no-verify` is precisely the flag that tells git to
skip the hook. The only place to stop the bypass is *before* the command runs, which is
what a `pre-bash` agent-hook does. This is the enforcement counterpart of the
`pre-commit-gate` skill.

## Fail-closed

`on_error: "closed"`. If the hook can't inspect the command (a malformed event, a crash),
it **blocks** rather than allows — a bypass slipping through a broken guard is the exact
failure this hook exists to prevent.

## Install

```bash
chmod +x block_no_verify.py
# edit the descriptor's "cmd" to this file's absolute path, then drop the descriptor
# into your harness's pre-bash hook directory.
```

## Test

```bash
echo '{"args":{"command":"git commit --no-verify -m x"}}' | ./block_no_verify.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block",...}  exit=10

echo '{"args":{"command":"git commit -m x"}}' | ./block_no_verify.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0
```

# block-no-verify

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 10 (runs early)

Denies a shell command that would bypass the pre-commit gate:

- `git commit --no-verify` / `git commit -n`
- `git push --no-verify`
- `git -c core.hooksPath=<...> commit/push` (a real `-c` config that repoints the hooks dir)
- inline hook-disabling env vars (`HUSKY=0`, `SKIP=...`, `LEFTHOOK=0`, ...)

## Parsed, not raw-matched

The verdict is made from the **parsed** command, never a raw substring of the whole string.
The hook tokenizes the command (the same `punctuation_chars=True` parser the #59 siblings use —
visual-proof-gate / skills-read-gate / require-review-before-commit), splits it into segments on
real separators (including fused `&&`/`;`/`|&`), peels inline env + wrapper prefixes
(`timeout`/`env`/`nice`/`stdbuf`/...), and only flags `--no-verify`/`-n` when it is a **real flag**
on a real `git commit`/`git push` segment.

So these are **allowed** (the old raw regex falsely blocked them):

- a commit MESSAGE that mentions `--no-verify`/`-n`/`core.hooksPath` (`-m "..."`, `-F file`,
  `-F -` heredoc) — the words are message text, not flags;
- a SIBLING command in a chain carrying `-n` (`grep -n x && git commit -m ok`);
- `git push -n` — for `push`, bare `-n` is `--dry-run`, **not** `--no-verify` (only the literal
  `git push --no-verify` long flag is a bypass; for `commit`, `-n` *is* `--no-verify`);
- committing a file whose path/content mentions no-verify.

And these are still **blocked** even when hidden (the old flat regex missed them):

- a `--no-verify` behind a wrapper (`timeout 60 git commit --no-verify`, `sudo git commit
  --no-verify`, `env FOO=bar git commit --no-verify`);
- a fused separator (`x;git commit --no-verify`);
- a `-c core.hooksPath=...` (separate or glued, case-insensitive) on a commit/push;
- a hook-disabling env on ANY command, inline or via `env` (`HUSKY=0 make`, `env HUSKY=0 git
  commit`).

### Heredoc bodies are data, not commands

A `git commit -F - <<'EOF' … EOF` heredoc BODY is stripped before parsing, so a body *line* that
looks like a command (`git commit --no-verify`, `HUSKY=0 make`) is treated as message data and does
**not** trip the gate — only the opener (`git commit -F -`) is inspected.

### Known limitations (deliberate precision trade)

A commit hidden inside one of these less-common shell constructs is **not** re-parsed and therefore
not gated — matching the `require-review-before-commit` sibling:

- a nested shell-string interpreter (`bash -c 'git commit --no-verify'`, `sh -c …`) or `xargs`;
- a command substitution (`$(git commit --no-verify)`, backticks);
- a `case` pattern body (`case $x in a) git commit --no-verify;; esac`);
- a wrapper *outside* a brace group (`time { git commit --no-verify; }`);
- a git-config **env injection** that repoints the hooks dir without `-c`
  (`GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null git commit`, or
  `GIT_CONFIG_GLOBAL=/tmp/evil git commit`) — the inline `-c core.hooksPath` form IS caught, but the
  multi-var env form is not.

This is a deliberate precision trade in MATCHING scope (not an error-policy choice — the hook still
fails *closed*, see below): under-matching these unusual forms is acceptable, while the common direct,
absolute-path, subshell `( … )`, brace-group `{ …; }`, control-keyword (`if/then`, `while/do`,
`for`, `until`), env-builtin (`export HUSKY=0`), and simple-wrapper (`timeout`/`env`/`nice`/`sudo`/…)
forms are fully covered.

## Why an agent-hook (not a git-hook)

A git hook *cannot* enforce this — `--no-verify` is precisely the flag that tells git to
skip the hook. The only place to stop the bypass is *before* the command runs, which is
what a `pre-bash` agent-hook does. This is the enforcement counterpart of the
`pre-commit-gate` skill.

## Fail-closed

`on_error: "closed"`. If the hook can't inspect the command (a malformed event, a crash),
it **blocks** rather than allows — a bypass slipping through a broken guard is the exact
failure this hook exists to prevent.

When the precise parse fails on the COMMAND (unbalanced quotes / exotic content), the decision
is by reason, and the BLOCK message is **actionable** (#113) so the failure reads as fixable, not
as a broken hook:

- **a plausible `git commit`/`push` with unbalanced quotes** (the common case: an apostrophe or
  backtick in a single-quoted `-m` message) → **blocked**, with a message that names the
  *unbalanced quotes* and points at the fix: pass the message from a file (`git commit -F <file>`)
  or a heredoc, which sidestep shell quoting. The decision can't be safely narrowed to allow — the
  gate can't rule out a `--no-verify` *outside* the quote, and a quote-recovery heuristic provably
  reopens that bypass — so it stays a BLOCK; only the wording improved.
- **an obfuscated `env -S`/`--split-string` form at a COMMAND HEAD** that could conceal a bypass →
  **blocked**, with a firm refusal (no "fix your quotes" softening — this is a deliberate-evasion
  shape). The `env -S` detection is anchored to a command head (string start / after a shell
  separator / behind an inline `VAR=val`), so a benign commit whose *message* merely mentions
  `env -S` and is unparseable for another reason (an apostrophe) gets the quoting hint above, not
  this refusal.
- **an unparseable command that is neither** (no `git`+`commit`/`push`, no obfuscation — e.g. a `tg`
  report with an unbalanced HTML body) → **allowed** (#40); blocking it was pure over-block.

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

# a message that merely MENTIONS the flag is allowed (parsed, not substring-matched):
echo '{"args":{"command":"git commit -m \"about --no-verify\""}}' | ./block_no_verify.py
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0
```

Unit + behavior tests live in [`tests/test_block_no_verify.py`](../../tests/test_block_no_verify.py):

```bash
uv run --with "pytest>=8,<9" python -m pytest tests/test_block_no_verify.py -q
```

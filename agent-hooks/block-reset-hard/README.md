# block-reset-hard

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 10 (runs early)

Denies a shell command that irreversibly wipes uncommitted/untracked work with no undo:

- `git reset --hard` (with or without a ref: `git reset --hard`, `git reset --hard HEAD~3`,
  `git reset --hard origin/main`, ...) — discards uncommitted **tracked** changes.
- `git clean` with a real `-f`/`--force` flag, alone or clustered with `-d`/`-x` in any
  short-flag order (`-fd`, `-df`, `-fdx`, `-xdf`, `-f -d`, `--force --force`, ...) —
  discards **untracked** files.

Both are the same failure class — a destructive rewrite of the working tree git cannot undo
afterward — which is why one hook and one escape hatch cover both.

Lets the safe, reversible alternatives through:

- `git checkout -- <file>` / `git restore <file>` (discard specific tracked files, scoped)
- `git reset` with no `--hard` (bare, `--mixed`, `--soft`) — never touches the working tree
- `git clean -n` / `git clean --dry-run` (preview only, no deletion)
- `git clean` with no force flag at all (git itself refuses to delete without `-f`)
- text that merely **mentions** "reset --hard" or "clean -fd" — a commit message, a code
  comment, a doc, a `grep` for the string — parsed via real argv, never substring-matched

## Why this incident needed a hook

A subagent working an unrelated PR ran `git reset --hard` mid-session in a checkout shared
with a different session and wiped that other session's uncommitted work (recovered, but it
exposed the gap). The incident was **accidental**, not a deliberate bypass attempt. This
hook's real value is turning an accidental destructive reset/clean into a **deliberate,
logged** one — it is **not** a hard wall an adversarial or confused agent can never get
through. The escape hatch below is intentionally self-service, same as every sibling hook in
this family (`block-no-verify`, `block-raw-pr-merge`). Don't oversell this as a true
user-consent gate.

## Why an agent-hook (not a git-hook)

Git has **no `pre-reset`/`pre-clean` hook point at all** — a git-hook literally cannot
intercept `reset`/`clean` before they run; the closest thing (`pre-commit`) fires at commit
time, long after a `reset --hard`/`clean -f` has already destroyed the working tree. The only
place to stop the side effect is **before the command runs**, which is what a `pre-bash`
agent-hook does — same reasoning as `block-no-verify` stopping `--no-verify` (the flag that
disables the very hook that would otherwise catch it).

## Parsed, not raw-matched

The verdict is made from the **parsed** command (same `punctuation_chars=True` shlex
tokenizer + segment-splitting the `#59` sibling hooks use), never a raw substring of the
whole string:

- the command is tokenized **line by line** — a newline is a command separator, same as `;`
  — so a two-line Bash call (`cd /repo` on line one, `git reset --hard` on line two) is still
  caught; a single flat shlex pass over the whole string would miss it entirely (the newline
  is just whitespace, so line one's `cd`/`echo` becomes argv[0] and line two is invisible);
- a `#` is only a comment at a **word boundary** — `foo#bar` is literal text to a real shell,
  not a comment start; shlex's default commenter gets this wrong (cuts at ANY unquoted `#`,
  even mid-word) and would silently truncate parsing, hiding a later chained command like
  `echo foo#bar && git clean -fd` (ported fix from `block-no-verify`/
  `require-review-before-commit`);
- the command is then split into shell segments on real separators (`&&`, `;`, `||`, `|`, ...);
- each segment's real argv is recovered by stripping leading shell-grouping/control-flow
  tokens (`(`, `{`, `!`, `if`, `then`, `do`, `while`, `until`, `for`, `case`, ...), inline
  `VAR=value` env assignments, and a table of common wrapper executables — `timeout`, `env`,
  `nice`, `ionice`, `nohup`, `setsid`, `stdbuf`, `time`, `unbuffer`, `command`, `sudo`, `doas`,
  `exec`, `taskset`, `chrt`, `setpriv`, `flock`, `runuser` — ported from
  `block-no-verify`/`require-ticket-before-commit`'s audited wrapper table, including each
  wrapper's own VALUE-taking flags (`sudo -u alice`, `nice -n 10`, `env -u FOO`) so the
  operand is correctly skipped instead of misread as the wrapped command;
- then git's own global options (`-C <dir>`, `-c key=val`, `--git-dir=...`, `--no-pager`,
  ...) are skipped so `git -C /some/path reset --hard` and `git --no-pager clean -fd` are
  still caught;
- only then is `argv[0]=="reset" && "--hard" in argv[1:]` or `argv[0]=="clean" && <a real
  force flag>` checked.

So all of these are caught, not just the bare form: `sudo -u alice git reset --hard`,
`nice -n 10 git clean -fd`, `time git reset --hard`, `command git reset --hard`,
`while git clean -fd; do ...; done`.

This is why a commit message, doc, or `grep` that merely contains the phrase "reset --hard"
or "clean -fd" as **text** is allowed — the words are data, not a parsed flag on a real git
invocation.

### `git clean` force-flag detection

Short-flag clusters are scanned character-by-character, stopping at `e` — `git clean` has
exactly one value-taking short option (`-e <pattern>`, glued form `-e<pattern>`), so the
letters after `e` in a cluster are that pattern's text, not further flags:

- `-fd`, `-df`, `-fdx`, `-xdf`, `-f -d`, `--force --force` → **force** (blocked)
- `-nf`, `-fn` → **force** (the `-n` doesn't cancel a real `-f` present anywhere in the
  clustering; conservative in the same direction as the rest of this guard)
- `-n -e"*.conf"` → **not force** (allowed — a dry-run with an exclude pattern)
- `-fe"*.o"` → **force** (the `f` comes before `e`, so it's a real flag, not part of the
  pattern)
- `-ef"*.o"` → **not force** (the `e` consumes the rest of the token as its pattern value —
  `f*.o"` — this is `-e` with a value, not `-e` plus `-f`)

## Escape hatch (controllable, not a hard wall)

One hatch, one name — it gates **both** `reset --hard` and `clean -f...` (mirrors
`block-raw-pr-merge`'s `ALLOW_RAW_PR_MERGE`):

```bash
# one-off, self-documenting in the command itself — the reliable form, works in every
# harness because it's read straight from the command text, no env persistence required:
git clean -fd   # no-reset-guard: aborted experiment, confirmed nothing else in this checkout

# session-wide override (reason REQUIRED, or it still blocks) — export it in the
# environment the HOOK PROCESS ITSELF inherits (e.g. before the agent/session starts, or
# in a harness that shares env across a run). A `VAR=val` PREFIX on the git command line
# does NOT work here: that prefix only sets the variable for that one command's own
# subprocess, and the pre-bash hook is invoked as a SEPARATE process by the host BEFORE
# the command ever runs — it never sees a not-yet-executed command's inline prefix.
export ALLOW_GIT_RESET_HARD=1
export ALLOW_GIT_RESET_HARD_REASON="recovering a known-bad worktree, verified nothing else uses it"
git reset --hard origin/main
```

A reasonless `ALLOW_GIT_RESET_HARD=1` is ignored and the command stays blocked — a silent
bypass of the bypass-guard is the exact failure this hook prevents. When in doubt, prefer
the inline `# no-reset-guard: ...` sentinel — it always works, regardless of how (or
whether) your harness persists environment variables across the hook boundary.

## Fail-closed

`on_error: "closed"`. If the hook can't inspect the command (a malformed event, a crash), it
**blocks** rather than allows — a destructive reset/clean slipping through a broken guard is
the exact failure this hook exists to stop.

When the command itself can't be parsed (unbalanced quotes) but the raw text plausibly
contains one of the two dangerous forms, the decision is **block** with a message that names
the unbalanced quotes, and — matching `block-raw-pr-merge`'s behavior exactly — the escape
hatch is **not** consulted on this path: an unparseable command that also looks like a bypass
attempt doesn't get a free pass just because it carries `# no-reset-guard: ...` text; fix the
quoting first. An unrelated command with an unrelated unbalanced quote (e.g. `grep won't
file`) is **allowed** — blocking it would be pure over-block.

## Known limitations

The escape hatch is **already** a deliberate, self-service bypass (by design — see above),
so hardening the parser against an agent that genuinely *wants* through is incoherent: it
would just use the hatch. The bar for what's fixed vs. documented here is: **would a
confused, non-evasive agent produce this exact command by accident?** Newline-separated
commands and mid-word `#` (both fixed above — see "Parsed, not raw-matched") clear that bar;
these don't:

- `git reset --har` (an unambiguous long-option prefix git itself accepts) is not detected —
  only the literal `--hard` spelling is matched. No one accidentally abbreviates a destructive
  flag they weren't trying to type in full.
- `git clean -e -f` (a bare `-e` immediately followed by a SEPARATE `-f`-shaped token) is
  conservatively treated as a real `-f` (**blocked**) by this hook — but real git parses that
  trailing token as `-e`'s exclude-pattern VALUE, confirmed empirically (`git clean -e -f`
  prints `clean.requireForce is true and -f not given: refusing to clean` — i.e. real git does
  **not** treat it as force). This is a **false block**, not a bypass — the safe-by-default
  direction for a destructive-action guard, and not worth getopt-style value tracking for such
  an exotic input shape.
- `git -c clean.requireForce=false clean -d` (or the same setting already sitting in someone's
  **ambient** gitconfig, not passed inline) makes a bare `clean -d` — no `-f` at all — actually
  delete, contradicting this hook's "no force flag = git refuses" assumption. Not detected:
  nobody sets this inline by accident, but if it's already in ambient config, a bare
  `clean -d` genuinely slips past this hook. Worth knowing, not worth building a
  git-config-value detector for.
- The inline `# no-reset-guard: <reason>` sentinel is matched against the **whole raw command
  string**, including inside quotes — `git commit -m "notes: no-reset-guard: x" && git reset
  --hard` would read as a valid override even though the text is commit-message data, not a
  real shell comment on the dangerous segment. Requires a crafted message to trigger; the
  hatch is self-service anyway, so scoping the sentinel to a genuine comment token isn't worth
  the added machinery here.
- A shell **alias** for `git` (`alias g=git`) is not resolved — universal to this whole hook
  family. Aliases only expand in an *interactive* shell by default; a harness running via
  `bash -c "<command>"` doesn't expand them anyway, so this is rarely reachable in practice.
- `env -S/--split-string '<command>'` (a re-tokenized inline command string), a nested
  shell-string interpreter (`bash -c 'git reset --hard'`), a command substitution
  (`$(git reset --hard)`), or a wrapper outside a brace group are not re-parsed — same
  documented gap as `block-no-verify`; all require deliberately crafted input, not an
  accidental shape.
- An unlisted/exotic wrapper (`unshare`, `nsenter`, `firejail`, ...) is not peeled — the
  wrapper table mirrors `block-no-verify`/`require-ticket-before-commit`'s audited set, not
  every process-isolation tool that exists.

## Install

```bash
chmod +x block_reset_hard.py
# edit the descriptor's "cmd" to this file's absolute path, then drop the descriptor
# into your harness's pre-bash hook directory. (rig apply does this for you.)
```

## Test

```bash
echo '{"args":{"command":"git reset --hard"}}' | ./block_reset_hard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block",...}  exit=10

echo '{"args":{"command":"git clean -fd"}}' | ./block_reset_hard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block",...}  exit=10

echo '{"args":{"command":"git checkout -- file.txt"}}' | ./block_reset_hard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

echo '{"args":{"command":"git clean -n"}}' | ./block_reset_hard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

echo '{"args":{"command":"git reset --hard  # no-reset-guard: recovering known-bad worktree"}}' \
  | ./block_reset_hard.py; echo "exit=$?"
# → decision":"allow" (escape hatch with a reason)  exit=0
```

Unit + behavior tests live in
[`tests/test_block_reset_hard.py`](../../tests/test_block_reset_hard.py):

```bash
uv run --with "pytest>=8,<9" python -m pytest tests/test_block_reset_hard.py -q
```

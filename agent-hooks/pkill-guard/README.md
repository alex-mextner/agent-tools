# pkill-guard

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 10 (runs early)

Denies a shell command that kills processes by **name/pattern match** — `pkill -f <name>`,
`killall <name>`, `kill $(pgrep <name>)`, `pgrep <name> | xargs kill` — when the pattern being
matched is a generic, widely-shared tool/process name (`node`, `codex`, `claude`, `review
diff`, `playwright`, ...). `pkill -f`/`killall`/`pgrep` match against **every process on the
machine** whose command line contains the pattern, not just the caller's own.

Ref: [2026-07-01 agent-ecosystem retrospective](../../../hyperide/docs/specs/2026-07-01-agent-ecosystem-retrospective.md),
gap **G-5**.

## Why this incident needed a hook

Two real incidents, both **accidental collateral damage**, not a deliberate bypass:

- 2026-06-26: a subagent's `pkill -f "review diff"` killed a **different session's** in-flight
  code review.
- 2026-06-27: a narrow-grep-based kill nearly killed another session's e2e matrix run.

`pkill`/`killall`/`pgrep` have no concept of "my own processes only" — a pattern that happens
to also appear in another concurrent session's command line kills that session's work too.
This hook turns "kill everything matching this word" into a deliberate, scoped, or
explicitly-approved action.

## What's blocked vs. allowed

A pattern is **dangerous** only when it is — or contains, as a whole word/phrase — a known
**shared** tool/process name (see `_SHARED_PROCESS_NAMES` in the script) **and** it carries no
session-scoping signal. This is a denylist of known-ambiguous names, not a blanket "block
every pattern kill":

- **Blocked**: `pkill -f "review diff"`, `killall node`, `kill $(pgrep -f codex)`,
  `pgrep -f playwright | xargs kill`, `ps aux | grep claude | awk '{print $2}' | xargs kill -9`
- **Allowed**:
  - `kill <pid> [<pid> ...]` (optionally with `-SIGNAL`/`-s SIGNAL`) — always PID-targeted,
    can't hit another session's process by name collision.
  - A pattern that also carries a session-scoping signal even if it names a shared tool —
    a path (`pkill -f "/Users/ultra/work/hyperide-worktrees/agent-x/.../vitest"`), the e2e
    harness's isolation prefix (`pkill -9 -f "hvsc-3-a1b2c3d4"`, the sanctioned recipe from
    `ext-test-projects`'s repo instructions), or any hex/uuid-looking or long-digit run.
  - A pattern not on the denylist at all — this hook fails **open** on unknown names; it gates
    known-ambiguous ones, not every `pkill` invocation on the machine.
  - `dev stop` (the project's own repo-scoped dev/e2e process stop command) never invokes
    pkill/killall/kill itself, so it is never touched by this hook.

## Parsed, not raw-matched

Detection is argv-based (shlex), same discipline as `block-reset-hard`/`block-raw-pr-merge`:
the command is tokenized (a newline is a command separator, same as `;`), split into
**pipeline groups** — separators `;`/`&&`/`||`/`&` start a new group, `|`/`|&` continue the
same group as a new stage — and each stage's real argv is recovered after stripping leading
shell-grouping tokens, inline `VAR=value` assignments, and a wrapper table (`sudo`, `timeout`,
`env`, `nice`, `time`, ...).

Three shapes are classified:

1. **Direct `pkill`/`killall`**: the pattern is the stage's own trailing positional argument.
2. **`kill` fed a command substitution**: `$(pgrep ...)` / `` `pgrep ...` `` spans are replaced
   with opaque placeholder tokens *before* shlex tokenizing (so the substitution's own internal
   whitespace doesn't fragment `kill`'s argv), then re-parsed as their own mini pipeline to find
   the wrapped `pgrep` pattern. One level of substitution nesting is resolved.
3. **A pipeline that resolves PIDs by pattern and kills them**: an earlier `pgrep`/`grep`/
   `egrep`/`fgrep` stage feeding a later `kill` or `xargs ... kill ...` stage in the same
   pipeline group — covers both `pgrep <pattern> | xargs kill` and the "narrow grep" shape
   `ps aux | grep <pattern> | ... | xargs kill`.

## No self-service bypass — Telegram hatch only

Deny-by-default. Request a one-time Telegram approval with a written justification:

```bash
RIG_HATCH_REQUEST_PKILL_GUARD="<reason>" pkill -f node
```

Routes through the shared `agenttools_hatch_escalation` helper (same mechanism as
`background-subagent-gate`/`block-raw-pr-merge`) — a blank/bare-flag value (`1`, `true`) is
rejected, no Telegram call is made, and the command is denied. Every attempt (approved or
denied) is auto-recorded in `overrides.log` (gap **G-8**, already shipped). Nothing set = the
block stands; use a PID-targeted `kill <pid>`, `dev stop`, or scope the pattern to something
session-unique instead of asking for an exception.

## Fail-closed

`on_error: "closed"`. A malformed event or a crash **blocks** rather than allows — a collateral
pattern-kill slipping through a broken guard is the exact failure this hook exists to stop.
Unbalanced quotes that can't be tokenized, when the raw text still plausibly names a denylisted
process via `pkill`/`killall`/`kill`/`pgrep`, are also treated as **unparseable → block**.

## Known limitations

- The denylist (`_SHARED_PROCESS_NAMES`) is necessarily incomplete. An unlisted shared name is
  not caught — this fails **open** toward not blocking legitimate, project-specific kills the
  hook has no reason to distrust, not toward silently missing a real collision (the two
  documented incidents are both on the denylist).
- Only **one level** of command-substitution nesting is resolved for `kill $(...)`; a
  substitution containing a further nested substitution is not recursed into.
- The pipeline scan classifies the pattern from the **earliest** `pgrep`/`grep`-family stage in
  a group that also has a kill-capable last stage; an exotic multi-grep pipeline with the real
  pattern in a later stage is not specially handled.
- A shell **alias** for `pkill`/`kill`/`killall` is not resolved — same documented gap as every
  sibling hook in this catalog (aliases don't expand under a harness's `bash -c` anyway).

## Install

```bash
chmod +x pkill_guard.py
# edit the descriptor's "cmd" to this file's absolute path, then drop the descriptor
# into your harness's pre-bash hook directory. (rig apply does this for you.)
```

## Test

```bash
echo '{"args":{"command":"pkill -f \"review diff\""}}' | ./pkill_guard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block",...}  exit=10

echo '{"args":{"command":"pkill -9 -f \"hvsc-3-a1b2c3d4\""}}' | ./pkill_guard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

echo '{"args":{"command":"kill 12345"}}' | ./pkill_guard.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0
```

Unit tests live in
[`tests/test_pkill_guard.py`](../../tests/test_pkill_guard.py):

```bash
uv run --with "pytest>=8,<9" python -m pytest tests/test_pkill_guard.py -q
```

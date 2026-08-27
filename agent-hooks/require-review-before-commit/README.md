# require-review-before-commit

**Point:** `pre-bash` · **Fail policy:** `open` · **Priority:** 20

On a `git commit`, checks that an AI code review ran recently for the current change. If
none did, it **blocks** with a reminder to review the uncommitted diff first.

## What it does NOT gate

- **Non-commit git ops** — `git stash`, `git worktree`, `git status`, `git add`. Only a real
  `git commit` segment is gated. The commit is detected from the *parsed argv*, not the raw
  string, so a `commit` token in a comment / message / pathspec / sibling command never trips
  it.
- **A commit WRAPPED in another program** — `sudo git commit`, `env VAR=1 git commit`, `time git
  commit`, `bash -c 'git commit …'`, a `(git commit …)` subshell or `{ git commit; }` group. The
  detector requires the
  segment's executable to BE git (or a path to it), so a wrapper is not recognized. This is a
  deliberate trade for precision (matching `commit` as a bare substring false-blocked `git help
  commit` / `git commit-graph`); the gate is process discipline (`on_error: open`), not a security
  boundary. The direct and absolute-path (`/usr/bin/git`) forms are fully covered.
- **Docs-only commits** — when every staged path is documentation (a doc extension
  `*.md`/`*.mdx`/`*.markdown`/`*.rst`/`*.adoc`/`*.rdoc`/`*.pod`, or a non-code file under a
  `docs/` directory), the commit is
  allowed without a review marker. **Source/script/config never counts as docs** — code even
  under `docs/` (`docs/conf.py`, `docs/build.py`, `docs/Makefile`) still requires review. A bare
  `.txt` requires review when it's *outside* `docs/` (e.g. a top-level `requirements.txt`); under
  `docs/` a `.txt` is treated as documentation (release notes, plans). This fast-path reads the
  staged *index*, so it does
  NOT fire for `git commit -a/-am`, `-p/--patch/--interactive`, `--pathspec-from-file`, an
  explicit pathspec, or a `--git-dir`/`GIT_DIR=…`-redirected commit — those can include content
  the cwd index doesn't list, so the gate stays.

## No self-service skip — external approval only

There is **no** `REVIEW_SKIP=1` inline-env bypass and **no** `[skip-review: <reason>]`
commit-message trailer any more. An agent could set either on its own commit, so those
"gates" were security theater — they are removed and the block is now **deny-by-default**.

For a genuine one-time exception, **ask the human** — or request a single approval by setting
`RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT="<written justification>"`. That routes one
Telegram approval request to Alex (deny-by-default): the env var must carry a real
justification — unset means the hook never contacts Telegram, and a blank or bare
`1`/`true`/`yes`/`on` is rejected without a Telegram call. A real justification runs the
trusted `tg-ctl ask`; **exit 0 allows**, and any nonzero exit / launch error / timeout denies
(the block then leads with `hatch escalation denied: <reason>`).

You can supply the justification either as an inline prefix on the gated command
(`RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT="…" git commit …`) or by exporting the var into
the harness environment. This is a **pre-bash** hook, so it parses the inline assignment out of
the command string the event carries — a pre-bash hook runs in its own process *before* the shell
evaluates the `VAR=x cmd` prefix, so the value never reaches its `os.environ`. An exported value
takes precedence over an inline one.

## How it knows a review ran

It looks for a **marker file** that gets touched on a successful review run, and checks the
marker is fresh (within a window, default 1h).

### PRIMARY: let review-cli touch it for you — no `touch` command, ever

**Before staging, check the index is what you expect** — `git status` (or `git diff
--cached --stat`) — especially in a shared/dirty worktree. `git add` only ADDS to the
index; it does not clear anything another process or agent already staged. If something
unrelated (or a secret like `.env`) is already staged, unstage it first (`git restore
--staged <path>`) so the recipe below reviews and commits only your intended change.

```bash
git add -- 'path/to/changed-file.py' && review diff --staged --task 'TICKET-CODE'
```

Stage the SPECIFIC files that make up your change, not `git add -A` — a broad add in a
dirty or shared worktree can sweep in unrelated edits (or untracked secrets) that then get
reviewed and committed as if they were intentional. Replace `path/to/changed-file.py` with
each file you actually changed (repeatable), and `TICKET-CODE` with your real ticket/task
id — do NOT wrap either in `<...>`: that's shell input-redirection syntax in bash/zsh, so
`git add <the files you changed>` is a parse error, not a placeholder an agent fills in.
**Keep BOTH placeholders quoted when you fill them in** — `git add -- 'real/path.py'`
(`--` first so a path starting with `-` isn't misread as a flag) and `--task
'TICKET-CODE'`. Neither `git add` nor review-cli's `--task` sanitizes its argument, so an
unquoted real value containing shell metacharacters (a filename like `fix;id.py`, a task id
copied from an untrusted integration as `OPS-7;id`) would execute as a second shell
command if pasted bare. **A single-quoted template is not a full escape**, either — a value
containing a literal `'` (e.g. a task id lifted verbatim from an external system as
`OPS-7'; id #`) closes the quote early and the rest still runs as shell. Only substitute a
value you chose yourself or one whose exact bytes you've checked — never splice raw
external/untrusted text into the command; if you must handle text you don't control,
construct the argument with a real shell-quoting function (e.g. Python's
`shlex.quote(value)`) instead of hand-wrapping it in `'...'`.

**A task code is REQUIRED by the current review-cli**, given EITHER as `--task 'CODE'`
(shown above, any task/ticket identifier — the Linear/GitHub issue this change belongs to)
OR by exporting `REVIEW_TASK_CODE='CODE'` first (quote it there too — the same unquoted
metacharacter risk applies to `export`) and omitting `--task`; omitting BOTH exits nonzero
immediately, runs no review, and touches no marker at all — `review diff --staged` with
neither form does NOT satisfy this gate.

review-cli's own `_touch_review_marker()` (`reviewlib/install.py`, wired into
`_stamp_if_staged_commit_review` in `reviewlib/modes/review.py`) writes `REVIEW_MARKER`
itself, in Python, the instant a `--staged` review passes. There is no shell `touch`
involved anywhere in this path, so there is nothing for a worktree-isolated session's
`$()`/`${...}`/bare-`$VAR` guard to trip on — the recipe is structurally immune, not just
carefully worded. **`--staged` is also required**: an unstaged `review diff` (default, no
flag) reviews the working tree but does NOT touch the marker (see
`reviewlib/modes/review.py`) — reviewing without `--staged` and then getting blocked here
anyway is the single most common reason an agent falls back to hand-rolling a `touch`,
which is where the guard trap below actually bites in practice. Stage first, then review
staged with a task code, and this gate is satisfied as a side effect of doing the review.

Configure via env:

- `REVIEW_MARKER` — marker file path (default `~/.cache/agent-tools/last-review`)
- `REVIEW_FRESH_WINDOW_S` — how recent the marker must be, in seconds (default `3600`)

### FALLBACK: manual touch (only when no reviewer actually ran)

Use this ONLY when a human, not an agent, already reviewed the change, or some other
process satisfied the review requirement outside review-cli — never as a shortcut to skip
running a real review; that is exactly the self-service bypass this gate was hardened
against (see "No self-service skip" below).

> **In a worktree-isolated Claude Code session, the touch must be a FLAT command — no
> `$(...)`, no `${...}`, no bare `$VAR`, not even split across two Bash calls.** Claude
> Code's own worktree-isolation Bash guard (separate from this gate, built into the CLI)
> refuses any Bash command whose parsed shape it can't statically resolve as "simple" — that
> includes `${VAR:-default}` parameter expansion AND a bare `$VAR` reference, not just
> `$(...)` command substitution — even when the command has zero `git` in it. The refusal
> text talks about "git operations", which is misleading here since neither `review` nor
> `touch` touches git. This means `touch "${REVIEW_MARKER:-default}"` gets refused, and so
> does `touch "$REVIEW_MARKER"` on its own — including as the SECOND of two separate Bash
> calls, since the expansion is still in that command's text. There is no way to reference
> the variable at all; you have to resolve its value first, then write the literal:
>
> 1. If `REVIEW_MARKER` isn't overridden, just `touch ~/.cache/agent-tools/last-review` — no
>    variable involved, done.
> 2. If it might be overridden, resolve it with a separate call that passes the var NAME as
>    a plain string argument rather than expanding it: `printenv REVIEW_MARKER`. `printenv`
>    never expands anything itself, so this call always passes the guard. Empty output means
>    unset — use the default.
> 3. `touch` whatever literal path `printenv` printed (or the default) as its own flat
>    command, e.g. `touch /custom/path/last-review` — still no `$()`/`${...}`/`$VAR`. If the
>    path has spaces or shell-special characters, single-quote the LITERAL value —
>    `touch '/custom/path with spaces/last-review'` — quoting a literal is not an expansion
>    (no `$` inside it), so it still passes the guard; it just makes the path parse as one
>    argument.
>
> Tracked upstream: anthropics/claude-code#88776 (duplicate of #84720, #86340, #87959) —
> Anthropic engineering acknowledged the false-positive on #86340.

## Why an agent-hook

The review has to happen *before* the commit, mid-session, while the diff is still
uncommitted — a git-hook fires too late to prompt "go review first" usefully, and can't
know whether a *session* review ran. This enforces the `ai-review-before-commit` /
`pre-commit-gate` skills.

## Fail-open, on purpose

`on_error: "open"`. This is process discipline, not a security boundary — a crash in the
check must never make committing impossible. (Contrast `block-no-verify` and
`block-secrets-write`, which are fail-closed.) It blocks only when it can *confirm* no
recent review ran.

## Test

```bash
chmod +x require_review.py
rm -f ~/.cache/agent-tools/last-review
echo '{"args":{"command":"git commit -m x"}}' | ./require_review.py; echo "exit=$?"
# → decision":"block ...  exit=10

mkdir -p ~/.cache/agent-tools && touch ~/.cache/agent-tools/last-review
echo '{"args":{"command":"git commit -m x"}}' | ./require_review.py; echo "exit=$?"
# → decision":"allow"  exit=0
```

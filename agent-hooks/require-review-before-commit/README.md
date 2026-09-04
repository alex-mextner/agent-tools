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
trusted `tg-ctl ask` with the question as a JSON ButtonRequest on stdin; **only a reply whose
decision is explicitly `allow` allows**, and an empty or unparseable reply, an explicit deny, any
nonzero exit, launch error, or timeout denies (the block then leads with
`hatch escalation denied: <reason>`).

You can supply the justification either as an inline prefix on the gated command
(`RIG_HATCH_REQUEST_REQUIRE_REVIEW_BEFORE_COMMIT="…" git commit …`) or by exporting the var into
the harness environment. This is a **pre-bash** hook, so it parses the inline assignment out of
the command string the event carries — a pre-bash hook runs in its own process *before* the shell
evaluates the `VAR=x cmd` prefix, so the value never reaches its `os.environ`. An exported value
takes precedence over an inline one.

## How it knows a review ran

It looks for a **marker file** that your review tool writes on a successful run, and
checks the marker is fresh (within a window, default 1h).

**The review tool writes the marker; you never `touch` it.** review-cli writes it from
exactly one shape of run — a COMPLETED `review diff --staged --task <CODE>` whose diff
came from the real index AND was small enough to reach every reviewer in full:

```bash
git add <exactly the files you are committing>   # NOT `git add -A`: see the caveats below
review diff --staged --task <CODE> -C <repo>     # passes -> writes the marker -> commit allowed
```

`--task <CODE>` is not decoration: `review diff` REQUIRES it (or an exported
`REVIEW_TASK_CODE`) and exits 2 without dispatching anything, which writes no marker and
leaves the commit blocked with a review that never ran. It is what files the run in
review-cli's iteration history; it has no effect on this gate beyond that.

"Completed" is the precise word, and the distinction matters: review-cli writes the marker
when every seat produced a usable verdict and the board did not degrade — REPORTED
FINDINGS DO NOT WITHHOLD IT. A review that comes back with a P1 still satisfies this gate.
That is deliberate, and it is why this hook is process discipline rather than a quality
bar: it enforces that a review HAPPENED, and reading the findings is on you. What does
withhold the marker is a review that could not be trusted to have covered the change — a
failed or degraded board, an unstaged or piped diff, a diff too big to reach the seats.

Stage exactly what you intend to commit, not `-A`. Two reasons: a docs-only commit that
sweeps in other staged files forfeits the docs-only fast path described above, and this
check is **mtime-based, not content-based** — it confirms that *a* review ran recently,
not that it reviewed the diff you are committing. So
`git add X; review diff --staged --task <CODE>; git commit -a` passes the gate with `Y`
never reviewed.

Staging exactly the commit's contents narrows that gap; it does not close it. The
invariant this gate actually needs is **do not mutate the index between the review and
the commit** — stage `Y` while the review of `X` is still running, and the marker written
at the end is fresh enough to wave `Y` through. A single agent working in sequence
satisfies that invariant for free; two agents sharing one checkout do not. Closing it for
real needs a diff-scoped stamp rather than an mtime, which is what the review-stamp in
the global git hook does and what agent-tools#507 tracks for this hook.

An unstaged `review diff`, a diff piped in on stdin, a diff truncated for dispatch, or a
failed review deliberately does NOT write the marker. From review-cli#350 onward it says
so on stderr instead of leaving it a mystery — including the case where the write itself
failed; older builds skip all of those silently, which is what this pairing fixes. Read
that line: each reason has its own fix, and only some of them are "re-run it `--staged`".
An oversized diff in particular is NOT fixed by re-running — it truncates again — so the
answer there is to split the change into smaller staged commits (or raise
`$REVIEW_DIFF_MAX_BYTES`) and review each part.

Hand-`touch`ing the marker is not a supported workaround: it certifies a review that
never happened, and for a headless agent it does not even work. An earlier version of
this README recommended exactly that `touch`, and two detached agents died obeying it.

Why the naive `touch` fails is worth being precise about, because "the agent cannot write
there" and "review-cli cannot write there" would otherwise contradict each other. The
restriction is on the AGENT's own actions, not on the filesystem: an agent runner such as
opencode screens the commands and file writes the agent itself issues, and its
`external_directory` policy rejects a write to `~/.cache/agent-tools/` outside the project
it was given. It does not follow a permitted command into the processes that command
spawns — `review …` is on the allow-list, so review-cli runs and writes the marker from
inside its own process without ever being screened. Same file, same permissions, different
actor.

That explains why the one-liner an agent reaches for first dies; it is NOT a guarantee
that self-certification is impossible, and this hook is not the place to look for one. A
determined caller can point `REVIEW_MARKER` at a path inside the project, or have a
committed script do the write — both are permitted writes, and neither is screened. This
hook is `on_error: open` process discipline, not a security boundary (see below): it makes
the honest path the easy one and the dishonest path an explicit, visible choice.

### Wiring a review tool that is not review-cli

This hook only stats a path, so any tool can satisfy it. The contract it actually
enforces is narrow — "a marker file whose mtime is within the freshness window" — and the
rest is on the tool: write that marker ONLY at the end of a review that actually passed,
and only for the staged diff. review-cli does exactly that. For any other tool, put the
marker write in the TOOL or in a wrapper script it runs on success — not in the agent's
own command line:

```bash
# a wrapper script on PATH, invoked by the agent as one command
your-review-tool --staged "$@" || exit $?
marker="${REVIEW_MARKER:-$HOME/.cache/agent-tools/last-review}"
mkdir -p "$(dirname "$marker")"   # the default dir does not exist on a fresh machine
: > "$marker"
```

The distinction is not cosmetic: a marker write inside a passing run is the tool
certifying its own work, while a `&& touch` the agent types is the agent certifying
itself — the shape this hook exists to make visible, and the one a headless permission
policy happens to stop outright.

Configure via env:

- `REVIEW_MARKER` — marker file path (default `~/.cache/agent-tools/last-review`).
  Exported-but-empty counts as unset, on both sides, so a blank value can't quietly point
  this gate at the current directory while the review tool writes its default path. The
  target must be a REGULAR FILE: a directory there is rejected rather than accepted, since
  a directory's mtime is refreshed by any unrelated file created in it and would keep the
  gate satisfied indefinitely.
- `REVIEW_FRESH_WINDOW_S` — how recent the marker must be, in seconds (default `3600`)

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

(The `touch` in the manual test recipe at the bottom of this file is the one exception:
it is a hook-level unit test of the freshness check, not a way to get a commit through.)

```bash
chmod +x require_review.py
rm -f ~/.cache/agent-tools/last-review
echo '{"args":{"command":"git commit -m x"}}' | ./require_review.py; echo "exit=$?"
# → decision":"block ...  exit=10

mkdir -p ~/.cache/agent-tools && touch ~/.cache/agent-tools/last-review
echo '{"args":{"command":"git commit -m x"}}' | ./require_review.py; echo "exit=$?"
# → decision":"allow"  exit=0
```

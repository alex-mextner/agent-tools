# orchestrator-stays-thin

**Points:** `pre-write` + `pre-bash` · **Fail policy:** `open` · **Priority:** 45

The orchestrator plans, dispatches, and verifies — it does **not** implement inline. When the
**main thread** is about to do implementation-shaped work itself, this gate nudges it to
delegate to a subagent or a Workflow. Enforces `delegate-work-to-subagents`.

One script binds two points via two descriptors; it branches on `event["point"]`:

- **`pre-write`** — a **code** Edit/Write (non-docs) by the main thread → warn-then-block.
  Docs are exempt: a path matching `\.(md|mdx|txt|rst)$` or under `docs/` is always allowed.
- **`pre-bash`** — a clearly **multi-step / implementation-shaped** Bash by the main thread →
  warn-then-block. Implementation-shaped = chained `> 2` steps (`&&`/`;`/`||`/`|`/newline), OR a
  heredoc, OR an obvious build/edit (`sed -i`, `tee`, `npm`/`cargo`/`make` build, **`git commit`/
  `git push`**, **test runs** `pytest`/`go test`/`npm`/`bun`/`cargo test`). A bare `>`/`>>`
  redirect is **not** implementation on its own (`python foo.py > out.log` is allowed).
  Read-only inspection **and sanctioned orchestration** are **never** blocked — including a chain
  of **any length** where every segment's head is inspection (`git status`/`log`/`diff`/`show`/
  `branch`, `ls`, `cat`, `grep`, `find`, `head`, `tail`, `wc`, …) **or** orchestration
  (`tg`, `review`, `git worktree list`), across `|`, `&&`, `;`, `||` or newline
  (`git status && ls`, `tg 'a' && tg 'b'`, `review diff && tg done`). But a chain that merely
  *starts* allowed (`git status && sed -i ...`, `tg done && git commit`), or that mixes in any
  build/edit/heredoc segment, is judged on its **full** content — not waved through on its prefix.
  Judgement is per-segment-**head**: a build token used only as an argument/needle of an allowed
  command (`cat tee.log`, `grep cargo notes`, `git log | rg gh`) stays allowed; only a
  build/edit/commit/`gh` *at a segment head* counts.

  **ALL `gh` is delegated (Alex tg#7103).** `gh ship`, `gh pr checks`/`view`, `gh run`, `gh api`
  — every gh subcommand — is implementation the orchestrator hands to a subagent, not inline work.
  This **reverts** the earlier `gh ship`/read-only-`gh` carve-out (agent-tools#159/#162): shipping
  a gated PR *and* CI/PR verification are a subagent's job. `gh` is not in the allow-list; it is an
  impl-signal, so an inline `gh ship 605` warn-then-blocks exactly like `git commit`. A dispatched
  subagent (`agent_id` present) is exempt and runs gh/ship freely — the gate governs the
  orchestrator only.

  **UPDATE (Alex tg#9977, agent-tools#159): one narrow exception restored — `gh ship <PR#>`.**
  The orchestrator may run the gated merge itself inline again, matched only on the literal shape
  `gh ship <bare-PR-number>` (head `gh`, next token exactly `ship`, next token all-ASCII-digits;
  everything AFTER that — not just flags/redirects but any further bare token too — unrestricted).
  `gh ship` with no PR number, `gh ship abc`, and every
  OTHER gh subcommand (`gh pr merge`, `gh run`, `gh pr checks`/`view`, `gh api`, …) still delegate
  — see "`gh ship <PR#>` — restored narrow carve-out" below for the full matrix.

  **UPDATE (Alex, direct Telegram authorization, agent-tools#159 thread): the `tg` carve-out is
  now formalized as an explicit predicate, replacing the old plain regex.** `tg` (send text,
  `--file`, `--photo`, `--format html`, `--tag`, `--reply-to`, `voice setup`, and every other
  tg-cli subcommand) has been sanctioned for the orchestrator since agent-tools#164 — scope
  unchanged, and NOT narrowed like `gh ship` (Alex: *"все команды tg-cli"* / "ALL tg-cli commands",
  explicitly broader than the single-shape gh-ship exception). This revision REPLACES `ORCH_ALLOW`'s
  old plain `tg\b` regex with `_is_tg_command`, a shlex-based predicate consulted by
  `_seg_is_allowed` the same way `_is_gh_ship_command` is — allow-side only, since `tg` (unlike
  `gh`) was never an impl-signal head, so there is nothing to exempt on that side — bare `tg` +
  self-contained env-prefix skip only, deliberately NO
  path-qualification (two review rounds concluded that adds unsafe or illusory scope, not real
  narrowing) — and, as a consequence, fixes agent-tools#370 for the `tg` case (a hyphen-suffixed
  lookalike like `tg-foo` no longer matches). See "`tg` —
  formalized carve-out" below.

## Per-repo opt-out (Alex tg#5743)

Default **ON** (opt-OUT — this gate has always been always-on, so an un-enrolled repo keeps
firing, no regression). A repo that legitimately does inline work on main (e.g. `3d-cli`)
exempts itself:

- `agent_hooks.orchestrator_only: false` in the repo's committed `rig.yaml` (rig-provisioned), or
- `RIG_ORCHESTRATOR_ONLY=0` (session/CI override).

This mirrors the opt-IN per-repo knob of the sibling `worktree-only-writes` guard.

**`gh` is delegated, not carved out (Alex tg#7103 — reverts #159/#162).** An earlier design gave
`gh ship` (and read-only `gh` reads) an *orchestrator* carve-out so the main thread could ship a
gated PR and verify CI inline. The CTO reversed that: the orchestrator delegates **all** `gh` to a
subagent — shipping *and* CI/PR verification included. So `gh ship`, `gh pr checks`/`view`,
`gh run`, `gh api` (GET or mutation) are each an impl-signal that warn-then-blocks for the
orchestrator, single or chained, with or without `cd`/read-only plumbing (`gh ship 605 2>&1 | tail
-30` blocks; a subagent runs it). Per-segment-head discipline is unchanged: `gh` as a
substring/needle (`grep 'gh ship' log`, `git log | rg gh`) is **not** a gh command and exempts
nothing. The **dispatched subagent** (`agent_id` present) is the one meant to verify and is
exempt for everything.

**UPDATE (Alex tg#9977, agent-tools#159):** `gh ship <PR#>` — the gated merge itself, matched
narrowly on a bare PR-number argument — was restored as a sanctioned orchestrator exception. See
"`gh ship <PR#>` — restored narrow carve-out" below. Every OTHER gh shape (including `gh ship`
with no PR number) is still exactly as described above: an impl-signal that warn-then-blocks.

**Report / verify carve-out — `tg` + read-only inspection (coordinator):** reporting to the user
and *read-only* verification stay at orchestrator altitude and must **not** require a subagent. A
line whose every segment head is `tg` (bare or env-prefixed, via `_is_tg_command` — see "`tg` —
formalized carve-out" below), `review`, `git worktree list`,
read-only inspection (incl. system-info `df`/`du`/`lsblk`/`free`/`ps`/… and filters
`jq`/`sort`/`cut`/…), or `cd` is never warned or blocked, of **any** length — there is no
minimum-count or specific-source requirement, it's a per-segment check
(`all(_seg_is_allowed(s) for s in segments)`), so a chain can be all-`tg`, all-read-only, or any
mix: `tg --format html '…' | tail -3 | grep merged`, `cat status.json | jq .title | head`,
`tg done; git status; git log`, `tg --file report.pdf 'caption'`.
Same per-segment discipline — a build/edit, heredoc, substitution, bare-`&`, mutating companion, or
any **`gh`** head forfeits it (`tg done && sed -i …`, `gh pr view && git push`, and now a bare
`gh pr view` are all implementation). **`curl` and `ssh` are deliberately NOT sanctioned** —
`curl -X POST`/`-d` mutates and `ssh host '…'` runs any remote command, so neither can be reliably
classified read-only; use the escape hatch or a subagent for those.

**Subagent-exempt:** a dispatched subagent (`agent_id` present) does the actual work, so it is
always allowed. This gate governs the orchestrator only. Because the hook uses `agent_id` to
**relax**, it reads **only** the sanitized `args.agent_id` — never a top-level `event.agent_id`
fallback, and never model-controlled `tool_input`. `cc_hook_bridge` forwards `args.agent_id` only
from CC's authoritative top-level event and drops any `tool_input`-forged copy (T2 precedence), and
never writes a top-level `agent_id`; a non-CC carrier wiring this hook must replicate that filtering
or a forged `agent_id` self-exempts the orchestrator (see `background-subagent-gate/README.md` for
the full contract). This matches the sibling `skills-read-gate`'s narrowed read (agent-tools#115).

## `gh ship <PR#>` — restored narrow carve-out (agent-tools#159, Alex tg#9977)

The orchestrator may run `gh ship <PR#>` inline — the ONE gh mutation that is sanctioned, same as
a dispatched subagent. Alex: *"the orchestrator CAN gh ship like agents can, but BY DEFAULT agents
do it"* — the default is still delegation; this is the exception, not a reopening of "all gh".

**Matched narrowly, on argv shape alone** (`_is_gh_ship_command`): the segment's command head must
be `gh` (basename/env-prefix normalized, same as `_is_gh_command`), the very next token must be
the literal `ship`, and the token after that must be a bare PR number (ASCII digits only,
non-empty). Anything AFTER the PR number is genuinely UNRESTRICTED — not just flags/redirects
(`--screenshot <path> <desc>`, `--repo <owner/repo>`, a trailing `2>&1` token) but ANY further bare
token too, including a second bare number (`gh ship 605 606`); this predicate recognizes the ship
*shape* only and does not vet ship's own arguments (`--skip-ci` has its own separate,
independently-gated live Telegram hatch inside `ci/ship/ship.sh` — this gate is not the place to
duplicate that; a second PR number is refused downstream by `ship.sh`'s own arg parser — "the lone
bare arg" — a second, independent gate this hook does not need to replicate).

**What matches (never warns, never blocks — same treatment as `tg`/`review`):**
- `gh ship 605`
- `gh ship 605 --screenshot out.png "desc"`
- `gh ship 605 --repo alex-mextner/agent-tools`
- `gh ship 605 2>&1 | tail -30 | grep -i merged` (a read-only tail companion)
- `cd /repo && gh ship 605` (the `cd` companion)
- `GH_PAGER=cat gh ship 605` (env-prefixed)
- `/usr/bin/gh ship 605` (path-qualified)

**What does NOT match — falls through to the general `gh` deny, still warn-then-blocks:**
- `gh ship` (no PR number)
- `gh ship abc` / `gh ship --help` (non-numeric argument)
- `gh ship $PR` / `gh ship "$PR_NUMBER"` — the hook sees the LITERAL pre-expansion text, so a
  genuinely numeric PR passed through a shell variable still misses (no shell runs before this
  hook inspects the string). Pass the literal digits: `gh ship 605`, not a variable.
- `gh ship --repo o/r 605` (a flag BEFORE the PR number — the number must be the token
  immediately after `ship`; `gh ship 605 --repo o/r`, number first, DOES match)
- `gh pr merge 605`, `gh run list`, `gh pr checks 605`, `gh api …` — every OTHER gh subcommand
- `gh ship 605 'oops` (an unbalanced-quote segment shlex cannot parse — conservative fallback)

**Does not launder a mutation elsewhere on the line** (agent-tools#159's original hardening,
unchanged): a `gh ship 605` tacked onto an implementation chain does not exempt the rest of it — a
build/edit (`sed -i … && gh ship 605`), a companion mutation (`gh ship 605 && git push`), a
mutation smuggled in a substitution (`gh ship 605 $(git push origin main)`), a sibling gh mutation
(`gh ship 605 | gh pr merge 606`), behind a bare `&` (`gh ship 605 & git push`), or a heredoc
anywhere still warn-then-blocks on the line's full content — the ship exemption is per-segment,
not per-line. What actually enforces this is a real chain operator (a genuine mutation becomes its
OWN segment, judged independently) and the substitution-liveness scanner (a LIVE `$()`/backtick/
`<()`/`>()` executes and is caught regardless). A sanctioned segment's own argument TEXT merely
*looking* like a mutation (`gh ship 605 tee`) does NOT block — that matches the SAME precedent
`tg`/`review` already have (`tg 'saw $(git push) in logs'` is allowed too); `tee` there is inert
text, never an executed command.

**Scope note:** ship's own flags are unvetted, so the carve-out also sanctions
`gh ship 605 --repo some/other-repo` — an inline merge into a DIFFERENT repository than the
orchestrator's cwd. Accepted within this gate's stated threat model (a cooperative orchestrator,
not an adversarial one — see "Fail-open, on purpose" below), not an oversight.

**Chaining note:** because the exemption is per-segment, `gh ship 605 && gh ship 606` (or any
number of chained `gh ship <PR#>`s) is ALSO sanctioned — each merge is individually gated by its
own full `ci/ship/ship.sh` pipeline (green CI, review quorum, screenshot, etc.), so chaining does
not weaken any ONE merge's own safety checks; it just lets the orchestrator run several
already-sanctioned merges on one line instead of several separate tool calls.

**Plumbing-sensitivity caveat:** the "never warns, never blocks" pipe forms above assume the ship
segment's own TEXT doesn't independently trip the `FIND_MUTATION` veto (`-delete`/`-exec`/…
appearing anywhere in the segment, e.g. inside a `--note` value). An UNCHAINED line with such text
still falls through to allowed (the veto only costs the fast path, and the resulting single-segment
line has nothing else to trip); but adding ANY companion — even a fully read-only `| tail -3 |
head -1` — pushes the chain to 3+ segments, where the `>= 3` chain-length fallback then applies,
flipping the SAME ship line to warn-then-block. Same pre-existing behavior `tg`/`review` already
have; more visible here only because this section advertises specific piped forms.

## `tg` — formalized carve-out (tg-carveout-159, Alex direct Telegram authorization)

`tg` (the tg-cli Telegram reporting tool) has been sanctioned for the orchestrator to run inline
since agent-tools#164 — every tg-cli subcommand, every flag. That stays **unchanged in scope**.
Alex, asked directly whether the orchestrator should get the same `gh ship` treatment for `tg`,
was explicit that `tg` should be **broader**, not narrower: *"оркестратор ещё и tg:* должен уметь,
т.е. все команды tg-cli"* — "the orchestrator should ALSO be able to run tg:*, i.e. ALL tg-cli
commands". Unlike `gh ship <PR#>` (one gh mutation carved out of an otherwise-delegated command),
`tg` has no subcommand this gate withholds — send text, `--file`, `--photo`, `--format html`,
`--tag`, `--reply-to`, `voice setup`, and everything else tg-cli exposes are all sanctioned, and
always have been.

**What changed: the old `ORCH_ALLOW` plain `tg\b` regex was REPLACED by `_is_tg_command`, a
shlex-based predicate — not kept alongside it.** An earlier draft of this PR added the predicate as
a pure addition next to the regex, reasoning it was lower-risk; review (Fable, round 3) correctly
identified that as the worst of both worlds — since `ORCH_ALLOW` was checked FIRST in
`_seg_is_allowed`'s OR-chain and matched a strict superset of what the new predicate could ever
grant, the addition was unreachable dead code that shipped zero actual behavior while adding a
second, silently-disagreeing definition of "is this segment `tg`". The predicate now REPLACES the
regex outright and is the SOLE authority — consulted by `_seg_is_allowed` the same way
`_is_gh_ship_command` is, but allow-side only: unlike `gh`, `tg` was never an impl-signal head, so
there is nothing to exempt in `_seg_is_impl_signal`.

**This also fixes agent-tools#370 for the `tg` case.** `\b` is a WORD-boundary, which also fires
before a hyphen, so the old regex incorrectly matched `tg-foo` — a DIFFERENT command — as
sanctioned orchestration. `_is_tg_command` is basename-exact (`toks[i] == "tg"`), so `tg-foo` is
now correctly rejected end-to-end: allowed alone or in a 2-segment chain (the same generic
single/short-chain gray zone any unclassified command gets under the "chained >2 steps" doctrine —
`tg-foo bar | tail` is still allowed, `tg-foo bar | tail -3 | head -1` warn-then-blocks), exactly
like any other non-orchestration command. `review`'s matching `\b` quirk (`review-foo`) is
**unchanged** — the surviving half of #370, out of scope for this PR.

**Deliberately NO path-qualification, despite `_is_gh_command` accepting any path — resolved by
two review rounds, not shipped as a guess.** Round 1 (Fable) found that mirroring
`_is_gh_command`'s blanket basename normalization over-grants in the UNSAFE direction here:
`_is_gh_command` is a DENY-direction predicate (over-matching there only routes more things to a
subagent, the safe failure mode), but a match in `_is_tg_command` means "never even warn" — a
RELATIVE path (`./tg`, `scripts/tg`) is trivial to place anywhere near the working directory. A
first fix attempt restricted the grant to ABSOLUTE paths only, reasoning that implies "a real,
already-installed binary at a fixed system location." Round 2 (Opus + Fable) disproved that
reasoning empirically: `/tmp/tg` or any other writable absolute path passes just as easily as a
relative one — the restriction narrowed nothing real. The remaining option, a small allowlist of
known bin directories (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`), was checked against
this machine's ACTUAL `tg` install and rejected: `which tg` resolves to `~/.files/bin/tg`, a
dotfiles-managed location not on any short "standard" list — proving a hardcoded allowlist would be
both unsafe-in-theory and unreliable-in-practice. Given this gate's own stated threat model
(discipline, not a security boundary — see "Fail-open, on purpose" below) and that the practical
need (Alex's ask) is already fully met by subcommand breadth alone, path-qualification was dropped
entirely.

**What matches (never warns, never blocks — same treatment `gh ship <PR#>` gets):**
- `tg 'shipped'`, `tg --format html '<b>done</b>'`, `tg --file report.pdf 'caption'`
- `tg --photo screenshot.png 'caption'`, `tg --tag report 'x'`, `tg --reply-to 12345 'answer'`
- `tg voice setup`, `tg help format`
- `TG_BOT_TOKEN=x tg 'shipped'` (env-prefixed)
- `gtimeout 30 tg 'shipped'` (wrapper-stripped, same as any other command)
- `tg 'a' | tail -3 | head -1`, `tg 'a' && review diff && git worktree list` (companions, any length)

**What does NOT match:** any path-qualified head — `/opt/homebrew/bin/tg`, `./tg`, `scripts/tg`,
`../tg` — relative OR absolute, falls through to the general (unclassified-command) treatment
instead (allowed alone or in a 2-segment chain, warn-then-blocks once chained into 3+ segments —
the same "chained >2 steps" doctrine any unclassified command gets). An unparseable
(unbalanced-quote) segment also does NOT match — this predicate denies-by-default on uncertain
input, matching `_is_gh_ship_command`'s own fallback direction (the old regex's "grant anyway"
fallback was justified only by parity with itself; once it became the sole authority that parity
argument no longer applied).

**Does not launder a mutation elsewhere on the line** — identical laundering resistance to
`gh ship`: a `tg` call tacked onto an implementation chain does not exempt the rest of it — a
build/edit (`sed -i … && tg 'done'`), a companion mutation (`tg 'done' && git push`), a mutation
smuggled in a substitution (`tg done $(sed -i 's/a/b/' f)`), behind a bare `&` (`tg 'done' & git
push`), a sibling `gh` mutation, a `(...)` subshell group, or a heredoc anywhere still
warn-then-blocks on the line's full content. Same mechanism as `gh ship`: chain-splitting makes a
real mutation its OWN segment, and the substitution-liveness scanner catches a LIVE `$()`/backtick/
`<()`/`>()` regardless of the outer segment's head. A `tg` argument's own TEXT merely *looking*
like a mutation (`tg 'saw $(git push) in logs'`, single-quoted — literal, never executes) does NOT
block — the pre-existing precedent, unchanged.

**Known, pre-existing gap, not introduced here:** a leading `VAR=val` env-prefix is accepted, and
an env prefix that affects command resolution (`PATH=`, `LD_PRELOAD=`) is not screened. This is
shared uniformly by every predicate in this file — `gh ship`, `_is_gh_command` — via
`_strip_wrappers`'s env-assignment stripping upstream of every one. Fixing it would mean changing
that shared helper, outside this predicate's scope.

**Subagent-exempt, same as everything else in this gate:** a dispatched subagent (`agent_id`
present) runs `tg` freely regardless of this predicate — it was never gated for a subagent.

## Tiering — WARN then BLOCK

The **first** offense in the TTL window **WARNs** (allow + message); a **repeat** in the
window **BLOCKs**. The tier is a marker file keyed by a hash of `(cwd, point)`, so `pre-write`
and `pre-bash` tier **independently** — a write WARN does not prime a bash BLOCK (or vice versa):

- `ORCH_THIN_MARKER_DIR` — marker dir (default `~/.cache/agent-tools/orchestrator-thin`)
- `ORCH_THIN_TTL_S` — warn-suppression window in seconds (default `900`)

This delivers the doctrine's "WARN then BLOCK" rather than a hard wall on the first inline edit.

> The env-configured marker dir is read at import time; CC re-invokes the script per call, so a
> per-session env change is always picked up on the next call — this is fine, not a footgun.

## No self-service bypass — external Telegram approval only

There is **no** env-var or inline escape hatch any more. The old `ALLOW_ORCHESTRATOR_WORK=1` +
`ALLOW_ORCHESTRATOR_WORK_REASON` env and the `# orchestrator-ok:` inline sentinel let the very
orchestrator this gate constrains grant itself an exception — security theater, not a permission
gate. Both were removed. (This is distinct from the per-repo **enable** knob
`RIG_ORCHESTRATOR_ONLY` / `agent_hooks.orchestrator_only`, which a repo owner — not the
constrained agent — sets to opt a repo out entirely; that stays.)

The gate still **WARNs first**; only a would-be **BLOCK** (a repeat offense within the TTL
window) is **deny-by-default**. For a genuine exception, ASK the human, or request a one-time
Telegram approval with a written justification:

```bash
# pre-bash point: the inline VAR=… prefix is parsed out of the command string, so it works
# even when the gated command is not first (`cd repo && RIG_HATCH_REQUEST_…="why" cmd`).
RIG_HATCH_REQUEST_ORCHESTRATOR_STAYS_THIN="trivial config tweak, no subagent worth it" \
  sed -i 's/a/b/' file
```

The inline prefix form works **only at the pre-bash point** (the hook parses the leading
assignment out of the command string; a pre-bash hook runs before the shell evaluates the
`VAR=x cmd` prefix, so the value never reaches its `os.environ`). At the **pre-write** point
(a code write via Edit/Write) there is no shell command to carry the prefix — the variable must
be **exported** into the harness environment so the hook reads it from `os.environ`. An exported
value takes precedence over an inline one at either point.

If the env var is unset, no Telegram call is made and the block simply stands. If it is present
but blank, whitespace-only, or a bare flag value (`1`/`true`/`yes`/`on`), the hook does not
contact Telegram and denies — a bare `1` is not a justification. A real justification runs
`tg-ctl ask` through a trusted absolute path (never ambient `PATH`); exit 0 allows, and any
nonzero exit, launch error, or timeout denies. An agent can *request*, not self-grant.

## Fail-open, on purpose

`on_error: "open"`. Delegation discipline, not a security boundary — a crash must never wedge
the main thread's ability to act.

## Test

The hook's exit code is the canonical signal (`0` allow · `10` block). Capture it on its OWN
line right after the pipe so what's printed is the HOOK's exit, not `echo`'s:

```bash
chmod +x orchestrator_stays_thin.py
# first offense → WARN (allow + message); repeat in the window → BLOCK
echo '{"point":"pre-write","cwd":"/r","args":{"file_path":"/r/src/a.ts"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # → exit=0 (first offense, WARN)
echo '{"point":"pre-write","cwd":"/r","args":{"file_path":"/r/src/a.ts"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # → exit=10 (repeat → BLOCK)

echo '{"point":"pre-write","cwd":"/r","args":{"file_path":"/r/README.md"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # docs → exit=0 (allow)
echo '{"point":"pre-bash","cwd":"/r","args":{"command":"git status"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # single read-only → exit=0 (allow)

echo '{"point":"pre-bash","cwd":"/r","args":{"command":"gh ship 605"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # gh ship <PR#> → exit=0 (allow, never warns — agent-tools#159)
echo '{"point":"pre-bash","cwd":"/r","args":{"command":"gh pr merge 605"}}' | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # every OTHER gh subcommand → exit=0 (first offense, WARN)

echo '{"point":"pre-bash","cwd":"/r","args":{"command":"TG_BOT_TOKEN=x tg --format html x"}}' \
  | ./orchestrator_stays_thin.py
rc=$?; echo "exit=$rc"   # env-prefixed tg → exit=0 (allow, never warns — tg-carveout-159)
```

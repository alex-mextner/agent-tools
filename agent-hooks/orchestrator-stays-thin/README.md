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

  **One narrow, doubly-gated heredoc exception (agent-tools#307).** The blanket heredoc block
  exists to catch a heredoc used to write arbitrary content into a *file* (`cat > f.py <<'EOF'
  ...`), bypassing the Edit/Write tool. It does **not** apply to a heredoc matching BOTH of these:
  1. **The shape** is `$(cat <<'DELIM' ...body... DELIM)` (the `<<-` dash variant and a
     double-quoted delimiter count too; the delimiter must be a plain `\w+` word) — a QUOTED
     heredoc delimiter guarantees the body is 100% literal text (no `$(...)`/backtick/`$VAR`
     expansion, whatever the body looks like), and a bare `cat` (no extra file argument) only
     echoes stdin, so the substitution's own VALUE can only ever be a plain string.
  2. **The position** is a plain, DOUBLE-QUOTED argument, starting a genuinely SEPARATE shell word
     (never a redirect target, never inside a `#` comment, never inside an open single-quote,
     never left UNQUOTED — an unquoted substitution is subject to bash's own word-splitting and
     globbing, see below) of an already-`tg`-headed segment (EXACT token, not merely a `tg`
     prefix) — deliberately NARROWER
     than the general `ORCH_ALLOW` allow-list (`git worktree list` is sanctioned orchestration but
     has no "safe argument" story, and `review` was dropped from this set entirely — see below — so
     neither counts here), wrapper-stripped the same way `_strip_wrappers` handles every other
     segment (`timeout 60 tg "$(cat <<'EOF' ...)"` / `env X=1 tg …` still qualify) — and at
     substitution depth 0, never nested inside another `$(...)`/backtick/`<(`/`>(` at all. A
     safe-shaped VALUE is only actually safe if whatever consumes it treats it as inert data,
     verified against real bash for each of these (not just reasoned about):
     - `eval "$(cat <<'EOF' ...)"` — `eval` re-parses its argument as a new command.
     - a nested `tg "$($(cat <<'EOF' ...))"` — a bare `$(<value>)` around another substitution
       runs `<value>` as a command (`x=$($(cat <<'EOF'\ntouch /tmp/marker\nEOF\n))` really creates
       the file).
     - `tg > "$(cat <<'EOF' ...)"` — the substitution is a REDIRECT TARGET (a filename), not a
       plain argument; tg's own stdout would be written to an attacker-chosen path.
     - `tg ok # $(cat <<'EOF' ...)` — the `$(cat <<'EOF'` sits inside a `#` comment, so bash never
       treats it as a heredoc at all; the following line is an ordinary, LIVE, separate command,
       not heredoc body.
     - `eval \` + newline + ` tg "$(cat <<'EOF' ...)"` — a backslash-newline is a real line
       CONTINUATION (removed before parsing), so this is ONE `eval` invocation, not a fresh
       `tg`-headed segment starting after the newline.
     - `tg >& "$(cat <<'EOF' ...)"` / `tg > path/"$(cat <<'EOF' ...)"` — `>&`/`>|`/zsh `>!` and a
       word-CONCATENATED target (no whitespace before the substitution) are still a redirect.
     - a backslash-escaped `)` inside a nested `eval`'s double-quoted argument, or an ANSI-C
       `$'...'` opener whose embedded `\'` does NOT end the string, can desync naive bracket/quote
       counting and either widen an unrelated later segment's head match or make substitution
       depth return to 0 too early — closed by rejecting outright on ANY backslash or `$'`
       sequence in the segment (a real tokenizer would be needed to handle escaping/ANSI-C-
       quoting everywhere it can appear; no legitimate report needs either before its heredoc
       argument).
     - a bare grouping/arithmetic `(` (a subshell or `$((...))`), or a QUOTED `)`/`"` inside a
       nested substitution's own literal text (e.g. an earlier heredoc's body, or `echo ')'`),
       both desync naive depth counting the same way — a genuinely-nested, re-executing heredoc
       can end up looking like it sits at depth 0. Fixed at the root: quote state is tracked PER
       SUBSTITUTION NESTING LEVEL (a stack, not a single global flag) — a quoted `)`/`(` is
       correctly recognized as literal at its OWN level and never affects depth, while a
       genuinely bare, unquoted `(` at any level properly opens (and its matching `)` properly
       closes) that SAME level — which also fixes over-blocking a harmless `tg "note (see
       below)" "$(cat <<'EOF' ...)"`.
     - `tg 'foo $(cat <<'EOF' ... EOF)'` — bash single quotes do NOT nest and have no escape
       mechanism, so this looks like one big quoted argument but is really an alternating series
       of quoted/UNQUOTED spans, hiding a genuinely LIVE `; echo PWNED ;` in the middle. Closed by
       rejecting outright whenever the consumer-head check can't cleanly parse the segment head
       even after the standard recovery (falling back to a raw regex match here was the bug).
     - `tg$(cat <<'EOF' ...)"` — with NO whitespace before the substitution, bash concatenates
       its value directly onto the command name (`tg` + `-ctl` from the body → the DIFFERENT
       command `tg-ctl`). Closed by requiring the substitution to start a genuinely separate word.
     - `tg"$(cat <<'EOF' ...)"` — the SAME word-concatenation exploit, but with a quote glued
       directly onto `tg` (no space at all): a quote character right before the substitution isn't
       by itself proof of a fresh word, since it's the same character that appears in the SAFE `tg
       "$(...)"` (space, then quote) shape too. Closed by checking what precedes the quote itself,
       not just the quote.
     - `tg-ctl "$(cat <<'EOF' ...)"` — the consumer-head check used a bare `\b` word boundary
       (`tg\b`), but `\b` only tests a word/non-word character transition, not "end of token" — it
       is satisfied right before the `-` in `tg-ctl` just as much as before a space, so a heredoc
       fed to the genuinely DIFFERENT command `tg-ctl` was wrongly treated as the vetted `tg`.
       Closed with an exact-token lookahead (`tg(?=\s|$)`) instead of `\b`.
     - `tg '$(cat <<'EOF'\n' ; printf LIVE ; x='\nEOF\n)'` — a MINIMAL single-quote non-nesting
       bypass (no prefix text needed): the `$(cat <<'EOF'` shape sits inside an OPEN top-level
       single-quote, where it is never a real substitution start at all from bash's point of view —
       the quote tracker computed this internally but never exposed it to the safety check. Closed
       by surfacing "currently inside an open top-level single-quote" and rejecting on it.
     - `tg $(cat <<'EOF' ...)` — **no surrounding double quotes at all** (agent-tools#307 review
       round 10, Codex P1). Every check up to this point implicitly assumed the substitution
       reaches `tg` as exactly ONE argument — true only when it's double-quoted, which every
       legitimate example of this idiom is. Left unquoted, bash word-splits (on IFS) and globs the
       result: a heredoc body of `--file /etc/passwd` really becomes the TWO separate argv
       elements `--file` and `/etc/passwd`, letting the heredoc body inject arbitrary CLI FLAGS
       into `tg`'s own invocation — not just literal text. Closed by requiring the candidate
       position to sit inside an open top-level double-quote.

  `review` was **dropped from the carve-out's own consumer set** in round 9: in the standard
  installed configuration, the sibling `no-long-inline-process` hook already intercepts an
  orchestrator-run `review …` at a lower priority number (35, before this hook's 45). This is a
  "typically true" argument, not an absolute one (round 10, Codex P2) — the hook-bridge continues
  to later descriptors whenever an earlier one *allows* (fail-open, a hatch-approved exception, or
  the sibling hook simply not being installed, since each agent-hook is independently installable),
  so it doesn't structurally guarantee interception. A dispatched subagent, by contrast, IS
  unconditionally exempt from this whole hook before the carve-out's logic runs at all. So there
  was no reachable path in the common case where collapsing a heredoc fed to `review` ever
  mattered. `review` remains sanctioned orchestration on its own (`ORCH_ALLOW`, no heredoc
  involved) — it just doesn't get a heredoc argument carved out.

  Even a body that passes every check above must not contain a **dash-prefixed line**
  (`--help`, `--no-feature ...`, `--file ...`) — `tg` extracts its own feature flags/options from
  argv *before* treating anything as message text, so a heredoc body that happens to look like a
  flag could change what `tg` *does*, not just what it prints (agent-tools#307 review round 11,
  Codex P1). This is a narrow, cheap mitigation for the most obvious shape, not a full fix for
  `tg`'s own argv parsing (a separate repo/concern — see the follow-up ticket below).

  This is the standard idiom for a multi-line/HTML `tg` report (`tg --format html "$(cat <<'EOF'
  ...)"`). Every span matching ALL conditions is collapsed to the neutral placeholder `$()`
  before classification, so the command is judged as if it were a plain string argument. Anything
  that fails ANY condition — an unquoted delimiter, a consuming command other than a bare
  `cat`, `eval`/`bash -c`/`source`/any non-sanctioned head, a nested substitution, a redirect
  target, a comment-hidden fake heredoc, a line-continued disguise, an unquoted substitution, or a
  bare heredoc redirected to a file — still hits the ordinary blanket block unchanged. A mutation
  chained *after* the safe idiom (`tg "$(cat <<'EOF' ...)" && git commit`) still warn-then-blocks
  on its own segment.

  **Two known, accepted safe-direction residuals** (over-block only — neither ever launders a
  dangerous command; fixing either requires a heredoc-body-aware mask, deferred to the follow-up
  ticket below):
  - A backtick `` ` `` pushes a nesting level but is never popped (bash's own closing token is
    another backtick, not a distinguishable character), so a properly-paired *earlier* `` `date` ``
    permanently inflates depth and can over-block a *later*, otherwise-safe heredoc in the same
    command (pinned by `test_paired_backticks_permanently_block_the_carveout_documented_bias`).
  - The quote/depth mask has no concept of "heredoc body" as an opaque zone, so a literal `"`
    inside an *earlier* heredoc's own body (fully inert to real bash) can still toggle that
    level's quote-tracking state and over-block a *later*, otherwise legitimate sibling heredoc
    (pinned by `test_stray_quote_in_earlier_heredoc_body_can_over_block_later_sibling_documented_bias`).

  **ALL `gh` is delegated (Alex tg#7103).** `gh ship`, `gh pr checks`/`view`, `gh run`, `gh api`
  — every gh subcommand — is implementation the orchestrator hands to a subagent, not inline work.
  This **reverts** the earlier `gh ship`/read-only-`gh` carve-out (agent-tools#159/#162): shipping
  a gated PR *and* CI/PR verification are a subagent's job. `gh` is not in the allow-list; it is an
  impl-signal, so an inline `gh ship 605` warn-then-blocks exactly like `git commit`. A dispatched
  subagent (`agent_id` present) is exempt and runs gh/ship freely — the gate governs the
  orchestrator only.

**Two pre-existing, general bugs closed alongside the carve-out (found by GitHub's automated
review on the #307 PR, PR #311):** both are verified reproducible on `main` with ZERO heredoc
involvement — the heredoc work didn't introduce either, but both are fixed here since they share
the same wrapper-stripping/chain-splitting machinery the carve-out reuses, and left them blocking
would leave the same hook trivially bypassable.
- **`time -f`/`-o` operand not recognized.** `_WRAPPER_OPT_ARGS["time"]` had no operand-taking
  flags registered, so `time -f tg pytest` was mis-stripped to `tg pytest` — reading `tg` as the
  wrapped command when GNU `time`'s `-f` actually consumes `tg` as its OWN format-string operand,
  and the REAL wrapped command (`pytest`) slipped past entirely. Fixed by registering `-f`/
  `--format`/`-o`/`--output` as operand-taking, the same way `env`/`nice`/`timeout` already are.
- **`_split_chain`/`_blank_single_quoted` had no backslash-escape awareness.** A backslash-escaped
  quote INSIDE a double-quoted span (`\"`, which does NOT end the string in real bash), an
  UNQUOTED `\"`/`\'`, and ANSI-C `$'...'` quoting (a different mode than a plain `'...'`, where
  `\'` does not close the string) were each mistaken for their naive/literal counterpart —
  swallowing a real trailing `;`/chain operator, or blanking a genuinely LIVE `$(...)` as if it
  were inert quoted text, hiding it from `_has_mutating_substitution` entirely. Both functions are
  now escape-PARITY-aware (a `\\"` pair cancels out with no net escaping effect — only an ODD
  backslash count escapes the following character), correctly distinguish a genuine ANSI-C opener
  from an escaped or `$$`-consumed `$`, and strip backslash-newline LINE CONTINUATIONS from each
  segment before any shlex-based head detection — closing a class of bypass where a command NAME
  split across two lines (`"gi\` + newline + `t" commit -m x`) evaded `git`/`gh` detection
  entirely, while no longer over-blocking the common `cmd \` + newline + `arg` formatting idiom.
  Every one of these was verified against real bash execution, not just reasoned about.
- **The blanket `HEREDOC` catch-all itself had no continuation-awareness.** This is the
  FOUNDATIONAL check this whole carve-out is layered on top of, and it requires a literal,
  adjacent `<<` in the raw command text. Making `_split_chain` correctly reassemble a
  continuation-hidden operator for its own segment-counting purposes (above) had an unintended
  side effect: the "3+ segments" fallback that used to accidentally catch a continuation-hidden
  heredoc (via the OLD, continuation-unaware splitter producing more segments) stopped being
  reliable once the splitter got more accurate — an empty-body, same-line-terminator heredoc could
  drop to exactly 2 segments and the write went completely undetected (`cat <\` + newline +
  `<EOF > /tmp/x` + newline + `EOF` really writes `/tmp/x`, verified, but was silently allowed).
  Fixed with a single `_join_continuations` pass applied ONCE, upstream of every other check in
  `_is_implementation_bash` — including the heredoc carve-out itself — so the blanket `HEREDOC`
  regex (and everything else) always sees the same reassembled text real bash does.

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
nothing. The **dispatched subagent** (`agent_id` present) is the one meant to ship/verify and is
exempt.

**Report / verify carve-out — `tg` + read-only inspection (coordinator):** reporting to the user
and *read-only* verification stay at orchestrator altitude and must **not** require a subagent. A
line whose every segment head is `tg`, `review`, `git worktree list`, read-only inspection (incl.
system-info `df`/`du`/`lsblk`/`free`/`ps`/… and filters `jq`/`sort`/`cut`/…), or `cd` — with at
least one `tg`/`review` head — is never warned or blocked, of **any** length: `tg --format html '…'
| tail -3 | grep merged`, `cat status.json | jq .title | head`, `tg done; git status; git log`.
Same per-segment discipline — a build/edit, heredoc (that hasn't already been collapsed by the
narrow exception above), substitution, bare-`&`, mutating companion, or any **`gh`** head forfeits
it (`tg done && sed -i …`, `gh pr view && git push`, and now a bare `gh pr view` are all
implementation). **`curl` and `ssh` are deliberately NOT sanctioned** —
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
```

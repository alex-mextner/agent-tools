# block-rg-pre

**Point:** `pre-bash` · **Fail policy:** `closed` · **Priority:** 10 (runs early)

Denies a shell command that runs `rg`/ripgrep with a real `--pre <COMMAND>` /
`--pre=<COMMAND>` flag — `rg --pre` runs COMMAND as an arbitrary preprocessor on **every
matched file**, turning the read-only search tool every agent is pre-allowed to run
(`Bash(rg:*)`) into an unrestricted local code-execution vector.

Ref: the 2026-07-01 agent-ecosystem retrospective (`hyperide` repo,
`docs/specs/2026-07-01-agent-ecosystem-retrospective.md`) flagged the `rg --pre` gap; rig-cli's
own `riglib/permissions.py` names this hook directly as the still-missing deep layer behind the
coarse per-harness glob deny rules (`CLAUDE_CODE_DENY_RULES`, `OPENCODE_DENY_RULES`,
`OMP_GUARD_DENY_RULES`). Not linked directly — `hyperide` is a separate, private repository.

## Why the glob belt alone isn't enough

The existing deny rules (`Bash(rg --pre:*)`, `rg*--pre*`, ...) are exact-token/glob matches
over the raw command string. Two real gaps only an argv-precise hook can close:

1. **No `--` end-of-options support**: `rg -- --pre .` is a literal, harmless search for the
   text `--pre` (everything after `--` is a positional argument, never a flag) — the glob belt
   denies it anyway.
2. **No flag-vs-value distinction**: `rg -e --pre .` searches for the literal pattern `--pre`
   via `-e`/`--regexp` — no `--pre` FLAG is present at all — but a glob/substring match can't
   tell "the text `--pre` used as a flag" from "the text `--pre` sitting in another flag's
   value slot" and denies it too.

**Caveat on a claude-code machine that still has the coarse glob deny rule installed**: that
glob rule runs as a NATIVE permission check, independent of and BEFORE this PreToolUse hook — so
its own (imprecise) verdict can still deny `rg -- --pre .` / `rg -e --pre .` before this hook
ever gets a chance to allow them, until the glob rule is relaxed once this hook is deployed
everywhere (a rig-cli-side coordination task, not done here). This hook's BLOCK path is
unaffected either way — it is strictly additive on top of the glob belt, never looser.

## What's blocked vs. allowed

- **Blocked**: `rg --pre 'sh -c ...' .`, `rg --pre=./run.sh .`, `sudo rg --pre curl .`,
  `timeout 5 rg --pre foo .`, `rg --pre foo . && echo done`, `env -S 'rg --pre foo .'`,
  `pgrep ... | xargs rg --pre foo`, `rg '>' --pre x .` (a bare quoted redirect-shaped
  positional), `rg "$(printf -- --pre)" ./x .` (unresolved command substitution in an rg
  argument — fails closed since the expanded value can't be verified statically),
  `RIPGREP_CONFIG_PATH=/tmp/x.rc rg pattern .` and `export RIPGREP_CONFIG_PATH=/tmp/x.rc; rg
  pattern .` (inline or same-command-exported config-file path that could inject `--pre` —
  fails closed since this hook doesn't read the file).
- **Allowed**:
  - `rg -- --pre .` — `--` ends option parsing; `--pre` after it is a literal positional.
  - `rg -e --pre .` — `--pre` is the VALUE of `-e`/`--regexp` (and every other rg flag that
    takes a required value: `-f`/`--file`, `-t`/`--type`, `-g`/`--glob`, ...), not a flag.
  - `rg --pre-glob '*.gz' .` alone — a **separate**, non-dangerous flag (has no effect without
    `--pre`, which is caught on its own); never conflated with `--pre` (exact-token match only).
  - `rg --pre '' .` / `rg --pre= .` — an explicit EMPTY preprocessor command, ripgrep's own
    no-op for "disable preprocessing", not the dangerous flag.
  - Any `rg` invocation with no `--pre` flag at all.

## Parsed, not raw-matched

Detection is argv-based (shlex), same discipline as `block-no-verify`/`pkill-guard`: the
command is tokenized (a newline is a command separator, same as `;`), split into segments on
every shell separator, and each segment's real argv is recovered after stripping shell
redirections (value-flag-aware — see below), inline `VAR=value` assignments, and a wrapper
table (`sudo`, `timeout`, `env` — including `env -S`/`--split-string`, re-inspected as a nested
command — `nice`, `taskset`, `chrt`, `setpriv`, `runuser`, `flock`, ...), ported from
`block-no-verify`'s audited wrapper-peeling machinery. A segment whose recovered argv[0] is `rg`
(bare, or a path to it) — or the WRAPPED command of an `xargs` invocation, e.g. `pgrep ... |
xargs rg --pre ...` — is scanned left to right for a real `--pre`/`--pre=COMMAND` flag, honoring
`--` end-of-options and correctly skipping the value of every other rg flag that takes one.

Redirect-stripping guards against a quoted `>`/`<`-shaped token being misread as a real shell
redirect two ways: it's protected when it's a KNOWN rg value-flag's literal VALUE (`-e '>'`,
`--regexp '>'`), and it's protected when the token FOLLOWING it (the redirect's would-be target)
looks like a flag itself — a real file target never legitimately starts with a bare `-`, so
`rg '>' --pre x .` (a bare quoted positional, nothing recognizable before it) is also caught.
Otherwise either shape would silently swallow the `>` AND the token after it — which could be a
real `--pre` — as "a redirect and its target".

## No self-service bypass — Telegram hatch only

Deny-by-default. Request a one-time Telegram approval with a written justification:

```bash
RIG_HATCH_REQUEST_BLOCK_RG_PRE="<reason>" rg --pre ./decompress.sh archive.gz
```

Routes through the shared `agenttools_hatch_escalation` helper (same mechanism as
`pkill-guard`/`background-subagent-gate`/`block-raw-pr-merge`) — a blank/bare-flag value is
rejected, no Telegram call is made, and the command is denied. Every attempt (approved or
denied) is auto-recorded in `overrides.log` (gap **G-8**, already shipped). Nothing set = the
block stands; run the preprocessor command directly instead of routing it through `rg --pre`.

## Fail-closed

`on_error: "closed"`. A malformed event (bad JSON) always blocks. A parse failure on the command
text itself blocks whenever the raw text still plausibly names an `rg ... --pre` invocation
(**unparseable → block**) or looks like an obfuscated `env -S`/`--split-string` construct at the
command head (**obfuscation → block**); text that can't plausibly be either is allowed — this is
a deliberately narrower guarantee than "any crash blocks everything", so this hook doesn't become
a blanket parser for every `rg`-unrelated command that merely fails to tokenize.

## Known limitations

- No heredoc awareness (unlike `block-no-verify`): a heredoc BODY that happens to contain a
  line looking like `rg --pre ...` is treated as a real command — an **over**-block, never a
  bypass, same accepted trade-off as `pkill-guard`.
- A command substitution used **as an argument** to a directly-typed `rg` invocation (e.g. `rg
  "$(printf -- --pre)" ./x .`) **is caught**: any unresolved `$(...)`/backtick token inside an rg
  segment's own argv fails the whole invocation closed, since shlex only strips quotes — it never
  evaluates a substitution — so the literal token never equals `--pre` even though the shell will
  expand it to exactly that. What remains unresolved is the entire `rg ... --pre ...` invocation
  hidden inside a substitution assigned elsewhere (`x=$(rg --pre evil .)`), where `argv[0]` is
  never literally `rg` to this hook at all. Same documented precision trade as every sibling
  hook's `bash -c '...'` limitation.
- A shell **alias** for `rg` is not resolved — aliases don't expand under a harness's `bash -c`
  anyway, same gap as every sibling hook.
- `find ... -exec rg --pre ... {} ';'` and `eval rg --pre ... .` are not resolved (neither
  `find` nor `eval` is a recognized wrapper) — documented, not fixed here (the coarse glob belt
  still catches the raw text on harnesses that have one). `eval` is deliberately **not** added
  to `_WRAPPERS` here alone (that table is SYNC'd verbatim with `block-no-verify`; adding it to
  only one side would break the sync invariant) — tracked as **agent-tools#421** (add `eval` to
  both tables together, evaluate `find -exec` resolution, and add an automated check that the
  two SYNC'd tables actually stay identical).
- **ANSI-C quoting (`$'...'`) and shell VARIABLE expansions** (`"$VAR"`, `${VAR}`, brace/glob
  expansion) are not interpreted — this hook reasons about the literal argv shlex recovers, same
  as every sibling hook. A payload assembled through an ANSI-C-quoted string, or a shell variable
  set earlier in the SAME command, is not resolved to its expanded form (`export X=--pre; rg
  "$X" ./pre.sh .` — the assignment and the later `"$X"` reference are two separate, unconnected
  tokens here). Same class of gap `pkill-guard`'s own docstring documents for `TARGET=node pkill
  -f "$TARGET"` — deliberately crafted evasion, not an accidental shape. (This is DISTINCT from
  `$(...)`/backtick COMMAND substitution, which IS caught — see "What's blocked" above — and
  from `RIPGREP_CONFIG_PATH` `export` tracking, which IS also caught within one command string —
  see below.)
- **`RIPGREP_CONFIG_PATH`** (or ripgrep's own default config-file discovery) can inject a
  `--pre` flag with nothing in the command's own argv — rg reads that file's flags itself,
  invisibly to any argv-level guard. This hook does not open and scan the referenced file (would
  grow it into a ripgrep config-file parser). A value set INLINE on the inspected command
  (`RIPGREP_CONFIG_PATH=/tmp/x.rc rg pattern .`, via the `env` wrapper, or via an earlier
  `export`/`declare` in the SAME command string) **is** visible to this hook and fails closed.
  What remains unfixed is a value that reaches rg without appearing anywhere in the command
  string this hook inspects at all — set by a genuinely separate, earlier Bash tool call, or
  already present in the environment the agent's shell started with. This hook does NOT read
  `os.environ` for this check (only the command string's own visible env), so it makes no claim
  either way about whether such a value would slip through — conservative in both directions. A
  real follow-up at the provisioning layer (rig-cli) if this becomes a real vector.
- No effective-state tracking across multiple `--pre`/`--no-pre` occurrences in one invocation:
  `rg --pre CMD --no-pre .` (which ripgrep's own docs say disables the preprocessor again) is
  still blocked — over-block only, never a bypass, and not worth the added parsing surface for a
  pattern nobody legitimately writes.

## Install

```bash
chmod +x block_rg_pre.py
# edit the descriptor's "cmd" to this file's absolute path, then drop the descriptor
# into your harness's pre-bash hook directory. (rig apply does this for you.)
```

## Test

```bash
echo '{"args":{"command":"rg --pre ./run.sh ."}}' | ./block_rg_pre.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"block",...}  exit=10

echo '{"args":{"command":"rg -- --pre ."}}' | ./block_rg_pre.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0

echo '{"args":{"command":"rg -e --pre ."}}' | ./block_rg_pre.py; echo "exit=$?"
# → {"hook_api":"agents-hooks/v1","decision":"allow"}  exit=0
```

Unit tests live in
[`tests/test_block_rg_pre.py`](../../tests/test_block_rg_pre.py):

```bash
uv run --with "pytest>=8,<9" python -m pytest tests/test_block_rg_pre.py -q
```

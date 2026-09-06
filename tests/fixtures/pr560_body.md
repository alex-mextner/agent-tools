Three small follow-ups in the same hook family (filed together, never dispatched), shipped as one PR.

## What changes

**1. `orchestrator-stays-thin` recognizes a print-only `sed` as read-only inspection (#541).**
A `sed` with no in-place flag (`-i`/`-i.bak`/`--in-place`, BSD `-I`), no script file (`-f`/`--file`), and no script command that writes or executes (`w`/`W`/`e` at a command position, the `w`/`e` flags of an `s` command) is now inspection like `head`/`tail`/`grep`, at any chain length. Implemented as a shlex-based predicate `_is_read_only_sed` consulted next to `READ_ONLY_BASH` (a plain `sed\b` in the regex would also grant `sed -i` and `sed 'w file'`). The literal incident chain from #533 — `sed -n '1,240p' SKILL.md && git diff --check && git status --short && git diff -- a.py` — is allowed on the first call for every harness instead of falling to the >=3-segment fallback. `sed -i` keeps tripping `BUILD_EDIT`. Grant-direction, so every ambiguity (unbalanced quotes, a `-l` cluster with differing GNU/BSD operand semantics, a script file) resolves to "not read-only", i.e. to the previous behavior. Review round 1 hardened two edges: the long options are an allowlist of EXACT spellings, so any GNU prefix abbreviation (`--in-plac=.bak`, `--fil=x`) or unknown option is not read-only; and a label / optional-operand command (`: b t T l q Q`) ends at `;`, so `sed -n ':x;w /tmp/out' file` is seen as a write.

**2. `background-subagent-gate` and `no-long-inline-process` get the same codex/opencode harness exemption `orchestrator-stays-thin` got in #544 (#542) — through ONE shared helper.**
`lib/agenttools_hatch_escalation` now exports `EXEMPT_HARNESSES` (`codex`, `opencode`, `omp`) and `is_exempt_harness(event)` (reads only the top-level bridge-set `event["harness"]`, fail-closed allowlist). Every gate already hard-loads that module through its hardened bootstrap, so no second loader was needed. `orchestrator-stays-thin` re-exports the constant and delegates to the helper (its `EXEMPT_HARNESSES` name stays for readers and tests). `omp` (#556, merged meanwhile) lives in the shared set, not in a hook-local copy — adding a harness is one line in the lib.

Two consequences stated plainly. (a) `background-subagent-gate` had a deliberate opencode path (`background: true` normalized into `run_in_background`, experimental-flag gated). With opencode exempt as a whole, that path is reached only if opencode is ever removed from the allowlist; the normalization is kept (documented as the signal for a future explicit "opencode is the orchestrator" knob), not deleted. (b) #497's opencode env-marker identity (`RIG_AGENT_ID` → `args.agent_id`) is unaffected and still matters — it is what makes the subagent-ONLY gates (`subagent-no-bg-longproc`, `subagent-no-monitor`) govern a rig-dispatched detached opencode agent. The three opencode bridge tests that asserted an end-to-end BLOCK for an opencode dispatch were reworked: the real bridge run now asserts allow (harness-exempt), and the property each guarded is pinned one layer down by feeding the bridge's own v1 event to the gate with the harness tag removed (`_run_gate_ungoverned`). If Alex would rather keep governing opencode dispatches in that gate, that is a one-line revert of the exemption in that hook only.

**3. `subagent-no-bg-longproc` states the verified wake mechanism instead of a false blanket (#546).**
Docstring, README, descriptor and block message no longer claim "a subagent is NOT re-invoked by a background-completion notification". They now state the split `subagent-no-monitor` (#439) proved empirically: a Bash `run_in_background: true` child IS harness-tracked and DOES resume the calling subagent; a Monitor watch and a shell-detached job (`&`, `setsid`, `nohup … &`) never do; a `--watch` loop never exits. The block message offers the same two alternatives `subagent-no-monitor`'s does, with the heartbeat example hoisted into a SYNC'd `HEARTBEAT_LOOP_EXAMPLE` constant in both hooks (a test pins them equal). Catalog entries in `agent-hooks/README.md`, `AGENTS.md` and the `subagent-no-monitor` descriptor drop the "broader than confirmed / tracked as #546" hedges.

The gated scope is deliberately unchanged (#546's own acceptance criterion 3): whether a bounded labeled process under `run_in_background: true` should keep being blocked is filed as #559.

## Acceptance proofs

All three run against THIS branch's hook scripts and bridges (`python3 <worktree>/agent-hooks/...`, `PYTHONPATH=<worktree>/lib python3 -m codex_hook_bridge|opencode_hook_bridge`), not the primary checkout's — hooks resolve from the primary checkout at fire time, so a naive live run would have proved main's behaviour. Fresh `ORCH_THIN_MARKER_DIR`, all hatch / agent-identity / opencode-experimental env unset. Long block messages trimmed to their first line.

### #541 — `orchestrator-stays-thin`, the literal #533 chain is allowed on the FIRST call

```
a) sed -n '1,240p' SKILL.md && git diff --check && git status --short && git diff -- a.py
   stdout: {"hook_api": "agents-hooks/v1", "decision": "allow"}          exit 0
b) control: sed -i 's/a/b/' a.py && git diff --check && git status --short && git diff -- a.py
   stderr: orchestrator-stays-thin: By design, not a bug: this session is the orchestrator…   (first-offense WARN; a repeat within the TTL blocks)
   stdout: {"hook_api": "agents-hooks/v1", "decision": "allow", "message": "By design, not a bug: …"}   exit 0
c) control (review round 1): sed -n ':x;w /tmp/out' file && git status && ls
   stdout: {"hook_api": "agents-hooks/v1", "decision": "block", "message": "By design, not a bug: …"}   exit 10
d) control (review round 1): sed --in-plac=.bak 's/a/b/' f.txt && git status && ls
   stdout: {"hook_api": "agents-hooks/v1", "decision": "block", "message": "By design, not a bug: …"}   exit 10
```

### #542 — a codex event through the REAL codex bridge is exempt; the same event untagged is governed

Descriptor `no-long-inline-process.pre-bash.json` → this branch's `no_long_inline_process.py`; event `{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"review diff --task GH-560 -C /repo"},"cwd":"/repo","session_id":"sess-1"}` (a labeled long process, inline, no `agent_id`).

```
a) CODEX_HOOKS_DIR=… python3 -m codex_hook_bridge PreToolUse
   stdout: <empty>  (= allow)                                              exit 0
b) control: the same v1 event fed to the hook directly, bridge `harness` tag removed
   stdout: {"hook_api": "agents-hooks/v1", "decision": "block", "message": "Run this in a BACKGROUND subagent, not the orchestrator: `review (multi-model, minutes-long)` is a long-running…"}   exit 10
```

### #542 — an opencode event through the REAL opencode bridge is exempt; the same event untagged is governed

Descriptor `background-subagent-gate.pre-agent.json` → this branch's `background_subagent_gate.py`; a non-trivial FOREGROUND `task` dispatch (`subagent_type: custom-worker`, two-line prompt, no `background` field, no env marker).

```
a) OPENCODE_HOOKS_DIR=… python3 -m opencode_hook_bridge tool.execute.before
   stdout: <empty>  (= allow)                                              exit 0
b) control: opencode_hook_bridge.dispatch.to_v1_event(...) (harness == "opencode" asserted), tag popped, fed to the gate directly
   stdout: {"hook_api": "agents-hooks/v1", "decision": "block", "message": "Dispatch this subagent in the BACKGROUND — foreground blocks the main thread until it finishes.…"}   exit 10
```

### #546 — the README now carries the rationale (excerpt of `agent-hooks/subagent-no-bg-longproc/README.md`)

> ## Which notifications actually re-invoke a subagent (the precise mechanism)
>
> An earlier revision of this README claimed a blanket *"a subagent is NOT re-invoked by a background-completion notification"*. That is **false for the general case** — the sibling `subagent-no-monitor` gate (agent-tools#439) proved the real split empirically, and agent-tools#546 reconciled this hook's wording to it:
>
> | Backgrounding shape | Resumes the subagent when done? |
> | --- | --- |
> | Bash tool call with **`run_in_background: true`** | **Yes** — the harness tracks that child against the agent that started it and re-invokes it with the output once the child exits (verified: a 40 s backgrounded `python3` sleep resumed its subagent after ~43 s with no further message). An ORDINARY, non-labeled command backgrounded this way is a fine shape for a subagent, and this gate allows it. |
> | **Monitor** watch | **Never** — Monitor has no harness-tracked child at all. `subagent-no-monitor` blocks every subagent Monitor call for that reason. |
> | Shell-**detached** job — trailing `&`, `setsid`, `nohup … &` | **Never** — the harness knows nothing about a job the shell forked behind its back; the Bash call returns at once and no completion ever arrives. |
> | A `--watch` loop | **Never** by any route — it never exits. |
>
> What this gate blocks, given that: a subagent backgrounding a **labeled** long process (`review`, `--watch`, a build/test suite, `sleep N>=10`) by **any** of the three backgrounding shapes, `run_in_background: true` included. For a detached job or a `--watch` loop the wedge is certain. For a *bounded* labeled process under `run_in_background: true` the harness **would** resume the subagent; whether that specific case should keep being blocked […] is a **scope** question deliberately left to a follow-up ticket ([agent-tools#559](https://github.com/alex-mextner/agent-tools/issues/559)) rather than folded into this wording fix — the gated scope is unchanged from agent-tools#52.

## Tests (TDD, red first — 40 failures before the implementation)
- `test_orchestrator_stays_thin.py`: read-only sed matrix (13 shapes, each also asserting the `_is_read_only_sed` predicate directly), not-read-only matrix (25 shapes incl. the round-1 `:x;w`, `5q;w`, `l;w`, `--in-plac`, `--fil` cases, each proved end-to-end on a 3-segment chain), the literal repro chain allowed with no harness and no marker primed, head-anchor and unparseable cases. The #533 control tests now use an `awk` chain (the sed one is read-only by design now).
- `test_background_subagent_gate.py`, `test_no_long_inline_process.py`: codex/opencode/omp allow; no tag / `claude-code` / unknown / blank / forged-in-args still block; the hook consults the shared constant at call time (emptying it re-governs codex).
- `test_agenttools_hatch_escalation.py`: `is_exempt_harness` contract.
- `test_opencode_hook_bridge.py`: the three end-to-end dispatch tests reconciled with the exemption (see 2b above).
- `test_subagent_no_bg_longproc.py`: doctrine consistency over all four surfaces + the catalog files; the two hooks' heartbeat constants equal; block output carries the reconciled message.
- Full suite locally after 75cb38e: 4605 passed, 6 skipped, exit 0 (`python3 -m pytest tests/ -q`; the only red ever seen was `test_global_review_gate.py` tripping on stale `.test-repos/` litter left by a killed run — environmental, reproduces on a clean `main`, green after `rm -rf .test-repos`).

## Review round 2 (post-push, 3 roles) — one real gap, fixed in 75cb38e

**Bracket expression in a regex address.** `sed -n '/[/]/w out.txt' f.txt` writes `out.txt` on BOTH GNU and BSD sed (verified live: `out.txt` created by `sed` and `gsed`), but the address scan ended the regex at the `/` inside `[/]`, ran off the end of the script and granted the write as read-only. The scan now honours POSIX bracket expressions exactly where sed does — a regex address and the `s` PATTERN (`[^…]`, a leading literal `]`, `[:class:]` members) — and NOT where it does not (the `s` replacement and `y` have no bracket semantics: `s/a/[/w out` and `y/[/]/;w out.txt` both write, also verified live). Anything sed itself rejects (an unterminated regex/bracket/`s`, an address without a command) now counts as a write instead of falling off the end into "read-only". 6 new read-only shapes + 9 new not-read-only shapes in the matrices, each not-read-only one proved end-to-end on a 3-segment chain. Codex (round 3, consistency) asked for `[=equiv=]` / `[.coll.]` rows too: both share the ONE code path the `[:class:]` row pins (`script[i+1] in ":=."` → find the matching `X]`), and `sed -n '/[[./.]]/w out.txt'` / `'/[[=/=]]/w out.txt'` were verified live (both seds write `out.txt`) and through the predicate (both not read-only) — not added as rows to keep the reviewed diff stamped as-is. Also from round 2: `_skip_one_sed_address` / `_skip_sed_address_clause` naming, and the opencode test helper's `pop("harness", None)`.

Live hook run (this branch's script, fresh marker dir):

```
sed -n '/[/]/w out.txt' f.txt && git status --short && ls
   1st: {"decision": "allow", "message": "By design, not a bug: …"}   (advisory WARN)
   2nd: {"decision": "block", "message": "By design, not a bug: …"}
sed -n '/[/]/p' f.txt && git status --short && ls
   {"decision": "allow"}  ×2  (no marker written)
sed -n '1,240p' SKILL.md && git diff --check && git status --short && git diff -- a.py
   {"decision": "allow"}  ×2  (the #533 chain, unchanged)
```

Declined from the same round, deliberately: moving `is_exempt_harness` out of `agenttools_hatch_escalation` into a new lib module (every gate already hard-loads that module through its hardened bootstrap — a second module means a second bootstrap per hook, which is the cost the PR avoids; the docstring states the tenant mismatch on purpose) and extracting the sed walker into its own file (out of scope for a three-ticket fix PR; fine as a follow-up if the walker grows again).


Refs #541, #542, #546, #559

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01EvQKpFsbDPyn6kgnau3XQV


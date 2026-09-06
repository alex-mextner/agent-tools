# chat-dictionary-gate

**Point:** `stop` · **Fail policy:** `open` · **Priority:** 10

Fires when the agent is about to **end its turn**. It reads the assistant's own
just-written reply from the session transcript, checks it against the SAME banned-word
dictionary already enforced for outgoing Telegram messages (`~/.claude/DICT.json`,
enforced there by `tg`, tg-cli#302), and **blocks the stop** (exit 10) when a real hit is
found. Closes a gap Alex has raised repeatedly, across many sessions: the Telegram
enforcement has zero effect on text typed directly into the Claude Code chat UI — a
totally separate surface that had no enforcement at all before this hook
(agent-tools#548).

## Why a Stop hook

Same reasoning as its sibling `stop-completion-selfcheck`: this has to fire at the moment
the agent is about to hand a finished reply back to the human, which is a turn-lifecycle
event, not a tool call or a git action.

## Cyrillic scoping

A rule with `"only_if_cyrillic": true` in DICT.json (e.g. `fork-to-alex`,
`land-landing-in-russian`) applies **only when the whole current-turn assistant text
contains a Cyrillic codepoint** (checked once, over the whole turn, as a single string —
not per-rule, not per-session-language). This mirrors DICT.json's own stated semantics and
tg-cli's `checkDictionary` implementation
(`tg-cli/features/cli/dict-gate.ts`). It is why a subagent's plain-English dispatch report
that happens to contain the ordinary word "fork" in a normal sentence is safe: the text has
no Cyrillic, so `fork-to-alex` never even runs against it — pinned by
`test_english_fork_in_ordinary_sentence_is_allowed`. The opposite case — a Russian message
borrowing an English loanword like "landing" — correctly turns the rule ON, pinned by
`test_russian_landing_loanword_blocks`.

## Cooldown vs. loop guard — two different mechanisms for two different problems

`stop-completion-selfcheck` uses a **cooldown**: it blocks at most once per TTL window as a
one-time reminder, then allows every subsequent stop until the window expires. That is
wrong for THIS hook — a banned word is a hard content violation, not a nudge, so it must be
re-checked and re-blocked on **every** Stop that still contains one; blocking an
already-clean turn costs nothing, and letting a violation "age out" defeats the entire
point.

What this hook needs instead is a **loop guard**: a per-session count of *consecutive*
violating Stops, capped at `CHAT_DICT_GATE_LOOP_GUARD_CAP` (default 3). Crossing the cap
allows the stop anyway — logged under its own decision string, `allow_loop_guard_cap`, not
a plain `allow` or `block`, so it's visible in `firings.jsonl` — and resets the streak
counter. A later clean turn also resets it to 0. The cap is **per unbroken streak, not a
lifetime count**: pinned by `test_loop_guard_caps_consecutive_blocks_then_allows`, which
drives the counter past the cap, confirms the allow, confirms a clean turn resets it to 0,
then confirms a fresh violation right after that reset blocks again from 1.

Without a cap, a model that cannot converge on clean wording — or a misclassification bug
in the transcript scan — could block the same session forever, which is strictly worse
than the banned word occasionally slipping through once.

## Retry-boundary scanning (the empirical reason the cap isn't decorative)

Duplicating `stop-completion-selfcheck`'s "assistant text since the last real user
message" scan **verbatim** would not be safe here, and this was checked empirically, not
assumed. Grepping real session transcripts for a genuine `stop-completion-selfcheck` block
(`grep -l "Before finishing, run the completion self-check" ~/.claude*/projects/*/*.jsonl
~/.claude-accounts/*/projects/*/*.jsonl`) and reading the records immediately around a hit
(e.g. `~/.claude/projects/-Users-ultra-xp-rig-cli/5a9d5cb6-*.jsonl`, lines 40-43) shows the
real shape CC injects for a Stop block:

- line 40: `type:"user"`, `isMeta:true`, `message.content` a **plain string** — not a
  content-block list — reading `"Stop hook feedback:\n<the block message>"`.
- line 41: an `attachment` record (`hook_blocking_error`).
- line 42: a `system`/`stop_hook_summary` record.
- line 43+: the model's next `assistant` turn — its retry.

That `isMeta:true` record is, by design, **not** a genuine user turn boundary —
`stop-completion-selfcheck`'s own `_is_real_user_turn_boundary` correctly treats it as
turn-continuation so a worked turn isn't misclassified after a self-check nudge. But for
THIS hook, treating it as continuation would mean the model's corrected rewrite text gets
concatenated together with the **original violating text from before the block**, forever
— the joined string can never look clean, and the loop guard cap would become the *only*
way out rather than a defense-in-depth backstop for a genuinely stuck model.

So this hook adds a second boundary kind on top of the duplicated real-user-boundary check:
`_is_retry_boundary` stops the reverse scan at any `type:"user"`, `isMeta:true` record whose
string content starts with `"Stop hook feedback:"` — whether the block that produced it was
this hook's own or a sibling stop-point hook's, since the shape is CC's, not specific to any
one hook. Text after that record is judged as this attempt's own; text before it was
already judged at a prior Stop. Pinned by two tests:
`test_rewrite_after_a_stop_block_that_fixed_the_wording_is_allowed` (a genuinely fixed
rewrite must be let through) and
`test_rewrite_after_a_stop_block_that_still_violates_blocks_again` (a fresh violation in the
retry must still be caught, not accidentally exempted by the boundary logic).

**Known related gap, out of scope for this hook**: `stop-completion-selfcheck`'s own
`_classify_turn` has the same blind spot for its own retries — after its own block, a retry
Stop within the same cooldown window re-reads pre-block text when scanning for tool-use.
Today this is latent because the fresh cooldown marker short-circuits the classification
call entirely on that retry; it becomes live once the TTL expires mid-retry. Tracked as a
follow-up, not fixed here (agent-tools' `AGENTS.md` "Dead code" guidance and this task's own
scope both say: fix the thing you're asked to fix, flag the rest).

## Fail-open, including on a broken dictionary — a deliberate divergence from tg-cli

`on_error: "open"` in the descriptor covers crashes and timeouts, same as every other
advisory hook in this catalog. This hook goes further: `on_error: "open"` governs the
**dictionary lifecycle itself**, not just process failures — a posture tg-cli (deliberately)
does not share.

- **A missing DEFAULT dictionary** (`~/.claude/DICT.json`, no override) → the gate is
  silently disabled, no stderr noise. A machine without Alex's personal dictionary is not
  an error (mirrors tg's `resolveDictionaryPath`/`loadDictionary` "disabled" case).
- **A missing EXPLICIT override** (`CHAT_DICT_GATE_DICT_PATH` pointing nowhere) → allowed,
  but WARNS. tg fails **closed** here (refuses every send) on the theory that an explicit
  path is either a real misconfiguration or the agent's own attempt at an off-switch and
  both deserve a hard stop. This hook allows instead: naming a path that doesn't resolve is
  still just a dictionary-lifecycle problem, and this hook's contract is that NO dictionary
  problem may trap an interactive turn. It still warns loudly (unlike the silent default
  case) because it's a real, actionable misconfiguration.
- **Malformed JSON, an invalid rule shape, or a rule regex that fails to compile** → the
  gate allows, with a stderr warning naming the file/rule. One broken rule does **not**
  take down the rest of the dictionary — every rule that DOES compile is still enforced
  (pinned by `test_bad_rule_does_not_disable_the_other_valid_rules`); this is stricter
  fault-tolerance than tg needs, because tg's failure mode (refuse one send) is cheap to
  retry and tg deliberately wants a broken file to be visible immediately, while this
  hook's failure mode (the user cannot end their turn) is not something a config typo
  should ever be able to cause, even partially. This includes a non-string `flags` field
  (e.g. `"flags": 5`) — review found this would otherwise raise an UNCAUGHT `TypeError`
  out of `_map_flags` (its `for ch in flags or ""` has no defense against a non-iterable),
  which would have propagated past `_compile_rule`'s own `except (re.error, ValueError)`
  and out of `main()` entirely, taking down every rule that hadn't compiled yet — an
  actual violation of the "one broken rule doesn't take down the rest" contract this
  section claims, not merely a hypothetical one.
- **A transient dictionary failure between two real violations does not corrupt the loop
  guard's streak.** If the dictionary breaks (or is briefly absent, e.g. mid-edit) on a
  Stop that falls BETWEEN two genuine violations in the same session, that Stop still
  resets any EXISTING counter for the session to 0 before returning — otherwise a later
  real violation would resume from a stale prior count and trip the cap earlier than
  `LOOP_GUARD_CAP` genuinely-consecutive violations warrant (a review finding). A session
  that has never violated anything still writes nothing (the counter file simply doesn't
  exist yet), preserving the "no dictionary -> no accumulating state" posture above.

**Why the postures differ**: tg's failure mode is "refuse one Telegram send" — cheap,
instantly retryable, and nothing else is blocked while it's wrong. This hook's failure mode
is "the user cannot end their interactive CLI turn" — a bad character in a config file
could otherwise wedge an entire session. Fail-open is the only defensible default for a
Stop-point gate; see `stop-completion-selfcheck`'s own README for the same argument applied
to its own domain.

## Multi-hook ordering at `stop` (why priority 10)

Read from `lib/agent_hooks_v1/runner.py` and `lib/cc_hook_bridge/dispatch.py`:

- `load_descriptors` sorts hooks for a point by **ascending `(priority, id)`** — a LOWER
  priority number runs FIRST.
- `dispatch()` runs the sorted list in order and returns on the **first** hook that emits
  `block` — "first block wins"; later hooks registered for the same point are **not**
  invoked for that Stop event at all.

Today there are two other descriptors at `point: "stop"`: `model-error-fallback`
(priority 20) and `stop-completion-selfcheck` (priority 50). `model-error-fallback` is
purely advisory — it never calls `sys.exit` with the block exit code, so it can never
short-circuit anything (verified by reading `model_error_fallback.py`: no path emits
`decision: "block"`). `stop-completion-selfcheck` **can** block (once per its own cooldown
window).

Combined with the retry-boundary design above, this makes the priority choice
**load-bearing, not a latency preference**: if this hook sorted AFTER
`stop-completion-selfcheck` and that hook's own cooldown allowed it to block first on the
same Stop event, the model's response to THAT block would inject its own
`"Stop hook feedback:"` retry-boundary record — and this hook, scanning again on the next
Stop, would treat everything before that boundary (including the original banned-word text)
as already-judged and stop looking at it, without this hook ever having actually run
against it. **Priority 10 puts this hook ahead of every hook that can currently emit exit
10 at `stop`**, so ON A NORMAL RUN a banned-word violation is caught on the exact Stop
event where it occurred, not masked by a different hook's block consuming that event
first. This is not an absolute guarantee, though (a review finding worth stating plainly
rather than overclaiming): this hook's own `on_error: "open"` contract means a timeout or
crash on event N still resolves to allow, letting `stop-completion-selfcheck` block N
instead and inject its own retry-boundary record — event N's text is then never evaluated
by THIS hook at all, on N or afterward. The ordering guarantee is "wins the priority race
when both hooks actually run," not "immune to this hook's own failure mode." If a future
hook is added at `stop` with a priority below 10 that can also block, the priority
argument itself must also be re-verified against it.

## Configuration

- `CHAT_DICT_GATE_DICT_PATH` — explicit dictionary path override. Unset by default (falls
  back to `CHAT_DICT_GATE_DICT_PATH_DEFAULT` / `~/.claude/DICT.json`). See "Fail-open" above
  for how a missing/explicit path differs from a missing default one.
- `CHAT_DICT_GATE_DICT_PATH_DEFAULT` — the default dictionary path (default
  `~/.claude/DICT.json`) — mostly useful for tests; not something a real deployment should
  need to change.
- `CHAT_DICT_GATE_MARKER_DIR` — where per-session loop-guard counters and the firings log
  live (default `~/.cache/agent-tools/chat-dict-gate`).
- `CHAT_DICT_GATE_FIRINGS_LOG` — where firing decisions are logged (default
  `<MARKER_DIR>/firings.jsonl`).
- `CHAT_DICT_GATE_LOOP_GUARD_CAP` — how many consecutive violating Stops (per session,
  per unbroken streak) are tolerated before this hook gives up and allows anyway (default
  `3`). Clamped to a minimum of 1 — a cap of 0 would silently never block at all.
- `CHAT_DICT_GATE_TRANSCRIPT_TAIL_LINES` / `CHAT_DICT_GATE_TRANSCRIPT_TAIL_BYTES` — same
  bounded-tail-read knobs as `stop-completion-selfcheck`'s `SELFCHECK_TRANSCRIPT_TAIL_*`
  (defaults `500` lines / `2000000` bytes).
- `CHAT_DICT_GATE_DISABLE` — kill switch, same convention as `SELFCHECK_DISABLE`: set to
  `1`/`true`/`yes`/`on` (case-insensitive), or drop an empty `DISABLED` file in
  `CHAT_DICT_GATE_MARKER_DIR`, to unconditionally allow every stop with **no** marker/log
  writes at all — checked before anything else, no redeploy needed.

## What a block actually proves — and doesn't

A Stop-hook block prevents the **turn** from ending and injects a corrective instruction the
model must act on next. It does **not** retroactively un-display text that has already
streamed to the screen — if the violating reply was already rendered before the model
decided to stop, the human may have already seen it for a moment before the correction
lands. This hook stops the banned word from being the FINAL, accepted state of the turn; it
is not a screen-content filter.

## Known limitations

- **A still-flushing transcript write** (review-cli finding). `_read_tail_records` skips a
  trailing line that fails `json.loads` — the documented, deliberate handling for a
  transcript file that is normally mid-append when Stop fires (see
  `stop-completion-selfcheck`'s identical handling and its own rationale). If CC's write
  of the assistant's OWN final reply is still in flight at the exact moment Stop fires,
  that reply's line could, in principle, be dropped from the scan, and this hook would
  then judge the PREVIOUS turn's text instead of the one that just triggered the Stop —
  a theoretical bypass, not a hypothetical one this hook introduces: it inherits the same
  risk `stop-completion-selfcheck` has always had, under the same design (this hook was
  told to duplicate that design, not to redesign it — see "Directory self-containment"
  below). Whether this is reachable in practice depends on a CC/bridge guarantee (a fully
  flushed transcript before invoking `stop` hooks) neither hook's test suite currently
  proves either way.
- **A retry with no text block at all** (e.g. a reply consisting only of a `tool_use`
  block) extracts as `""` and allows — the pre-block violating text remains the last
  RENDERED assistant text even though this hook does not re-flag it. Narrow, not a
  blocker: see "What a block actually proves" above — this hook was never a screen-content
  filter, only a gate on the turn's own final accepted text.
- **`session_id`'s `cwd` fallback can collide across two concurrent sessions in the same
  directory, and this CAN let a first-time violation through unblocked** (review-cli
  finding, corrected across two review rounds — the first pass here understated the
  impact). When CC's Stop event carries no `session_id` at all (a supported bridge shape —
  `cc_hook_bridge`'s own Stop test exercises exactly this), `session_id()` falls back to
  `event.get("cwd")`. Two independent CC sessions working in the identical directory at
  the same time then share one loop-guard counter. Concretely: if session A's violations
  have already pushed the shared counter to `LOOP_GUARD_CAP`, session B's very FIRST
  violation reads that same counter, computes `new_count > LOOP_GUARD_CAP`, and is
  allowed straight through as `allow_loop_guard_cap` — never blocked even once. This is a
  real, if narrow, way a banned word can go un-blocked (it requires BOTH sessions to lack
  a `session_id` AND share a `cwd`), not merely a timing nuance as an earlier version of
  this note claimed. Not fixed here: `stop-completion-selfcheck`'s `session_id()` has the
  identical fallback chain and the identical exposure, and there is no more authoritative
  session identifier available in the v1 event to fall back to instead — redesigning
  session identity is a bigger, cross-hook decision than this task's scope. Worth a
  follow-up ticket if this bridge shape (no `session_id` at all) turns out to be common in
  practice rather than a rare edge case.

## Block message contract

On a hit, the message:

1. Quotes the exact matched text, verbatim.
2. Names the violated rule's `id`.
3. States its `why`.
4. States the exact `replacement` text from the rule.
5. Explicitly tells the model not to name the banned word while explaining the fix (the
   natural failure mode is a rewrite like "changing X to Y" that names the banned word as
   X, which re-triggers the same rule on the explanation itself).

If multiple rules/occurrences matched, every distinct `(rule id, matched text)` pair is
listed once.

## Directory self-containment

The transcript tail-read and reverse-scan logic in `chat_dict_gate.py` is intentionally
**duplicated**, not imported, from
`agent-hooks/stop-completion-selfcheck/stop_selfcheck.py` — trimmed to the subset this hook
needs (assistant TEXT only, no `had_tool_use` tracking) and extended with the
retry-boundary kind above. This follows the catalog convention that each
`agent-hooks/<name>/` directory is a self-contained deployable unit (a descriptor + its
executable + a README, nothing cross-directory) — see `AGENTS.md`'s hook table. If
`stop_selfcheck.py`'s tail-read logic changes, check whether the same fix applies here; it
won't happen automatically.

Review raised this duplication (~130 lines shared with `stop_selfcheck.py`) as a drift
risk and suggested a small shared internal module. That is a reasonable idea for the
catalog as a whole, but it conflicts with the explicit, deliberate convention this task was
scoped against (directory self-containment, stated above and in `AGENTS.md`'s hook table) —
introducing a new shared-lib convention is a bigger, catalog-wide architectural decision
than this one hook should make unilaterally. Flagged as a follow-up worth a separate
ticket, not adopted here.

## Test

```bash
chmod +x chat_dict_gate.py

# Clean reply -> allow
cat > /tmp/t1.jsonl <<'EOF'
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"All good."}]}}
EOF
echo '{"event_id":"sess-1","args":{"transcript_path":"/tmp/t1.jsonl"}}' | ./chat_dict_gate.py; echo "exit=$?"
# -> {"hook_api":"agents-hooks/v1","decision":"allow"} exit=0

# Banned word -> block, quoting the rule (using the real dictionary's English-only
# "sycophancy-en" rule here so this example needs no non-English example text)
cat > /tmp/t2.jsonl <<'EOF'
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"is this fix correct?"}]}}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"You're absolutely right, this fix is correct."}]}}
EOF
echo '{"event_id":"sess-2","args":{"transcript_path":"/tmp/t2.jsonl"}}' | ./chat_dict_gate.py; echo "exit=$?"
# -> decision":"block", message names sycophancy-en, quotes "You're absolutely right", exit=10

cat ~/.cache/agent-tools/chat-dict-gate/firings.jsonl
# -> one JSON line per invocation above (allow, block)

CHAT_DICT_GATE_DISABLE=1 sh -c '
  echo "{\"event_id\":\"sess-3\",\"args\":{\"transcript_path\":\"/tmp/t2.jsonl\"}}" | ./chat_dict_gate.py; echo "exit=$?"'
# -> decision":"allow", exit=0 (kill switch — same violating transcript, but disabled)
```

Run the pytest suite from the repo root:

```bash
python -m pytest -q tests/test_chat_dictionary_gate.py
```

# model-error-fallback

An `agents-hooks/v1` hook that makes the **model-fallback** discipline automatic: it counts
repeated transient model errors and, past a threshold, switches the agent's executor down a
cross-harness chain instead of letting a throttle wedge the work.

## The chain

```
claude:fable  ->  claude:opus  ->  oc:GLM-5.2  ->  codex:gpt5.5  ->  omp:k3
```

`<harness>:<model>`. Falling within Claude (`fable` -> `opus`) is an in-process **model
swap**; crossing the harness boundary (`claude` -> `oc`/`codex`/`omp`) is a **re-dispatch**
of the unit of work to that harness as an executor (`opencode run` / `codex exec`). The chain
deliberately crosses harnesses so a whole-provider outage (an Anthropic throttle) doesn't
stall everything — the next step runs on a *different provider's quota*.

This is the same order the [`model-fallback` skill](../../skills/universal/model-fallback/SKILL.md)
mandates for humans/agents; the hook is the enforcement layer so it isn't a prose promise.

## One chain definition, every harness

The chain is read from **one** place — `lib/contracts/models.yaml` under `fallback_chain:`.
rig provisions this hook into cc (via `settings.json` + `cc_hook_bridge`), codex, opencode,
pi, and omp, and each reads the SAME manifest list, so every harness agrees on the priority. The
default baked into `fallback_chain.py` is the offline fallback for a host that can't load
the manifest — it mirrors the manifest, which stays the source of truth.

> Note on `model` validity: `harness` / `model` are free strings (tool wiring), NOT
> validated against the model board's `models:` list — the schema only checks shape, not
> that `gpt5.5` / `GLM-5.2` / `fable` resolve. A typo'd id loads fine and surfaces only when
> the target harness's own model selector rejects it at switch time. Keep the chain's ids in
> sync with what each harness accepts.

## How it counts and switches (the pure logic)

All of it lives in `fallback_chain.py` (pure, dependency-free, unit-tested):

- **Classify** each turn. Only a *transient model error* — rate-limit / overload / 5xx /
  "temporarily limiting" / overloaded / 429 — counts. A normal failure (a failing test,
  wrong code, a refusal) is **ignored** for the chain: switching executor wouldn't fix it
  and would waste reserve quota. (Same retryable-vs-fatal split as review-cli's resilience.)
- **Count** *consecutive* transient errors at the current step. On the Nth (`threshold`,
  default 3, env `AGENTTOOLS_FALLBACK_THRESHOLD`), drop to the next step.
- **Recover / return-to-top.** A successful turn resets the counter and promotes one step
  back toward the preferred model, so a transient throttle never pins the work on the
  last-resort executor forever.
- **Exhaustion.** At the last step there is nowhere left to fall — the hook surfaces that
  loudly (fail loud, never pretend success).

## Output contract

Advisory (`on_error: open`) — it **always allows**; it never blocks a turn. The instruction
rides in the protocol output:

```json
{
  "hook_api": "agents-hooks/v1",
  "decision": "allow",
  "message": "model-error-fallback: 3 transient errors on claude:fable: in-harness model swap -> claude:opus is now the active executor",
  "fallback": {
    "active": "claude:opus", "harness": "claude", "model": "opus",
    "kind": "fall", "switched": true, "crosses_harness": false,
    "exhausted": false, "reason": "..."
  }
}
```

`exhausted: true` is the structured "the chain is spent — fail loud" signal (a transient
error hit the threshold on the last-resort executor with nowhere left to fall), so a host
detects it without grepping `reason`.

`kind` is the DIRECTION (`none` / `fall` / `recover`). Whether the move is an in-process
model swap or a cross-harness change is the separate `crosses_harness` flag, computed from
the two steps' harnesses — so a **recovery that crosses a harness** (e.g. `oc:GLM-5.2` ->
`claude:opus`) is flagged too, not just a fall.

A cross-harness change carries a machine-readable target, but which key depends on the
direction, because the semantics differ:

- **`fall` → `redispatch_to: {harness, model}`** — the work FAILED on the old executor, so
  re-dispatch THIS unit of work to the new harness (shell out, re-run it there). Safe:
  nothing completed.
- **`recover` → `prefer_next: {harness, model}`** — recovery fires AFTER a successful turn,
  so the work is already done. The host must NOT re-run it (that would duplicate side
  effects); it should just prefer this executor for the NEXT unit of work. A separate key so
  a naive host can't accidentally re-dispatch finished work.

## State

The consecutive-error count is persisted per unit of work (keyed by `task_id` /
`event_id` / `session_id`) under `$AGENTTOOLS_FALLBACK_STATE_DIR` (temp-dir default), so the
count survives across turns. The write is atomic (temp file + `os.replace`). A
missing/corrupt snapshot resets to the top of the chain (logged) — safe, because the worst
case is re-walking from the preferred model. When a unit of work returns to the pristine top
(zero pending errors, preferred model), its state file is removed, so a healthy workload
leaves nothing behind.

> Known limitations (all low severity, documented so they're not surprises):
> - **Recovery needs an explicit success signal.** Return-to-top only fires on a turn the
>   host reports as clean (`success` truthy or `error: false`). A turn with *no* outcome
>   signal is a no-op, so a host that never signals success will leave a fallen task pinned
>   on the lower executor. If you want auto-recovery, signal success on clean turns.
> - **No TTL/sweep on the state dir.** A task that fell and never recovered keeps its state
>   file until something prunes it (a few small JSON files per stranded task id). A periodic
>   prune is a follow-up.
> - **No file lock.** State is read-modify-write per turn; two truly-concurrent turns of the
>   *same* task id could lose a count increment (last-writer-wins via atomic replace — no
>   corruption, just an undercount). Stop-hook turns are normally sequential, so this is rare.

## Event payload it reads

Host-mapped keys on `args`: `error` / `model_error` (bool), `success` (bool),
`error_text` / `detail` (the ERROR channel — a provider error string),
`task_id` / `session_id` (unit-of-work id).

> `output` / `message` are accepted in the payload but **deliberately NOT used** — not
> merely "not classified", fully ignored. A Stop hook's `output` is the agent's normal
> response, which routinely contains "rate limiting" / "throttle" / "overloaded" / "quota"
> in legitimate work; reading it would fire false fallbacks. Don't expect `output` to
> influence the decision.

Precedence: an explicit truthy `error` / `model_error` wins over `success` — a host that
maps `success` to a coarse "turn completed" must not be able to mask a real error and starve
the fallback. Both flags are read as truthy (`success: 1` and `error: 1` both count).

When there is no explicit error flag, an error is inferred ONLY from the error channel
(`error_text` / `detail`). With no signal at all (no flag, no error string) it is a no-op —
the chain is untouched.

## Installing

1. Set the descriptor's `cmd` to the absolute path of `model_error_fallback.py` and
   `chmod +x` it (rig does this at provision time).
2. Drop the descriptor into your harness's `stop` hook directory.
3. In Claude Code, `cc_hook_bridge` carries it on the `Stop` event (it is inert without
   the bridge, like every agent-hook — see `../README.md`).

## Tests

`tests/test_model_error_fallback.py` — the count/threshold/switch/recover/exhaustion state
machine, the transient-vs-normal classifier, manifest-chain building, snapshot round-trip,
and the hook's stdin/stdout protocol. Run:

```
uv run --with pytest --with pyyaml python -m pytest tests/test_model_error_fallback.py -q
```

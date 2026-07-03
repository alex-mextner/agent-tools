# lint-on-write — feed lint errors back the moment the agent writes them

**Point:** `post-write` · **Fail policy:** `on_error: open` (advisory) · **Priority:** 70
(after `format-on-write`'s 60, so the linter sees the formatted file)

## What it does

After the agent writes/edits a source file, this hook runs the repo's **configured
linter on just that file** and — when the linter reports findings — signals the v1 BLOCK
(exit 10) with the linter output as the message. On the `post-write` point that is
**feedback, not prevention**: the write already landed; the `cc_hook_bridge` translates it
into Claude Code's PostToolUse `{"decision": "block", "reason": …}`, which surfaces the
lint errors to the agent immediately instead of letting them pile up until pre-commit/CI.

Single-file scope keeps it fast (oxlint/ruff finish in milliseconds); a hard internal
timeout (`RUN_TIMEOUT_S = 8`, under the descriptor's `timeout_ms: 10000`) guarantees a
slow linter degrades to a warned no-op, never a stalled edit loop.

## Which linter runs — detection, not hardcoding

The linter comes from the repo's own configuration, checked in this order per extension
(first configured + available tool wins; repo-local bin always beats a global):

| files | candidates (in order) | config signal for a GLOBAL tool |
| ----- | --------------------- | ------------------------------- |
| `.js .jsx .mjs .cjs .ts .tsx .mts .cts` | `oxlint` → `biome lint` → `eslint` | `.oxlintrc.json` / package.json mention; `biome.json(c)`; `eslint.config.*` / `.eslintrc*` / package.json mention |
| `.py .pyi` | `ruff check` | `ruff.toml` / `.ruff.toml` / `[tool.ruff]` in pyproject.toml |

These config files are exactly what rig's `linters` area (`rig.yaml → linters.items`)
provisions into a repo — so declaring a linter in rig.yaml and `rig apply`-ing it is what
turns this hook on for that repo. No config → clean no-op. Only CODE files are linted;
`.json`/`.css`/`.md` styling is `format-on-write`'s job. Paths under
`node_modules/.git/dist/build/.venv/vendor/__pycache__` are skipped.

## Exit semantics

| linter result | hook answer |
| ------------- | ----------- |
| exit 0 (clean) | allow |
| exit 1 (findings) | **exit 10** + `message` = truncated linter output (→ PostToolUse feedback) |
| exit ≥2 / crash / timeout / missing | allow + stderr warning (tool error ≠ findings) |

Only ERRORS surface: linters exit 0 on warnings-only by default, so warnings never
interrupt the agent — deliberately (the feedback channel is for problems that would fail
the pre-commit/CI gate, not style nags). ESLint must be ≥ 8.53 (the `--no-warn-ignored`
flag); an older eslint rejects the flag → exit 2 → the tool-error row above (warned
allow), i.e. the hook silently degrades to a no-op rather than misfiring.

Every failure mode of the hook itself also resolves to allow (`on_error: open`): lint
feedback must never wedge the agent. The pre-commit git-hook remains the enforcing gate.

## Escape hatch

`NO_LINT_HOOK=1` skips the hook entirely (e.g. bulk mechanical rewrites where interim
states are intentionally non-lint-clean).

## Install

Via rig (`agent_hooks.all: true` installs the whole catalog): `rig apply` copies the
descriptor into `~/.claude/hooks/` with `cmd` rewritten to this script's absolute path.
It fires through the `cc_hook_bridge` PostToolUse registration
(matcher `Edit|Write|MultiEdit|NotebookEdit`) that rig writes into `settings.json`.

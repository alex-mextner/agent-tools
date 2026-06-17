# agenttools-errors

One **shared error/exit-code layer** for every CLI in the agent-tools ecosystem. Every tool —
`review`, `rig`, `tg`, `draw`, `3d`, `task` — wants the **same** shape for a failure: a
three-part message (**what** went wrong, **why**, **how** to fix it) plus a **stable, per-class
exit code** so a calling script can branch on the failure class. Instead of each tool
hand-rolling that (rig already grew its own `riglib/errors.py`; the others print a bare
`Error: failed` and `exit(1)`), this is the single shared copy.

**Stdlib only.** Zero runtime dependencies — `dataclasses` + `os`/`sys`/`shutil` (the last for
the one `shutil.which` dependency probe). So a consumer's `--help`/`--version` stays fast and
offline.

This is the **error-system v2** the roadmap calls for: "one shared `errors` + `help` module,
consumed by review/rig/tg/draw/3d/task".

## Exit codes (PUBLIC CONTRACT — scripts branch on these)

| Code | Constant | Meaning |
| --- | --- | --- |
| `0` | `EXIT_OK` | success |
| `1` | `EXIT_INTERNAL` | unexpected internal failure (a bug — see the traceback) |
| `2` | `EXIT_USAGE` (alias `EXIT_CONFIG`) | invalid argument or malformed config |
| `3` | `EXIT_DRIFT` | declared config and disk disagree (drift) |
| `4` | `EXIT_UNKNOWN_ITEM` | named item does not exist (typo / removed slot) |
| `5` | `EXIT_MISSING_TARGET` | a referenced path/binary/file is gone |
| `6` | `EXIT_NOT_A_REPO` | a repo-scoped command run outside a git repository |
| `7` | `EXIT_NETWORK` | a network/remote operation failed |
| `8` | `EXIT_PERMISSION` | a permission or authentication failure |
| `127` | `EXIT_MISSING_DEP` | a required external tool/binary isn't installed (shell convention) |

The lower numbers (`0/1/2/3/4/5/6/127`) match rig's existing contract, so **rig can adopt this
module without renumbering**. `EXIT_NETWORK=7` and `EXIT_PERMISSION=8` are **additions** beyond
rig's set — before a tool adopts this module, confirm it doesn't already use `7`/`8` for
something else, or its existing semantics would silently renumber. `EXIT_CONFIG` is an alias
**constant** for `EXIT_USAGE` (rig's spelling); the `ConfigError` **class**, however, is a
*subclass* of `UsageError`, not the same class — `except UsageError` catches a `ConfigError`,
but `except ConfigError` does **not** catch a plain `UsageError`. `EXIT_CODES` is the
`{code: (name, meaning)}` table for `--help` / docs rendering.

Changing a value is a **breaking change** — keep them stable.

## The structured error

```python
from agenttools_errors import UsageError, MissingDepError, guard, require_tool

def run() -> int:
    if not cfg.exists():
        raise UsageError(
            what=f"config not found: {cfg}",          # the symptom (one line)
            why="the --config path doesn't exist",     # root cause + context
            fix=f"create {cfg} or pass --config <path>" # a concrete command/edit
        )
    # shells out to an external tool — guard it, get a 127 + install hint on a miss:
    openscad = require_tool(
        "openscad", needed_for="to produce the mesh",
        install="brew install openscad", rerun="3d render model.scad",
    )
    ...
    return 0

raise SystemExit(guard(run))   # renders the block + returns the stable exit code
```

`guard(fn)` runs `fn`, renders any `AgentToolError` as the 3-part block (on stderr) and returns
its exit code; a **non**-`AgentToolError` (a real bug) is **not** swallowed — it propagates so
the traceback is visible (an unhandled crash → exit 1, distinct from a *diagnosed* problem ≥ 2).

Rendered:

```
error: `openscad` is not installed
  why: this command needs openscad to produce the mesh
  fix: install it, then re-run: 3d render model.scad
  install: brew install openscad
```

Color follows `NO_COLOR` / `FORCE_COLOR` / TTY (same precedence as `agenttools_help`).

## Error types

`AgentToolError` (base, exit 1) and the per-class subclasses that pin their exit code:
`UsageError`/`ConfigError` (2), `DriftError` (3), `UnknownItemError` (4), `MissingTargetError`
(5), `NotARepoError` (6), `NetworkError` (7), `PermissionDeniedError` (8), `MissingDepError`
(127). Each is a dataclass with `what` / `why` / `fix` / `install` fields.

## Builders & heuristics

- `unknown_item_error(category=, bad=, known=, config_path=, key=, removed=)` — builds the
  precise "unknown item" error: removed-slot → empty-catalog → did-you-mean → list-known, in
  that priority. Never prints the useless "known: none".
- `did_you_mean(bad, candidates)` — nearest candidate within an edit-distance threshold (caps
  at 3, ties break alphabetically), or `None`.
- `RemovedSlotRegistry` / `RemovedSlot` — each tool seeds its own registry of removed catalog
  slots so a lingering config reference cites the removal PR + the fix.
- `missing_dep_error(...)` / `require_tool(...)` — the missing-dependency family (127 + install
  hint).
- `missing_target_error(...)`, `not_a_repo_error(...)` — the remaining structured builders.

## Tests

```
uv run --with pytest python -m pytest tests/test_agenttools_errors.py -q
```

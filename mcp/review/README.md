# review MCP — multi-model code review / quorum / brainstorm / visual

This documents the MCP slot for a **read-only multi-model review tool**. The MCP server is
a thin wrapper around such a CLI; this README describes the capability it should expose, so
any agent can call it instead of relying on a single self-review.

> This is a slot description, not a vendored implementation. Point the wrapper at your own
> review CLI (or build one over the model providers you use). Nothing here ships a specific
> provider, endpoint, or key.

## Why this MCP exists

Three universal rules become *executable* once review is an MCP tool:

- **`ai-review-before-commit`** — run a review on the uncommitted diff before committing.
- **`gan-critic-loop`** — the critic is a *different* model than the generator, so the
  review imports an outside perspective instead of grading its own homework.
- **`visual-proof-cycle`** — a `--visual` mode has a vision model judge a rendered
  screenshot (keep / rollback), the verification half of the visual loop.

## Suggested tool surface

| Tool        | Purpose                                                                 |
| ----------- | ----------------------------------------------------------------------- |
| `review`    | Multi-model review of the current diff (or a path). Returns findings.   |
| `quorum`    | Pose a contested technical question to several models; return where they agree/disagree, with cited evidence. |
| `brainstorm`| Rotating expert personas explore an open design space across rounds.    |
| `visual`    | Vision-model verdict on a screenshot/render: keep vs rollback + reason. |

All read-only — review must never mutate the working tree. Each should emit machine-readable
output (`--json`) so agents and hooks can branch on the verdict, and a meaningful exit code
(see `../../skills/by-type/cli/structured-exit-codes`): e.g. exit `10` for a blocking
rollback verdict so it composes with the `agents-hooks/v1` block signal.

## Connecting to the hooks

- **`require-review-before-commit`** (agent-hook) — have the review tool touch the review
  marker on a successful run, so the commit gate knows a review happened. See
  `../../agent-hooks/require-review-before-commit/`.
- **Visual gate via `agents-hooks/v1`** — the `visual` mode pairs with a `pre-send` /
  `pre-write` hook that blocks shipping an unstyled/blank/broken render (exit 10 →
  rollback). The hook framework is documented in `../../agent-hooks/README.md`.

## Optional: notification / photo hook

A review tool that produces a visual verdict can route the annotated screenshot to a
notification channel (e.g. a chat message) on a rollback, so a human sees the failed render.
Keep any such integration behind config — never hardcode a channel id, token, or endpoint.

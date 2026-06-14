# AI review (CI) — run a review CLI on the PR diff, post findings

Generalizes the "an AI reviewer comments on every PR" pattern: a CI step runs a
**configurable** review CLI over the PR diff and posts the findings as a sticky PR comment.
**Advisory by default** — it informs a human, it does not auto-block the merge. An LLM
reviewer has false positives; gating on it directly is a trap (see the `ai-review-before-commit`
skill — treat findings as peer review, not gospel).

## Vendor-neutral by design

Nothing here hard-codes a model or provider. You point `AI_REVIEW_CMD` / `AI_REVIEW_TOOL`
at whatever reviewer you run — `codex exec review`, a `review`-style multi-model CLI,
`claude`, `gemini`, `opencode`, or your own script — and supply that tool's API key via a
secret. Two shorthands are built in (`codex`, `review`); anything else is a full custom
command.

## What's here

| File | For | Does |
| ---- | --- | ---- |
| `ai-review.sh` | **Any CI** + local pre-commit | Computes the diff (base…head), feeds it to the configured reviewer, prints + optionally files the findings. Diff is passed via `{DIFF_FILE}` (a temp-file path) or STDIN — never inlined as text (the diff is attacker-controlled). |
| `workflow.yml` | **GitHub Actions** | Runs `ai-review.sh` on each PR and posts a sticky comment via SHA-pinned `peter-evans` actions. |

## Setup (two things you must provide)

1. **Install your reviewer** — edit the `Install review CLI` step in `workflow.yml`
   (`npm i -g …`, `pipx install …`, a curl installer, etc.).
2. **Provide its API key** — set a repo/org **secret** and wire it into the run step's
   `env:`. Use a generic name (`AI_REVIEW_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`).

## Quick start (local)

```bash
# codex reviews the branch diff vs origin/main (the diff this script computes):
sh ci/ai-review/ai-review.sh

# a custom reviewer that takes a diff file:
AI_REVIEW_CMD='my-reviewer --diff {DIFF_FILE}' sh ci/ai-review/ai-review.sh

# local review of UNCOMMITTED work instead of the committed diff:
AI_REVIEW_CMD='codex exec review --uncommitted' sh ci/ai-review/ai-review.sh
```

## Reviews the computed diff (works in CI)

The script computes the PR diff (`base...head`) and feeds **that** to the reviewer (via
STDIN or `{DIFF_FILE}`). This matters: in CI the PR is already **committed**, so a
"review my uncommitted changes" command would see nothing. The built-in `codex` default
pipes the computed diff in, so it reviews the actual PR. For local pre-commit review of
*uncommitted* work, set `AI_REVIEW_CMD='codex exec review --uncommitted'`.

## Knobs

- `AI_REVIEW_TOOL` — `codex` is the only built-in default; for any other reviewer set
  `AI_REVIEW_CMD`.
- `AI_REVIEW_CMD` — full custom command; supports `{DIFF_FILE}` (temp-file path) or STDIN.
  The diff text is never inlined into the command (it's attacker-controlled — inlining into
  a shell `eval` would be an injection / key-exfiltration hole).
- `AI_REVIEW_BASE` / `AI_REVIEW_HEAD` — diff range (default `origin/main`…`HEAD`).
- `AI_REVIEW_OUT` — also write findings to this file.
- `AI_REVIEW_FAIL` — `1` to exit non-zero **only if the tool itself errors** (never on
  findings — advisory).

## Security — why `pull_request`, not `pull_request_target`

This triggers on `pull_request`, so a **fork** PR runs with a read-only token and **no
access to secrets** — your API key cannot be exfiltrated by malicious PR code. The trade-off:
it can't comment on fork PRs (no write token there), so the job is `if:`-skipped on forks.
Do **not** switch to `pull_request_target` to enable fork commenting unless you fully grasp
the secret-exfiltration risk of running PR-controlled code with a privileged token.

## Pinning

The `peter-evans/find-comment` and `create-or-update-comment` actions are SHA-pinned (with
the version in a trailing comment). `actions/checkout` likewise.

## Related

- Skill: `ai-review-before-commit` — the habit this automates.
- [`../../mcp/`](../../mcp/) — the multi-model `review` MCP slot, for an interactive panel.

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
AI_REVIEW_CMD='my-reviewer --uncommitted' sh ci/ai-review/ai-review.sh
```

This script is for CI: it reviews the diff it computes (`base...head`), which is empty
when your work is only staged, and it never writes the commit-gate marker. It is NOT the
way to satisfy `require-review-before-commit` before a local commit — for that, stage the
change and run `review diff --staged --task <CODE> -C <repo>`, which reviews the real
index and writes the marker itself. (The example here used to name a `codex exec` flag
that no longer exists; two detached agents died following that same dead command where
another file gave it as gate advice.)

## Reviews the computed diff (works in CI)

The script computes the PR diff (`base...head`) and feeds **that** to the reviewer (via
STDIN or `{DIFF_FILE}`). This matters: in CI the PR is already **committed**, so a
"review my uncommitted changes" command would see nothing. The built-in `codex` default
pipes the computed diff in, so it reviews the actual PR. For local review of work in
progress, point `AI_REVIEW_CMD` at a reviewer that reads the working tree itself — and
for the pre-commit gate specifically, use `review diff --staged --task <CODE> -C <repo>`,
which is the run that writes the gate's marker.

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

## Security — the secret-exfiltration trap (and how this avoids it)

This job holds an API-key secret. The trap most "AI review on PR" workflows fall into: they
trigger on `pull_request`, **check out the PR head, and run a script the PR can edit** — so
any contributor who can open a **same-repo branch** PR can rewrite that script and steal the
key. Skipping *forks* does **not** close that hole (same-repo branches still have it).

This workflow closes it by treating the PR's code as **data, never as code**:

- It uses `pull_request_target` and checks out the **base** branch — the script and install
  step that run are the **trusted base copies**, never the PR's.
- It fetches the PR diff (the PR head SHA) and feeds **that** to the reviewer — the diff is
  **read**, never executed.
- It runs **no build/test/install of PR code** (that would execute it under the privileged
  trigger). Keep it diff-only — do not add such a step.

Caveat of any base-run workflow: it does **not** run on the PR that first introduces it.
The reviewer reviews the diff regardless of fork-vs-branch, and can comment on both (the
base-context token has `pull-requests: write`).

## Pinning

The `peter-evans/find-comment` and `create-or-update-comment` actions are SHA-pinned (with
the version in a trailing comment). `actions/checkout` likewise.

## Related

- Skill: `ai-review-before-commit` — the habit this automates.
- [`../../mcp/`](../../mcp/) — the multi-model `review` MCP slot, for an interactive panel.

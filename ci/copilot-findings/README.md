# Copilot AI findings — surface (and optionally gate)

**Honest answer first: there is no single "Copilot findings" API.** Copilot surfaces its
findings through **two existing, separate** GitHub surfaces. This slot reads **both** — it's
a surfacing/aggregation tool over real APIs, not access to a hidden feed.

## The two surfaces

| Surface | What it is | API used | Availability |
| ------- | ---------- | -------- | ------------ |
| **Copilot code review** | When you request a review from Copilot, its comments arrive as ordinary **PR review comments** authored by `copilot-pull-request-reviewer[bot]`. | `GET repos/{o}/{r}/pulls/{pr}/comments`, filtered by that author | Any repo with Copilot code review **requested** (manually or via repo setting "automatically request Copilot review"). |
| **Copilot Autofix** | Suggested fixes attached to **code-scanning alerts**. The "finding" is the alert; the "Copilot" part is the autofix suggestion on it. | `GET repos/{o}/{r}/code-scanning/alerts?ref=…` | Needs **GitHub Advanced Security** (private repos) or a **public** repo. |

So "gate on Copilot findings" decomposes into "gate on Copilot **review comments**" and
"gate on **code-scanning alerts**" — both of which already have APIs. This script does both.

## What's here

| File | Does |
| ---- | ---- |
| `copilot-findings.sh` | Lists Copilot review comments + open code-scanning alerts on a PR; prints them; `GATE=1` fails if any exist. |
| `workflow.yml` | Runs the script per PR as an **advisory** check (log only unless `GATE=1`). |

## Quick start

```bash
cp ci/copilot-findings/workflow.yml .github/workflows/copilot-findings.yml
# The workflow runs `bash ci/copilot-findings/copilot-findings.sh`, so the script must be
# present at that path — vendor the ci/ dir, or copy the script and adjust the run: path:
cp ci/copilot-findings/copilot-findings.sh .github/scripts/copilot-findings.sh
# Local:
PR_NUMBER=123 sh ci/copilot-findings/copilot-findings.sh
```

## Knobs

- `COPILOT_REVIEW_BOT` — the review-comment author treated as Copilot (default
  `copilot-pull-request-reviewer[bot]`; GitHub may rename — verify with
  `gh api repos/{o}/{r}/pulls/{pr}/comments --jq '.[].user.login' | sort -u`).
- `GATE=1` — fail the check if any Copilot finding is present. **Default off** — Copilot
  review is advisory (false positives happen); surfacing beats hard-gating. If you do gate,
  gate on the **code-scanning** half (real security alerts), not the review-comment half.

## The manual flow (when the API path doesn't apply)

If your repo has neither Copilot review requested nor code scanning (no GHAS, private):

1. **Copilot code review** — enable it: repo/org Settings → Copilot → "Automatically
   request Copilot review", or click *Reviewers → Copilot* on each PR. Findings then appear
   as review comments → the [`../review-threads/`](../review-threads/) gate makes you
   resolve them before merge (that's your enforcement, no special Copilot API needed).
2. **Copilot Autofix** — needs code scanning. Without GHAS on a private repo you can't get
   it; use [`../codeql/`](../codeql/) self-gate + [`../sast/`](../sast/) for equivalent
   coverage and the [`../ai-review/`](../ai-review/) slot for an AI reviewer you fully
   control (model + prompt + key).

## Honest limitations

- No webhook/event fires specifically for "Copilot finished its review" — the workflow runs
  on PR open/sync and may run before Copilot posts; re-run (`workflow_dispatch`) to refresh.
- Bot login names and the autofix API shape are GitHub-controlled and can change; the script
  degrades gracefully (prints "unavailable") rather than failing the job spuriously.
- For a reviewer you fully own (model, prompt, gating policy), prefer
  [`../ai-review/`](../ai-review/) over depending on Copilot's surfaces.

## When to use

When your org already uses Copilot review/Autofix and you want those findings **visible in
CI** (and, for the security half, optionally gating) alongside the rest of this suite.

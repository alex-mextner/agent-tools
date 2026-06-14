# CodeQL (CI) — GitHub's semantic code-analysis engine

**Standard engine = [github/codeql-action](https://github.com/github/codeql-action).**
CodeQL is GitHub's own SAST. Use it for deep, semantic security analysis (taint tracking,
data-flow) that pattern scanners (see [`../sast/`](../sast/), semgrep) can't do. Run both —
they are complementary, not redundant.

## Two variants — pick by your repo's plan

| File | Use when | How it gates |
| ---- | -------- | ------------ |
| `workflow.yml` | **Public repo**, OR private repo **with** GitHub Advanced Security | Uploads SARIF to the Code Scanning dashboard; gate merges via branch protection on the `Code scanning results / CodeQL` check. |
| `workflow-selfgate.yml` | **Private repo WITHOUT** GitHub Advanced Security | Runs the same analysis, keeps the SARIF local, and **fails the job in-line on findings** — no dashboard, no GHAS, $0. |

The self-gate variant exists because, without GHAS, the dashboard upload returns
`403 "Advanced Security must be enabled"`. Rather than lose CodeQL entirely on a private
repo, the self-gate parses the SARIF itself and fails the check. This is the high-value,
non-obvious piece — most CodeQL templates silently assume GHAS.

## Quick start

```bash
# Public repo / GHAS:
cp ci/codeql/workflow.yml .github/workflows/codeql.yml

# Private repo, no GHAS:
cp ci/codeql/workflow-selfgate.yml .github/workflows/codeql.yml
```

Then edit the `language` matrix for your stack.

## Knobs

- **Languages** — the `language:` matrix. `javascript-typescript` is the *combined* JS+TS
  language (don't also list them separately). Compiled languages (`java-kotlin`, `cpp`,
  `csharp`, `go`) typically need `build-mode: autobuild`.
- **Query suite** — `queries: security-extended` (broad). Use `security-and-quality` for
  even more (adds maintainability), or drop the line for the default suite.
- **Severity floor** (self-gate only) — `GATE_LEVELS` env. `"error warning"` (default,
  recommended) gates on error+warning; `error` gates only the highest severity.
- **Suppression** (self-gate only) — `// codeql[<rule-id>]` on the flagged line or the one
  above it suppresses a single justified false positive. Greppable and reviewable — not a
  silent baseline.

## Pinning / supply-chain

`github/codeql-action` is a **first-party GitHub action** pinned by **major tag (`@v4`)** —
GitHub's own docs recommend this because the bundled CodeQL CLI rotates frequently and a
stale SHA pins you to old analysis. If your supply-chain policy requires SHA pins even for
first-party actions, resolve `github/codeql-action`'s SHA and pin it, accepting the manual
bump cost. `actions/checkout` IS SHA-pinned here.

## When to use

Always, for any repo with real source code — CodeQL is the deepest free SAST GitHub offers.
Pair it with semgrep ([`../sast/`](../sast/)) for fast pattern rules and with secret-scan
([`../secret-scan/`](../secret-scan/)) for credentials.

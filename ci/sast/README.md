# SAST (CI) — semgrep

**Static-analysis standard (OSS) = [semgrep](https://semgrep.dev).** Fast, pattern-based,
multi-language, with a large community ruleset. This slot ships it as a pinned GitHub
Action **and** a generic shell runner.

## semgrep vs CodeQL — run both

| | semgrep ([here](.)) | CodeQL ([`../codeql/`](../codeql/)) |
| --- | --- | --- |
| Engine | Pattern / syntactic + light data-flow | Deep semantic taint & data-flow |
| Speed | Seconds | Minutes |
| Rules | Huge community registry (`p/...` packs) | GitHub's curated query suites |
| Best at | Broad coverage, custom org rules, fast PR feedback | Real injection/taint bugs across functions |

They catch different bug classes. A serious setup runs **both** (plus secret-scan).

## What's here

| File | For | Does |
| ---- | --- | ---- |
| `workflow.yml` | **GitHub Actions** | `semgrep/semgrep-action`, **SHA-pinned**. Drop into `.github/workflows/`. Block tier (fails on findings). |
| `sast.sh` | **Any other CI** (GitLab/Jenkins/Buildkite/cron) + local pre-push | POSIX-sh runner; installs semgrep if missing; block + opt-in warn tier. |

## Quick start

```bash
# GitHub Actions:
cp ci/sast/workflow.yml .github/workflows/sast.yml

# Other CI — one shell step:
sh ci/sast/sast.sh
```

## Knobs

- **Ruleset** — `config: auto` (workflow) / `SEMGREP_CONFIG` (shell). `auto` pulls registry
  rules for detected languages. Swap for `p/security-audit`, `p/owasp-top-ten`, `p/secrets`,
  `p/ci`, a local `.semgrep.yml`, or several space-separated.
- **Tier** — block (default; fails on findings — correct for a required CI check) vs warn
  (`SAST_WARN=1` in shell, or `continue-on-error: true` in the workflow).
- **No token needed** for OSS rules. `SEMGREP_APP_TOKEN` is only for Semgrep's managed
  AppSec Platform (optional).

## False positives — the escape hatch

1. **Inline** — a `// nosemgrep` (or `# nosemgrep`) comment on the flagged line; add the
   rule id (`// nosemgrep: rule-id`) to suppress only that one rule.
2. **File/dir** — a `.semgrepignore` at the repo root (gitignore syntax).
3. Never delete the CI step to "fix" a finding — triage it.

## Pinning / supply-chain

`semgrep/semgrep-action` is pinned to a **commit SHA** (`# v1`), not a moving tag. Bump
deliberately. `actions/checkout` likewise.

## When to use

Every repo with source code, for fast PR-time static analysis. It's the cheap, broad net;
CodeQL is the deep net.

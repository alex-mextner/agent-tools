# Dependency audit (free, no GHAS)

Block a PR/push whose dependency tree has a known **high+ vulnerability** — using only OSS
tooling, no GitHub Advanced Security and no paid action.

- **`workflow.yml`** — GitHub Actions gate. Runs `dep-audit.sh` (below). Drop into
  `.github/workflows/`.
- **`dep-audit.sh`** — generic multi-ecosystem audit (bun / npm / pnpm / yarn audit,
  pip-audit, cargo-audit, govulncheck). Audits the **whole tree**. Works in any CI or locally.

## Why not `actions/dependency-review-action`?

GitHub's `dependency-review-action` **requires GitHub Advanced Security (GHAS, paid) on
private repos** — it hard-fails `"Dependency review is not supported on this repository …
requires GitHub Advanced Security"` *even when the (free) Dependency Graph is enabled*. (The
graph alone is not enough; the action gates on GHAS for private repos.) So we dropped it.

`dep-audit.sh` covers the same ground for free, and arguably better:

| | `dependency-review-action` (GHAS) | `dep-audit.sh` (OSS) |
|---|---|---|
| Cost | GHAS (paid) on private repos | free |
| Scope | the PR **diff** (new deps only) | the **whole tree** (incl. pre-existing) |
| Vuln source | GitHub Advisory DB | each ecosystem's auditor (npm/bun → GitHub Advisory DB; pip-audit → OSV; cargo-audit → RustSec; govulncheck → Go DB) |
| **License policy** | **allow/deny licenses ✅** | **not covered ⚠️** |

The **one** thing we lose is **license-policy enforcement** (deny AGPL/GPL etc.). Fill it
with an OSS license gate (`license-checker` / `cargo-deny` / `pip-licenses`) — see
`ci/license-policy/` and agent-tools#21.

## Quick start

```bash
cp ci/dependency-review/workflow.yml .github/workflows/dependency-review.yml
# add the toolchain setup your ecosystems need (see the comments in workflow.yml);
# ubuntu-latest already ships node+npm so npm-only repos need nothing extra.

# Run the same audit locally / in any other CI:
sh ci/dependency-review/dep-audit.sh
```

> If your repo already has its own dependency-audit job, you don't need this one — don't
> double up.

## Knobs (`dep-audit.sh`)

- `DEP_AUDIT_LEVEL` — `low|moderate|high|critical` (default `high`).
- `DEP_AUDIT_ALLOW_MISSING` — `1` to fail-OPEN when a manifest is found but its scanner
  isn't installed. **Default `0` (fail-CLOSED):** a detected ecosystem with no usable scanner
  is a gate failure, not a silent skip — otherwise "no audit ran" masquerades as "no vulns".
  Install the named tool in your CI image, or set this to `1` to intentionally skip.

## Relationship to other slots

- [`../secret-scan/`](../secret-scan/) — credentials, not dependencies.
- [`../sast/`](../sast/) & [`../codeql/`](../codeql/) — your *own* code, not third-party deps.
- [`../license-policy/`](../license-policy/) — dependency **licenses** (the gap above).
- This slot — third-party **dependency vulnerabilities**.

## Note on lockfiles audit tools miss

GitHub's Dependabot does not parse every lockfile format (e.g. `bun.lock`). `dep-audit.sh`
runs the ecosystem's own tool (`bun audit` reads `bun.lock` directly), so vulnerabilities
invisible to Dependabot still get caught.

## When to use

Any repo that pulls third-party dependencies — i.e. nearly all of them.

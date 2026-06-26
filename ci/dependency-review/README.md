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

## Tamper-resistance (agent-tools#129)

This is a merge-**blocking** gate, so it must not run a copy of the script the PR can edit
(a PR could otherwise weaken the very gate it has to pass). Like `ci/leftover-grep` and
`ci/review-threads`, `workflow.yml` runs under **`pull_request_target`**: the **base-branch
(trusted) copy** of both the workflow and `dep-audit.sh` run; the PR's version is ignored.
The PR's **manifests/lockfiles** are fetched as **DATA** into a side worktree and audited
there — the native auditors (`npm`/`pnpm`/`yarn`/`bun audit`, `pip-audit`, `cargo-audit`)
only **read** the lockfile and query an advisory DB; they do **not** run the package's
install/lifecycle scripts, so no PR-controlled code executes. **Hard rule:** never add an
`npm install` / `bun install` / build step or check the PR head onto the workspace — that
would execute PR code under the privileged trigger. Like any base-run gate, it does not run
on the PR that first introduces it.

## Quick start

```bash
cp ci/dependency-review/workflow.yml .github/workflows/dependency-review.yml
# `setup-bun` ships ENABLED (bot repos use bun.lock; without it a bun.lock repo fail-CLOSES
# because dep-audit.sh can't find `bun`). Add the toolchain for your other ecosystems (see
# the comments in workflow.yml); ubuntu-latest already ships node+npm so npm-only repos need
# nothing extra — trim setup-bun if you have no bun.lock.

# Run the same audit locally / in any other CI (optional dir arg, default '.'):
sh ci/dependency-review/dep-audit.sh           # audit the current tree
sh ci/dependency-review/dep-audit.sh path/to/checkout   # audit another checkout's lockfiles
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

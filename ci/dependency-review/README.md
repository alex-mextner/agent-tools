# Dependency review + license check

Block a PR that **introduces** a vulnerable or disallowed-license dependency, and audit the
deps already in the tree.

- **`workflow.yml`** — GitHub's official **`actions/dependency-review-action`**. PR-time
  gate: diffs the dependency manifests/lockfiles and fails on newly added deps with known
  vulns (≥ a severity threshold) or a denied license. "Don't let it IN."
- **`dep-audit.sh`** — generic multi-ecosystem audit of what's **already** there (bun / npm
  / pnpm / yarn / pip-audit / cargo-audit / govulncheck). For non-GitHub CI, or a repo
  without the Dependency Graph.

## Availability — read this first

`dependency-review-action` needs GitHub's **Dependency Graph**:

- **Public repos** — on by default. Use `workflow.yml`.
- **Private repos** — needs **GitHub Advanced Security** (Settings → Code security →
  Dependency graph). Without GHAS the action errors → use `dep-audit.sh` in a plain CI step
  instead.

## Quick start

```bash
# Public / GHAS repo — the PR-time "don't let a bad dep in" gate:
cp ci/dependency-review/workflow.yml .github/workflows/dependency-review.yml

# Any repo — audit existing deps (CI step or local):
sh ci/dependency-review/dep-audit.sh
```

## Knobs

**workflow.yml:**
- `fail-on-severity` — `low|moderate|high|critical` (default `high`).
- `allow-licenses` (strict allow-list) **or** `deny-licenses` (block-list). Set one. The
  template denies copyleft (AGPL/GPL) — adjust to your policy.
- `comment-summary-in-pr` — `always | on-failure | never`.

**dep-audit.sh:**
- `DEP_AUDIT_LEVEL` — `low|moderate|high|critical` (default `high`).

## Relationship to other slots

- [`../secret-scan/`](../secret-scan/) — credentials, not dependencies.
- [`../sast/`](../sast/) & [`../codeql/`](../codeql/) — your *own* code, not third-party deps.
- This slot — third-party **dependencies**: new ones at PR time (action) and existing ones
  (script). Together they cover "secrets + my code + my deps."

## Note on lockfiles audit tools miss

GitHub's Dependabot does not parse every lockfile format (e.g. `bun.lock`). The `dep-audit.sh`
fallback runs the ecosystem's own tool (`bun audit` reads `bun.lock` directly), so
vulnerabilities invisible to Dependabot still get caught. Keep both.

## When to use

Any repo that pulls third-party dependencies — i.e. nearly all of them.

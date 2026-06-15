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
- **Private repos** — the graph must be turned on at **Settings → Code security →
  Dependency graph**. This is **free** and does **not** require GitHub Advanced Security
  (GHAS is only needed for the Code Scanning *dashboard*, not the graph). Until it's on,
  `dependency-review-action` would hard-fail with "Dependency review is not supported on
  this repository".

**Graceful degradation (built in):** `workflow.yml` no longer hard-fails on a graph-less
repo. A preflight step probes the Dependency Graph (the repo's SBOM endpoint 200s when it's
enabled, 404s when not) and **skips the gate cleanly with a notice** when the graph is off —
the job stays green and the notice links to the enable page. The instant you enable the
graph, the gate goes live on the next run with no edit. While it's off, run `dep-audit.sh`
(below) in a plain CI step to still audit existing deps.

> Recommended: have your provisioning tool (e.g. `rig apply`) enable the graph on apply —
> `gh api -X PATCH repos/{owner}/{repo} -F security_and_analysis.dependency_graph.status=enabled`
> (also enable Dependabot vulnerability alerts) — so the gate is live from day one instead
> of skipping.

## Quick start

```bash
# Any repo — the PR-time "don't let a bad dep in" gate. Runs where the Dependency Graph is
# enabled, skips cleanly (with a notice + enable link) where it isn't:
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
- `DEP_AUDIT_ALLOW_MISSING` — `1` to fail-OPEN when a manifest is found but its scanner
  isn't installed. **Default `0` (fail-CLOSED):** a detected ecosystem with no usable scanner
  is a gate failure, not a silent skip — otherwise "no audit ran" masquerades as "no vulns".
  Install the named tool in your CI image, or set this to `1` to intentionally skip.

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

# OSS license-policy gate (free, no GHAS)

Block a PR/push whose dependency tree pulls in a **deny-listed (copyleft) license** —
GPL / AGPL / LGPL / MPL / EPL / SSPL / CC-BY-NC and friends — using only OSS tooling, no
GitHub Advanced Security and no paid action. This is the **license** half of GitHub's
`dependency-review-action`; the **vulnerability** half lives in
[`../dependency-review/`](../dependency-review/).

- **`workflow.yml`** — GitHub Actions gate. Runs `license-audit.sh` (below). Drop into
  `.github/workflows/`.
- **`license-audit.sh`** — generic multi-ecosystem license scan (node `license-checker`,
  python `pip-licenses`, rust `cargo-deny`, go `go-licenses`). Scans the **whole tree**.
  Works in any CI or locally.

## Why not `actions/dependency-review-action`?

It enforces an allow/deny **license** policy — but **requires GitHub Advanced Security (GHAS,
paid) on private repos**, the same wall that made us drop it for the vulnerability gate (see
[`../dependency-review/`](../dependency-review/)). `license-audit.sh` covers the same ground
for free:

| | `dependency-review-action` (GHAS) | `license-audit.sh` (OSS) |
|---|---|---|
| Cost | GHAS (paid) on private repos | free |
| Scope | the PR **diff** (new deps only) | the **whole tree** (incl. pre-existing) |
| Engine | GitHub's license metadata | each ecosystem's reporter (license-checker / pip-licenses / cargo-deny / go-licenses) |
| Policy | allow/deny list | **default-deny-copyleft** (deny pattern + name allow-list) |

## Policy model: default-deny-copyleft

The whole license universe is **allowed** except a **deny-list of copyleft families**
(`LICENSE_DENY_PATTERN`). Permissive licenses (MIT / BSD / Apache / ISC / …) pass; a
dependency whose declared license matches the deny pattern **fails the gate** until a human
reviews it.

Deny-by-**pattern** rather than allow-by-**list** on purpose: a hard allow-list rejects every
new permissive SPDX id nobody enumerated yet (false-positive churn on each new dep), while the
copyleft deny-list is the smaller, more stable surface to maintain. An **undeclared/UNKNOWN**
license is a violation by default — you can't prove an unstated license is compliant.

## Quick start

```bash
cp ci/license-policy/workflow.yml .github/workflows/license-policy.yml
# install the license reporter for each ecosystem your repo uses (see workflow.yml comments):
#   node   -> npm install -g license-checker
#   python -> pipx install pip-licenses
#   rust   -> cargo install cargo-deny   (carries its own deny.toml policy)
#   go     -> go install github.com/google/go-licenses@latest

# Run the same scan locally / in any other CI:
sh ci/license-policy/license-audit.sh
```

> If your repo already enforces licenses (its own `cargo-deny`, or a paid GHAS action), you
> don't need this one — don't double up.

## Knobs (`license-audit.sh`)

- `LICENSE_DENY_PATTERN` — case-insensitive ERE matched against each dep's license string; a
  match is a violation. Default denies the copyleft families above. Relax it if your policy
  permits weak copyleft (e.g. drop `LGPL`/`MPL`/`EPL`).
- `LICENSE_ALLOW` — space/comma-separated dependency **names** to exempt (e.g. a GPL
  build-time-only tool you've cleared). Exact, case-insensitive.
- `LICENSE_ALLOW_MISSING` — `1` to fail-OPEN when a manifest is found but its license reporter
  isn't installed, **or when the reporter runs but emits zero records** (usually deps not
  installed). **Default `0` (fail-CLOSED):** a detected ecosystem that produces no scan is a
  gate failure, not a silent skip — otherwise "no scan ran" masquerades as "all clear".
- `LICENSE_UNKNOWN_OK` — `1` to treat an UNKNOWN/undeclared license as allowed. **Default `0`:**
  an undeclared license is a violation.

### Caveats

- **Rust uses its own policy.** The `LICENSE_DENY_PATTERN` / `LICENSE_ALLOW` /
  `LICENSE_UNKNOWN_OK` knobs apply to node/python/go only. The rust path delegates to
  `cargo-deny`, whose license policy lives in **your repo's `deny.toml`** (`[licenses]`
  section). The gate **requires a `deny.toml`** and fails closed without one — cargo-deny's
  built-in default is permissive and would pass copyleft silently.
- **Python scans the installed environment.** `pip-licenses` reports the licenses of packages
  *installed in the active environment*, so the CI step must `pip install` the project's deps
  first for the scan to reflect the real tree. The gate fails closed if the reporter finds
  nothing (see `LICENSE_ALLOW_MISSING`).
- **The deny match is a heuristic on the license STRING.** It matches SPDX ids (`GPL-3.0`),
  glued short forms (`GPLv3`), and full classifier names (`GNU General Public License`). It can
  over-match an unrelated string that contains a copyleft token — use `LICENSE_ALLOW` to clear
  a false positive, or tighten `LICENSE_DENY_PATTERN`.

## Relationship to other slots

- [`../dependency-review/`](../dependency-review/) — dependency **vulnerabilities** (the
  other half of the GHAS dependency-review gap).
- [`../secret-scan/`](../secret-scan/) — credentials, not dependencies.
- [`../sast/`](../sast/) & [`../codeql/`](../codeql/) — your *own* code, not third-party deps.
- This slot — dependency **licenses**.

## When to use

Any repo that ships or distributes software built on third-party dependencies and must keep
its license obligations clean (i.e. almost any product repo). Pure-internal tools with no
distribution can skip it.

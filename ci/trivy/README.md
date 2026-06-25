# Trivy (CI) — filesystem vuln / secret / misconfig scan

**Scanner standard (OSS) = [Trivy](https://trivy.dev)** (Aqua Security). One tool, several bug
classes in a single `trivy fs` pass over the checked-out repo:

- **vuln** — OS + language **dependency** CVEs (reads your lockfiles/manifests);
- **secret** — hardcoded credentials in the tree;
- **misconfig** — IaC / Dockerfile / Kubernetes misconfigurations.

This slot ships it as a **SHA-pinned GitHub Action** and a **generic shell runner**, so a repo
that today carries a bespoke Trivy step gets the same gate from the catalog instead of
copy-pasting one (agent-tools#24).

## What's here

| File | For | Does |
| ---- | --- | ---- |
| `workflow.yml` | **GitHub Actions** | `aquasecurity/trivy-action`, **SHA-pinned**. `trivy fs` scan, drop into `.github/workflows/`. Block tier (fails on HIGH/CRITICAL). |
| `trivy-scan.sh` | **Any other CI** (GitLab/Jenkins/Buildkite/cron) + local pre-push | POSIX-sh runner; installs Trivy if missing; same block + opt-in warn tier. |

## Quick start

```bash
# GitHub Actions:
cp ci/trivy/workflow.yml .github/workflows/trivy.yml

# Other CI — one shell step:
sh ci/trivy/trivy-scan.sh
```

## Knobs

| Concern | Workflow | Shell |
| --- | --- | --- |
| Scanners | `scanners: vuln,secret,misconfig` | `TRIVY_SCANNERS` |
| Fail severity | `severity: HIGH,CRITICAL` | `TRIVY_SEVERITY` |
| Target path | `scan-ref: .` | `TRIVY_TARGET` (default `.`) |
| Ignore unfixed CVEs | `ignore-unfixed: true` | `TRIVY_IGNORE_UNFIXED` (default `1`; only the exact string `1` enables it — anything else is off) |
| Tier | `exit-code: '1'` (block) / `'0'` (warn) | `TRIVY_WARN=1` for warn |
| Skip vendored dirs | `skip-dirs: node_modules,vendor` | `TRIVY_EXTRA="--skip-dirs node_modules,vendor"` |
| Auto-install | — | `TRIVY_AUTOINSTALL` (default `0` — fail closed; `1` opts into unpinned `curl\|sh`) |
| Auto-install dir | — | `TRIVY_INSTALL_DIR` (default `${TMPDIR:-/tmp}/trivy-bin` — absolute, outside the scanned tree; only with `TRIVY_AUTOINSTALL=1`. A relative override is normalized under cwd — the append-to-PATH rule, not the location, is the shadowing defense) |

`ignore-unfixed` keeps the gate actionable: a CVE with no released fix can't be fixed by you,
so failing on it only teaches people to bypass the gate. Flip it off (`TRIVY_IGNORE_UNFIXED=0`
/ `ignore-unfixed: false`) if your policy requires flagging unfixed CVEs too.

**Warn tier never fails the build — including on scanner errors.** In the **shell** runner,
`TRIVY_WARN=1` exits 0 unconditionally after surfacing trivy's output, so neither a finding
*nor* an operational trivy error (DB unreachable, a bad flag) fails the build. In the
**workflow**, `exit-code: '0'` only suppresses the *findings* exit — an operational error still
fails the step; for the same unconditional guarantee use **`continue-on-error: true`** on the
step instead. Block tier (the default) fails on *any* non-zero trivy exit — a finding or a
scanner error — so a broken scan can't pass as clean.

## False positives — the escape hatch

1. **`.trivyignore`** at the repo root — one CVE id / finding id per line (with a comment
   noting why it's accepted). Trivy reads it by default.
2. Inline secret/misconfig suppressions via Trivy's `# trivy:ignore:<id>` annotations.
3. Never delete the CI step to "fix" a finding — triage it.

## Relationship to other slots

- [`../secret-scan/`](../secret-scan/) — **gitleaks**, credentials only. Trivy's `secret`
  scanner overlaps; run gitleaks as the dedicated credential gate and Trivy for the broader
  vuln+misconfig net. They're complementary, not redundant.
- [`../dependency-review/`](../dependency-review/) — dependency **vulnerabilities** via each
  ecosystem's native auditor. Trivy's `vuln` scanner is an alternative engine (its own DB);
  pick one as the dep-vuln gate, or run both for defense in depth.
- [`../license-policy/`](../license-policy/) — dependency **licenses** (Trivy does not gate
  licenses here).
- [`../sast/`](../sast/) & [`../codeql/`](../codeql/) — your **own source code**, not the
  dependency/IaC tree.

## Pinning / supply-chain

`aquasecurity/trivy-action` is pinned to a **commit SHA** (`# v0.36.0`), not a moving tag;
`actions/checkout` likewise. Bump deliberately.

> **Shell-runner caveat:** the shell runner does **not** auto-install Trivy by default — if
> Trivy is missing it **fails closed** (exit 2) with an install hint. Set `TRIVY_AUTOINSTALL=1`
> to opt into the convenience fallback, which fetches the upstream `install.sh` from the `main`
> branch (`raw.githubusercontent.com/.../main/contrib/install.sh`) — **neither the installer
> script nor the trivy binary version it pulls is pinned** (it grabs the latest release),
> unlike the workflow where the *action* is SHA-pinned. Auto-install is off by default because
> running an unpinned `curl | sh` on an untrusted (e.g. fork-PR) runner is a supply-chain risk
> for a security gate. For a hardened CI image, install Trivy ahead of time (`brew install
> trivy`, a pinned release tarball, or the GitHub Action) so the runner finds it on PATH.

## When to use

Any repo that pulls dependencies, builds container images, or carries IaC — i.e. nearly all
of them. It's the broad cross-class net; pair it with the source-code SAST slots.

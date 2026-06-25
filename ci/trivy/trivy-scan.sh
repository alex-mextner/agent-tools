#!/usr/bin/env sh
# Trivy filesystem scan — generic shell runner for any CI (GitLab / Jenkins / Buildkite / cron)
# or a local pre-push check. Mirrors ci/trivy/workflow.yml so non-GitHub CI is first-class.
#
# Standard engine = Trivy (https://trivy.dev). Runs `trivy fs` over the repo, scanning for
# dependency/OS vulnerabilities, hardcoded secrets, and IaC/Dockerfile misconfigurations in
# one pass, and EXITS NON-ZERO on a finding at/above the severity threshold (block tier — the
# right CI default). Set TRIVY_WARN=1 to report findings without failing (warn tier).
#
# Knobs (env):
#   TRIVY_SCANNERS   comma-list of scanners. Default "vuln,secret,misconfig".
#   TRIVY_SEVERITY   comma-list of severities to FAIL on. Default "HIGH,CRITICAL".
#   TRIVY_TARGET     path to scan. Default "." (repo root).
#   TRIVY_IGNORE_UNFIXED  "1" (default) = don't fail on a CVE with no fix available yet;
#                    "0" = fail on unfixed CVEs too.
#   TRIVY_WARN       "1" = never fail the build (warn tier). Default block.
#   TRIVY_EXTRA      extra flags passed through to `trivy fs`.
#   TRIVY_AUTOINSTALL  "1" = allow auto-install of trivy when it's missing (unpinned upstream
#                    curl|sh). Default 0 = fail closed with an install hint (safer for a gate,
#                    esp. on fork PRs). Prefer pre-installing trivy on the runner.
#   TRIVY_INSTALL_DIR  where the auto-installer drops the trivy binary (only with
#                    TRIVY_AUTOINSTALL=1). Default "${TMPDIR:-/tmp}/trivy-bin" — an ABSOLUTE path
#                    OUTSIDE the scanned tree (a relative ./bin would let a repo-local binary
#                    shadow PATH). A relative override is normalized to absolute.
#
# Usage:
#   sh ci/trivy/trivy-scan.sh
#   TRIVY_SEVERITY="CRITICAL" sh ci/trivy/trivy-scan.sh
#   TRIVY_WARN=1 sh ci/trivy/trivy-scan.sh
set -eu

TRIVY_SCANNERS="${TRIVY_SCANNERS:-vuln,secret,misconfig}"
TRIVY_SEVERITY="${TRIVY_SEVERITY:-HIGH,CRITICAL}"
TRIVY_TARGET="${TRIVY_TARGET:-.}"
TRIVY_IGNORE_UNFIXED="${TRIVY_IGNORE_UNFIXED:-1}"
TRIVY_WARN="${TRIVY_WARN:-0}"
TRIVY_EXTRA="${TRIVY_EXTRA:-}"

ensure_trivy() {
  if command -v trivy >/dev/null 2>&1; then return 0; fi
  # Auto-install is OPT-IN. Fetching+running the upstream installer (unpinned `curl | sh` from
  # the `main` branch) on every missing-trivy run is a supply-chain risk for a security gate —
  # especially on a fork PR. Default is fail-closed with an install hint; set
  # TRIVY_AUTOINSTALL=1 to allow the convenience auto-install on a trusted runner.
  if [ "${TRIVY_AUTOINSTALL:-0}" != "1" ]; then
    echo "[trivy] ERROR: trivy not found and auto-install is off." >&2
    echo "        Install it (brew install trivy / https://trivy.dev/latest/getting-started/installation/)" >&2
    echo "        or set TRIVY_AUTOINSTALL=1 to fetch the upstream installer (unpinned curl|sh)." >&2
    return 1
  fi
  echo "[trivy] trivy not found — attempting install (TRIVY_AUTOINSTALL=1)…" >&2
  if command -v brew >/dev/null 2>&1; then
    brew install trivy >/dev/null 2>&1 && return 0
  fi
  # Install dir. The DEFAULT is an absolute path under $TMPDIR — OUTSIDE the scanned tree (the
  # default target is `.`). The real shadowing defense is that the dir is APPENDED to PATH
  # (never prepended) — a repo-local `./bin/head` (or any tool the script later calls) can't
  # then shadow the system binary and run attacker-controlled code from a scanned PR. A relative
  # TRIVY_INSTALL_DIR override is normalized to absolute under cwd (so it MAY land inside the
  # scanned tree — the append-not-prepend rule is what keeps that safe; prefer an absolute
  # override outside the tree if you set one).
  install_dir="${TRIVY_INSTALL_DIR:-${TMPDIR:-/tmp}/trivy-bin}"
  case "$install_dir" in
    /*) : ;;  # absolute — used as-is
    *) install_dir="$(pwd)/$install_dir" ;;  # normalize a relative override to absolute (under cwd)
  esac
  if command -v curl >/dev/null 2>&1; then
    mkdir -p "$install_dir" || return 1
    # NB: the installer script (fetched from the `main` branch) and the trivy binary it pulls
    # are NOT version-pinned — prefer a pre-installed trivy on a hardened runner (see README).
    if curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh \
         | sh -s -- -b "$install_dir"; then
      PATH="$PATH:$install_dir"; export PATH   # APPEND: never let the scanned tree shadow PATH
      command -v trivy >/dev/null 2>&1 && return 0
    fi
    echo "[trivy] installer failed (output above)." >&2
  fi
  echo "[trivy] ERROR: could not install trivy automatically." >&2
  echo "        Install it manually: brew install trivy  (or see https://trivy.dev/latest/getting-started/installation/)" >&2
  return 1
}

ensure_trivy || exit 2

# Block tier passes --exit-code 1 (trivy fails on a finding at/above --severity); warn tier
# passes --exit-code 0 so trivy still PRINTS findings but never fails the build.
EXIT_FLAG=1
[ "$TRIVY_WARN" = "1" ] && EXIT_FLAG=0

set -- fs --scanners "$TRIVY_SCANNERS" --severity "$TRIVY_SEVERITY" --exit-code "$EXIT_FLAG"
[ "$TRIVY_IGNORE_UNFIXED" = "1" ] && set -- "$@" --ignore-unfixed

echo "[trivy] $(trivy --version 2>/dev/null | head -n1 || echo '?') — scanners: $TRIVY_SCANNERS, severity: $TRIVY_SEVERITY, target: $TRIVY_TARGET" >&2

# Capture trivy's OWN exit code. We must NOT gate on `if trivy …; then` and read `$?` in the
# fall-through: after an `if` whose condition is false (and no else), POSIX `$?` is the `if`
# statement's status — which is 0 — so the runner would print FAIL but `exit 0`, leaving CI
# green on a real finding (the whole point of block tier). Disable errexit only around the
# scanner so a non-zero result is captured, not fatal-on-the-spot.
set +e
# `set -f` (noglob) for the unquoted $TRIVY_EXTRA expansion: we WANT word-splitting (so a knob
# like `--skip-files *.lock` becomes separate args) but NOT pathname expansion — without noglob,
# a glob in TRIVY_EXTRA would be expanded against the cwd (mangling the flag, or leaving a
# literal on no match) before trivy ever sees it. Restored right after.
set -f
# shellcheck disable=SC2086  # TRIVY_EXTRA is an intentional word-split of pass-through flags
trivy "$@" $TRIVY_EXTRA "$TRIVY_TARGET"
rc=$?
set +f
set -e

# Warn tier honors "never fails the build" UNCONDITIONALLY — `--exit-code 0` only suppresses
# the FINDINGS exit code, but an operational trivy error (DB unreachable, a bad TRIVY_EXTRA
# flag, a typo'd scanner) still returns non-zero. So in warn tier we exit 0 regardless of `rc`,
# after surfacing whatever trivy printed.
if [ "$TRIVY_WARN" = "1" ]; then
  if [ "$rc" -eq 0 ]; then
    echo "[trivy] WARN tier — findings (if any) reported above, NOT failing (TRIVY_WARN=1)." >&2
  else
    echo "[trivy] WARN tier — trivy exited $rc (findings and/or a scanner error), NOT failing the build (TRIVY_WARN=1)." >&2
  fi
  exit 0
fi

# Block tier: trivy's exit code is the gate verdict (0 = clean, non-zero = finding or error).
if [ "$rc" -eq 0 ]; then
  echo "[trivy] PASS — no findings at $TRIVY_SEVERITY." >&2
  exit 0
fi
echo "[trivy] FAIL — trivy exited $rc (findings at $TRIVY_SEVERITY, or a scanner error). Fix the" >&2
echo "        findings / upgrade the dep / add a justified .trivyignore entry, or use TRIVY_WARN=1." >&2
exit "$rc"

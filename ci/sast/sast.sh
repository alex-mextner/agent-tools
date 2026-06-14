#!/usr/bin/env sh
# SAST via semgrep — generic shell runner for any CI (GitLab / Jenkins / Buildkite / cron)
# or a local pre-push check. Mirrors ci/sast/workflow.yml so non-GitHub CI is first-class.
#
# Standard engine = semgrep (https://semgrep.dev). Installs it if missing (pip/pipx/brew),
# runs the configured ruleset, and EXITS NON-ZERO on findings (block tier — the right CI
# default). Set SAST_WARN=1 to report findings without failing (warn tier).
#
# Knobs (env):
#   SEMGREP_CONFIG   ruleset(s). Default "auto" (registry rules for detected languages).
#                    Alternatives: p/ci, p/security-audit, p/owasp-top-ten, ./.semgrep.yml,
#                    or several space-separated.
#   SEMGREP_TARGET   path to scan. Default "." (repo root).
#   SAST_WARN        "1" = never fail the build (warn tier). Default block.
#   SEMGREP_EXTRA    extra flags passed through to `semgrep scan`.
#
# Usage:
#   sh ci/sast/sast.sh
#   SEMGREP_CONFIG="p/security-audit p/secrets" sh ci/sast/sast.sh
#   SAST_WARN=1 sh ci/sast/sast.sh
set -eu

SEMGREP_CONFIG="${SEMGREP_CONFIG:-auto}"
SEMGREP_TARGET="${SEMGREP_TARGET:-.}"
SAST_WARN="${SAST_WARN:-0}"
SEMGREP_EXTRA="${SEMGREP_EXTRA:-}"

ensure_semgrep() {
  if command -v semgrep >/dev/null 2>&1; then return 0; fi
  echo "[sast] semgrep not found — attempting install…" >&2
  if command -v pipx >/dev/null 2>&1; then
    pipx install semgrep >/dev/null 2>&1 && return 0
  fi
  if command -v pip3 >/dev/null 2>&1; then
    pip3 install --user --quiet semgrep && return 0
  fi
  if command -v pip >/dev/null 2>&1; then
    pip install --user --quiet semgrep && return 0
  fi
  if command -v brew >/dev/null 2>&1; then
    brew install semgrep >/dev/null 2>&1 && return 0
  fi
  echo "[sast] ERROR: could not install semgrep automatically." >&2
  echo "       Install it manually: pipx install semgrep  (or pip install semgrep, brew install semgrep)" >&2
  return 1
}

ensure_semgrep || exit 2

# Build the --config flags (semgrep accepts repeated --config).
set --
for cfg in $SEMGREP_CONFIG; do
  set -- "$@" --config "$cfg"
done

echo "[sast] semgrep $(semgrep --version 2>/dev/null || echo '?') — config: $SEMGREP_CONFIG, target: $SEMGREP_TARGET" >&2

# --error makes semgrep exit non-zero on findings; we own the exit policy below.
# shellcheck disable=SC2086
if semgrep scan "$@" --error $SEMGREP_EXTRA "$SEMGREP_TARGET"; then
  echo "[sast] PASS — no findings." >&2
  exit 0
fi

rc=$?
if [ "$SAST_WARN" = "1" ]; then
  echo "[sast] WARN tier — findings reported above, NOT failing the build (SAST_WARN=1)." >&2
  exit 0
fi
echo "[sast] FAIL — semgrep reported findings (exit $rc). Fix them, or use an inline" >&2
echo "       // nosemgrep comment / .semgrepignore for justified false positives." >&2
exit "$rc"

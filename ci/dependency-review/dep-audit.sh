#!/usr/bin/env bash
# Dependency vulnerability audit — generic fallback for any CI or a repo WITHOUT GitHub's
# Dependency Graph (where actions/dependency-review-action can't run). Auto-detects the
# package manager and runs its native audit, failing on high/critical advisories.
#
# This is the "what's already in the tree" audit. The PR-time "don't let a new bad dep IN"
# gate is workflow.yml (dependency-review-action) — prefer that on public/GHAS repos.
#
# Detects, in order: bun, npm/pnpm/yarn (node), pip-audit (python), cargo-audit (rust),
# govulncheck (go). Runs every ecosystem it finds a manifest for.
#
# Knobs (env):
#   DEP_AUDIT_LEVEL   minimum severity to FAIL on: low | moderate | high | critical.
#                     Default: high.
#
# Usage: sh ci/dependency-review/dep-audit.sh
set -eu

LEVEL="${DEP_AUDIT_LEVEL:-high}"
rc=0
ran=0

note() { echo "[dep-audit] $*" >&2; }

if [ -f bun.lock ] || [ -f bun.lockb ]; then
  if command -v bun >/dev/null 2>&1; then
    ran=1; note "bun audit --audit-level=$LEVEL"
    bun audit --audit-level="$LEVEL" || rc=1
  else
    note "bun lockfile present but bun not installed — skipping."
  fi
elif [ -f package.json ]; then
  if [ -f pnpm-lock.yaml ] && command -v pnpm >/dev/null 2>&1; then
    ran=1; note "pnpm audit --audit-level $LEVEL"; pnpm audit --audit-level "$LEVEL" || rc=1
  elif [ -f yarn.lock ] && command -v yarn >/dev/null 2>&1; then
    ran=1; note "yarn npm audit (yarn berry) — failing on $LEVEL+"; yarn npm audit --severity "$LEVEL" || rc=1
  elif command -v npm >/dev/null 2>&1; then
    ran=1; note "npm audit --audit-level=$LEVEL"; npm audit --audit-level="$LEVEL" || rc=1
  fi
fi

if [ -f requirements.txt ] || [ -f pyproject.toml ] || [ -f poetry.lock ]; then
  if command -v pip-audit >/dev/null 2>&1; then
    ran=1; note "pip-audit"; pip-audit || rc=1
  else
    note "python manifest present but pip-audit not installed (pipx install pip-audit) — skipping."
  fi
fi

if [ -f Cargo.lock ]; then
  if command -v cargo-audit >/dev/null 2>&1 || cargo audit --version >/dev/null 2>&1; then
    ran=1; note "cargo audit"; cargo audit || rc=1
  else
    note "Cargo.lock present but cargo-audit not installed (cargo install cargo-audit) — skipping."
  fi
fi

if [ -f go.mod ]; then
  if command -v govulncheck >/dev/null 2>&1; then
    ran=1; note "govulncheck ./..."; govulncheck ./... || rc=1
  else
    note "go.mod present but govulncheck not installed (go install golang.org/x/vuln/cmd/govulncheck@latest) — skipping."
  fi
fi

if [ "$ran" = "0" ]; then
  note "no supported manifest/lockfile found — nothing to audit."
  exit 0
fi
[ "$rc" = "0" ] && note "PASS — no advisories at $LEVEL+." || note "FAIL — advisories at $LEVEL+ above."
exit "$rc"

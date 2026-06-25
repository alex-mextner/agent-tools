#!/usr/bin/env bash
# OSS license-policy audit — fail when a dependency carries a DENY-listed (copyleft) license.
#
# This is the license half of the GHAS `dependency-review-action` (which enforces an
# allow/deny LICENSE policy on the PR diff but needs GitHub Advanced Security on private
# repos). This gate is free, runs in any CI, and audits the WHOLE dependency tree using each
# ecosystem's own license reporter — the gap `ci/dependency-review/` (vulns only) calls out.
#
# Policy model: DEFAULT-DENY-COPYLEFT. The whole license universe is allowed EXCEPT a
# deny-list of strong/network copyleft families (GPL/AGPL/LGPL/MPL/EPL/EUPL/CDDL/SSPL/CC-BY-NC
# …). A dependency whose declared license matches the deny pattern fails the gate. This is the
# common OSS-compliance posture: permissive (MIT/BSD/Apache/ISC/…) is fine; copyleft that can
# impose obligations on YOUR distribution is blocked until a human reviews it (add it to
# LICENSE_ALLOW, or relax LICENSE_DENY_PATTERN). We deny-by-pattern rather than allow-by-list
# because a hard allow-list rejects every new permissive license SPDX id nobody enumerated yet
# (false-positive churn); a copyleft deny-list is the smaller, more stable surface to maintain.
#
# Detects, in order: node (license-checker), python (pip-licenses), rust (cargo-deny / a
# Cargo metadata fallback), go (go-licenses). Runs every ecosystem it finds a manifest for.
#
# Knobs (env):
#   LICENSE_DENY_PATTERN  ERE matched (case-insensitive) against each dep's license string. A
#                         match = a policy violation. Default below covers the copyleft families.
#   LICENSE_ALLOW         space/comma-separated dependency names to EXEMPT from the deny check
#                         (e.g. a GPL build-time-only tool you've cleared). Matched against the
#                         package name, case-insensitive, exact.
#   LICENSE_ALLOW_MISSING "1" = DON'T fail when a manifest is found but its license reporter
#                         isn't installed (fail-OPEN). Default 0 = fail CLOSED: a detected
#                         ecosystem with no usable reporter is a gate failure, not a silent
#                         skip — otherwise "no scan ran" masquerades as "all clear".
#   LICENSE_UNKNOWN_OK    "1" = treat an UNKNOWN/empty license as allowed. Default 0 = an
#                         undeclared license is a violation (you can't prove it's compliant).
#
# Usage: sh ci/license-policy/license-audit.sh
set -eu

# Strong + network copyleft families, plus non-commercial CC. Matched case-insensitively
# against whatever the ecosystem reporter emits — which is BOTH SPDX ids (`GPL-3.0-or-later`,
# `LGPL-2.1`) AND human classifier names (pip-licenses emits e.g.
# `GNU General Public License v3 (GPLv3)`). So the pattern must catch:
#   - SPDX abbreviations with a digit/hyphen/eol after them (`GPL-3.0`, `AGPL-3.0`, `MPL-2.0`);
#   - the glued `vN` forms (`GPLv3`, `AGPLv3`, `LGPLv2`) — a LETTER follows the abbreviation;
#   - the full English names (`… General Public License`, `Affero`, `Mozilla Public License`,
#     `Eclipse Public License`, `Common Public License`, `Common Development and Distribution`).
# A bare standalone `GPL`/`LGPL`/`MPL`/`EPL`/`CPL`/`IPL`/`QPL`/`RPL`/`OSL` token (separator or
# eol on BOTH sides) is also denied. LGPL/MPL/EPL (weak copyleft) are denied by default —
# file-level obligations still attach to distribution; relax LICENSE_DENY_PATTERN to permit
# them. The alternation order matters: the glued-letter and full-name alternatives come FIRST
# so they win before the boundary-anchored bare-token alternative is tried.
DEFAULT_DENY='(A?GPL|LGPL)[ _-]?v?[0-9]|(A?GPL|LGPL)[ _-]?v[0-9]|GPLv|AGPLv|LGPLv|(MPL|EPL|EUPL|OSL|RPL|CPL|IPL|QPL)[ _-]?v?[0-9]|(General Public License|Affero|Lesser General Public License|Mozilla Public License|Eclipse Public License|Common Public License|Common Development and Distribution|Sleepycat|European Union Public License|CeCILL|Open Software License)|(^|[^A-Za-z])(A?GPL|LGPL|MPL|EPL|EUPL|CDDL|SSPL|OSL|CECILL|CC-BY-NC|CC-BY-SA|Sleepycat|RPL|QPL|IPL|CPL)([^A-Za-z]|$)'
DENY="${LICENSE_DENY_PATTERN:-$DEFAULT_DENY}"
ALLOW_NAMES="${LICENSE_ALLOW:-}"
ALLOW_MISSING="${LICENSE_ALLOW_MISSING:-0}"
UNKNOWN_OK="${LICENSE_UNKNOWN_OK:-0}"

violations=0
ran=0
missing=0

note() { echo "[license-audit] $*" >&2; }

miss() {
  if [ "$ALLOW_MISSING" = "1" ]; then
    note "$* — skipping (LICENSE_ALLOW_MISSING=1)."
  else
    note "$* — FAILING (no license scan performed; set LICENSE_ALLOW_MISSING=1 to allow)."
    missing=$((missing+1))
  fi
}

# True (rc 0) iff $1 (a package name) is on the allow-list, case-insensitive exact match.
is_allowed_name() {
  [ -n "$ALLOW_NAMES" ] || return 1
  _needle=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  for _a in $(printf '%s' "$ALLOW_NAMES" | tr ',' ' '); do
    [ "$(printf '%s' "$_a" | tr '[:upper:]' '[:lower:]')" = "$_needle" ] && return 0
  done
  return 1
}

# Evaluate one "name<TAB>license" record against the policy. Increments $violations on a hit.
# Reads name+license as args so each ecosystem just normalizes its reporter to NAME\tLICENSE.
check_record() {
  _name="$1"; _license="$2"
  [ -n "$_name" ] || return 0
  if is_allowed_name "$_name"; then
    note "allow-listed: $_name ($_license) — exempt"
    return 0
  fi
  # Undeclared / unknown license.
  case "$_license" in
    ""|"UNKNOWN"|"unknown"|"NOASSERTION"|"null")
      if [ "$UNKNOWN_OK" = "1" ]; then
        note "unknown license: $_name — allowed (LICENSE_UNKNOWN_OK=1)"
        return 0
      fi
      note "VIOLATION: $_name has an UNKNOWN/undeclared license (set LICENSE_UNKNOWN_OK=1 to allow, or allow-list it)"
      violations=$((violations+1))
      return 0
      ;;
  esac
  if printf '%s' "$_license" | grep -Eiq "$DENY"; then
    note "VIOLATION: $_name -> $_license (matches deny policy)"
    violations=$((violations+1))
  fi
}

# All temp files land here so a single EXIT trap removes them even on an early `set -e` exit.
_TMPFILES=""
_mktemp() { _t=$(mktemp); _TMPFILES="$_TMPFILES $_t"; printf '%s' "$_t"; }
# shellcheck disable=SC2086  # word-splitting $_TMPFILES into separate args is intended
trap 'rm -f $_TMPFILES' EXIT INT TERM

# Feed a temp file of "NAME<TAB>LICENSE" records to check_record, ITERATING IN THE CURRENT
# SHELL, and return the number of records read. A `reporter | while read` pipeline runs the
# loop in a SUBSHELL, so a `violations++` inside it is lost when the subshell exits — the gate
# would print VIOLATION lines yet still PASS. Reading from a redirected file (`while … done <
# "$file"`) keeps the loop in this shell so the counter survives. (Caught by a real GPL-dep
# self-test; do not "simplify" it back into a pipe.) The record count lets the caller treat a
# reporter that RAN but emitted NOTHING as a fail-closed condition (an empty result is "no scan
# happened", not "all clear").
_records_seen=0
check_records_file() {
  _records_seen=0
  while IFS="$(printf '\t')" read -r _n _l; do
    [ -n "$_n" ] || continue
    _records_seen=$((_records_seen+1))
    check_record "$_n" "$_l"
  done < "$1"
}

# A reporter that ran but produced zero records: fail closed (same rule as a missing reporter).
# An empty result usually means deps aren't installed / the build didn't run — "no scan" must
# not masquerade as "no copyleft".
empty_scan() {
  if [ "$ALLOW_MISSING" = "1" ]; then
    note "$* produced no records — skipping (LICENSE_ALLOW_MISSING=1)."
  else
    note "$* produced NO license records — FAILING (deps likely not installed; a license scan that finds nothing is not a clean bill). Set LICENSE_ALLOW_MISSING=1 to allow."
    missing=$((missing+1))
  fi
}

scan_node() {
  [ -f package.json ] || return 0
  if command -v license-checker >/dev/null 2>&1; then
    ran=1; note "license-checker (node)"
    _tmp=$(_mktemp)
    # --json -> { "pkg@ver": { "licenses": "MIT", ... }, ... }. Reduce to name<TAB>license.
    license-checker --production --json 2>/dev/null \
      | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const o=JSON.parse(s||"{}");for(const k of Object.keys(o)){const n=k.replace(/@[^@]*$/,"");let l=o[k].licenses;if(Array.isArray(l))l=l.join(" OR ");process.stdout.write(n+"\t"+(l||"UNKNOWN")+"\n");}})' \
      > "$_tmp"
    check_records_file "$_tmp"
    [ "$_records_seen" -gt 0 ] || empty_scan "license-checker"
  elif command -v npx >/dev/null 2>&1; then
    miss "package.json present but license-checker not installed (npm i -g license-checker)"
  else
    miss "package.json present but no node/license-checker available"
  fi
}

scan_python() {
  { [ -f pyproject.toml ] || [ -f requirements.txt ] || [ -f poetry.lock ]; } || return 0
  if command -v pip-licenses >/dev/null 2>&1; then
    ran=1; note "pip-licenses (python)"
    _tmp=$(_mktemp)
    # --format=csv: "Name","Version","License". Skip header; emit name<TAB>license.
    pip-licenses --format=csv --with-system 2>/dev/null \
      | tail -n +2 \
      | sed -E 's/^"([^"]*)","[^"]*","([^"]*)".*$/\1\t\2/' \
      > "$_tmp"
    check_records_file "$_tmp"
    [ "$_records_seen" -gt 0 ] || empty_scan "pip-licenses"
  else
    miss "python manifest present but pip-licenses not installed (pipx install pip-licenses)"
  fi
}

scan_rust() {
  [ -f Cargo.toml ] || [ -f Cargo.lock ] || return 0
  # cargo-deny carries its OWN license policy in deny.toml — the shell-side knobs
  # (LICENSE_DENY_PATTERN / LICENSE_ALLOW / LICENSE_UNKNOWN_OK) do NOT apply to rust; the policy
  # lives in your deny.toml (see the README's rust note). Without a deny.toml, cargo-deny runs a
  # permissive default that can pass copyleft silently — so we REQUIRE a deny.toml and fail
  # closed when it's absent rather than give a false green.
  if ! { command -v cargo-deny >/dev/null 2>&1 || cargo deny --version >/dev/null 2>&1; }; then
    miss "Cargo manifest present but cargo-deny not installed (cargo install cargo-deny)"
    return 0
  fi
  if [ ! -f deny.toml ] && [ ! -f .deny.toml ]; then
    miss "Cargo manifest present but no deny.toml — cargo-deny's default is permissive and would PASS copyleft silently; add a deny.toml with your [licenses] policy"
    return 0
  fi
  ran=1; note "cargo-deny check licenses (rust; policy from deny.toml)"
  cargo deny check licenses || violations=$((violations+1))
}

scan_go() {
  [ -f go.mod ] || return 0
  if command -v go-licenses >/dev/null 2>&1; then
    ran=1; note "go-licenses (go)"
    _tmp=$(_mktemp)
    # csv: module,license-url,license-name. Emit module<TAB>license-name.
    go-licenses report ./... 2>/dev/null \
      | awk -F, 'NF>=3{print $1"\t"$3}' \
      > "$_tmp"
    check_records_file "$_tmp"
    [ "$_records_seen" -gt 0 ] || empty_scan "go-licenses"
  else
    miss "go.mod present but go-licenses not installed (go install github.com/google/go-licenses@latest)"
  fi
}

scan_node
scan_python
scan_rust
scan_go

if [ "$ran" = "0" ] && [ "$missing" = "0" ]; then
  note "no supported manifest found — nothing to license-scan."
  exit 0
fi
if [ "$missing" -gt 0 ]; then
  note "FAIL — $missing detected ecosystem(s) had no usable license reporter (fail-closed). Install the tool(s) above, or set LICENSE_ALLOW_MISSING=1."
  exit 1
fi
if [ "$violations" -gt 0 ]; then
  note "FAIL — $violations dependency(ies) violate the license policy. Replace the dep, or allow-list it via LICENSE_ALLOW / relax LICENSE_DENY_PATTERN."
  exit 1
fi
note "PASS — no deps match the deny policy."
exit 0

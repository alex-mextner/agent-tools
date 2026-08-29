#!/usr/bin/env bash
# install.sh — install the vscode-orphan-reaper as a launchd LaunchAgent that
# runs every 15 minutes (StartInterval=900s). macOS only.
#
# MANUAL install for now (see this dir's README for why this isn't yet wired
# into `rig apply`). Idempotent: re-running overwrites the plist in place and
# reloads it, so it's safe to re-run after an update to reap_vscode_orphans.py.
#
# USAGE
#   ./install.sh            # install + load
#   ./install.sh --uninstall  # unload + remove
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.hyperide.vscode-orphan-reaper"
PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs"
LOG_PATH="${LOG_DIR}/${LABEL}.log"

if [[ "${1:-}" == "--uninstall" ]]; then
  echo "install.sh: unloading ${LABEL}"
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || launchctl unload "$PLIST_DST" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "install.sh: removed ${PLIST_DST}"
  exit 0
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "install.sh: this reaper targets macOS launchd only — nothing to install on $(uname -s)." >&2
  exit 1
fi

# Prefer the SYSTEM python3, not `command -v python3` (review-caught P2,
# agent-tools#477 round 5): this script is normally run from the required
# fresh feature worktree, and if that worktree has a local virtualenv
# activated at install time, `command -v python3` resolves to
# `<worktree>/.venv/bin/python3` -- baked permanently into the plist even
# though the reaper script itself is now copied to stable storage (the
# earlier P2 fix above). Worktree removal then breaks the interpreter path
# for every future launchd interval, same failure shape the stable-script-
# path fix already closed for the script itself. The reaper's own module
# docstring already documents the assumption this fixes: "Stdlib only -- no
# third-party deps, so it can run standalone under launchd's bare
# /usr/bin/python3 without a virtualenv." `-x` alone only proves the path is
# an executable FILE, not a WORKING interpreter (review-caught, agent-tools#477
# round 6): on a macOS machine without Xcode Command Line Tools installed,
# /usr/bin/python3 is a real, executable STUB that fails on every invocation
# (or pops a "install command line developer tools" GUI prompt) -- `-x`
# alone would still pick it, silently baking a permanently broken
# interpreter into the plist. Actually running it is the only way to tell.
# `command -v python3` is kept as the fallback for that case (and for an
# unusual system missing /usr/bin/python3 entirely).
if [[ -x /usr/bin/python3 ]] && /usr/bin/python3 -c '' >/dev/null 2>&1; then
  PYTHON3=/usr/bin/python3
elif command -v python3 >/dev/null 2>&1; then
  PYTHON3="$(command -v python3)"
else
  echo "install.sh: python3 not found (neither /usr/bin/python3 nor on PATH)" >&2
  exit 1
fi
SOURCE_SCRIPT_PATH="${SCRIPT_DIR}/reap_vscode_orphans.py"
[[ -f "$SOURCE_SCRIPT_PATH" ]] || { echo "install.sh: missing ${SOURCE_SCRIPT_PATH}" >&2; exit 1; }

# Copy the script to a STABLE, machine-level location before pointing the
# plist at it (review-caught P2): if install.sh runs from the required fresh
# feature worktree (see this repo's worktree-isolation policy), SCRIPT_DIR is
# inside that worktree. Normal post-merge cleanup deletes the worktree and
# its branch, which would leave every subsequent 15-min launchd interval
# targeting a now-nonexistent path — a silent, permanent stop with no error
# surfaced anywhere. Re-running install.sh (e.g. after an update) re-copies,
# so this stays in sync with whatever worktree/checkout you installed from
# last, same as the plist itself.
STABLE_DIR="$HOME/.local/share/agent-tools/vscode-orphan-reaper"
mkdir -p "$STABLE_DIR"
SCRIPT_PATH="${STABLE_DIR}/reap_vscode_orphans.py"
cp "$SOURCE_SCRIPT_PATH" "$SCRIPT_PATH"
chmod +x "$SCRIPT_PATH"

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$PLIST_DST")"

# XML-escape each substituted value before it goes into the plist — an
# unescaped '&' (a real possibility in usernames/paths, e.g. "AT&T Backup")
# corrupts the XML, and since re-install first `launchctl bootout`s the OLD
# working plist before loading the new one, a corrupt replacement would leave
# the reaper unloaded entirely rather than just failing to update (caught in
# review). `plutil -lint` below is the second, independent gate: even if an
# escape case is missed, a genuinely malformed plist is never installed.
xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"
  s="${s//</&lt;}"
  s="${s//>/&gt;}"
  printf '%s' "$s"
}
# sed's REPLACEMENT side treats an unescaped '&' as "the whole matched text"
# and '\' as an escape introducer (review-caught P2): xml_escape's OWN output
# for a literal '&' is the string "&amp;", which itself contains an
# unescaped '&' — fed straight into `sed -e "s#...#${VALUE}#"`, sed expands
# that '&' to the matched placeholder text (e.g. "__PYTHON3__") instead of
# emitting a literal character, corrupting the substitution while still
# passing `plutil -lint` (the result is valid-looking but wrong XML/text).
# Apply this SECOND, after xml_escape, so both escaping layers compose
# correctly: XML rules for the plist's own syntax, sed rules for how sed
# treats the replacement text it's given.
sed_replacement_escape() {
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//&/\\&}"
  s="${s//#/\\#}"  # '#' is this script's sed delimiter (s#...#...#)
  printf '%s' "$s"
}
PYTHON3_ESC="$(sed_replacement_escape "$(xml_escape "$PYTHON3")")"
SCRIPT_PATH_ESC="$(sed_replacement_escape "$(xml_escape "$SCRIPT_PATH")")"
LOG_PATH_ESC="$(sed_replacement_escape "$(xml_escape "$LOG_PATH")")"

TMP_PLIST="$(mktemp "${TMPDIR:-/tmp}/vscode-orphan-reaper-plist.XXXXXX")"
trap 'rm -f "$TMP_PLIST"' EXIT

sed \
  -e "s#__PYTHON3__#${PYTHON3_ESC}#" \
  -e "s#__SCRIPT_PATH__#${SCRIPT_PATH_ESC}#" \
  -e "s#__LOG_PATH__#${LOG_PATH_ESC}#" \
  "${SCRIPT_DIR}/com.hyperide.vscode-orphan-reaper.plist.template" > "$TMP_PLIST"

if ! plutil -lint -s "$TMP_PLIST" >/dev/null 2>&1; then
  echo "install.sh: rendered plist failed plutil -lint — refusing to install a malformed unit:" >&2
  plutil -lint "$TMP_PLIST" >&2 || true
  exit 1
fi

# Only unload the OLD unit once the NEW one is confirmed well-formed and
# staged — never bootout before we know the replacement can actually load.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || launchctl unload "$PLIST_DST" 2>/dev/null || true
mv "$TMP_PLIST" "$PLIST_DST"
trap - EXIT
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || launchctl load "$PLIST_DST"

echo "install.sh: installed ${LABEL} — runs every 15 min via launchd, log at ${LOG_PATH}"
echo "install.sh: verify with: launchctl list | grep ${LABEL}"

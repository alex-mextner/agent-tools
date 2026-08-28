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

command -v python3 >/dev/null 2>&1 || { echo "install.sh: python3 not found on PATH" >&2; exit 1; }
PYTHON3="$(command -v python3)"
SCRIPT_PATH="${SCRIPT_DIR}/reap_vscode_orphans.py"
[[ -f "$SCRIPT_PATH" ]] || { echo "install.sh: missing ${SCRIPT_PATH}" >&2; exit 1; }

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
PYTHON3_ESC="$(xml_escape "$PYTHON3")"
SCRIPT_PATH_ESC="$(xml_escape "$SCRIPT_PATH")"
LOG_PATH_ESC="$(xml_escape "$LOG_PATH")"

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

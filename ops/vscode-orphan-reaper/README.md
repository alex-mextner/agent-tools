# vscode-orphan-reaper

Periodic (every 15 min, macOS launchd) sweep that kills leaked, isolated VS Code
E2E-harness Electron windows — the residual case not covered by the PRIMARY fix,
which is the pre-launch sweep now built into `ext-test-projects/e2e/setup/
electron-app.ts` (`reapOrphanedIsolatedVSCodeProcesses`, called from
`launchVSCode()` right alongside the existing `killStrayVSCodeProcesses`). That
pre-launch sweep fires on every new harness launch — frequent on a busy day, which
is exactly when the 2026-08-27/28 incident happened — so it catches the common
case immediately. This script catches what's left: no new launch happens for a
while, so nothing triggers a sweep, and an orphaned window just sits there
consuming memory until something else notices it.

See `reap_vscode_orphans.py`'s own module docstring for the full incident
writeup, the safety reasoning (two-tier age gate: `ppid == 1` + 15 min, or any
parent + 90 min — never kill a live matrix run out from under itself), and why
the kill predicate is a POSITIVE match only (`/hvsc-` + `--extensionDevelopmentPath`
in argv), never the absence of markers — it can never touch the user's real
editor or an unrelated Electron app.

## Status: manually installed, NOT yet wired into `rig apply`

This is honest scope, not an oversight: `rig apply`'s existing machine-wide
provisioning (via `lib/agenttools_service`) only knows how to install a
`RunAtLoad` + `KeepAlive` LaunchAgent for a long-running DAEMON (start once, stay
up, restart on crash) — it has no support for a `StartInterval` periodic job that
runs briefly and exits, which is what this script needs. Building that support
into `agenttools_service` (or a sibling periodic-job abstraction) is real,
separate infrastructure work beyond what this pass could safely land unsupervised
alongside the two other changes in the same PR — tracked as a follow-up (see the
PR description / linked ticket for the tracking issue number).

Until that lands, install by hand:

```
./install.sh              # renders the plist, installs to ~/Library/LaunchAgents, loads it
./install.sh --uninstall  # unloads and removes it
launchctl list | grep com.hyperide.vscode-orphan-reaper   # verify it's loaded
```

Logs go to `~/Library/Logs/com.hyperide.vscode-orphan-reaper.log`.

## Documented coupling (no automated drift guard)

The `hvsc-<n>-<uuid>` userDataDir naming convention and the
`--extensionDevelopmentPath` argv marker are OWNED by
`ext-test-projects/e2e/setup/electron-app.ts`. This script duplicates that
positive-match predicate in Python rather than importing it — a standalone OS
process under launchd has no access to that repo's TypeScript toolchain, and
this script needs to keep working even if that repo is temporarily unavailable.
If the naming convention ever changes there, `_ISOLATED_MARKERS` /
`is_orphaned_isolated_instance` must change here too, by hand. There is currently
no automated test or CI check that would catch the two drifting apart — a real
gap, also part of the follow-up ticket above.

## Testing

```
uv run --with pytest python -m pytest tests/test_reap_vscode_orphans.py -q
```

Manual dry run against the real machine (kills nothing, just reports):

```
python3 reap_vscode_orphans.py --dry-run
```

#!/usr/bin/env python3
"""reap_vscode_orphans.py — periodic sweep for leaked, isolated VS Code E2E-harness
Electron windows (macOS).

WHY THIS EXISTS (2026-08-27/28 incident, see agent-hooks/heavy-op-memory-gate/
heavy_op_memory_gate.py for the fuller writeup): the `hyperide` repo's e2e test
harness (`ext-test-projects/e2e/setup/electron-app.ts`, `launchVSCode()`) launches
fully isolated VS Code windows for capture/debug/matrix scripts — a distinct
`--user-data-dir=<tmp>/hvsc-<worker>-<uuid>` + `--extensionDevelopmentPath=...` per
launch. Teardown (`closeVSCode`/`forceCloseVSCode`) explicitly kills that process
tree and `fs.rm`s its userDataDir — but ONLY IF the launching script reaches its
`finally` block. When the script is killed instead (session churn, an interrupted
agent, a hung Electron that never returns control), the window is orphaned: no
script is left alive to ever call teardown. A live launch also does a PRE-LAUNCH
sweep of exactly this class of orphan (see that TypeScript file's
`reapOrphanedIsolatedVSCodeProcesses`) — but that only fires on the NEXT launch. On
a day with no new launch for a while (or right when the orphan was created), the
window just sits there, stuck on its own settings-restart modal, consuming real
memory. 340 `hvsc-*` userDataDir fossils were found under the tmp dir on this
machine spanning 2026-07-14..2026-08-28 (20 from 2026-08-28 alone) — every one is a
launch whose teardown never ran, i.e. a launch whose kill-the-process-tree step
also never ran. This script is the OS-level backstop for the residual case: a
periodic launchd job that sweeps regardless of whether anything is about to launch.

DELIBERATE COUPLING (documented, not hidden — and NOT env-var-configurable,
see `_USER_DATA_DIR_RE`'s own comment for why a configurable version was tried
and reverted for safety): the `hvsc-<n>-<uuid>` naming convention and the
`--extensionDevelopmentPath` argv marker are OWNED by
`ext-test-projects/e2e/setup/electron-app.ts` in the `hyperide` repo. This
script duplicates that positive-match predicate (NOT the inverse-match
`killStrayVSCodeProcesses` uses) rather than importing it from that repo,
because this script must run as a standalone OS process (launchd has
no access to a TypeScript toolchain) and must keep working even if that repo is
temporarily unavailable/moved. There is still no automated drift guard between
this default and hyperide's actual convention if that one changes — a real gap,
see this directory's README for the tracked follow-up.

SAFETY — same two-tier age gate as the pre-launch sweep, and for the same reason
(never kill a live matrix run or long capture script out from under itself):
  - ppid == 1 (reparented to launchd, i.e. its owning script/shell is provably
    gone) + elapsed >= REPARENTED_MIN_AGE_S is reaped.
  - ANY isolated instance, regardless of parent, is reaped once elapsed >=
    STALE_MIN_AGE_S (a generous bar past any real matrix run).
Never matches on the ABSENCE of markers — only a confirmed positive match
(a structurally valid hvsc-shaped `--user-data-dir=` value AND an actual,
token-boundary `--extensionDevelopmentPath` flag — see `_has_isolation_markers`)
is ever a candidate. The
distinguishing test is NOT the binary name (verified live on this machine: the
real installed editor really does run as `.../MacOS/Code`, distinct from the
`.../MacOS/Electron` this greps for, contrary to an earlier review comment that
claimed otherwise without checking) — it's the two-marker conjunction, which is
why `is_orphaned_isolated_instance` checks it independently of which `ps` pattern
found the candidate in the first place.

TREE KILL, NOT SINGLE-PID KILL: the Electron MAIN process is only the anchor used
to find a candidate — its renderer/GPU/extension-host helper processes run under
different binary names (`Code Helper (Renderer)` etc.) and would survive a
single-pid kill, leaving the memory leak only partially closed. Once an anchor is
confirmed orphaned, this script extracts its `--user-data-dir=<value>` and kills
EVERY process (anchor + helpers) sharing that exact value — the same shared-value
reap `reapVSCodeProcessesByToken` already does on the TypeScript side for a normal
teardown. Each pid is RE-READ and RE-VERIFIED (still carries that value)
immediately before it is signalled, closing the PID-reuse race a plain
list-then-kill has (a matched pid could exit and be recycled by an unrelated
process between listing and killing).

USAGE
    reap_vscode_orphans.py              # reap + report
    reap_vscode_orphans.py --dry-run    # report only, kill nothing
    reap_vscode_orphans.py --json       # machine-readable report on stdout

Stdlib only — no third-party deps, so it can run standalone under launchd's bare
`/usr/bin/python3` without a virtualenv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

REPARENTED_MIN_AGE_S = 15 * 60  # 15 min, only when ppid == 1
STALE_MIN_AGE_S = 90 * 60  # 90 min, regardless of parent

_ELECTRON_PATTERN = "Visual Studio Code.app/Contents/MacOS/Electron"
# NOT configurable via env var (reverted after a review-round safety finding —
# see this directory's README "Documented coupling" section and the tracked
# follow-up ticket there for the full analysis). A prior version accepted
# VSCODE_ORPHAN_REAPER_USERDATADIR_MARKER as an arbitrary substring, validated
# only by length. That was demonstrably unsafe: a real, running VS Code
# session's argv legitimately contains `--user-data-dir=/Users/.../Application
# Support/Code` (verified live on this machine), so a marker as plausible as
# "Code" would match ANY real session's userDataDir value -- including an
# actively-debugged extension-development window (which also legitimately
# carries `--extensionDevelopmentPath`, the OTHER required marker) older than
# 90 minutes, and get it SIGKILLed. No length/scope heuristic closes this for
# an arbitrary string; a safe version would need to require a structural
# pattern (e.g. a numeric worker-id path segment, matching this convention's
# own shape), not a raw substring -- that's real, non-trivial follow-up work,
# not something to guess at here. Hardcoded to hyperide's actual, current
# convention in the meantime.
# No `_ISOLATED_MARKERS` constant (review-caught, agent-tools#477 rounds 5-6):
# an earlier version kept a `("/hvsc-", "--extensionDevelopmentPath")` tuple
# and checked membership via `all(marker in args for marker in _ISOLATED_MARKERS)`
# -- a raw substring scan that let a real window's own --extensionDevelopmentPath
# VALUE coincidentally containing "/hvsc-" (round 5), or an unrelated argument's
# value coincidentally containing the literal text "--extensionDevelopmentPath"
# (round 6), satisfy the markers on their own. The two markers are now each
# enforced by their own ANCHORED check (`_extract_user_data_dir` /
# `_EXTENSION_DEV_PATH_RE`) inside the single `_has_isolation_markers`
# predicate below -- removed the unused tuple rather than leave a
# do-not-use-this-as-a-substring-scan footgun sitting next to dead code.
# Anchored to a TOKEN BOUNDARY (start-of-string or preceding whitespace),
# not a bare substring (review-caught P1, agent-tools#477 round 2): argv is
# `ps`-joined by single spaces, so a real `--user-data-dir=` FLAG always
# starts right after a space or at position 0. Without this anchor,
# `re.search` also matches the literal text "--user-data-dir=" occurring
# INSIDE an unrelated argument's own VALUE -- e.g. a single argv token
# `--note=--user-data-dir=/tmp/hvsc-2-deadbeef` (some other flag whose value
# happens to embed that string) would extract the target directory and let
# that unrelated process be treated as sharing it, defeating the exact-value
# match fix below (`_extract_user_data_dir` equality in
# `_pids_sharing_user_data_dir` / `_kill_tree`) with a different bypass.
#
# The captured VALUE itself is required to look like an isolated instance's
# own `hvsc-<n>-...` path, not just any `--user-data-dir=` value (review-
# caught P1, agent-tools#477 round 3): `re.search` (not `finditer`) stops at
# the FIRST match, so a process with TWO `--user-data-dir=` flags -- a real
# profile first, an isolated `hvsc-*` override second, the exact shape a
# wrapper script that appends an override flag produces -- would otherwise
# extract the REAL profile's path from the first occurrence. That value then
# gets used to find/kill every OTHER process sharing it (`_carries_user_data_dir`
# in `_pids_sharing_user_data_dir` / `_kill_tree`) -- which could be the
# actual live VS Code session running that real profile. Mirrors the
# equivalent fix already shipped on the TypeScript side
# (`extractUserDataDirValue` in `ext-test-projects/e2e/setup/electron-app.ts`,
# same rationale, same anchor shape) -- this script's own module docstring
# already describes duplicating that predicate; this brings the two back in
# sync on this specific case too.
_USER_DATA_DIR_RE = re.compile(r"(?:^|\s)--user-data-dir=(\S*/hvsc-\d+-\S*)")

# Same token-boundary anchoring as _USER_DATA_DIR_RE, applied to the SECOND
# isolation marker (review-caught P1, agent-tools#477 round 6): an unanchored
# `"--extensionDevelopmentPath" in args` substring check matches that literal
# text occurring inside an UNRELATED argument's own value -- e.g. a single
# argv token `--note=--extensionDevelopmentPath` -- letting a process with a
# real, valid hvsc-shaped --user-data-dir= (e.g. a genuine helper process, or
# any process whose --user-data-dir= happens to be hvsc-shaped) but NO actual
# --extensionDevelopmentPath flag satisfy both markers anyway. `(?:=|\s|$)`
# after the flag name additionally rejects a longer flag name that merely
# starts with this text (defense in depth; not the reported vector but the
# same anchoring principle).
_EXTENSION_DEV_PATH_RE = re.compile(r"(?:^|\s)--extensionDevelopmentPath(?:=|\s|$)")

# `ps -o pid=,ppid=,etime=,args=` row shape: pid, ppid, a NO-SPACE elapsed-time
# field (`[[dd-]hh:]mm:ss`), then args (may itself contain spaces).
_PS_ROW = re.compile(r"^(\d+)\s+(\d+)\s+(\S+)\s+(.*)$")


def _warn(msg: str) -> None:
    # launchd routes this process's stderr to the configured log file (see
    # install.sh / the plist template) — a warning here is a VISIBLE event in
    # that log, not silent. This is the fix for the class of bug this script
    # itself shipped with once already: `ps -eo ... etimes= ...` doesn't exist
    # on macOS (`etimes` is a Linux/procps extension; BSD `ps` only has
    # `etime`), `ps` still exits 0 and just drops the unrecognized column, and
    # the old row regex then silently failed to match every single line —
    # this reaper ran every 15 minutes reporting "0 orphans" forever while
    # parsing exactly zero real rows. Caught in review, fixed by switching to
    # `etime=` + `_parse_bsd_elapsed_time` below; this warning is the backstop
    # for the NEXT such silent-parsing regression, not a fix for this one.
    print(f"reap-vscode-orphans: WARNING: {msg}", file=sys.stderr)


def _parse_bsd_elapsed_time(etime: str) -> Optional[float]:
    """Parse BSD `ps -o etime=` format (`[[dd-]hh:]mm:ss`) into seconds.

    Mirrors `parseBsdElapsedTime` in `ext-test-projects/e2e/setup/
    electron-app.ts` (kept in sync by hand — see the module docstring's
    DELIBERATE COUPLING note; same drift risk as `_ISOLATED_MARKERS`).
    """
    m = re.match(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$", etime.strip())
    if not m:
        return None
    days, hours, minutes, seconds = m.groups()
    total = int(minutes) * 60 + int(seconds)
    if hours is not None:
        total += int(hours) * 3600
    if days is not None:
        total += int(days) * 86400
    return float(total)


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    age_s: float
    args: str


@dataclass
class ReapReport:
    reaped: list[ProcessInfo] = field(default_factory=list)
    skipped: list[ProcessInfo] = field(default_factory=list)
    killed_pids: list[int] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "reaped": [asdict(p) for p in self.reaped],
            "reaped_count": len(self.reaped),
            "killed_pid_count": len(self.killed_pids),
            "skipped_isolated_count": len(self.skipped),
        }


def is_orphaned_isolated_instance(args: str, ppid: int, age_s: float) -> bool:
    """Pure predicate — see module docstring's SAFETY section. Mirrors
    `isOrphanedIsolatedInstance` in `ext-test-projects/e2e/setup/electron-app.ts`
    (kept in sync by hand; no shared source between the two languages/repos).

    Requires a STRUCTURALLY VALID hvsc-shaped `--user-data-dir=` value (via
    `_extract_user_data_dir`), not a raw `/hvsc-` substring anywhere in the
    full argv (review-caught P1, agent-tools#477 round 5): a real, live VS
    Code window's `--extensionDevelopmentPath` value can itself legitimately
    contain the text "/hvsc-" (e.g. an extension author testing against a
    fixture project named with that string) with a completely normal or
    absent `--user-data-dir=`. Checking `/hvsc-` as a bare substring across
    the WHOLE args string let that one argument satisfy the marker on its
    own; past the 90-minute stale-regardless bar, this would classify a real,
    unrelated, live window as an orphan and SIGKILL it -- the exact
    wrongful-kill class every other fix in this file exists to prevent. The
    `--extensionDevelopmentPath` marker is ALSO a token-boundary anchored
    check, not a bare substring (review-caught P1, agent-tools#477 round 6 --
    the same literal-text-embedded-in-an-unrelated-value vector applies to
    either marker; see `_EXTENSION_DEV_PATH_RE`)."""
    if not _has_isolation_markers(args):
        return False
    reparented = ppid == 1 and age_s >= REPARENTED_MIN_AGE_S
    stale_regardless = age_s >= STALE_MIN_AGE_S
    return reparented or stale_regardless


def _has_isolation_markers(args: str) -> bool:
    """True when `args` carries BOTH isolation markers this convention
    requires: a structurally valid hvsc-shaped `--user-data-dir=` value (not
    a bare `/hvsc-` substring anywhere in argv — see
    `is_orphaned_isolated_instance`'s docstring) and an actual, token-boundary
    `--extensionDevelopmentPath` flag (not that literal text occurring inside
    an unrelated argument's own value — review-caught P1, agent-tools#477
    round 6, same anchoring principle as `_USER_DATA_DIR_RE`). The ONLY
    marker check either `is_orphaned_isolated_instance` (kill decision) or
    `sweep`'s "skipped, still within age grace" reporting branch uses — both
    must route through this single function so the two can't silently
    diverge (a stricter kill-decision predicate with a looser reporting
    predicate would still be kill-safe, but would misreport a non-isolated
    process as a "skipped isolated instance")."""
    return _extract_user_data_dir(args) is not None and _EXTENSION_DEV_PATH_RE.search(args) is not None


def _extract_user_data_dir(args: str) -> Optional[str]:
    m = _USER_DATA_DIR_RE.search(args)
    return m.group(1) if m else None


def _carries_user_data_dir(args: str, user_data_dir: str) -> bool:
    """True when `args` carry `user_data_dir` as their OWN `--user-data-dir=`
    value (exact flag-value equality via `_extract_user_data_dir`, not a
    substring mention — review-caught P2, agent-tools#477). The ONLY
    predicate either `_pids_sharing_user_data_dir` (listing) or `_kill_tree`
    (re-verify-before-kill) uses to decide "does this process belong to the
    same isolated instance" — both MUST route through this single function,
    not duplicate the comparison, so a future edit to the matching rule
    can't tighten one call site and silently leave the other looser (listed
    as a candidate, then skipped at kill time defeats the tree-kill; or
    listed loosely and NOT re-verified strictly would defeat the PID-reuse
    guard)."""
    return _extract_user_data_dir(args) == user_data_dir


def _run_ps(run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> Optional[str]:
    """Raw `ps -eo pid=,ppid=,etime=,args=` stdout for EVERY process, or None
    on failure (never `[]`-shaped success — see `_warn`'s docstring note).

    `-ww` (review-caught P1, agent-tools#477 round 5): BSD `ps` truncates the
    displayed command line to terminal width unless wide output is
    requested; a real VS Code Electron argv (many flags) can exceed that,
    silently cutting off `--user-data-dir=` or `--extensionDevelopmentPath`
    before this ever sees them. The row still parses fine -- candidate
    discovery just silently misses the orphan (false-negative direction,
    same fail-safe posture as everything else in this file, but still a
    real functional gap this reaper exists to close). `ps -ww` is this
    repo's existing documented fix for the identical truncation class
    elsewhere (see ROADMAP.md's "#35 -- daemon: read full cmdline via
    ps -ww"); this call and `_reread_args` below now match that pattern."""
    try:
        proc = run(["ps", "-eo", "pid=,ppid=,etime=,args=", "-ww"], capture_output=True, text=True, timeout=10)
    except Exception as exc:
        _warn(f"ps invocation failed: {exc}")
        return None
    if proc.returncode != 0:
        _warn(f"ps exited {proc.returncode}: {(proc.stderr or '').strip()}")
        return None
    return proc.stdout


def _parse_ps_rows(stdout: str) -> list[ProcessInfo]:
    out: list[ProcessInfo] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = _PS_ROW.match(stripped)
        if not m:
            continue
        pid_s, ppid_s, etime_s, args = m.groups()
        age_s = _parse_bsd_elapsed_time(etime_s)
        if age_s is None:
            continue
        try:
            out.append(ProcessInfo(pid=int(pid_s), ppid=int(ppid_s), age_s=age_s, args=args))
        except ValueError:
            continue
    return out


def _list_electron_processes(run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> list[ProcessInfo]:
    """Candidate ANCHOR processes: every `MacOS/Electron` row. This is a
    narrower view than `_run_ps`'s full process table — used only to FIND
    candidates; killing walks the full table again by `--user-data-dir=`
    value (see `_pids_sharing_user_data_dir`) to also catch helper/renderer
    processes that don't match this binary-name pattern.
    """
    stdout = _run_ps(run)
    if stdout is None:
        return []
    return [p for p in _parse_ps_rows(stdout) if _ELECTRON_PATTERN in p.args]


def _pids_sharing_user_data_dir(
    user_data_dir: str, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run
) -> list[int]:
    """Every pid (main + renderer/GPU/extension-host helpers) whose argv
    contains an EXACT `--user-data-dir=<value>` match, from a FRESH `ps`
    call. Returns `[]` (not the anchor) on a `ps` failure — the caller falls
    back to the anchor pid alone rather than treating failure as "found
    nothing to add", which would silently narrow a tree-kill to a
    single-pid kill without any visible signal (the failure itself is
    already warned by `_run_ps`).

    Uses `_carries_user_data_dir` (exact flag-value equality — review-caught
    P2), not a raw substring test (`user_data_dir in p.args`): a bare
    substring test matches any process whose argv merely MENTIONS the
    orphan's directory as a path fragment — a concurrent `du
    /tmp/hvsc-1-abc…` sweep, another `--user-data-dir=` whose value has this
    one as a prefix (`/tmp/hvsc-1-abc` inside `/tmp/hvsc-1-abcdef`) — and
    would SIGKILL an unrelated process.
    """
    stdout = _run_ps(run)
    if stdout is None:
        return []
    return [p.pid for p in _parse_ps_rows(stdout) if _carries_user_data_dir(p.args, user_data_dir)]


def _reread_args(pid: int, run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run) -> Optional[str]:
    """Re-read `pid`'s CURRENT argv immediately before killing it — the
    PID-reuse race guard: a pid gathered by an earlier `ps` snapshot may have
    exited and been recycled by an unrelated process by the time we get
    around to signalling it. Returns None when the pid no longer exists
    (`ps -p` exits non-zero) or on any read failure — the caller must treat
    that as "do not kill", not as "assume it's still the same process".

    `-ww` (review-caught P1, agent-tools#477 round 5, same as `_run_ps`
    above): without it, a truncated re-read could falsely report "no
    --user-data-dir= value" for a genuinely still-matching process, refusing
    a legitimate kill -- fail-safe direction, but still wrong."""
    try:
        proc = run(["ps", "-o", "args=", "-p", str(pid), "-ww"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    args = proc.stdout.strip()
    return args or None


def _kill_tree(
    anchor: ProcessInfo,
    *,
    list_by_user_data_dir: Callable[[str], list[int]],
    reread_args: Callable[[int], Optional[str]],
    kill: Callable[[int], None],
) -> list[int]:
    """Kill `anchor` and every process sharing its `--user-data-dir=` value,
    each re-verified immediately before signalling. Returns the pids actually
    killed. Falls back to the anchor pid alone when no value is extractable
    (e.g. a malformed/unexpected argv shape) or the lookup fails."""
    user_data_dir = _extract_user_data_dir(anchor.args)
    candidates = list_by_user_data_dir(user_data_dir) if user_data_dir else []
    if anchor.pid not in candidates:
        candidates = [anchor.pid, *candidates]

    killed: list[int] = []
    for pid in candidates:
        current_args = reread_args(pid)
        if current_args is None:
            continue  # already gone — not an error, just nothing left to do
        # `_carries_user_data_dir` on the RE-READ process's own argv (exact
        # match, review-caught P2, same predicate as
        # `_pids_sharing_user_data_dir` above — see that function's
        # docstring for why a substring test here would let a pid recycled
        # into an unrelated process sail through the PID-reuse guard and get
        # SIGKILLed, defeating the whole point of re-reading before killing).
        #
        # No `user_data_dir` extracted (review-caught P2, agent-tools#477
        # round 4): this is the anchor-only fallback (malformed/unexpected
        # argv shape) — `candidates` is just `[anchor.pid]` here, nothing to
        # compare a shared value against. Falling through with NO check at
        # all would defeat the PID-reuse guard entirely for this branch: if
        # the anchor exited and its pid got reused by an unrelated process
        # between listing and this re-read, that process would be killed
        # unconditionally. Falls back to exact identity against the
        # ORIGINAL anchor argv instead — the strongest re-verification
        # available without a partial value to key on.
        if user_data_dir is not None:
            if not _carries_user_data_dir(current_args, user_data_dir):
                continue  # PID reused by an unrelated process since listing — do NOT kill
        elif current_args != anchor.args:
            continue  # PID reused by an unrelated process since listing — do NOT kill
        try:
            kill(pid)
            killed.append(pid)
        except (ProcessLookupError, PermissionError):
            continue  # gone the instant before kill, or not ours to signal — best effort
    return killed


def _default_kill(pid: int) -> None:
    os.kill(pid, signal.SIGKILL)


def sweep(
    list_processes: Optional[Callable[[], list[ProcessInfo]]] = None,
    list_by_user_data_dir: Optional[Callable[[str], list[int]]] = None,
    reread_args: Optional[Callable[[int], Optional[str]]] = None,
    kill: Optional[Callable[[int], None]] = None,
    dry_run: bool = False,
) -> ReapReport:
    # Defaults resolved INSIDE the body (not bound as parameter defaults) so
    # monkeypatching the module-level `_list_electron_processes` /
    # `_pids_sharing_user_data_dir` / `_reread_args` names — as `main()`'s
    # tests do, exercising the real end-to-end call path `main()` itself
    # takes — actually takes effect. A parameter default binds the original
    # function object at import time and silently ignores a patch (the same
    # footgun documented and fixed for `read_pressure_level` in the sibling
    # heavy-op-memory-gate hook).
    list_processes = list_processes or _list_electron_processes
    list_by_user_data_dir = list_by_user_data_dir or _pids_sharing_user_data_dir
    reread_args = reread_args or _reread_args
    kill = kill or _default_kill

    report = ReapReport(dry_run=dry_run)
    for info in list_processes():
        if not is_orphaned_isolated_instance(info.args, info.ppid, info.age_s):
            # Includes both "not isolated at all" (never touched — positive-match
            # only) and "isolated but still within its age grace" (reported as
            # skipped so `main()`'s summary distinguishes the two).
            if _has_isolation_markers(info.args):
                report.skipped.append(info)
            continue
        if dry_run:
            report.reaped.append(info)
            continue
        killed_pids = _kill_tree(
            info, list_by_user_data_dir=list_by_user_data_dir, reread_args=reread_args, kill=kill
        )
        if killed_pids:
            report.reaped.append(info)
            report.killed_pids.extend(killed_pids)
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report only, kill nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable report on stdout")
    args = parser.parse_args(argv)

    report = sweep(dry_run=args.dry_run)

    if args.json:
        print(json.dumps(report.to_dict()))
    else:
        verb = "would reap" if args.dry_run else "reaped"
        if report.reaped:
            for p in report.reaped:
                print(
                    f"reap-vscode-orphans: {verb} pid={p.pid} ppid={p.ppid} "
                    f"age={round(p.age_s / 60)}min"
                )
        killed_note = "" if args.dry_run else f" ({len(report.killed_pids)} process(es) signalled, incl. helpers)"
        print(
            f"reap-vscode-orphans: {verb} {len(report.reaped)} orphan(s){killed_note}, "
            f"{len(report.skipped)} isolated instance(s) still within age grace"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

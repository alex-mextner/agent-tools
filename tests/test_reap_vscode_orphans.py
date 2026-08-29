"""Tests for ops/vscode-orphan-reaper/reap_vscode_orphans.py — the periodic
launchd sweep for leaked, isolated VS Code E2E-harness Electron windows.

Complementary to the pre-launch sweep in `ext-test-projects/e2e/setup/
electron-app.ts` (`reapOrphanedIsolatedVSCodeProcesses` /
`isOrphanedIsolatedInstance`) — this Python script mirrors the same predicate for
the residual case: no new launch happens for a while, so the pre-launch sweep
never fires. See the script's own module docstring for the full incident writeup
and the documented (unenforced) coupling between the two implementations.

Covers three real bugs caught by independent review before this shipped:
  1. `ps -eo ... etimes= ...` is a Linux/procps keyword, not a macOS one — BSD
     `ps` silently drops it (still exits 0) and the old row parser then matched
     zero real rows, forever. Fixed via `etime=` + `_parse_bsd_elapsed_time`.
  2. Killing only the anchor (Electron main) pid leaves renderer/GPU/
     extension-host helper processes running. Fixed via tree-kill by shared
     `--user-data-dir=` value (`_kill_tree` / `_pids_sharing_user_data_dir`).
  3. A pid gathered by an earlier snapshot could be reused by an unrelated
     process before the kill signal reaches it. Fixed via `_reread_args`
     re-verification immediately before each kill.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_reap_vscode_orphans.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "ops"
    / "vscode-orphan-reaper"
    / "reap_vscode_orphans.py"
)
_spec = importlib.util.spec_from_file_location("reap_vscode_orphans", _SCRIPT)
assert _spec and _spec.loader
reaper = importlib.util.module_from_spec(_spec)
# Register in sys.modules BEFORE exec: the module defines @dataclass classes, and
# dataclass field resolution looks the module up via sys.modules[cls.__module__] —
# without this it raises AttributeError on a None lookup (Python 3.13).
sys.modules[_spec.name] = reaper
_spec.loader.exec_module(reaper)

ISOLATED_ARGS = (
    "/Applications/Visual Studio Code.app/Contents/MacOS/Electron "
    "--disable-workspace-trust --user-data-dir=/tmp/hvsc-2-deadbeef "
    "--extensionDevelopmentPath=/ext/path"
)
REAL_EDITOR_ARGS = (
    "/Applications/Visual Studio Code.app/Contents/MacOS/Electron "
    "--user-data-dir=/Users/ultra/Library/Application Support/Code"
)


# ── _parse_bsd_elapsed_time — the exact bug that shipped once already ────────

def test_parses_mm_ss():
    assert reaper._parse_bsd_elapsed_time("00:00") == 0
    assert reaper._parse_bsd_elapsed_time("05:23") == 5 * 60 + 23


def test_parses_hh_mm_ss():
    assert reaper._parse_bsd_elapsed_time("01:05:23") == 3600 + 5 * 60 + 23


def test_parses_dd_hh_mm_ss_the_real_launchd_shape_seen_live():
    assert reaper._parse_bsd_elapsed_time("46-03:41:12") == 46 * 86400 + 3 * 3600 + 41 * 60 + 12


def test_tolerates_surrounding_whitespace():
    assert reaper._parse_bsd_elapsed_time("  00:05  ") == 5


def test_returns_none_on_unparseable_input():
    assert reaper._parse_bsd_elapsed_time("not-a-time") is None
    assert reaper._parse_bsd_elapsed_time("") is None
    assert reaper._parse_bsd_elapsed_time("900") is None  # the OLD (wrong) etimes=-style raw number


# ── is_orphaned_isolated_instance — pure predicate ────────────────────────────

def test_never_reaps_missing_isolation_markers():
    assert reaper.is_orphaned_isolated_instance(REAL_EDITOR_ARGS, ppid=1, age_s=reaper.STALE_MIN_AGE_S * 10) is False


def test_young_reparented_instance_gets_grace():
    assert reaper.is_orphaned_isolated_instance(ISOLATED_ARGS, ppid=1, age_s=reaper.REPARENTED_MIN_AGE_S - 1) is False


def test_reparented_instance_reaped_past_short_bar():
    assert reaper.is_orphaned_isolated_instance(ISOLATED_ARGS, ppid=1, age_s=reaper.REPARENTED_MIN_AGE_S) is True


def test_live_parent_instance_not_reaped_below_stale_bar():
    assert reaper.is_orphaned_isolated_instance(ISOLATED_ARGS, ppid=4242, age_s=reaper.STALE_MIN_AGE_S - 1) is False


def test_live_parent_instance_reaped_past_stale_bar():
    assert reaper.is_orphaned_isolated_instance(ISOLATED_ARGS, ppid=4242, age_s=reaper.STALE_MIN_AGE_S) is True


# ── _extract_user_data_dir ────────────────────────────────────────────────────

def test_extracts_user_data_dir_value():
    assert reaper._extract_user_data_dir(ISOLATED_ARGS) == "/tmp/hvsc-2-deadbeef"


def test_extract_user_data_dir_none_when_absent():
    assert reaper._extract_user_data_dir("some other args with no marker") is None


def test_extract_user_data_dir_ignores_the_flag_embedded_in_an_unrelated_arguments_value():
    """Review-caught P1 (agent-tools#477 round 2): a real `--user-data-dir=`
    flag always starts at a token boundary (`ps` joins argv with spaces), so
    the regex must not match the literal text "--user-data-dir=" occurring
    INSIDE another argument's own value -- e.g. some unrelated flag whose
    value happens to embed that exact string as one argv token."""
    assert reaper._extract_user_data_dir(
        "/usr/bin/unrelated --note=--user-data-dir=/tmp/hvsc-2-deadbeef"
    ) is None


def test_extract_user_data_dir_matches_at_start_of_string():
    assert reaper._extract_user_data_dir("--user-data-dir=/tmp/hvsc-2-deadbeef --flag") == "/tmp/hvsc-2-deadbeef"


def test_extract_user_data_dir_skips_a_real_profile_flag_to_find_the_hvsc_one():
    """Review-caught P1 (agent-tools#477 round 3): a process with TWO
    --user-data-dir= flags -- a real profile first, an isolated hvsc-*
    override second (the shape a wrapper script that appends an override
    flag produces) -- must extract the hvsc-* value, not the first
    occurrence. `re.search` alone (no value-shape requirement) would return
    the real profile's path, and every OTHER process sharing THAT path
    (i.e. a live real VS Code session) would then look like a match. Mirrors
    the equivalent fix on the TypeScript side (extractUserDataDirValue)."""
    args = (
        "/Applications/Visual Studio Code.app/Contents/MacOS/Electron "
        "--user-data-dir=/Users/ultra/Library/Application Support/Code "
        "--user-data-dir=/tmp/hvsc-2-deadbeef --extensionDevelopmentPath=/ext/path"
    )
    assert reaper._extract_user_data_dir(args) == "/tmp/hvsc-2-deadbeef"


def test_extract_user_data_dir_none_for_a_non_hvsc_value_even_if_present():
    """A --user-data-dir= value that doesn't look like this convention's
    hvsc-<n>-... shape is not extracted at all -- e.g. a real editor session
    with no isolated flag anywhere has nothing for this function to find."""
    assert reaper._extract_user_data_dir(REAL_EDITOR_ARGS) is None


# ── _carries_user_data_dir ──────────────────────────────────────────────────

def test_carries_user_data_dir_true_for_exact_match():
    assert reaper._carries_user_data_dir(ISOLATED_ARGS, "/tmp/hvsc-2-deadbeef") is True


def test_carries_user_data_dir_false_for_embedded_mention():
    args = "/usr/bin/unrelated --note=--user-data-dir=/tmp/hvsc-2-deadbeef"
    assert reaper._carries_user_data_dir(args, "/tmp/hvsc-2-deadbeef") is False


# ── _kill_tree — tree kill + PID-reuse re-verification ────────────────────────

def _anchor(pid=100, ppid=1, age_s=None, args=ISOLATED_ARGS):
    if age_s is None:
        age_s = reaper.STALE_MIN_AGE_S
    return reaper.ProcessInfo(pid=pid, ppid=ppid, age_s=age_s, args=args)


def test_kill_tree_kills_anchor_and_every_sibling_sharing_the_user_data_dir():
    """The tree-kill fix: renderer/GPU helper pids (101, 102) don't match the
    Electron binary pattern used to FIND the anchor, but share its
    --user-data-dir= value and must be killed too."""
    anchor = _anchor(pid=100)
    killed = []
    result = reaper._kill_tree(
        anchor,
        list_by_user_data_dir=lambda udd: [100, 101, 102] if udd == "/tmp/hvsc-2-deadbeef" else [],
        reread_args=lambda pid: ISOLATED_ARGS,  # still there, still carries the marker
        kill=lambda pid: killed.append(pid),
    )
    assert sorted(result) == [100, 101, 102]
    assert sorted(killed) == [100, 101, 102]


def test_kill_tree_falls_back_to_anchor_only_when_no_user_data_dir_extractable():
    anchor = _anchor(pid=100, args="Electron --extensionDevelopmentPath=/x (no user-data-dir flag)")
    killed = []
    result = reaper._kill_tree(
        anchor,
        list_by_user_data_dir=lambda udd: [999],  # must never be called with no value
        reread_args=lambda pid: anchor.args,
        kill=lambda pid: killed.append(pid),
    )
    assert result == [100]
    assert killed == [100]


def test_kill_tree_refuses_to_kill_a_pid_whose_reread_args_no_longer_matches():
    """PID-reuse race guard: pid 101 was in the token-scan snapshot, but by
    the time we re-read it right before killing, its argv no longer carries
    the marker — it's a DIFFERENT process now (reused pid). Must not kill it."""
    anchor = _anchor(pid=100)

    def reread(pid):
        if pid == 100:
            return ISOLATED_ARGS
        return "some totally unrelated process --flag=x"  # pid 101 was reused

    killed = []
    result = reaper._kill_tree(
        anchor,
        list_by_user_data_dir=lambda udd: [100, 101],
        reread_args=reread,
        kill=lambda pid: killed.append(pid),
    )
    assert result == [100]
    assert killed == [100]


def test_kill_tree_refuses_to_kill_a_pid_reused_by_a_process_that_merely_mentions_the_path():
    """Same P2, the PID-reuse re-verification side: pid 101 is recycled into
    an unrelated `du`/cleanup process whose argv happens to CONTAIN the
    target directory as a substring (not its own --user-data-dir= value). A
    substring re-check here would wave it through and SIGKILL it despite the
    PID-reuse guard existing for exactly this purpose."""
    anchor = _anchor(pid=100)

    def reread(pid):
        if pid == 100:
            return ISOLATED_ARGS
        return "/usr/bin/du -sh /tmp/hvsc-2-deadbeef"  # pid 101 reused by an unrelated du

    killed = []
    result = reaper._kill_tree(
        anchor,
        list_by_user_data_dir=lambda udd: [100, 101],
        reread_args=reread,
        kill=lambda pid: killed.append(pid),
    )
    assert result == [100]
    assert killed == [100]


def test_kill_tree_skips_a_pid_that_already_exited(monkeypatch):
    anchor = _anchor(pid=100)

    def reread(pid):
        return None if pid == 101 else ISOLATED_ARGS  # 101 already gone

    killed = []
    result = reaper._kill_tree(
        anchor,
        list_by_user_data_dir=lambda udd: [100, 101],
        reread_args=reread,
        kill=lambda pid: killed.append(pid),
    )
    assert result == [100]
    assert killed == [100]


def test_kill_tree_catches_permission_error_and_continues():
    anchor = _anchor(pid=100)

    def kill(pid):
        if pid == 100:
            raise PermissionError()
        pass

    result = reaper._kill_tree(
        anchor,
        list_by_user_data_dir=lambda udd: [100, 101],
        reread_args=lambda pid: ISOLATED_ARGS,
        kill=kill,
    )
    assert result == [101]  # 100 failed with PermissionError, 101 still reaped


def test_kill_tree_catches_process_lookup_error_and_continues():
    anchor = _anchor(pid=100)

    def kill(pid):
        if pid == 100:
            raise ProcessLookupError()

    result = reaper._kill_tree(
        anchor,
        list_by_user_data_dir=lambda udd: [100, 101],
        reread_args=lambda pid: ISOLATED_ARGS,
        kill=kill,
    )
    assert result == [101]


# ── sweep — orchestration over injected processes/tree-kill/reread seams ─────

def test_sweep_kills_only_confirmed_orphans():
    procs = [
        _anchor(pid=100, ppid=1, age_s=reaper.REPARENTED_MIN_AGE_S),         # orphan
        _anchor(pid=200, ppid=555, age_s=10),                                # live parent, young -> skip
        _anchor(pid=300, ppid=1, age_s=reaper.STALE_MIN_AGE_S * 5, args=REAL_EDITOR_ARGS),  # not isolated
    ]
    killed = []
    report = reaper.sweep(
        list_processes=lambda: procs,
        list_by_user_data_dir=lambda udd: [],
        reread_args=lambda pid: ISOLATED_ARGS,
        kill=lambda pid: killed.append(pid),
    )
    assert killed == [100]
    assert [p.pid for p in report.reaped] == [100]
    assert report.killed_pids == [100]
    assert [p.pid for p in report.skipped] == [200]


def test_sweep_reports_helper_pids_in_killed_pids_but_anchor_in_reaped():
    anchor = _anchor(pid=100)
    report = reaper.sweep(
        list_processes=lambda: [anchor],
        list_by_user_data_dir=lambda udd: [100, 101, 102],
        reread_args=lambda pid: ISOLATED_ARGS,
        kill=lambda pid: None,
    )
    assert [p.pid for p in report.reaped] == [100]  # one ANCHOR reported...
    assert sorted(report.killed_pids) == [100, 101, 102]  # ...but 3 pids actually signalled


def test_sweep_dry_run_never_kills():
    procs = [_anchor(pid=100, age_s=reaper.STALE_MIN_AGE_S)]
    killed = []
    report = reaper.sweep(
        list_processes=lambda: procs,
        kill=lambda pid: killed.append(pid),
        dry_run=True,
    )
    assert killed == []
    assert report.dry_run is True
    assert [p.pid for p in report.reaped] == [100]  # reported as "would reap"
    assert report.killed_pids == []


def test_sweep_handles_empty_process_list():
    report = reaper.sweep(list_processes=lambda: [])
    assert report.reaped == []
    assert report.skipped == []


def test_sweep_never_touches_non_isolated_processes():
    procs = [_anchor(pid=300, ppid=1, age_s=reaper.STALE_MIN_AGE_S * 5, args=REAL_EDITOR_ARGS)]
    killed = []
    report = reaper.sweep(list_processes=lambda: procs, kill=lambda pid: killed.append(pid))
    assert killed == []
    assert report.reaped == []
    assert report.skipped == []  # not isolated at all — not even reported as "in grace"


# ── _list_electron_processes / _run_ps — ps output parsing (real BSD shape) ──

class _FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_list_electron_processes_parses_real_bsd_etime_shape_and_filters_non_electron():
    ps_output = (
        f"  100     1  00:15:00 {ISOLATED_ARGS}\n"
        "  200     1  00:15:00 /usr/bin/some-other-app --flag\n"
        f" 300  4242     00:01:00 {REAL_EDITOR_ARGS}\n"
    )
    result = reaper._list_electron_processes(run=lambda *a, **k: _FakeCompletedProcess(ps_output))
    pids = sorted(p.pid for p in result)
    assert pids == [100, 300]
    # And the age actually parsed to real seconds, not silently dropped/zeroed:
    by_pid = {p.pid: p for p in result}
    assert by_pid[100].age_s == 15 * 60


def test_list_electron_processes_skips_a_row_with_an_unparseable_etime_column():
    """Regression pin for the exact bug that shipped once: if `ps` ever drops
    or mangles the elapsed-time column again, the row must be SKIPPED (and
    warned about via _run_ps's caller), never silently treated as age=0."""
    ps_output = f"  100     1  garbage {ISOLATED_ARGS}\n"
    result = reaper._list_electron_processes(run=lambda *a, **k: _FakeCompletedProcess(ps_output))
    assert result == []


def test_list_electron_processes_returns_empty_on_ps_failure(capsys):
    result = reaper._list_electron_processes(run=lambda *a, **k: _FakeCompletedProcess("", returncode=1, stderr="boom"))
    assert result == []
    assert "WARNING" in capsys.readouterr().err  # ps failure must be VISIBLE, not silent


def test_list_electron_processes_returns_empty_when_ps_raises(capsys):
    def raiser(*a, **k):
        raise OSError("ps not found")

    assert reaper._list_electron_processes(run=raiser) == []
    assert "WARNING" in capsys.readouterr().err


# ── _pids_sharing_user_data_dir / _reread_args ────────────────────────────────

def test_pids_sharing_user_data_dir_matches_helpers_too():
    ps_output = (
        f"  100     1  00:15:00 {ISOLATED_ARGS}\n"
        "  101   100  00:15:00 /Applications/Visual Studio Code.app/Contents/Frameworks/"
        "Code Helper (Renderer).app/Contents/MacOS/Code Helper (Renderer) "
        "--user-data-dir=/tmp/hvsc-2-deadbeef --type=renderer\n"
        "  999     1  00:15:00 /some/unrelated/process --user-data-dir=/tmp/other\n"
    )
    result = reaper._pids_sharing_user_data_dir(
        "/tmp/hvsc-2-deadbeef", run=lambda *a, **k: _FakeCompletedProcess(ps_output)
    )
    assert sorted(result) == [100, 101]


def test_pids_sharing_user_data_dir_excludes_a_process_that_merely_mentions_the_path():
    """Review-caught P2 (agent-tools#477): a substring test on the raw argv
    string matches ANY process whose args happen to mention the target
    directory as a path fragment — a concurrent `du`/`rm -rf` sweep of it,
    for example — not just processes that ACTUALLY carry it as their own
    `--user-data-dir=` value. Must match on the exact flag value only."""
    ps_output = (
        f"  100     1  00:15:00 {ISOLATED_ARGS}\n"
        "  200     1  00:15:00 /usr/bin/du -sh /tmp/hvsc-2-deadbeef\n"
        "  201     1  00:15:00 /bin/rm -rf /tmp/hvsc-2-deadbeef\n"
    )
    result = reaper._pids_sharing_user_data_dir(
        "/tmp/hvsc-2-deadbeef", run=lambda *a, **k: _FakeCompletedProcess(ps_output)
    )
    assert result == [100]


def test_pids_sharing_user_data_dir_excludes_a_process_with_the_flag_embedded_in_another_value():
    """Review-caught P1 (agent-tools#477 round 2): the SAME class of bug one
    level up the call stack -- a process whose argv embeds the literal text
    "--user-data-dir=<target>" inside an unrelated flag's own value (not as
    its own real flag) must not be treated as sharing the directory."""
    ps_output = (
        f"  100     1  00:15:00 {ISOLATED_ARGS}\n"
        "  400     1  00:15:00 /usr/bin/unrelated --note=--user-data-dir=/tmp/hvsc-2-deadbeef\n"
    )
    result = reaper._pids_sharing_user_data_dir(
        "/tmp/hvsc-2-deadbeef", run=lambda *a, **k: _FakeCompletedProcess(ps_output)
    )
    assert result == [100]


def test_pids_sharing_user_data_dir_excludes_a_value_with_this_path_as_a_prefix():
    """Same P2: another isolated instance's --user-data-dir= value that has
    the target directory as a STRING PREFIX (`/tmp/hvsc-2-deadbeef` inside
    `/tmp/hvsc-2-deadbeefxyz`) must not be swept in as a sibling."""
    ps_output = (
        f"  100     1  00:15:00 {ISOLATED_ARGS}\n"
        "  300     1  00:15:00 /Applications/Visual Studio Code.app/Contents/MacOS/Electron "
        "--user-data-dir=/tmp/hvsc-2-deadbeefxyz --extensionDevelopmentPath=/ext/path\n"
    )
    result = reaper._pids_sharing_user_data_dir(
        "/tmp/hvsc-2-deadbeef", run=lambda *a, **k: _FakeCompletedProcess(ps_output)
    )
    assert result == [100]


def test_pids_sharing_user_data_dir_empty_on_ps_failure(capsys):
    result = reaper._pids_sharing_user_data_dir(
        "/tmp/hvsc-2-deadbeef", run=lambda *a, **k: _FakeCompletedProcess("", returncode=1)
    )
    assert result == []
    assert "WARNING" in capsys.readouterr().err


def test_reread_args_returns_current_argv_for_a_live_pid():
    result = reaper._reread_args(123, run=lambda *a, **k: _FakeCompletedProcess(ISOLATED_ARGS + "\n"))
    assert result == ISOLATED_ARGS


def test_reread_args_none_for_a_dead_pid():
    result = reaper._reread_args(123, run=lambda *a, **k: _FakeCompletedProcess("", returncode=1))
    assert result is None


def test_reread_args_none_on_exception():
    def raiser(*a, **k):
        raise OSError("no such process")

    assert reaper._reread_args(123, run=raiser) is None


def test_reread_args_live_against_this_test_process():
    """Real ps integration (no fakes) — closes the untested-real-parsing gap
    review flagged: this test's own pid is guaranteed alive."""
    import os

    result = reaper._reread_args(os.getpid())
    assert result is not None
    assert len(result) > 0


# ── main() — CLI wiring, exercising sweep()'s REAL defaults via monkeypatch ──

def test_main_dry_run_reports_without_killing(monkeypatch, capsys):
    procs = [_anchor(pid=100, age_s=reaper.STALE_MIN_AGE_S)]
    killed = []
    monkeypatch.setattr(reaper, "_list_electron_processes", lambda run=None: procs)
    monkeypatch.setattr(reaper, "_default_kill", lambda pid: killed.append(pid))
    code = reaper.main(["--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "would reap" in out
    assert killed == []  # dry-run: sweep() never reaches _kill_tree at all


def test_main_real_run_kills_via_the_real_default_wiring(monkeypatch, capsys):
    """Exercises main() -> sweep() -> _kill_tree with ONLY the module-level
    defaults monkeypatched (not `sweep` itself) — the fix for the vacuous
    version of this test review flagged (which mocked `sweep` wholesale, so
    neither `killed` nor the `_list_electron_processes` patch were live)."""
    procs = [_anchor(pid=100, age_s=reaper.STALE_MIN_AGE_S)]
    killed = []
    monkeypatch.setattr(reaper, "_list_electron_processes", lambda run=None: procs)
    monkeypatch.setattr(reaper, "_pids_sharing_user_data_dir", lambda udd, run=None: [100])
    monkeypatch.setattr(reaper, "_reread_args", lambda pid, run=None: ISOLATED_ARGS)
    monkeypatch.setattr(reaper, "_default_kill", lambda pid: killed.append(pid))
    code = reaper.main([])
    assert code == 0
    assert killed == [100]
    out = capsys.readouterr().out
    assert "reaped 1 orphan" in out


def test_main_json_output_is_valid_json(monkeypatch, capsys):
    monkeypatch.setattr(reaper, "_list_electron_processes", lambda run=None: [])
    code = reaper.main(["--json"])
    assert code == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["reaped_count"] == 0
    assert payload["killed_pid_count"] == 0

"""Tests for the rig-detached-opencode launcher — the canonical rig-owned detached-agent
launcher for opencode (#476), shipped as the `rig-detached-opencode` universal SKILL
(skills/universal/rig-detached-opencode/) so rig provisions it to
~/.agents/skills/rig-detached-opencode/ on every managed machine (PR #497: `bin/` is not
a rig-discovered carrier).

The launcher exports RIG_AGENT_ID=<name> and RIG_DETACHED_AGENT=1 for a nohup-detached
`opencode run` child, so the opencode hook bridge classifies the child session as a
dispatched subagent on every tool call. These tests NEVER launch a real opencode: the
happy path shadows `opencode` with a stub executable that records its environment and
arguments, echoes into the launcher's log file, and sleeps long enough to prove the
launcher returned without waiting for it.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_rig_detached_opencode.py -q
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

_LAUNCHER = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "universal"
    / "rig-detached-opencode"
    / "rig-detached-opencode"
)
_LOG_DIR = Path("/tmp/agent-logs")

_STUB_SLEEP_S = 2.0


@pytest.fixture(scope="module", autouse=True)
def _launcher_executable():
    if not os.access(_LAUNCHER, os.X_OK):
        _LAUNCHER.chmod(0o755)


def _stub_opencode(bin_dir: Path, marker: Path) -> None:
    """A stub `opencode` that records env/args/cwd, streams into stdout (which the
    launcher redirects into the agent log), and sleeps past the launcher's return."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "opencode"
    stub.write_text(
        "#!/bin/sh\n"
        "{\n"
        f'  echo "RIG_AGENT_ID=$RIG_AGENT_ID"\n'
        f'  echo "RIG_DETACHED_AGENT=$RIG_DETACHED_AGENT"\n'
        f'  echo "PWD=$PWD"\n'
        f'  echo "ARGS=$*"\n'
        f'}} > "{marker}"\n'
        'echo "stub-opencode-started"\n'
        f"sleep {_STUB_SLEEP_S}\n"
        'echo "stub-opencode-done"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run_launcher(
    *args: str,
    path_override: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if path_override is not None:
        # Stub dir FIRST so the stub shadows any real opencode on PATH.
        env["PATH"] = str(path_override) + os.pathsep + env.get("PATH", "")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(_LAUNCHER), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _poll_for(path: Path, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    pytest.fail(f"{path} never appeared within {timeout_s}s")


# ── argument validation → exit 2 ──────────────────────────────────────────────────────────

def test_no_args_prints_usage_and_exits_2():
    proc = _run_launcher()
    assert proc.returncode == 2
    assert "usage:" in proc.stderr.lower()


def test_too_many_args_exits_2():
    proc = _run_launcher("a", "b", "c", "d")
    assert proc.returncode == 2
    assert "usage:" in proc.stderr.lower()


@pytest.mark.parametrize("bad", ["bad name", "../evil", "-leading", "a/b", ""])
def test_invalid_agent_name_exits_2(tmp_path, bad):
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")
    proc = _run_launcher(bad, str(brief))
    assert proc.returncode == 2
    assert "invalid agent name" in proc.stderr


def test_missing_brief_file_exits_2(tmp_path):
    proc = _run_launcher("agent-x", str(tmp_path / "nope.md"))
    assert proc.returncode == 2
    assert "missing or empty" in proc.stderr


def test_empty_brief_file_exits_2(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("", encoding="utf-8")
    proc = _run_launcher("agent-x", str(brief))
    assert proc.returncode == 2
    assert "missing or empty" in proc.stderr


def test_missing_workdir_exits_2(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")
    proc = _run_launcher("agent-x", str(brief), str(tmp_path / "no-dir"))
    assert proc.returncode == 2
    assert "not a directory" in proc.stderr


def test_opencode_not_on_path_exits_127(tmp_path, monkeypatch):
    """No `opencode` resolvable at all → 127 (mirrors the shell's not-found code).
    PATH keeps the OS utility dirs (bash/date/mkdir/nohup for the launcher's shebang and
    body) but drops every dir that can hold a real opencode (homebrew, npm globals)."""
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    brief = tmp_path / "brief.md"
    brief.write_text("do the thing", encoding="utf-8")
    proc = _run_launcher("agent-x", str(brief))
    assert proc.returncode == 127
    assert "not found on PATH" in proc.stderr


# ── happy path with a stub opencode ───────────────────────────────────────────────────────

def test_happy_path_exports_markers_and_detaches(tmp_path):
    name = "oc476-test-agent"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    log = _LOG_DIR / f"{name}.log"
    log.unlink(missing_ok=True)
    _stub_opencode(stub_dir, marker)

    workdir = tmp_path / "work"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("Mission: write ok to the handoff file and exit.", encoding="utf-8")

    started = time.monotonic()
    proc = _run_launcher(name, str(brief), str(workdir), path_override=stub_dir)
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stderr
    # Detachment: the launcher must return well before the stub's sleep finishes.
    assert elapsed < _STUB_SLEEP_S - 0.5, f"launcher blocked for {elapsed:.2f}s"
    assert f"detached agent '{name}' launched (pid" in proc.stdout
    assert str(log) in proc.stdout

    # The launcher appended its own launch line to the log immediately.
    _poll_for(log)
    first_line = log.read_text(encoding="utf-8").splitlines()[0]
    assert f"launching detached agent '{name}'" in first_line

    # The child ran with the identity markers exported, in the requested workdir,
    # with the brief's content as the `opencode run` message.
    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    assert f"RIG_AGENT_ID={name}" in recorded
    assert "RIG_DETACHED_AGENT=1" in recorded
    assert f"PWD={workdir}" in recorded
    assert "run --title" in recorded
    assert name in recorded
    assert "write ok to the handoff file" in recorded

    # The child's stdout streams into the same log file.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if "stub-opencode-started" in log.read_text(encoding="utf-8"):
            break
        time.sleep(0.1)
    assert "stub-opencode-started" in log.read_text(encoding="utf-8")


def test_relative_brief_with_different_workdir_reaches_the_child(tmp_path, monkeypatch):
    """A RELATIVE brief path + a DIFFERENT [workdir] must still deliver the brief's
    content to the child (codex P1, PR #497).

    Validation (`[ -s "$brief" ]`) ran against the caller's cwd, but the launcher then
    `cd`s to workdir; a `$(cat "$brief")` evaluated AFTER the cd resolved the relative
    path in the wrong directory, failed silently inside the backgrounded command
    substitution, and launched opencode with an EMPTY prompt while exiting 0. The
    launcher now reads the brief before changing directory."""
    name = "oc476-test-relative-brief"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_opencode(stub_dir, marker)

    caller_cwd = tmp_path / "caller"
    caller_cwd.mkdir()
    (caller_cwd / "brief.md").write_text(
        "Mission: relative-brief-sentinel must reach the child.", encoding="utf-8"
    )
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    # Not in workdir: a post-cd `cat brief.md` would fail there.
    assert not (workdir / "brief.md").exists()

    monkeypatch.chdir(caller_cwd)
    proc = _run_launcher(name, "brief.md", str(workdir), path_override=stub_dir)

    assert proc.returncode == 0, proc.stderr
    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    assert f"PWD={workdir}" in recorded
    assert "relative-brief-sentinel must reach the child" in recorded


def test_log_dir_is_private_and_symlink_log_is_refused(tmp_path):
    """Review finding (GH-497 round 1): the log holds the brief + the child's transcript.
    The launcher must create a 0700 log dir it owns, write a 0600 log, and refuse a
    pre-planted symlink at the log path instead of following it."""
    name = "oc476-test-private-log"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_opencode(stub_dir, marker)
    brief = tmp_path / "brief.md"
    brief.write_text("private brief", encoding="utf-8")
    log_dir = tmp_path / "agent-logs"

    proc = _run_launcher(
        name, str(brief), str(tmp_path), path_override=stub_dir,
        extra_env={"RIG_AGENT_LOG_DIR": str(log_dir)},
    )
    assert proc.returncode == 0, proc.stderr
    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    log = log_dir / f"{name}.log"
    _poll_for(log)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600

    # a symlink planted at the log path of ANOTHER agent name must be refused, not followed
    victim = tmp_path / "victim.txt"
    victim.write_text("do not clobber", encoding="utf-8")
    planted = log_dir / "oc476-test-planted.log"
    planted.symlink_to(victim)
    proc = _run_launcher(
        "oc476-test-planted", str(brief), str(tmp_path), path_override=stub_dir,
        extra_env={"RIG_AGENT_LOG_DIR": str(log_dir)},
    )
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    assert victim.read_text(encoding="utf-8") == "do not clobber"


def test_happy_path_defaults_workdir_to_cwd(tmp_path, monkeypatch):
    name = "oc476-test-cwd-agent"
    stub_dir = tmp_path / "bin"
    marker = tmp_path / "stub-env.txt"
    _stub_opencode(stub_dir, marker)
    workdir = tmp_path / "here"
    workdir.mkdir()
    brief = tmp_path / "brief.md"
    brief.write_text("brief body", encoding="utf-8")

    monkeypatch.chdir(workdir)
    proc = _run_launcher(name, str(brief), path_override=stub_dir)

    assert proc.returncode == 0, proc.stderr
    _poll_for(marker)
    recorded = marker.read_text(encoding="utf-8")
    assert f"PWD={workdir}" in recorded


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))

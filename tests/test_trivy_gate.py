"""Tests for the ci/trivy/ gate — the Trivy filesystem-scan CI slot (agent-tools#24).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_trivy_gate.py -q

The gate is a portable POSIX shell runner (``ci/trivy/trivy-scan.sh``) wrapping
``trivy fs``. These tests shell out to the real script with a STUBBED ``trivy`` on PATH (so no
network / no real scanner is needed) and assert the gate's contract:

- **block tier**: when trivy exits non-zero (a finding), the runner FAILS (exit != 0). This is
  the load-bearing regression: an earlier impl gated on ``if trivy …; then`` and read ``$?`` in
  the fall-through, where ``$?`` is the *if-statement's* status (0 when the condition is false)
  — so it printed FAIL but exited 0, leaving CI green on a real HIGH/CRITICAL finding;
- **pass**: a clean scan (trivy exit 0) → runner exit 0;
- **warn tier** (``TRIVY_WARN=1``): never fails the build, and passes ``--exit-code 0`` to
  trivy so the scanner itself doesn't fail either;
- **arg construction**: scanners / severity / target / ignore-unfixed flow into the trivy
  invocation, and the knobs override them.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "ci" / "trivy" / "trivy-scan.sh"


def _stub_trivy(bin_dir: Path, *, scan_exit: int) -> Path:
    """Write a fake ``trivy`` on PATH: ``--version`` prints + exits 0; a scan echoes its argv
    to a sidecar file and exits *scan_exit*."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    argv_log = bin_dir / "trivy-argv.txt"
    stub = bin_dir / "trivy"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "Version: 0.0.0-stub"; exit 0; fi\n'
        f'printf "%s\\n" "$*" > "{argv_log}"\n'
        f"exit {scan_exit}\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return argv_log


def _run(cwd: Path, bin_dir: Path, env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["sh", str(SCRIPT)], cwd=cwd, env=env, capture_output=True, text=True, timeout=60
    )
    return proc.returncode, proc.stderr


def test_block_tier_fails_on_finding(tmp_path: Path):
    """REGRESSION: a finding (trivy exit 1) must FAIL the runner, not exit 0."""
    bin_dir = tmp_path / "bin"
    _stub_trivy(bin_dir, scan_exit=1)
    rc, err = _run(tmp_path, bin_dir)
    assert rc != 0, f"block tier must fail on a finding; got exit 0\n{err}"
    assert "FAIL" in err


def test_block_tier_passes_when_clean(tmp_path: Path):
    """A clean scan (trivy exit 0) → runner passes."""
    bin_dir = tmp_path / "bin"
    _stub_trivy(bin_dir, scan_exit=0)
    rc, err = _run(tmp_path, bin_dir)
    assert rc == 0, err
    assert "PASS" in err


def test_warn_tier_never_fails_and_sets_exit_code_zero(tmp_path: Path):
    """TRIVY_WARN=1: runner passes AND tells trivy --exit-code 0 (so the scanner won't fail)."""
    bin_dir = tmp_path / "bin"
    argv_log = _stub_trivy(bin_dir, scan_exit=0)
    rc, err = _run(tmp_path, bin_dir, env_extra={"TRIVY_WARN": "1"})
    assert rc == 0, err
    assert "WARN tier" in err
    assert "--exit-code 0" in argv_log.read_text()


def test_warn_tier_does_not_fail_on_scanner_error(tmp_path: Path):
    """TRIVY_WARN=1 must NOT fail even when trivy exits non-zero (DB error / bad flag).

    Regression: warn previously relied solely on passing --exit-code 0, which only suppresses
    the FINDINGS exit. An operational trivy error still returns non-zero; warn must exit 0
    unconditionally to honor its 'never fails the build' contract.
    """
    bin_dir = tmp_path / "bin"
    _stub_trivy(bin_dir, scan_exit=2)  # 2 = trivy operational error
    rc, err = _run(tmp_path, bin_dir, env_extra={"TRIVY_WARN": "1"})
    assert rc == 0, f"warn tier must not fail on a scanner error; got {rc}\n{err}"
    assert "WARN tier" in err


def test_block_tier_fails_on_scanner_error(tmp_path: Path):
    """Block tier fails on a non-zero trivy exit even when it's an error, not a finding —
    a broken scan must not pass as clean."""
    bin_dir = tmp_path / "bin"
    _stub_trivy(bin_dir, scan_exit=2)
    rc, err = _run(tmp_path, bin_dir)
    assert rc == 2, err
    assert "FAIL" in err


def test_target_and_extra_flow_into_invocation(tmp_path: Path):
    """TRIVY_TARGET lands at the END of argv; TRIVY_EXTRA flags are word-split through."""
    bin_dir = tmp_path / "bin"
    argv_log = _stub_trivy(bin_dir, scan_exit=0)
    sub = tmp_path / "sub"
    sub.mkdir()
    _run(
        tmp_path,
        bin_dir,
        env_extra={"TRIVY_TARGET": "sub", "TRIVY_EXTRA": "--skip-dirs node_modules --quiet"},
    )
    argv = argv_log.read_text().strip()
    assert argv.endswith(" sub"), argv  # target is the last positional
    assert "--skip-dirs node_modules" in argv
    assert "--quiet" in argv


def test_default_args(tmp_path: Path):
    """Default invocation builds the expected trivy fs argv."""
    bin_dir = tmp_path / "bin"
    argv_log = _stub_trivy(bin_dir, scan_exit=0)
    _run(tmp_path, bin_dir)
    argv = argv_log.read_text()
    assert argv.startswith("fs ")
    assert "--scanners vuln,secret,misconfig" in argv
    assert "--severity HIGH,CRITICAL" in argv
    assert "--exit-code 1" in argv  # block tier passes 1 to trivy
    assert "--ignore-unfixed" in argv


def test_knobs_override_args(tmp_path: Path):
    """TRIVY_SEVERITY / TRIVY_SCANNERS / TRIVY_IGNORE_UNFIXED=0 flow into the invocation."""
    bin_dir = tmp_path / "bin"
    argv_log = _stub_trivy(bin_dir, scan_exit=0)
    _run(
        tmp_path,
        bin_dir,
        env_extra={
            "TRIVY_SEVERITY": "CRITICAL",
            "TRIVY_SCANNERS": "vuln",
            "TRIVY_IGNORE_UNFIXED": "0",
        },
    )
    argv = argv_log.read_text()
    assert "--severity CRITICAL" in argv
    assert "--scanners vuln " in argv
    assert "--ignore-unfixed" not in argv


def _only_sh_path(tmp_path: Path) -> str:
    """A PATH containing ONLY `sh` (symlinked) — no trivy / brew / curl resolvable."""
    only_sh = tmp_path / "onlysh"
    only_sh.mkdir()
    os.symlink(shutil.which("sh") or "/bin/sh", only_sh / "sh")
    return str(only_sh)


def test_autoinstall_off_by_default_fails_closed(tmp_path: Path):
    """Default (TRIVY_AUTOINSTALL unset): a missing trivy fails closed (exit 2) — no curl|sh."""
    env = dict(os.environ)
    env["PATH"] = _only_sh_path(tmp_path)
    proc = subprocess.run(
        ["sh", str(SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 2, proc.stderr
    assert "auto-install is off" in proc.stderr
    assert "TRIVY_AUTOINSTALL=1" in proc.stderr


def test_fail_closed_when_trivy_absent_and_uninstallable(tmp_path: Path):
    """With auto-install ON but no trivy/brew/curl on PATH, the runner can't install → exit 2."""
    env = dict(os.environ)
    env["PATH"] = _only_sh_path(tmp_path)
    env["TRIVY_AUTOINSTALL"] = "1"
    proc = subprocess.run(
        ["sh", str(SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 2, proc.stderr
    assert "could not install trivy" in proc.stderr


def test_relative_install_dir_normalized_to_absolute_no_prepend(tmp_path: Path):
    """A relative TRIVY_INSTALL_DIR override is normalized to absolute and PATH is appended.

    Behavioral check of the shadowing defense: with auto-install ON, a relative install dir, and
    a curl stub that 'installs' nothing, the script must (a) create the dir under the ABSOLUTE
    cwd (not leave it relative), and (b) NOT place the install dir ahead of the system PATH.
    """
    env = dict(os.environ)
    # PATH: only sh + a curl stub that succeeds but installs no trivy (so the post-check fails
    # and we exit 2 — we're asserting the dir handling, not a successful install).
    pathdir = tmp_path / "p"
    pathdir.mkdir()
    os.symlink(shutil.which("sh") or "/bin/sh", pathdir / "sh")
    os.symlink(shutil.which("mkdir") or "/bin/mkdir", pathdir / "mkdir")
    os.symlink(shutil.which("pwd") or "/bin/pwd", pathdir / "pwd")
    curl = pathdir / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n")  # outputs nothing; the piped `sh` installs nothing
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    env["PATH"] = str(pathdir)
    env["TRIVY_AUTOINSTALL"] = "1"
    env["TRIVY_INSTALL_DIR"] = "relbin"  # relative override -> must become $(pwd)/relbin
    proc = subprocess.run(
        ["sh", str(SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    # Install failed (curl stub installed nothing) → exit 2, fail closed.
    assert proc.returncode == 2, proc.stderr
    # The relative override was created as an ABSOLUTE dir under cwd (normalization happened).
    assert (tmp_path / "relbin").is_dir(), "relative TRIVY_INSTALL_DIR must be created under cwd"


def test_installer_does_not_prepend_relative_dir_to_path():
    """Static guard: no PATH assignment puts a new dir AHEAD of the existing PATH (the
    shadowing vector). Covers the common prepend spellings (`PATH=`, `export PATH=`, `$PATH`,
    `${PATH}`). The safe form is an append (`$PATH:` / `${PATH}:` first)."""
    src = SCRIPT.read_text()
    for raw in src.splitlines():
        line = raw.strip()
        # Normalize `export PATH=...` to `PATH=...` for the check.
        if line.startswith("export "):
            line = line[len("export "):]
        if not line.startswith("PATH="):
            continue
        # Quote-independent prepend detection: a prepend puts the NEW dir before the old PATH,
        # i.e. the value contains `:$PATH` / `:${PATH}` (quoted or not). The safe append form is
        # `$PATH:...` / `${PATH}:...`, where `$PATH` is at the FRONT and not preceded by `:`.
        if re.search(r":\$\{?PATH\}?", line):
            raise AssertionError(
                f"PATH must be APPENDED, never prepended; found a prepend: {line!r}"
            )
    # The default install dir is absolute (TMPDIR-based), not a relative ./bin.
    assert "${TRIVY_INSTALL_DIR:-${TMPDIR:-/tmp}" in src
    assert ":-./bin}" not in src, "install dir must not default to a relative ./bin"


def test_repo_local_trivy_does_not_shadow_installed_one(tmp_path: Path):
    """Behavioral shadowing defense: a malicious `./trivy` in the scanned cwd must NOT be the
    one that runs — the system/preinstalled trivy (resolved from PATH) wins. This is the whole
    point of appending (not prepending) the install dir and never adding `.`/cwd to PATH."""
    # A repo-local ./trivy that would "find a CRITICAL" if it were ever executed.
    evil = tmp_path / "trivy"
    evil.write_text(
        '#!/bin/sh\n'
        '[ "$1" = "--version" ] && { echo "EVIL"; exit 0; }\n'
        'echo "EVIL-TRIVY-RAN" >&2\nexit 1\n'
    )
    evil.chmod(evil.stat().st_mode | stat.S_IEXEC)
    # A legit trivy on PATH (a clean-scan stub) that should be the one selected.
    bin_dir = tmp_path / "bin"
    _stub_trivy(bin_dir, scan_exit=0)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"
    proc = subprocess.run(
        ["sh", str(SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )
    assert "EVIL-TRIVY-RAN" not in proc.stderr, "repo-local ./trivy must NOT shadow PATH trivy"
    assert proc.returncode == 0, proc.stderr
    assert "PASS" in proc.stderr


def test_trivy_extra_glob_is_not_pathname_expanded(tmp_path: Path):
    """A glob in TRIVY_EXTRA must reach trivy LITERALLY (noglob), not be expanded against cwd.

    Regression: the unquoted $TRIVY_EXTRA word-split also triggered pathname expansion, so a
    knob like `--skip-files *.lock` would be rewritten by the files in cwd. `set -f` fixes it.
    """
    bin_dir = tmp_path / "bin"
    argv_log = _stub_trivy(bin_dir, scan_exit=0)
    # Seed cwd with files a bare `*.lock` glob WOULD expand to, to prove it does not.
    (tmp_path / "a.lock").write_text("x")
    (tmp_path / "b.lock").write_text("y")
    _run(tmp_path, bin_dir, env_extra={"TRIVY_EXTRA": "--skip-files *.lock"})
    argv = argv_log.read_text()
    assert "--skip-files *.lock" in argv, f"glob must pass literally, got: {argv!r}"
    assert "a.lock" not in argv and "b.lock" not in argv, "glob was expanded against cwd"


def test_workflow_actions_are_sha_pinned():
    """Every `uses:` in workflow.yml must pin a 40-hex commit SHA (supply-chain hygiene) —
    not a floating tag like @v0.36. Mirrors the static guard on the shell runner's PATH."""
    wf = (REPO_ROOT / "ci" / "trivy" / "workflow.yml").read_text()
    uses_lines = [
        line.strip() for line in wf.splitlines() if line.strip().startswith("uses:")
    ]
    assert uses_lines, "expected at least one `uses:` in workflow.yml"
    sha_re = re.compile(r"uses:\s*\S+@[0-9a-f]{40}\b")
    for line in uses_lines:
        assert sha_re.search(line), f"action not SHA-pinned: {line!r}"


def test_workflow_ships_block_tier_defaults():
    """The workflow must ship in BLOCK tier with the documented defaults — guards against a
    silent edit flipping exit-code to '0' (warn) or dropping a scanner."""
    wf = (REPO_ROOT / "ci" / "trivy" / "workflow.yml").read_text()
    assert "exit-code: '1'" in wf, "workflow must default to block tier (exit-code '1')"
    assert "scanners: vuln,secret,misconfig" in wf
    assert "severity: HIGH,CRITICAL" in wf
    assert "scan-type: fs" in wf
    assert "timeout-minutes:" in wf, "the trivy job should cap its runtime"


def test_script_is_executable_and_shellcheck_clean():
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK), "trivy-scan.sh must be executable"
    if shutil.which("shellcheck"):
        proc = subprocess.run(
            ["shellcheck", str(SCRIPT)], capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

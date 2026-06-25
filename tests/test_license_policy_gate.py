"""Tests for the ci/license-policy/ gate — the OSS license-policy CI slot (agent-tools#21).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_license_policy_gate.py -q

The gate is a portable POSIX shell script (``ci/license-policy/license-audit.sh``) that scans
dependency licenses across ecosystems and fails on a default-deny-copyleft policy. These tests
shell out to the real script and assert its *policy logic* directly — the part that has to be
right for the gate to mean anything:

- the deny pattern catches every copyleft FORM the ecosystem reporters actually emit (SPDX ids
  ``GPL-3.0``, glued ``GPLv3``, and full classifier names ``GNU General Public License v3``),
  while passing permissive licenses (MIT / Apache / BSD / ISC). A miss here is a silent
  fail-OPEN — a GPL dep ships;
- the subshell-counter regression: a reporter that emits VIOLATION records must FAIL (exit 1),
  not print violations and still PASS. (The first impl iterated records in a pipe subshell so
  the violation counter was discarded — caught by a real GPL-dep self-test.)
- fail-CLOSED when a manifest is present but its reporter is absent, fail-OPEN only with the
  explicit ``LICENSE_ALLOW_MISSING=1`` escape hatch;
- the allow-list exempts a named dependency; UNKNOWN licenses are a violation by default.

To exercise the deny pattern WITHOUT installing every ecosystem's reporter, the tests stub a
``pip-licenses`` on PATH that prints a fixed CSV — the script's parsing + policy path runs for
real against it. The empty-tree and missing-reporter paths need no stub.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "ci" / "license-policy" / "license-audit.sh"


def _run(cwd: Path, env_extra: dict[str, str] | None = None, path_prepend: Path | None = None):
    """Run license-audit.sh in *cwd*; return (returncode, stderr)."""
    env = dict(os.environ)
    if path_prepend is not None:
        env["PATH"] = f"{path_prepend}{os.pathsep}{env.get('PATH', '')}"
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["sh", str(SCRIPT)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stderr


def _stub_pip_licenses(bin_dir: Path, csv_body: str) -> None:
    """Write a fake ``pip-licenses`` on PATH that prints *csv_body* (a full CSV incl. header)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "pip-licenses"
    # Heredoc with the literal CSV; --format/--with-system args are ignored by the stub.
    stub.write_text("#!/bin/sh\ncat <<'CSV'\n" + csv_body + "\nCSV\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _py_manifest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")


# ── deny-pattern matrix (the load-bearing correctness) ──────────────────────────────────────
# Each row: (license-string-as-the-reporter-emits-it, should_fail).
DENY_MATRIX = [
    # SPDX ids
    ("GPL-3.0-or-later", True),
    ("AGPL-3.0", True),
    ("LGPL-2.1", True),
    ("MPL-2.0", True),
    ("EPL-2.0", True),
    ("SSPL-1.0", True),
    # glued vN forms (pip-licenses classifier short form)
    ("GPLv3", True),
    ("AGPLv3", True),
    ("LGPLv2", True),
    # full English classifier names (pip-licenses default)
    ("GNU General Public License v3 (GPLv3)", True),
    ("GNU Lesser General Public License v2 or later (LGPLv2+)", True),
    ("GNU Affero General Public License v3", True),
    ("Mozilla Public License 2.0 (MPL 2.0)", True),
    ("Eclipse Public License", True),
    # permissive — must PASS
    ("MIT", False),
    ("MIT License", False),
    ("Apache-2.0", False),
    ("Apache Software License", False),
    ("BSD-3-Clause", False),
    ("ISC", False),
    ("Python Software Foundation License", False),
    ("The Unlicense", False),
]


@pytest.mark.parametrize("license_str,should_fail", DENY_MATRIX)
def test_deny_pattern_matrix(tmp_path: Path, license_str: str, should_fail: bool):
    """The policy fails iff the dep's license is copyleft, across every emitted FORM."""
    _py_manifest(tmp_path)
    csv = '"Name","Version","License"\n' f'"somepkg","1.0","{license_str}"'
    _stub_pip_licenses(tmp_path / "bin", csv)
    rc, err = _run(tmp_path, path_prepend=tmp_path / "bin")
    if should_fail:
        assert rc == 1, f"expected DENY for {license_str!r}, got pass\n{err}"
        assert "VIOLATION" in err
    else:
        assert rc == 0, f"expected ALLOW for {license_str!r}, got fail\n{err}"


def test_violation_actually_fails_not_just_prints(tmp_path: Path):
    """Regression: a printed VIOLATION must FAIL the gate (the pipe-subshell counter bug)."""
    _py_manifest(tmp_path)
    csv = (
        '"Name","Version","License"\n'
        '"clean","1.0","MIT"\n'
        '"bad","2.0","GPL-3.0-or-later"'
    )
    _stub_pip_licenses(tmp_path / "bin", csv)
    rc, err = _run(tmp_path, path_prepend=tmp_path / "bin")
    assert "VIOLATION: bad" in err
    assert rc == 1, "a VIOLATION must fail the gate, not pass it"


def test_allow_list_exempts_a_named_dep(tmp_path: Path):
    """LICENSE_ALLOW exempts a flagged dependency by name → gate passes."""
    _py_manifest(tmp_path)
    csv = '"Name","Version","License"\n"bad","2.0","GPL-3.0-or-later"'
    _stub_pip_licenses(tmp_path / "bin", csv)
    rc, err = _run(tmp_path, path_prepend=tmp_path / "bin", env_extra={"LICENSE_ALLOW": "bad"})
    assert rc == 0, err
    assert "allow-listed: bad" in err


def test_unknown_license_is_a_violation_by_default(tmp_path: Path):
    """An UNKNOWN/undeclared license fails by default; LICENSE_UNKNOWN_OK=1 relaxes it."""
    _py_manifest(tmp_path)
    csv = '"Name","Version","License"\n"mystery","1.0","UNKNOWN"'
    _stub_pip_licenses(tmp_path / "bin", csv)
    rc, _ = _run(tmp_path, path_prepend=tmp_path / "bin")
    assert rc == 1
    rc_ok, _ = _run(
        tmp_path, path_prepend=tmp_path / "bin", env_extra={"LICENSE_UNKNOWN_OK": "1"}
    )
    assert rc_ok == 0


def test_fail_closed_when_reporter_missing(tmp_path: Path):
    """A python manifest with no pip-licenses on PATH fails closed (no silent skip)."""
    _py_manifest(tmp_path)
    # PATH without pip-licenses: point PATH at an empty bin so the manifest is detected but the
    # reporter resolves to nothing. (sh/sed/tail still resolve from the system dirs in PATH.)
    rc, err = _run(tmp_path, env_extra={"PATH": "/usr/bin:/bin"})
    assert rc == 1
    assert "pip-licenses not installed" in err


def test_fail_open_with_allow_missing(tmp_path: Path):
    """LICENSE_ALLOW_MISSING=1 turns a missing reporter into a non-fatal skip."""
    _py_manifest(tmp_path)
    rc, _ = _run(
        tmp_path, env_extra={"PATH": "/usr/bin:/bin", "LICENSE_ALLOW_MISSING": "1"}
    )
    assert rc == 0


def test_empty_reporter_output_fails_closed(tmp_path: Path):
    """A reporter that RAN but emitted zero records fails closed (no scan != all clear)."""
    _py_manifest(tmp_path)
    _stub_pip_licenses(tmp_path / "bin", '"Name","Version","License"')  # header only, no rows
    rc, err = _run(tmp_path, path_prepend=tmp_path / "bin")
    assert rc == 1
    assert "NO license records" in err


def test_clean_empty_tree_passes(tmp_path: Path):
    """No supported manifest anywhere → nothing to scan → pass."""
    rc, err = _run(tmp_path)
    assert rc == 0
    assert "nothing to license-scan" in err


def _stub_node_license_checker(bin_dir: Path, json_body: str) -> None:
    """Stub ``license-checker`` + a minimal ``node`` so scan_node's reducer runs for real."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    lc = bin_dir / "license-checker"
    lc.write_text("#!/bin/sh\ncat <<'JSON'\n" + json_body + "\nJSON\n")
    lc.chmod(lc.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_reducer_flags_copyleft(tmp_path: Path):
    """scan_node: license-checker JSON with a GPL dep → VIOLATION + fail."""
    (tmp_path / "package.json").write_text('{"name":"x","version":"1.0.0"}')
    body = '{"good@1.0.0":{"licenses":"MIT"},"bad@2.0.0":{"licenses":"GPL-3.0-or-later"}}'
    _stub_node_license_checker(tmp_path / "bin", body)
    rc, err = _run(tmp_path, path_prepend=tmp_path / "bin")
    assert "VIOLATION: bad" in err
    assert rc == 1


def test_go_reporter_flags_copyleft(tmp_path: Path):
    """scan_go: a go-licenses CSV report with a GPL module → VIOLATION + fail."""
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gl = bin_dir / "go-licenses"
    gl.write_text(
        "#!/bin/sh\ncat <<'CSV'\n"
        "example.com/good,https://x/LICENSE,MIT\n"
        "example.com/bad,https://x/LICENSE,GPL-3.0\n"
        "CSV\n"
    )
    gl.chmod(gl.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rc, err = _run(tmp_path, path_prepend=bin_dir)
    assert "VIOLATION: example.com/bad" in err
    assert rc == 1


def test_rust_without_deny_toml_fails_closed(tmp_path: Path):
    """scan_rust: a Cargo manifest with cargo-deny present but NO deny.toml fails closed."""
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Stub a cargo-deny that would PASS if invoked — proving the gate fails on the missing
    # deny.toml BEFORE delegating, not on cargo-deny's verdict.
    cd = bin_dir / "cargo-deny"
    cd.write_text("#!/bin/sh\nexit 0\n")
    cd.chmod(cd.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rc, err = _run(tmp_path, path_prepend=bin_dir)
    assert rc == 1
    assert "no deny.toml" in err


def test_script_is_executable_and_shellcheck_clean():
    """The shipped script is executable; if shellcheck is available, it is clean."""
    assert SCRIPT.exists()
    assert os.access(SCRIPT, os.X_OK), "license-audit.sh must be executable"
    if shutil.which("shellcheck"):
        proc = subprocess.run(
            ["shellcheck", str(SCRIPT)], capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

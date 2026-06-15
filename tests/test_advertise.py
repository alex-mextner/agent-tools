"""Tests for advertise — the shared `install-skill` core (SKILL.md + safe harness link).

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_advertise.py -q

Every test points ``skills_dir``/``harness_dirs`` at a tmp dir, so the suite never touches
the developer's real ``~/.agents/skills`` / ``~/.claude/skills``. The cases mirror the four
the task names — fresh install, idempotent re-run, a wrong symlink re-pointed, a real dir
left untouched — plus the edge cases the naive copies get wrong (dangling link, backup /
overwrite conflict modes, self-link, source-dir copy, multi-harness, best-effort errors).
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import advertise  # noqa: E402
from advertise import HarnessLink, InstallResult, install_skill  # noqa: E402

SKILL_MD = """\
---
name: demo
description: a test skill
---

# demo
"""


@pytest.fixture
def dirs(tmp_path):
    """Isolated (skills_dir, harness_dir) under a tmp tree."""
    return tmp_path / "agents" / "skills", tmp_path / "claude" / "skills"


def _install(skills, harness, *, tool="demo", skill_md=SKILL_MD, **kw):
    return install_skill(
        tool, skill_md, skills_dir=skills, harness_dirs=[harness], **kw
    )


# --------------------------------------------------------------------------- fresh install


def test_fresh_install_writes_md_and_links(dirs):
    skills, harness = dirs
    res = _install(skills, harness)

    assert isinstance(res, InstallResult)
    assert res.ok and bool(res) and res.changed
    md = skills / "demo" / "SKILL.md"
    assert md.is_file() and md.read_text() == SKILL_MD
    assert res.skill_status == "written"

    link = harness / "demo"
    assert link.is_symlink(), "skill not symlinked into the harness dir"
    assert link.resolve() == (skills / "demo").resolve()
    assert (link / "SKILL.md").is_file()  # the link resolves to the real skill

    (hl,) = res.harness_links
    assert hl.status == "linked" and hl.ok


def test_fresh_install_creates_missing_parent_dirs(tmp_path):
    skills = tmp_path / "deep" / "a" / "agents"
    harness = tmp_path / "deep" / "b" / "claude"
    res = _install(skills, harness)
    assert res.ok
    assert (skills / "demo" / "SKILL.md").is_file()
    assert (harness / "demo").is_symlink()


# --------------------------------------------------------------------------- idempotency


def test_idempotent_rerun_is_clean_noop(dirs):
    skills, harness = dirs
    first = _install(skills, harness)
    assert first.changed

    second = _install(skills, harness)
    assert second.ok
    assert second.skill_status == "current"
    assert not second.changed, "a second identical run must not mutate anything"
    (hl,) = second.harness_links
    assert hl.status == "current"


def test_rerun_after_content_change_rewrites(dirs):
    skills, harness = dirs
    _install(skills, harness)
    res = _install(skills, harness, skill_md=SKILL_MD + "\nmore\n")
    assert res.skill_status == "written" and res.changed
    assert (skills / "demo" / "SKILL.md").read_text().endswith("more\n")


# --------------------------------------------------------------------------- wrong symlink


def test_wrong_symlink_is_repointed(dirs):
    skills, harness = dirs
    # A stale link points somewhere else entirely.
    harness.mkdir(parents=True)
    bogus = harness.parent / "elsewhere"
    bogus.mkdir()
    link = harness / "demo"
    link.symlink_to(bogus)

    res = _install(skills, harness)
    assert res.ok and res.changed
    assert link.resolve() == (skills / "demo").resolve(), "stale link not re-pointed"
    (hl,) = res.harness_links
    assert hl.status == "repointed"


def test_dangling_symlink_is_repaired(dirs):
    skills, harness = dirs
    # The naive `not link.exists()` copies treat a dangling link as "present" and never
    # fix it; we use is_symlink(), so a broken link is repaired.
    harness.mkdir(parents=True)
    link = harness / "demo"
    link.symlink_to(harness.parent / "does-not-exist")
    assert not link.exists() and link.is_symlink()

    res = _install(skills, harness)
    assert res.ok
    assert link.resolve() == (skills / "demo").resolve()
    (hl,) = res.harness_links
    assert hl.status == "repointed"


# --------------------------------------------------------------------------- real dir guard


def test_real_dir_left_untouched_by_default(dirs):
    skills, harness = dirs
    # A hand-authored skill already occupies the harness path as a REAL dir.
    real = harness / "demo"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("hand-authored\n")

    res = _install(skills, harness)
    # The skill itself is still written into the agents dir...
    assert (skills / "demo" / "SKILL.md").read_text() == SKILL_MD
    # ...but the harness dir is left exactly as the human left it.
    assert not real.is_symlink()
    assert (real / "SKILL.md").read_text() == "hand-authored\n"
    (hl,) = res.harness_links
    assert hl.status == "conflict" and not hl.ok
    assert res.ok is False, "an unresolved real-dir conflict means the link isn't usable"


def test_real_file_left_untouched_by_default(dirs):
    skills, harness = dirs
    harness.mkdir(parents=True)
    real = harness / "demo"
    real.write_text("not a skill\n")
    res = _install(skills, harness)
    assert real.is_file() and not real.is_symlink()
    assert real.read_text() == "not a skill\n"
    assert res.harness_links[0].status == "conflict"


def test_real_dir_backed_up_then_linked(dirs):
    skills, harness = dirs
    real = harness / "demo"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("hand-authored\n")

    res = _install(skills, harness, on_conflict="backup")
    link = harness / "demo"
    assert link.is_symlink() and link.resolve() == (skills / "demo").resolve()
    backup = harness / "demo.bak"
    assert backup.is_dir() and (backup / "SKILL.md").read_text() == "hand-authored\n"
    hl = res.harness_links[0]
    assert hl.status == "backed-up" and hl.ok and hl.detail == str(backup)


def test_backup_does_not_collide(dirs):
    skills, harness = dirs
    real = harness / "demo"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("v1\n")
    (harness / "demo.bak").mkdir()  # a prior backup already sits there
    res = _install(skills, harness, on_conflict="backup")
    assert (harness / "demo.bak.1").is_dir()  # picked a non-colliding name
    assert res.harness_links[0].status == "backed-up"


def test_real_dir_overwritten(dirs):
    skills, harness = dirs
    real = harness / "demo"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("hand-authored\n")
    res = _install(skills, harness, on_conflict="overwrite")
    link = harness / "demo"
    assert link.is_symlink() and link.resolve() == (skills / "demo").resolve()
    assert not (harness / "demo.bak").exists()
    assert res.harness_links[0].status == "overwritten"


# --------------------------------------------------------------------------- self-link guard


def test_no_self_link_when_harness_is_agents_dir(dirs):
    skills, _ = dirs
    # Harness dir == the agents skill dir itself; the skill already lives where the harness
    # scans, so no link is created (and crucially, no dir-into-itself link).
    res = install_skill("demo", SKILL_MD, skills_dir=skills, harness_dirs=[skills])
    assert (skills / "demo" / "SKILL.md").is_file()
    (hl,) = res.harness_links
    assert hl.status == "self" and hl.ok
    # No "demo/demo" self-link was created.
    assert not (skills / "demo" / "demo").exists()


# --------------------------------------------------------------------------- multi-harness


def test_links_into_multiple_harnesses(tmp_path):
    skills = tmp_path / "agents"
    h1 = tmp_path / "claude" / "skills"
    h2 = tmp_path / "codex" / "skills"
    res = install_skill("demo", SKILL_MD, skills_dir=skills, harness_dirs=[h1, h2])
    assert res.ok
    assert (h1 / "demo").resolve() == (skills / "demo").resolve()
    assert (h2 / "demo").resolve() == (skills / "demo").resolve()
    assert {hl.status for hl in res.harness_links} == {"linked"}


# --------------------------------------------------------------------------- source-dir mode


def test_source_dir_copies_skill_and_assets(tmp_path):
    skills = tmp_path / "agents"
    src = tmp_path / "src-skill"
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text(SKILL_MD)
    (src / "reference.md").write_text("ref\n")
    (src / "scripts" / "run.sh").write_text("#!/bin/sh\n")

    res = install_skill(
        "demo", source_dir=src, skills_dir=skills, harness_dirs=[tmp_path / "claude"]
    )
    assert res.ok and res.changed
    dest = skills / "demo"
    assert (dest / "SKILL.md").read_text() == SKILL_MD
    assert (dest / "reference.md").read_text() == "ref\n"
    assert (dest / "scripts" / "run.sh").read_text() == "#!/bin/sh\n"


def test_source_dir_idempotent(tmp_path):
    skills = tmp_path / "agents"
    src = tmp_path / "src-skill"
    src.mkdir()
    (src / "SKILL.md").write_text(SKILL_MD)
    harness = tmp_path / "claude"
    install_skill("demo", source_dir=src, skills_dir=skills, harness_dirs=[harness])
    res = install_skill("demo", source_dir=src, skills_dir=skills, harness_dirs=[harness])
    assert res.skill_status == "current" and not res.changed


def test_source_dir_without_skill_md_errors(tmp_path):
    src = tmp_path / "src-skill"
    src.mkdir()
    (src / "notes.txt").write_text("x\n")
    with pytest.raises(FileNotFoundError):
        install_skill("demo", source_dir=src, skills_dir=tmp_path / "a", harness_dirs=[])


def test_missing_source_dir_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        install_skill(
            "demo", source_dir=tmp_path / "nope", skills_dir=tmp_path / "a", harness_dirs=[]
        )


# --------------------------------------------------------------------------- argument guards


def test_requires_exactly_one_content_source(tmp_path):
    with pytest.raises(ValueError):
        install_skill("demo", skills_dir=tmp_path, harness_dirs=[])  # neither
    with pytest.raises(ValueError):
        install_skill(  # both
            "demo", SKILL_MD, source_dir=tmp_path, skills_dir=tmp_path, harness_dirs=[]
        )


@pytest.mark.parametrize("bad", ["", "a/b", ".", "..", "x" + os.sep + "y"])
def test_rejects_unsafe_tool_names(bad, tmp_path):
    with pytest.raises(ValueError):
        install_skill(bad, SKILL_MD, skills_dir=tmp_path, harness_dirs=[])


def test_rejects_invalid_on_conflict(dirs):
    skills, harness = dirs
    # A typo must NOT fall through to the destructive overwrite branch.
    real = harness / "demo"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("hand-authored\n")
    with pytest.raises(ValueError):
        _install(skills, harness, on_conflict="clobber")  # type: ignore[arg-type]
    # The real dir is untouched (the call refused before any filesystem mutation).
    assert (real / "SKILL.md").read_text() == "hand-authored\n"


def test_invalid_on_conflict_does_not_create_skill_dir(tmp_path):
    skills = tmp_path / "agents"
    with pytest.raises(ValueError):
        install_skill("demo", SKILL_MD, skills_dir=skills, harness_dirs=[], on_conflict="x")  # type: ignore[arg-type]
    assert not (skills / "demo").exists(), "a rejected call must not leave a skill dir"


def test_bare_string_harness_dir_is_not_iterated_per_char(tmp_path):
    # A bare string must be treated as ONE dir, not iterated by character (which would
    # scatter links under ~, ., /, …). This is the public-API footgun guard.
    skills = tmp_path / "agents"
    harness = tmp_path / "claude" / "skills"
    res = install_skill("demo", SKILL_MD, skills_dir=skills, harness_dirs=str(harness))
    assert res.ok
    assert len(res.harness_links) == 1
    assert (harness / "demo").is_symlink()


def test_no_orphan_skill_dir_on_source_error(tmp_path):
    skills = tmp_path / "agents"
    with pytest.raises(FileNotFoundError):
        install_skill(
            "demo", source_dir=tmp_path / "nope", skills_dir=skills, harness_dirs=[]
        )
    # Validation runs BEFORE mkdir, so no half-made <skills>/demo/ is left behind.
    assert not (skills / "demo").exists()


# --------------------------------------------------------------------------- mode bits


def test_source_dir_preserves_executable_bit(tmp_path):
    skills = tmp_path / "agents"
    src = tmp_path / "src-skill"
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text(SKILL_MD)
    script = src / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)

    install_skill("demo", source_dir=src, skills_dir=skills, harness_dirs=[tmp_path / "h"])
    dst = skills / "demo" / "scripts" / "run.sh"
    assert dst.stat().st_mode & stat.S_IXUSR, "executable bit dropped on copy"


def test_source_dir_resyncs_when_only_mode_changes(tmp_path):
    skills = tmp_path / "agents"
    src = tmp_path / "src-skill"
    src.mkdir()
    (src / "SKILL.md").write_text(SKILL_MD)
    script = src / "run.sh"
    script.write_text("#!/bin/sh\n")
    harness = tmp_path / "h"
    install_skill("demo", source_dir=src, skills_dir=skills, harness_dirs=[harness])
    # Make the source executable AFTER the first install; content is unchanged.
    script.chmod(0o755)
    res = install_skill("demo", source_dir=src, skills_dir=skills, harness_dirs=[harness])
    assert res.skill_status == "written", "a mode-only change must re-sync, not no-op"
    assert (skills / "demo" / "run.sh").stat().st_mode & stat.S_IXUSR


# --------------------------------------------------------------------------- CLI (_main)


def test_main_installs_and_relinks(tmp_path, capsys):
    from advertise.core import _main

    md = tmp_path / "SKILL.md"
    md.write_text(SKILL_MD)
    skills = tmp_path / "agents"
    harness = tmp_path / "claude"
    rc = _main([
        "demo", "--skill-md", str(md),
        "--skills-dir", str(skills), "--harness-dir", str(harness),
    ])
    assert rc == 0
    assert (skills / "demo" / "SKILL.md").read_text() == SKILL_MD
    assert (harness / "demo").is_symlink()
    out = capsys.readouterr().out
    assert "wrote skill" in out and "linked into harness dir" in out


def test_main_returns_nonzero_on_conflict(tmp_path):
    from advertise.core import _main

    md = tmp_path / "SKILL.md"
    md.write_text(SKILL_MD)
    skills = tmp_path / "agents"
    harness = tmp_path / "claude"
    real = harness / "demo"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("hand-authored\n")
    rc = _main([
        "demo", "--skill-md", str(md),
        "--skills-dir", str(skills), "--harness-dir", str(harness),
    ])
    assert rc == 1, "an unresolved harness conflict should be a non-zero exit"
    assert (real / "SKILL.md").read_text() == "hand-authored\n"


def test_main_source_dir_mode(tmp_path):
    from advertise.core import _main

    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text(SKILL_MD)
    skills = tmp_path / "agents"
    rc = _main([
        "demo", "--source-dir", str(src),
        "--skills-dir", str(skills), "--harness-dir", str(tmp_path / "h"),
    ])
    assert rc == 0
    assert (skills / "demo" / "SKILL.md").read_text() == SKILL_MD


# --------------------------------------------------------------------------- best-effort links


def test_link_failure_is_recorded_not_raised(dirs, monkeypatch):
    skills, harness = dirs

    def boom(*_a, **_k):
        raise OSError("no symlinks here")

    monkeypatch.setattr(Path, "symlink_to", boom)
    res = _install(skills, harness)
    # The SKILL.md still landed — that's the load-bearing step.
    assert (skills / "demo" / "SKILL.md").read_text() == SKILL_MD
    assert res.skill_status == "written"
    hl = res.harness_links[0]
    assert hl.status == "error" and not hl.ok
    assert "no symlinks" in hl.detail
    assert res.ok is False


# --------------------------------------------------------------------------- expansion / api


def test_tilde_paths_expand_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = install_skill("demo", SKILL_MD)  # all-default paths under the tmp HOME
    assert (tmp_path / ".agents" / "skills" / "demo" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "demo").is_symlink()
    assert res.ok


def test_summary_lines_describe_each_target(dirs):
    skills, harness = dirs
    res = _install(skills, harness)
    lines = res.summary_lines()
    assert any("wrote skill" in ln for ln in lines)
    assert any("linked into harness dir" in ln for ln in lines)


def test_public_api_surface():
    for name in ("install_skill", "InstallResult", "HarnessLink",
                 "DEFAULT_SKILLS_DIR", "DEFAULT_HARNESS_DIRS"):
        assert hasattr(advertise, name)
    assert isinstance(HarnessLink, type) and isinstance(InstallResult, type)

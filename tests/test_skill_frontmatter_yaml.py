"""Every `skills/**/SKILL.md` must carry valid YAML frontmatter.

Regression coverage for three skills (decision-request-discipline, model-fallback,
strict-ticket-discipline) that shipped with a broken frontmatter block: a long,
free-hand `description:` scalar, left UNQUOTED, contained a literal `: ` colon-space
in running prose ("fold them in: that routing", "the discipline: retry", "cold: what
is concretely wrong") — YAML reads that as the start of a nested mapping key. (A
stray apostrophe in an unquoted plain scalar, as decision-request-discipline's fixed
value also has, is legal YAML on its own and was never the trigger; the fix quotes
and doubles it only because a quoted scalar is now used, not because the apostrophe
itself broke anything.) This was invisible to `python -m pytest` (nothing in the
suite ever parsed a SKILL.md as YAML) and only surfaced live, at skill-load time, in
a harness that actually parses the frontmatter (codex: "failed to load skill ...:
invalid YAML: did not find expected key").

PyYAML is an optional dependency in this repo. Per the documented house convention
(see tests/test_review_threads_gate.py and tests/test_ci_gate_bugs_129.py), the
`importorskip` is per-test, NOT module-level: a module-level skip would also skip
the yaml-free glob/count guards below, silently recreating the exact "invisible to
pytest" failure mode this module exists to close, in any environment without
PyYAML (CI always has it via TEST_EXTRA_DEPS, but a local `python -m pytest` may not).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_FILES = sorted((REPO_ROOT / "skills").rglob("SKILL.md"))


def _frontmatter_text(path: Path) -> str | None:
    """Extract the YAML frontmatter block, parsing `---` fences as whole LINES.

    Splitting on the bare substring `---` (an earlier version of this helper did)
    would truncate the frontmatter early if a legally-quoted `description:` value
    ever contained the literal three-dash substring itself (a CLI flag triplet, a
    diff hunk header pasted into prose, ...) — that's a real value, not a fence, and
    must not end the block. A line is a fence only when it is `---` (or `...`) with
    nothing else on it, matching how YAML frontmatter is actually delimited.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() in ("---", "..."):
            return "\n".join(lines[1:i])
    return None


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_skill_frontmatter_is_valid_yaml(path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    fm_text = _frontmatter_text(path)
    assert fm_text is not None, f"{path}: missing or malformed --- frontmatter fence"
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:  # pragma: no cover - failure path is the point of this test
        pytest.fail(f"{path}: invalid YAML frontmatter: {exc}")
    assert isinstance(data, dict), f"{path}: frontmatter did not parse to a mapping"
    assert data.get("name"), f"{path}: frontmatter missing a non-empty 'name'"
    assert data.get("description"), f"{path}: frontmatter missing a non-empty 'description'"
    # The skill's directory name is its stable identity handle (rig.yaml and every harness
    # key off the path, not the frontmatter text) — a `name:` that drifts from the directory
    # it lives in breaks discovery silently, with no YAML error to catch it otherwise.
    assert data["name"] == path.parent.name, (
        f"{path}: frontmatter name {data['name']!r} does not match its directory "
        f"{path.parent.name!r}"
    )


def test_skill_glob_found_files() -> None:
    # Guards against a path/glob typo silently turning every parametrized case above into
    # a no-op (an empty SKILL_FILES list collects zero test cases and reports "0 passed").
    # Needs no YAML parser, so it must run even where PyYAML is absent — see module docstring.
    assert SKILL_FILES, "skills/**/SKILL.md matched nothing — path/glob typo"


def test_skill_catalog_size_floor() -> None:
    # A separate, named tripwire (not the typo guard above): today's catalog is 98 files;
    # this floor is deliberately loose so ordinary catalog growth/shrinkage doesn't fail a
    # YAML-frontmatter test suite for an unrelated reason. Raise/lower this number as the
    # catalog genuinely changes size, rather than treating a failure here as a YAML bug.
    assert len(SKILL_FILES) > 50, f"expected >50 SKILL.md files, found {len(SKILL_FILES)}"

"""advertise.core — write a tool's SKILL.md and link it into each harness skill dir.

REACHED AT RUNTIME
    ``<tool> install-skill`` → its CLI calls :func:`install_skill`. Also runnable as a
    standalone module for ad-hoc use::

        python -m advertise <tool> --skill-md path/to/SKILL.md

INVARIANTS
    - stdlib only at import time. Nothing here imports a third-party package.
    - The SKILL.md write is the one load-bearing action; every harness link is
      best-effort and recorded on the result (never raises). So a host without symlink
      support, or a hand-authored real dir in the way, still gets the skill written and
      ``install-skill`` still reports success.
    - ``on_conflict`` only governs a REAL (non-symlink) dir/file sitting where the
      harness link should go. A wrong SYMLINK is always re-pointed (it's ours to manage);
      a real dir is hand-authored content we refuse to touch unless told to.

PAST BUGS THIS GUARDS AGAINST
    - The naive copies (review/tg/draw) link only when ``not link.exists()`` — so once a
      stale/wrong link exists they never fix it, and a broken (dangling) link reads as
      "exists" and is left wrong forever. Here a wrong link is always re-pointed and
      ``is_symlink`` (not ``exists``) is the test, so a dangling link is repaired.
    - ``exists()`` follows symlinks; a dangling link would slip past a real-dir guard.
      The conflict check uses ``is_symlink()`` first, then ``lexists`` semantics, so a
      symlink is never mistaken for the real dir it's being compared against.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Literal, Optional, Sequence, Union

PathLike = Union[str, "os.PathLike[str]"]

# The Agent Skills standard location every harness-agnostic skill copy lands in.
DEFAULT_SKILLS_DIR = "~/.agents/skills"
# Harness discovery dirs that must additionally see the skill (a skill that lives only in
# ~/.agents/skills is invisible to a harness, which lists/loads from its own dir). Today
# that's claude-code's ~/.claude/skills; add more here when another harness needs a link.
DEFAULT_HARNESS_DIRS: tuple[str, ...] = ("~/.claude/skills",)

OnConflict = Literal["skip", "backup", "overwrite"]
_VALID_CONFLICTS: tuple[str, ...] = ("skip", "backup", "overwrite")
LinkStatus = Literal[
    "linked",       # created a fresh symlink
    "current",      # symlink already correct — no-op
    "repointed",    # a wrong symlink was re-pointed at the installed skill
    "self",         # harness dir IS the agents skill dir — no self-link needed
    "conflict",     # a REAL dir/file is in the way and on_conflict="skip"
    "backed-up",    # a REAL dir/file was moved aside, then the link created
    "overwritten",  # a REAL dir/file was removed, then the link created
    "error",        # an OSError prevented linking (best-effort; recorded, not raised)
]


@dataclass
class HarnessLink:
    """The outcome of linking the installed skill into one harness discovery dir."""

    harness_dir: Path
    link: Path
    status: LinkStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True when the harness can now discover the skill (or intentionally needs no link)."""
        return self.status not in ("conflict", "error")


@dataclass
class InstallResult:
    """The outcome of an :func:`install_skill` run. Truthy when the skill is installed."""

    tool: str
    skill_dir: Path
    skill_md: Path
    #: "written" (created/updated) or "current" (already byte-identical — idempotent no-op).
    skill_status: Literal["written", "current"] = "current"
    harness_links: List[HarnessLink] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when this run mutated the filesystem (wrote the skill or (re)made a link)."""
        if self.skill_status == "written":
            return True
        return any(
            link.status in ("linked", "repointed", "backed-up", "overwritten")
            for link in self.harness_links
        )

    @property
    def ok(self) -> bool:
        """True when every harness link is usable (or a clean no-op).

        The SKILL.md write is unconditional: it either succeeds before this result exists or
        raises, so reaching here means the skill IS written — ``ok`` then reflects only
        whether each harness can discover it (False on a real-dir ``conflict`` or a link
        ``error``).
        """
        return all(link.ok for link in self.harness_links)

    def __bool__(self) -> bool:
        return self.ok

    def summary_lines(self) -> List[str]:
        """Human-readable per-target lines, e.g. for a CLI to print after the run."""
        verb = "wrote skill →" if self.skill_status == "written" else "skill already current at"
        lines = [f"{self.tool}: {verb} {self.skill_md}"]
        for link in self.harness_links:
            lines.append(f"{self.tool}: {_LINK_MESSAGES[link.status]} {link.link}"
                         + (f" ({link.detail})" if link.detail else ""))
        return lines


_LINK_MESSAGES: dict[str, str] = {
    "linked": "linked into harness dir →",
    "current": "harness link already current →",
    "repointed": "re-pointed harness link →",
    "self": "harness dir is the agents skill dir; no self-link needed at",
    "conflict": "a real dir/file exists — left untouched (not our symlink) at",
    "backed-up": "backed up a real dir/file and linked →",
    "overwritten": "replaced a real dir/file and linked →",
    "error": "warning — could not create harness skill link",
}


def _expand(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.expanduser(os.fspath(path)))


def install_skill(
    tool: str,
    skill_md: Optional[str] = None,
    *,
    source_dir: Optional[PathLike] = None,
    skills_dir: PathLike = DEFAULT_SKILLS_DIR,
    harness_dirs: Union[PathLike, Sequence[PathLike]] = DEFAULT_HARNESS_DIRS,
    on_conflict: OnConflict = "skip",
) -> InstallResult:
    """Install ``tool``'s Agent Skill and link it into each harness discovery dir.

    Provide the skill content ONE of two ways:

    - ``skill_md=<str>`` — the SKILL.md text (the inline-string model rig/task/review use).
      Written to ``<skills_dir>/<tool>/SKILL.md``, idempotently (byte-identical = no-op).
    - ``source_dir=<path>`` — copy a skill directory (SKILL.md + any sibling assets,
      preserving each file's mode bits) into ``<skills_dir>/<tool>/``. Use this when a
      skill ships more than its SKILL.md. This is an OVERLAY copy: it adds/refreshes files
      from ``source_dir`` but does NOT prune destination files that no longer exist in the
      source (a re-install never deletes what a prior install or the user left behind).

    Then symlink ``<skills_dir>/<tool>`` into each ``harness_dirs`` entry as ``<tool>``,
    with the safe-symlink discipline (see the module docstring): correct link → no-op,
    wrong link → re-pointed, real dir → governed by ``on_conflict``
    (``skip`` = never clobber, ``backup`` = move aside, ``overwrite`` = replace).
    ``harness_dirs`` may be a single path or a sequence of them.

    Returns an :class:`InstallResult`. Idempotent: a second run with the same inputs is a
    clean no-op (``result.changed`` is False). Raises ``ValueError`` for bad arguments and
    ``FileNotFoundError`` for a missing ``source_dir`` (validated BEFORE anything is written,
    so a bad call never leaves a half-made skill dir); harness-link failures are recorded on
    the result, never raised.
    """
    if not tool or "/" in tool or os.sep in tool or tool in (".", ".."):
        raise ValueError(f"invalid tool name: {tool!r}")
    if (skill_md is None) == (source_dir is None):
        raise ValueError("pass exactly one of skill_md=<text> or source_dir=<path>")
    if on_conflict not in _VALID_CONFLICTS:
        # Guard the destructive overwrite branch: an unvalidated typo must never fall
        # through and delete a real harness dir/file.
        raise ValueError(
            f"on_conflict must be one of {_VALID_CONFLICTS!r}, got {on_conflict!r}"
        )
    # Validate source BEFORE the mkdir, so a missing/invalid source_dir doesn't leave an
    # orphaned empty <skills_dir>/<tool>/ behind.
    src_path = _validate_source_dir(_expand(source_dir)) if source_dir is not None else None

    skills_root = _expand(skills_dir)
    skill_dir = skills_root / tool
    skill_dir.mkdir(parents=True, exist_ok=True)

    if src_path is not None:
        skill_status = _sync_source_dir(src_path, skill_dir)
    else:
        assert skill_md is not None  # narrowed by the XOR check above
        skill_status = _write_skill_md(skill_dir, skill_md)

    md_path = skill_dir / "SKILL.md"
    result = InstallResult(
        tool=tool, skill_dir=skill_dir, skill_md=md_path, skill_status=skill_status
    )
    for harness in _as_dirs(harness_dirs):
        result.harness_links.append(
            _link_into_harness(tool, skill_dir, _expand(harness), on_conflict)
        )
    return result


def _as_dirs(harness_dirs: Union[PathLike, Sequence[PathLike]]) -> Sequence[PathLike]:
    """Normalize a single path OR a sequence of paths into a sequence.

    A bare ``str``/``os.PathLike`` is wrapped into a one-element list — otherwise a string
    would iterate by CHARACTER and scatter links under ``~``, ``.``, ``/``, … (a public-API
    footgun for the adopting CLIs).
    """
    if isinstance(harness_dirs, (str, os.PathLike)):
        return [harness_dirs]
    return harness_dirs


def _write_skill_md(skill_dir: Path, skill_md: str) -> Literal["written", "current"]:
    target = skill_dir / "SKILL.md"
    if target.is_file() and target.read_text(encoding="utf-8") == skill_md:
        return "current"
    target.write_text(skill_md, encoding="utf-8")
    return "written"


def _validate_source_dir(source: Path) -> Path:
    """Assert ``source`` is a real skill dir (a directory containing a SKILL.md)."""
    if not source.is_dir():
        raise FileNotFoundError(f"source_dir is not a directory: {source}")
    if not (source / "SKILL.md").is_file():
        raise FileNotFoundError(f"source_dir has no SKILL.md: {source}")
    return source


def _sync_source_dir(source: Path, skill_dir: Path) -> Literal["written", "current"]:
    """Overlay every file under ``source`` into ``skill_dir``, preserving mode bits.

    Idempotent: a file already byte-identical AND mode-identical is skipped. ``shutil.copy2``
    carries the executable bit, so a shipped ``scripts/run.sh`` stays runnable after install
    (a plain ``read_bytes``/``write_bytes`` would silently drop it). Overlay, not mirror —
    destination-only files are left alone (documented on ``install_skill``).
    """
    changed = False
    for src in sorted(_iter_files(source)):
        rel = src.relative_to(source)
        dst = skill_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if _same_file(src, dst):
            continue
        shutil.copy2(src, dst)  # copies content + mode + mtime
        changed = True
    return "written" if changed else "current"


def _same_file(src: Path, dst: Path) -> bool:
    """True when ``dst`` already matches ``src`` in content AND mode (so a re-copy is a no-op)."""
    if not dst.is_file() or dst.is_symlink():
        return False
    src_st, dst_st = src.stat(), dst.stat()
    import stat as _stat

    if _stat.S_IMODE(src_st.st_mode) != _stat.S_IMODE(dst_st.st_mode):
        return False
    if src_st.st_size != dst_st.st_size:
        return False
    return src.read_bytes() == dst.read_bytes()


def _iter_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            yield p


def _link_into_harness(
    tool: str, skill_dir: Path, harness_dir: Path, on_conflict: OnConflict
) -> HarnessLink:
    """Symlink ``skill_dir`` into ``harness_dir`` as ``tool`` (idempotent, safe)."""
    dest = skill_dir.resolve()
    link = harness_dir / tool

    # No self-link when the harness dir IS the agents skill dir (~/.agents/skills as harness):
    # the skill already lives at exactly the path the harness scans.
    if harness_dir.resolve() == skill_dir.parent.resolve():
        return HarnessLink(harness_dir, link, "self")

    try:
        harness_dir.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            # A SYMLINK is ours to manage: correct → no-op, wrong → re-point (this is the
            # bug the naive `not exists()` copies have — they never repair a stale link).
            if _resolves_to(link, dest):
                return HarnessLink(harness_dir, link, "current")
            link.unlink()
            link.symlink_to(dest)
            return HarnessLink(harness_dir, link, "repointed")
        if os.path.lexists(link):
            # A REAL dir/file — hand-authored content. Governed by on_conflict.
            if on_conflict == "skip":
                return HarnessLink(harness_dir, link, "conflict")
            if on_conflict == "backup":
                backup = _backup_path(link)
                os.rename(link, backup)
                link.symlink_to(dest)
                return HarnessLink(harness_dir, link, "backed-up", detail=str(backup))
            # overwrite
            if link.is_dir() and not link.is_symlink():
                shutil.rmtree(link)
            else:
                link.unlink()
            link.symlink_to(dest)
            return HarnessLink(harness_dir, link, "overwritten")
        link.symlink_to(dest)
        return HarnessLink(harness_dir, link, "linked")
    except OSError as exc:
        return HarnessLink(harness_dir, link, "error", detail=str(exc))


def _resolves_to(link: Path, dest: Path) -> bool:
    """True when ``link`` (a symlink) points at ``dest``, even if its target is relative."""
    try:
        return link.resolve() == dest
    except OSError:
        return False


def _backup_path(link: Path) -> Path:
    """A non-colliding ``<name>.bak`` (then ``.bak.1``, ``.bak.2``, …) beside ``link``."""
    base = link.with_name(link.name + ".bak")
    if not os.path.lexists(base):
        return base
    i = 1
    while os.path.lexists(base.with_name(base.name + f".{i}")):
        i += 1
    return base.with_name(base.name + f".{i}")


def _main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="advertise",
        description="Write a tool's SKILL.md and link it into each harness skill dir.",
    )
    parser.add_argument("tool", help="the tool name (skill dir name)")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--skill-md", help="path to a SKILL.md file to install")
    src.add_argument("--source-dir", help="path to a skill dir to copy in (SKILL.md + assets)")
    parser.add_argument("--skills-dir", default=DEFAULT_SKILLS_DIR)
    parser.add_argument(
        "--harness-dir", action="append", dest="harness_dirs",
        help="repeatable; overrides the default harness dirs",
    )
    parser.add_argument(
        "--on-conflict", choices=("skip", "backup", "overwrite"), default="skip",
        help="what to do with a REAL dir/file already at the harness path (default: skip)",
    )
    args = parser.parse_args(argv)

    skill_md_text: Optional[str] = None
    if args.skill_md is not None:
        skill_md_text = _expand(args.skill_md).read_text(encoding="utf-8")
    result = install_skill(
        args.tool,
        skill_md_text,
        source_dir=args.source_dir,
        skills_dir=args.skills_dir,
        harness_dirs=tuple(args.harness_dirs) if args.harness_dirs else DEFAULT_HARNESS_DIRS,
        on_conflict=args.on_conflict,
    )
    for line in result.summary_lines():
        print(line)
    return 0 if result.ok else 1

"""advertise — the shared `install-skill` core every agent-tools CLI duplicates.

WHAT THIS IS
    Every tool CLI (`rig`, `review`, `tg`, `draw`, `task`, `3d`, …) ships a
    ``<tool> install-skill`` command that registers its Agent Skill so harnesses
    auto-discover it. The mechanism is identical and was copy-pasted ~4+ times:

      1. Write/refresh ``SKILL.md`` into ``~/.agents/skills/<tool>/`` (the Agent Skills
         standard location, read by Claude Code, Codex, opencode, Cursor).
      2. Symlink that skill dir into each harness discovery dir (claude-code:
         ``~/.claude/skills/<tool>``) — a skill that lives only in ``~/.agents/skills``
         is invisible to the harness, which lists/loads from its own dir.

    This module is that core, lifted into one importable, well-tested library so the
    CLIs can drop their hand-rolled copies and ``from advertise import install_skill``.

SAFE-SYMLINK DISCIPLINE (mirrors rig's ``riglib.install`` + the ``link_skill_harness``
apply action — the most careful of the existing copies):
    - A CORRECT symlink (already points at the installed skill) is a no-op.
    - A WRONG symlink (points elsewhere, e.g. a stale skill dir) is re-pointed.
    - A REAL (non-symlink) dir/file already at the harness path is NEVER clobbered by
      default (``on_conflict="skip"``) — hand-authored content wins. ``"backup"`` moves
      it aside first; ``"overwrite"`` replaces it. Idempotent: a re-run is a clean no-op.
    - A self-link is skipped when a harness dir IS the agents skill dir itself
      (``~/.agents/skills`` configured as the harness), so the tool never links a dir
      into itself.

INVARIANTS
    - stdlib only at import time (the repo's lazy-import rule). No third-party deps.
    - The SKILL.md write is the load-bearing step; harness linking is best-effort —
      a link failure (no symlink support, a race, a permission error) is recorded on the
      result and never raises, so ``install-skill`` still succeeds with the skill written.
    - HOME-relative ``~`` paths are expanded; callers may pass absolute paths or override
      every directory (so the whole thing is unit-testable under a tmp ``$HOME``).
"""

from __future__ import annotations

from .core import (
    DEFAULT_HARNESS_DIRS,
    DEFAULT_SKILLS_DIR,
    HarnessLink,
    InstallResult,
    install_skill,
)

__all__ = [
    "DEFAULT_HARNESS_DIRS",
    "DEFAULT_SKILLS_DIR",
    "HarnessLink",
    "InstallResult",
    "install_skill",
]

__version__ = "0.1.0"

"""agenttools_config — a generalized two-layer config-cascade loader.

WHAT THIS IS
    The tool-agnostic LOADING / CASCADE / PATH logic lifted out of ``rig-cli``'s
    ``riglib.config`` so any ecosystem CLI can do::

        from agenttools_config import load_config

        loaded = load_config(tool="rig", repo_root=Path("/path/to/repo"))
        cfg = loaded.data          # the cascaded dict
        loaded.layers              # which layers were present, in merge order

    Two layers, cascaded by **location** (no scope flag):

    1. **Global** — ``$XDG_CONFIG_HOME/<tool>/config.yaml`` (or, when ``XDG_CONFIG_HOME``
       is unset, ``~/.config/<tool>/config.yaml``). Machine-wide defaults a developer
       carries across repos.
    2. **Per-repo** — ``<tool>.yaml`` at the repo root. Committed by default; the
       reproducible source of truth, and it **overrides** the global layer.

    The merge is a deep dict merge: per-repo keys win, nested dicts merge recursively,
    scalars and lists replace wholesale (a list in the repo file fully replaces the
    global list — lists are treated as atomic decisions, not appended, to keep the
    result predictable).

    What is DELIBERATELY left out: any tool's domain schema. The reference impl knew
    rig's keys (``skills``/``ci``/``mcp``/…); this library knows none. Pass a
    ``schema_validate`` callable to enforce your own schema fail-closed.

WHY A SHARED LIB
    ``rig-cli`` already ships this cascade by hand. ``review-cli``, ``task-cli``, and any
    future Python CLI want the SAME ``~/.config/<tool>`` + per-repo overlay semantics.
    One importable loader keeps the path resolution, the XDG handling, and the merge
    rule identical everywhere instead of re-derived per tool.

INVARIANTS
    - **stdlib only at import time.** ``yaml`` is imported lazily inside the loader so
      ``<tool> --help`` / ``doctor`` work even when PyYAML is not installed.
    - **Fail-closed on a malformed file** — a non-mapping root, unreadable file, or
      invalid YAML raises :class:`ConfigError`, never a raw ``yaml`` traceback.
    - **The ``schema_validate`` hook is sovereign.** It runs on the merged dict after the
      cascade; if it raises, the exception is surfaced to the caller unchanged.
"""

from __future__ import annotations

from .core import (
    ConfigError,
    LoadedConfig,
    deep_merge,
    global_config_path,
    load_config,
    repo_config_path,
)

__all__ = [
    "ConfigError",
    "LoadedConfig",
    "deep_merge",
    "global_config_path",
    "load_config",
    "repo_config_path",
]

__version__ = "0.1.0"

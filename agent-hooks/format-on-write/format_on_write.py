#!/usr/bin/env python3
"""agents-hooks/v1 post-write hook — format the file the agent just wrote.

Runs the project's configured formatter on each file the agent writes/edits, in place,
so formatting is a harness concern instead of a per-project manual step. Fires on
`post-write` (after the file lands on disk — a formatter rewrites a real file, so it must
exist first). NEVER blocks: every failure mode (no formatter, tool missing, formatter
non-zero, timeout, bad event) resolves to `allow`; the pre-commit git-hook stays the gate.
Escape hatch: NO_FORMAT_HOOK=1.

See README.md (next to this file) for the full rationale, the detection table, and the
"why post-write, not pre-write" discussion.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

HOOK_API = "agents-hooks/v1"
ESCAPE_HATCH_ENV = "NO_FORMAT_HOOK"
# Per-file formatter run budget. The host's own timeout_ms is the hard ceiling; this is a
# softer internal bound so a wedged formatter can't eat the whole host budget.
RUN_TIMEOUT_S = 8


def emit_allow() -> None:
    # Trailing newline: line-oriented hosts read the protocol line with readline().
    sys.stdout.write(json.dumps({"hook_api": HOOK_API, "decision": "allow"}) + "\n")
    sys.stdout.flush()


def log(msg: str) -> None:
    sys.stderr.write(f"format-on-write: {msg}\n")


def find_up(start: Path, *names: str) -> Path | None:
    """Walk up from `start`'s directory to the filesystem root looking for any of `names`.

    Returns the directory containing the first match (so callers can resolve a sibling,
    e.g. node_modules next to the package.json that mentioned the tool)."""
    here = start if start.is_dir() else start.parent
    for d in [here, *here.parents]:
        for name in names:
            if (d / name).exists():
                return d
    return None


def repo_root(start: Path) -> Path | None:
    # The repo root anchors both the package.json lookup and the node_modules/.bin lookup,
    # so repo-LOCAL formatter detection only fires inside a git worktree. Outside one
    # (root is None) only globally-installed tools (gofmt/rustfmt/a global oxfmt) apply.
    return find_up(start, ".git")


def package_json_mentions(root: Path | None, *needles: str) -> bool:
    """Heuristic: True if the repo-root package.json text contains any needle.

    A plain substring test, not a parse — it can match a tool name inside an unrelated
    string. That's acceptable here because the detector ALSO requires the tool to be
    present (local bin or on PATH) before using it; this is only a cheap "does the project
    look like it uses tool X" signal, gated by availability. Kept lenient on purpose: a
    formatter that's mentioned-but-absent simply falls through to the next candidate."""
    if root is None:
        return False
    pj = root / "package.json"
    if not pj.is_file():
        return False
    try:
        text = pj.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(n in text for n in needles)


def local_bin(root: Path | None, tool: str) -> str | None:
    """Prefer a repo-local node_modules/.bin/<tool> if present and executable."""
    if root is None:
        return None
    cand = root / "node_modules" / ".bin" / tool
    if cand.is_file() and os.access(cand, os.X_OK):
        return str(cand)
    return None


def has_global(tool: str) -> bool:
    return shutil.which(tool) is not None


# ── Detection table ────────────────────────────────────────────────────────────────────
# Maps a file extension to an ordered list of formatter candidates. The FIRST candidate
# whose tool is configured/available for this repo wins. Each candidate is a small spec:
#   detect(root) -> str | None     # the runnable command (abs local bin or bare global), or None
#   argv(cmd, file) -> list[str]   # how to invoke it to format `file` in place
#
# Order encodes preference: a repo-local tool over a global one; oxfmt ahead of
# prettier/biome for JS/TS; the language's canonical formatter for the rest. The table is
# fixed (not user-configurable) — a deliberately small, common set; an unsupported tool
# simply falls through to a no-op (the escape hatch / pre-commit gate remain available).

JS_TS_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
             ".json", ".jsonc", ".css", ".md", ".mdx", ".html", ".yaml", ".yml"}


def _oxfmt_detect(root: Path | None) -> str | None:
    # repo-local oxfmt (lefthook pattern), else package.json mentions it + global present
    lb = local_bin(root, "oxfmt")
    if lb:
        return lb
    if package_json_mentions(root, "oxfmt") and has_global("oxfmt"):
        return "oxfmt"
    return None


def _prettier_detect(root: Path | None) -> str | None:
    lb = local_bin(root, "prettier")
    if lb:
        return lb
    if package_json_mentions(root, "prettier") and has_global("prettier"):
        return "prettier"
    return None


def _biome_detect(root: Path | None) -> str | None:
    lb = local_bin(root, "biome")
    if lb:
        return lb
    # biome configures via biome.json / biome.jsonc at the repo root
    if root and ((root / "biome.json").exists() or (root / "biome.jsonc").exists()):
        if has_global("biome"):
            return "biome"
    return None


def _tool_detect(tool: str) -> Callable[[Path | None], str | None]:
    def detect(root: Path | None) -> str | None:
        lb = local_bin(root, tool)
        return lb or (tool if has_global(tool) else None)
    return detect


def _ruff_configured(root: Path | None) -> bool:
    """True when the repo CONFIGURES ruff: a ruff.toml/.ruff.toml, or a [tool.ruff] (or
    [tool.ruff.*]) section in pyproject.toml. Mirrors the biome/prettier "needs a config
    signal before using a GLOBAL tool" rule."""
    if not root:
        return False
    if (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        return True
    pp = root / "pyproject.toml"
    if pp.is_file():
        try:
            text = pp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return re.search(r"(?m)^\s*\[tool\.ruff(\.[^\]]+)?\]", text) is not None
    return False


def _ruff_detect(root: Path | None) -> str | None:
    # A repo-local ruff (.venv/node_modules) is an explicit opt-in — use it. But a GLOBAL ruff
    # is the project's formatter ONLY if the repo CONFIGURES ruff; else a repo set up for Black
    # (with ruff merely installed globally) would be silently reformatted by ruff.
    lb = local_bin(root, "ruff")
    if lb:
        return lb
    if _ruff_configured(root) and has_global("ruff"):
        return "ruff"
    return None


# argv builders
def _argv_oxfmt(cmd: str, f: str) -> list[str]:
    return [cmd, "--write", "--no-error-on-unmatched-pattern", f]


def _argv_prettier(cmd: str, f: str) -> list[str]:
    return [cmd, "--write", f]


def _argv_biome(cmd: str, f: str) -> list[str]:
    return [cmd, "format", "--write", f]


def _argv_black(cmd: str, f: str) -> list[str]:
    return [cmd, "-q", f]


def _argv_ruff(cmd: str, f: str) -> list[str]:
    return [cmd, "format", f]


def _argv_gofmt(cmd: str, f: str) -> list[str]:
    return [cmd, "-w", f]


def _argv_rustfmt(cmd: str, f: str) -> list[str]:
    return [cmd, f]


# extension → ordered candidate specs: (detect(root) -> cmd|None, argv(cmd, file) -> [str])
Detector = Callable[[Path | None], str | None]
ArgvBuilder = Callable[[str, str], list[str]]
TABLE: dict[str, list[tuple[Detector, ArgvBuilder]]] = {}
for _ext in JS_TS_EXT:
    TABLE[_ext] = [
        (_oxfmt_detect, _argv_oxfmt),
        (_prettier_detect, _argv_prettier),
        (_biome_detect, _argv_biome),
    ]
for _ext in (".py", ".pyi"):
    TABLE[_ext] = [
        (_ruff_detect, _argv_ruff),
        (_tool_detect("black"), _argv_black),
    ]
TABLE[".go"] = [(_tool_detect("gofmt"), _argv_gofmt)]
TABLE[".rs"] = [(_tool_detect("rustfmt"), _argv_rustfmt)]


def resolve_path(event: dict) -> Path | None:
    args = event.get("args") or {}
    raw = (
        args.get("path")
        or args.get("file_path")
        or args.get("filePath")
        or event.get("path")
        or ""
    )
    if not isinstance(raw, str) or not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        cwd = event.get("cwd")
        if isinstance(cwd, str) and cwd:
            p = Path(cwd) / p
    return p


def main() -> int:
    # Escape hatch: bail before doing anything.
    if os.environ.get(ESCAPE_HATCH_ENV) == "1":
        log(f"{ESCAPE_HATCH_ENV}=1 — skipping (allow)")
        emit_allow()
        return 0

    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        log(f"could not parse event: {exc} — allow (formatting is advisory)")
        emit_allow()
        return 0
    if not isinstance(event, dict):
        log("event was not a JSON object — allow (formatting is advisory)")
        emit_allow()
        return 0

    path = resolve_path(event)
    if path is None or not path.is_file():
        log(f"no written file to format (path={path!r}) — allow")
        emit_allow()
        return 0

    ext = path.suffix.lower()
    candidates = TABLE.get(ext)
    if not candidates:
        log(f"no formatter mapping for '{ext}' — allow (no-op)")
        emit_allow()
        return 0

    root = repo_root(path)
    for detect, argv in candidates:
        cmd = detect(root)
        if not cmd:
            continue
        cmd_argv = argv(cmd, str(path))
        tool_name = Path(cmd).name
        try:
            # noqa: S603,S607 -- the executable is a formatter from a fixed allowlist
            # (resolved to an absolute repo-local bin, or a bare global name like "gofmt"
            # that we deliberately let PATH resolve); the only data input is the written
            # file path, passed as its own argv element (never shell-interpolated).
            proc = subprocess.run(  # noqa: S603
                cmd_argv,
                capture_output=True,
                text=True,
                timeout=RUN_TIMEOUT_S,
                cwd=str(root) if root is not None else None,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"formatter '{tool_name}' failed to run ({exc}) — allow (no-op)")
            emit_allow()
            return 0
        if proc.returncode == 0:
            log(f"formatted {path.name} with {tool_name}")
        else:
            # Non-zero (e.g. syntax error in a half-written file) — don't block, just note.
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = err[-1] if err else "non-zero exit"
            log(f"formatter '{tool_name}' exited {proc.returncode}: {tail} — allow")
        emit_allow()
        return 0

    log(f"no configured/available formatter for '{ext}' in this repo — allow (no-op)")
    emit_allow()
    return 0


if __name__ == "__main__":
    sys.exit(main())

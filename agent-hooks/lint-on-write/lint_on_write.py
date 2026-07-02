#!/usr/bin/env python3
"""agents-hooks/v1 post-write hook — lint the file the agent just wrote, feed errors back.

Runs the repo's CONFIGURED linter on each source file the agent writes/edits — single-file
scope, so it stays fast — and, when the linter reports findings, exits 10 (v1 BLOCK) with
the linter output as the message. On the `post-write` point the bridge translates that
into Claude Code's PostToolUse ``{"decision": "block", "reason": …}`` FEEDBACK: the write
already happened (nothing is vetoed), but the agent sees its lint errors immediately
instead of discovering them at pre-commit/CI time.

Which linter runs is never hardcoded per-machine: it is detected from the repo's own
config — a repo-local bin, a config file (`.oxlintrc.json`, `biome.json`, `eslint.config.*`,
`ruff.toml` / `[tool.ruff]`), or a package.json mention — which includes exactly the config
files rig's `linters` area provisions from rig.yaml. No config → clean no-op.

Every FAILURE mode (no linter, tool missing, linter crash/timeout, bad event) resolves to
`allow` — only genuine findings (linter exit 1) produce feedback; the pre-commit git-hook
stays the gate. Escape hatch: NO_LINT_HOOK=1.

See README.md (next to this file) for the detection table and rationale.
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
ESCAPE_HATCH_ENV = "NO_LINT_HOOK"
# Per-file linter run budget. The host's own timeout_ms is the hard ceiling; this is a
# softer internal bound so a wedged linter can't eat the whole host budget. A linter that
# can't finish one file in this window is treated as unavailable (warn + allow), so a slow
# lint can never stall every edit.
RUN_TIMEOUT_S = 8
# Cap on the feedback we surface — enough to show the findings, not a wall of text.
MAX_MESSAGE_LINES = 40
MAX_MESSAGE_CHARS = 2000
# Findings exit code shared by every linter in the table (oxlint/biome/eslint/ruff all
# exit 1 on findings, >=2 on a tool/config error).
FINDINGS_EXIT = 1
# Cheap path-segment skip: never lint generated/vendored trees.
SKIP_SEGMENTS = {"node_modules", ".git", "dist", "build", ".venv", "vendor", "__pycache__"}


def emit(decision: str, message: str = "") -> None:
    out: dict = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    # Trailing newline: line-oriented hosts read the protocol line with readline().
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def log(msg: str) -> None:
    sys.stderr.write(f"lint-on-write: {msg}\n")


def find_up(start: Path, *names: str) -> Path | None:
    """Walk up from `start`'s directory looking for any of `names`; return the dir containing it."""
    here = start if start.is_dir() else start.parent
    for d in [here, *here.parents]:
        for name in names:
            if (d / name).exists():
                return d
    return None


def repo_root(start: Path) -> Path | None:
    # Anchors config lookup + repo-local bin lookup. Outside a git repo every detector
    # returns None (config signals are root-relative), so the hook is a clean no-op there.
    return find_up(start, ".git")


def package_json_mentions(root: Path | None, *needles: str) -> bool:
    """Cheap "does the project look like it uses tool X" signal (same heuristic as
    format-on-write): substring test on the repo-root package.json, always gated by the
    tool actually being present before it is used."""
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
    """Prefer a repo-local executable: node_modules/.bin/<tool> or .venv/bin/<tool>."""
    if root is None:
        return None
    for rel in (("node_modules", ".bin", tool), (".venv", "bin", tool)):
        cand = root.joinpath(*rel)
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def has_global(tool: str) -> bool:
    return shutil.which(tool) is not None


# ── Detection table ────────────────────────────────────────────────────────────────────
# Maps a file extension to an ordered list of linter candidates; the FIRST candidate whose
# tool is configured/available for this repo wins. Order encodes preference: repo-local
# over global, and the FAST linters (oxlint, biome, ruff) ahead of eslint. Only CODE files
# are linted (no .json/.css/.md — style/format is format-on-write's job).

JS_TS_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
_ESLINT_CONFIGS = (
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts",
    ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yaml", ".eslintrc.yml",
)


def _oxlint_detect(root: Path | None) -> str | None:
    lb = local_bin(root, "oxlint")
    if lb:
        return lb
    configured = (root is not None and (root / ".oxlintrc.json").exists()) or package_json_mentions(root, "oxlint")
    if configured and has_global("oxlint"):
        return "oxlint"
    return None


def _biome_detect(root: Path | None) -> str | None:
    lb = local_bin(root, "biome")
    if lb:
        return lb
    if root and ((root / "biome.json").exists() or (root / "biome.jsonc").exists()) and has_global("biome"):
        return "biome"
    return None


def _eslint_detect(root: Path | None) -> str | None:
    # eslint with NO config errors out (exit 2 → allow), so require a config signal or a
    # package.json mention before using a global; a repo-local bin is an explicit opt-in.
    lb = local_bin(root, "eslint")
    if lb:
        return lb
    configured = root is not None and any((root / c).exists() for c in _ESLINT_CONFIGS)
    if (configured or package_json_mentions(root, "eslint")) and has_global("eslint"):
        return "eslint"
    return None


def _ruff_configured(root: Path | None) -> bool:
    """True when the repo CONFIGURES ruff: ruff.toml/.ruff.toml or a [tool.ruff] section —
    exactly the config file rig's `linters` area writes for a ruff linter item."""
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
    lb = local_bin(root, "ruff")
    if lb:
        return lb
    if _ruff_configured(root) and has_global("ruff"):
        return "ruff"
    return None


# argv builders — always single-file scope, never repo-wide.
def _argv_oxlint(cmd: str, f: str) -> list[str]:
    return [cmd, f]


def _argv_biome(cmd: str, f: str) -> list[str]:
    return [cmd, "lint", f]


def _argv_eslint(cmd: str, f: str) -> list[str]:
    # --no-warn-ignored: an eslint-ignored file must be a quiet allow, not a warning.
    # Requires ESLint >= 8.53; an older eslint rejects the flag with exit 2, which the
    # tool-error path degrades to a warned allow (advisory hook — documented in README).
    return [cmd, "--no-warn-ignored", f]


def _argv_ruff(cmd: str, f: str) -> list[str]:
    return [cmd, "check", f]


Detector = Callable[[Path | None], str | None]
ArgvBuilder = Callable[[str, str], list[str]]
TABLE: dict[str, list[tuple[Detector, ArgvBuilder]]] = {}
for _ext in JS_TS_EXT:
    TABLE[_ext] = [
        (_oxlint_detect, _argv_oxlint),
        (_biome_detect, _argv_biome),
        (_eslint_detect, _argv_eslint),
    ]
for _ext in (".py", ".pyi"):
    TABLE[_ext] = [(_ruff_detect, _argv_ruff)]


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


def truncate_output(text: str) -> str:
    lines = text.strip().splitlines()
    clipped = lines[:MAX_MESSAGE_LINES]
    out = "\n".join(clipped)
    if len(out) > MAX_MESSAGE_CHARS:
        out = out[:MAX_MESSAGE_CHARS]
    if len(lines) > MAX_MESSAGE_LINES or len("\n".join(lines)) > MAX_MESSAGE_CHARS:
        out += "\n… (output truncated)"
    return out


def read_event() -> dict | None:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        log(f"could not parse event: {exc} — allow (lint is advisory)")
        return None
    if not isinstance(event, dict):
        log("event was not a JSON object — allow (lint is advisory)")
        return None
    return event


def lintable_path(event: dict) -> Path | None:
    """The written file, or None when there is nothing this hook should lint."""
    path = resolve_path(event)
    if path is None or not path.is_file():
        log(f"no written file to lint (path={path!r}) — allow")
        return None
    if SKIP_SEGMENTS.intersection(path.parts):
        log(f"skipping generated/vendored path {path} — allow")
        return None
    if path.suffix.lower() not in TABLE:
        log(f"no linter mapping for '{path.suffix}' — allow (no-op)")
        return None
    return path


def run_linter(path: Path) -> int:
    """Detect + run the repo's linter on `path`; emit the protocol answer. Returns exit code."""
    root = repo_root(path)
    for detect, argv in TABLE[path.suffix.lower()]:
        cmd = detect(root)
        if not cmd:
            continue
        cmd_argv = argv(cmd, str(path))
        tool_name = Path(cmd).name
        try:
            # noqa comment rationale: the executable comes from the fixed allowlist above
            # (repo-local bin or a bare global name PATH resolves); the only data input is
            # the written file path, passed as its own argv element — never shell-parsed.
            proc = subprocess.run(  # noqa: S603
                cmd_argv,
                capture_output=True,
                text=True,
                timeout=RUN_TIMEOUT_S,
                cwd=str(root) if root is not None else None,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"linter '{tool_name}' failed to run ({exc}) — allow (no-op)")
            emit("allow")
            return 0
        if proc.returncode == 0:
            log(f"{tool_name}: {path.name} clean")
            emit("allow")
            return 0
        if proc.returncode == FINDINGS_EXIT:
            findings = truncate_output(proc.stdout or proc.stderr or "")
            log(f"{tool_name}: findings in {path.name}")
            emit("block", (
                f"{tool_name} found problems in the file you just wrote ({path}):\n\n"
                f"{findings}\n\n"
                f"Fix these now (the write itself went through; this is lint feedback). "
                f"Escape hatch for intentional cases: {ESCAPE_HATCH_ENV}=1."
            ))
            return 10
        # >=2: a tool/config error, not findings — advisory hook, never punish the agent.
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = err[-1] if err else "non-zero exit"
        log(f"linter '{tool_name}' exited {proc.returncode}: {tail} — allow")
        emit("allow")
        return 0
    log(f"no configured/available linter for '{path.suffix}' in this repo — allow (no-op)")
    emit("allow")
    return 0


def main() -> int:
    if os.environ.get(ESCAPE_HATCH_ENV) == "1":
        log(f"{ESCAPE_HATCH_ENV}=1 — skipping (allow)")
        emit("allow")
        return 0
    event = read_event()
    if event is None:
        emit("allow")
        return 0
    path = lintable_path(event)
    if path is None:
        emit("allow")
        return 0
    return run_linter(path)


if __name__ == "__main__":
    sys.exit(main())

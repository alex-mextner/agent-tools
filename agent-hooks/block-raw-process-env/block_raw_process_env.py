#!/usr/bin/env python3
"""agents-hooks/v1 pre-write hook — flag raw env access in feature dirs.

Flags a write/edit that introduces a raw environment read (`process.env.X`,
`import.meta.env.X`, `os.environ[...]`, `os.getenv(...)`) inside configured FEATURE
directories — where config should be read through a single validated loader instead
(see the config-loadconfig skill). The config-loader file/dir itself is exempt: it is
the ONE place allowed to touch the environment.

By default this is advisory (warn + allow). Set BLOCK_PROCESS_ENV_STRICT=1 to block.

Configuration via env (so it adapts to any project layout):
  PROCESS_ENV_FEATURE_DIRS   colon-separated path fragments to watch
                             (default: "src/bot:src/services:src/commands:src/features")
  PROCESS_ENV_CONFIG_PATHS   colon-separated fragments that ARE the config loader and
                             are therefore exempt (default: "config:/env/:loadConfig")

Contract (agents-hooks/v1):
  stdin  : JSON event; target path in args.path/args.file_path, content in args.content
  stdout : protocol JSON only       exit 0 : allow   exit 10 : BLOCK   other : error

on_error is "open": a discipline nudge must never break writing files.
"""

from __future__ import annotations

import json
import os
import re
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

STRICT = os.environ.get("BLOCK_PROCESS_ENV_STRICT") == "1"

FEATURE_DIRS = os.environ.get(
    "PROCESS_ENV_FEATURE_DIRS", "src/bot:src/services:src/commands:src/features"
).split(":")
CONFIG_PATHS = os.environ.get(
    "PROCESS_ENV_CONFIG_PATHS", "config:/env/:loadConfig"
).split(":")

RAW_ENV = re.compile(
    r"process\.env\.\w+"
    r"|import\.meta\.env\.\w+"
    r"|os\.environ\s*\[\s*['\"]"
    r"|os\.getenv\s*\("
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"block-raw-process-env: {msg}\n")


def in_feature_dir(path: str) -> bool:
    return any(frag and frag in path for frag in FEATURE_DIRS)


def is_config_loader(path: str) -> bool:
    return any(frag and frag in path for frag in CONFIG_PATHS)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        emit("allow")
        return 0

    args = event.get("args") or {}
    path = args.get("path") or args.get("file_path") or ""
    content = args.get("content") or args.get("new_string") or args.get("text") or ""
    if not isinstance(path, str):
        path = str(path)
    if not isinstance(content, str):
        content = str(content)

    if not path or not in_feature_dir(path) or is_config_loader(path):
        emit("allow")  # outside watched dirs, or this IS the config loader → fine
        return 0

    if RAW_ENV.search(content):
        msg = (
            f"Raw environment access in a feature file ({path}). Read config through "
            "a single validated loader (e.g. loadConfig()) and inject it, instead of "
            "reaching into process.env / os.environ here. See the config-loadconfig "
            "skill."
        )
        if STRICT:
            emit("block", msg)
            return BLOCK_EXIT_CODE
        warn(msg)
        emit("allow", msg)
        return 0

    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())

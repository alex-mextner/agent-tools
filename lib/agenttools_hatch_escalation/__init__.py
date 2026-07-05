"""Shared Telegram hatch escalation helper for agent-hook scripts.

The helper is intentionally small and stdlib-only: hook scripts import it directly from the
agent-tools checkout, before package installation can be assumed. It turns a per-hook env var
(`RIG_HATCH_REQUEST_<HOOK_ID>`) into a one-time `tg-ctl ask` call through a trusted absolute
path. It never consults ambient PATH.
"""

from __future__ import annotations

import os
import signal
import subprocess  # noqa: S404 - runs an already-resolved absolute tg-ctl path
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

MAX_TG_CTL_TIMEOUT_S = 900.0
DEFAULT_TG_CTL_TIMEOUT_S = MAX_TG_CTL_TIMEOUT_S
DEFAULT_PROCESS_MARGIN_S = 30.0
_DETAIL_CAP = 500
_RIG_TG_CTL_KEY = "tg_ctl_path"
_BARE_FLAG_VALUES = frozenset({"1", "true", "yes", "on"})
_TRUSTED_TG_CTL_PATHS = (
    Path("/Users/ultra/.files/bin/tg-ctl"),
    Path("/usr/local/bin/tg-ctl"),
    Path("/opt/homebrew/bin/tg-ctl"),
)


@dataclass(frozen=True)
class HatchApprovalResult:
    """Result of checking one hook's Telegram hatch request."""

    requested: bool
    approved: bool
    reason: str
    env_var: str
    env_present: bool = False
    tg_ctl_path: str | None = None

    @property
    def should_stop(self) -> bool:
        """True when the hook should not fall through to later approval mechanisms."""

        return self.env_present


def hatch_env_var(hook_id: str) -> str:
    """The env var that requests a Telegram hatch for a canonical hook id."""

    canonical = hook_id.strip().upper().replace("-", "_")
    return f"RIG_HATCH_REQUEST_{canonical}"


def request_hatch_approval(
    hook_id: str,
    context: Mapping[str, object] | None,
    *,
    cwd: str,
    env: Mapping[str, str] | None = None,
    tg_ctl_candidates: Sequence[Path | str] | None = None,
    timeout_s: float = DEFAULT_TG_CTL_TIMEOUT_S,
    process_margin_s: float = DEFAULT_PROCESS_MARGIN_S,
) -> HatchApprovalResult:
    """Request Telegram approval when this hook's hatch env var carries a real justification.

    Unset env vars mean "hatch not requested" and do not block later mechanisms. A present but
    blank/bare-flag env var is an invalid request: no Telegram call is made and the hook should
    deny rather than falling through to `approval_cmd`.
    """

    env_map = env if env is not None else os.environ
    env_var = hatch_env_var(hook_id)
    raw = env_map.get(env_var)
    if raw is None:
        return HatchApprovalResult(
            requested=False,
            approved=False,
            reason=f"{env_var} is not set; Telegram hatch escalation not requested",
            env_var=env_var,
            env_present=False,
        )
    try:
        return _request_present_hatch_approval(
            hook_id,
            context or {},
            cwd=cwd,
            env_var=env_var,
            raw=raw,
            tg_ctl_candidates=tg_ctl_candidates,
            timeout_s=timeout_s,
            process_margin_s=process_margin_s,
        )
    except Exception as exc:  # noqa: BLE001 - a hatch request must never fail open.
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"Telegram hatch escalation errored: {exc}",
            env_var=env_var,
            env_present=True,
        )


def _request_present_hatch_approval(
    hook_id: str,
    context: Mapping[str, object],
    *,
    cwd: str,
    env_var: str,
    raw: str,
    tg_ctl_candidates: Sequence[Path | str] | None,
    timeout_s: float,
    process_margin_s: float,
) -> HatchApprovalResult:
    justification = raw.strip()
    if not justification:
        return HatchApprovalResult(
            requested=False,
            approved=False,
            reason=f"{env_var} is blank; Telegram hatch escalation denied",
            env_var=env_var,
            env_present=True,
        )
    if justification.lower() in _BARE_FLAG_VALUES:
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"{env_var} needs a written justification, not bare {justification!r}",
            env_var=env_var,
            env_present=True,
        )

    tg_ctl = _find_tg_ctl(cwd, tg_ctl_candidates)
    if tg_ctl is None:
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason="tg-ctl is not available at a trusted executable path",
            env_var=env_var,
            env_present=True,
        )

    effective_timeout = _bounded_timeout(timeout_s)
    question = _question(hook_id, justification, context, cwd)
    argv = [str(tg_ctl), "ask", question, "--timeout", _format_seconds(effective_timeout)]
    proc_timeout = effective_timeout + max(process_margin_s, 0.0)

    try:
        proc = subprocess.Popen(  # noqa: S603 - argv[0] is an absolute, executable tg-ctl path
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"tg-ctl failed to launch: {exc}",
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl),
        )
    try:
        out, err = proc.communicate(timeout=proc_timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"tg-ctl ask timed out after {effective_timeout:.0f}s",
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl),
        )
    except (OSError, ValueError) as exc:
        _kill_process_group(proc)
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=f"tg-ctl ask errored: {exc}",
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl),
        )

    detail = ((out or "").strip() or (err or "").strip())[:_DETAIL_CAP]
    if proc.returncode != 0:
        reason = f"tg-ctl ask denied (exit {proc.returncode})"
        if detail:
            reason = f"{reason}: {detail}"
        return HatchApprovalResult(
            requested=True,
            approved=False,
            reason=reason,
            env_var=env_var,
            env_present=True,
            tg_ctl_path=str(tg_ctl),
        )

    return HatchApprovalResult(
        requested=True,
        approved=True,
        reason=detail or "approved by tg-ctl ask",
        env_var=env_var,
        env_present=True,
        tg_ctl_path=str(tg_ctl),
    )


def _bounded_timeout(timeout_s: float) -> float:
    try:
        value = float(timeout_s)
    except (TypeError, ValueError):
        return DEFAULT_TG_CTL_TIMEOUT_S
    if value <= 0:
        return DEFAULT_TG_CTL_TIMEOUT_S
    return min(value, MAX_TG_CTL_TIMEOUT_S)


def _format_seconds(timeout_s: float) -> str:
    if timeout_s.is_integer():
        return str(int(timeout_s))
    return str(timeout_s)


def _find_tg_ctl(cwd: str, candidates: Sequence[Path | str] | None) -> Path | None:
    seen: set[Path] = set()
    for candidate in _candidate_paths(cwd, candidates):
        resolved = _resolve_executable(candidate)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        return resolved
    return None


def _candidate_paths(cwd: str, candidates: Sequence[Path | str] | None) -> list[Path | str]:
    out: list[Path | str] = []
    rig_path = _rig_tg_ctl_path(cwd)
    if rig_path is not None:
        out.append(rig_path)
    out.extend(_TRUSTED_TG_CTL_PATHS if candidates is None else candidates)
    return out


def _resolve_executable(candidate: Path | str) -> Path | None:
    path = Path(os.path.expanduser(str(candidate)))
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return resolved


def _rig_tg_ctl_path(cwd: str) -> str | None:
    root = _find_rig_yaml(cwd)
    if root is None:
        return None
    try:
        text = (root / "rig.yaml").read_text(encoding="utf-8")
    except OSError:
        return None
    value = _agent_hooks_raw(text, _RIG_TG_CTL_KEY)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _find_rig_yaml(cwd: str) -> Path | None:
    try:
        here = Path(cwd or ".").resolve()
    except OSError:
        return None
    for directory in (here, *here.parents):
        if (directory / "rig.yaml").is_file():
            return directory
    return None


def _agent_hooks_raw(rig_yaml_text: str, key: str) -> str | None:
    in_block = False
    child_indent: int | None = None
    for raw in rig_yaml_text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        head = line.strip().split(":", 1)[0].strip()
        if indent == 0:
            in_block = head == "agent_hooks"
            child_indent = None
            continue
        if not in_block:
            continue
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            continue
        if head == key and ":" in line.strip():
            return line.strip().split(":", 1)[1].strip().strip("\"'")
    return None


def _question(
    hook_id: str,
    justification: str,
    context: Mapping[str, object],
    cwd: str,
) -> str:
    lines = [
        "Approve this one-time agent-hook hatch request?",
        f"Hook: {hook_id}",
        f"Justification: {justification}",
        f"CWD: {cwd}",
    ]
    for key in sorted(context):
        value = context[key]
        if value is None or value == "":
            continue
        lines.append(f"{key}: {_context_value(value)}")
    return "\n".join(lines)


def _context_value(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > 1000:
        return f"{text[:1000]}..."
    return text


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=1)
    except (subprocess.TimeoutExpired, OSError):
        pass


__all__ = [
    "DEFAULT_TG_CTL_TIMEOUT_S",
    "HatchApprovalResult",
    "MAX_TG_CTL_TIMEOUT_S",
    "hatch_env_var",
    "request_hatch_approval",
]

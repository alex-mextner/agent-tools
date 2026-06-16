"""Core implementation of tmux text/key injection.

The public surface (``inject``, ``send_keys``, ``has_session``, ``list_panes``,
``resolve_target``, ``InjectResult``, ``Target``) is re-exported from the package
``__init__``; import from there, not from this module.

What this does
--------------
A finishing agent (e.g. task-cli on completion) wants to post "done, X unblocked" into
*another* agent's interactive pane. The mechanism is tmux's ``send-keys``: it types text
into a target pane exactly as if a human had typed it, optionally followed by Enter so the
receiving REPL/agent actually processes the line.

Design notes
------------
* **Never raises on the operational path.** Injecting into another agent's pane is a
  best-effort side-channel, not load-bearing control flow. tmux being absent, the server
  not running, or the target pane having gone away must degrade to a structured failure
  result, not an exception that aborts the finishing agent. (Programmer errors — passing a
  non-string ``text`` — still raise: those are bugs, not environment conditions.)
* **``literal`` controls key interpretation.** ``tmux send-keys`` normally interprets its
  arguments as key *names* (``Enter``, ``C-c``, ``Escape``). With ``-l`` it sends the bytes
  literally. We default to literal so a message containing the word ``Enter`` or a ``C-c``
  substring is typed verbatim instead of being interpreted as a control key — the safe
  default for "post this human-readable text". ``literal=False`` is the escape hatch for
  callers that genuinely want to send key names.
* **``enter`` is a SEPARATE send-keys call**, not ``text + "\\n"`` appended to a literal
  send. Under ``-l`` a trailing newline byte is sent as a literal LF, which many readline
  shells treat differently from a real Return; issuing ``send-keys Enter`` (interpreted,
  no ``-l``) as a second invocation presses the actual Return key. So one literal text
  send + one interpreted ``Enter`` send — the latter only when ``enter=True``.
* **stdlib only.** ``subprocess`` + ``shutil.which``. The argv is always built as a fixed
  list and handed to ``subprocess.run`` directly — never a shell string — so there is no
  quoting/``shlex`` surface to get wrong. The ``shutil``/``subprocess`` imports are deferred
  into the call path (not module top) to honour the ecosystem's "light import, lazy heavy"
  rule and keep importing the package free.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional, Sequence, Union

# Name of the tmux binary; overridable via env for odd installs / tests-in-CI that want to
# point at a stub. Resolution still goes through ``shutil.which`` so a bare name works.
_TMUX_ENV = "AGENTTOOLS_TMUX_BIN"
_DEFAULT_TMUX = "tmux"

# Exit-class sentinels for ``InjectResult.error`` so callers can branch without string
# matching. ``None`` error means success.
ERR_NO_TMUX = "no-tmux"  # the tmux binary is not on PATH
ERR_NO_SERVER = "no-server"  # tmux is installed but no server / target is running
ERR_BAD_TARGET = "bad-target"  # target string could not be parsed / does not exist
ERR_SEND_FAILED = "send-failed"  # send-keys ran but tmux reported a non-zero exit
ERR_TIMEOUT = "timeout"  # tmux did not return within the timeout (hung)


@dataclass(frozen=True)
class Target:
    """A resolved tmux send-keys target.

    tmux accepts two shapes after ``-t``:

    * a **pane id** like ``%3`` (server-unique, stable across renames), or
    * a **``session:window.pane`` address** like ``work:1.0`` (any component optional;
      ``work``, ``work:1``, ``:1.0`` and ``%3`` are all valid tmux targets).

    We keep the original string verbatim in :attr:`raw` and hand exactly that to tmux —
    tmux's own target grammar is richer than anything we'd reimplement, so we parse only
    enough to *report* the components and to know whether a leading ``%`` means "pane id".
    """

    raw: str
    is_pane_id: bool = False
    session: Optional[str] = None
    window: Optional[str] = None
    pane: Optional[str] = None

    def as_tmux_arg(self) -> str:
        """The exact string to pass after ``-t`` (always the original target)."""
        return self.raw


@dataclass(frozen=True)
class InjectResult:
    """Outcome of an :func:`inject` / :func:`send_keys` call. Never an exception.

    ``ok`` is the single boolean to branch on. On failure, ``error`` is one of the
    ``ERR_*`` sentinels and ``message`` is a human-readable explanation; ``argv`` records
    the exact command(s) that were (or would have been) run, for logging and for tests.

    Note on ``argv``: an ``enter=True`` injection runs TWO ``send-keys`` invocations (the
    literal text, then an interpreted ``Enter``); their argvs are stored **concatenated**
    into one flat sequence (``[..., TEXT, tmux, send-keys, ..., Enter]``). It's a faithful
    record for logging, not a single shell-ready command line — split on the tmux binary if
    you need the individual commands.
    """

    ok: bool
    target: Optional[str] = None
    argv: Sequence[str] = field(default_factory=tuple)
    error: Optional[str] = None
    message: str = ""
    returncode: Optional[int] = None
    stderr: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _tmux_bin() -> str:
    """The configured tmux binary name (env override or the default ``tmux``)."""
    return os.environ.get(_TMUX_ENV) or _DEFAULT_TMUX


def _which_tmux() -> Optional[str]:
    """Resolve the tmux binary to an absolute path, or ``None`` if absent.

    ``shutil`` is imported lazily (call path, not module top) per the ecosystem's
    light-import rule. An absolute/relative path that exists is honoured directly so a
    test stub or an unusual install location works without being on ``PATH``.
    """
    import shutil  # lazy: keep package import free of heavy/optional surface

    name = _tmux_bin()
    # Precedence: an explicit path (contains a separator, e.g. AGENTTOOLS_TMUX_BIN=/opt/tmux)
    # is honoured directly so it's used verbatim even if it also happens to be on PATH; a
    # bare name is resolved through PATH via ``which``.
    if os.path.sep in name:
        if os.path.isfile(name) and os.access(name, os.X_OK):
            return name
        return None
    return shutil.which(name)


def resolve_target(target: Union[str, "Target"]) -> "Target":
    """Parse a target string into a :class:`Target` (or pass a ``Target`` through).

    Recognises a tmux **pane id** (a leading ``%`` followed by digits, e.g. ``%12``) and
    otherwise splits a ``session:window.pane`` address into its optional components. The
    raw string is preserved and is what actually gets handed to tmux — this parse exists to
    *describe* the target and to flag pane ids, not to validate against a live server.

    The component split is **best-effort**; ``raw`` (and thus what reaches tmux) is always
    authoritative. The window/pane split is on the LAST ``.``, so a window name that itself
    contains a dot may be attributed to ``window``/``pane`` differently than tmux would —
    inspect ``raw`` if you need the ground truth. A degenerate address like ``":"`` parses
    to all-``None`` components and is returned as a (tmux-rejectable) :class:`Target` rather
    than raising; only an empty/whitespace string is treated as a caller bug.

    Raises ``TypeError`` for a non-string, non-``Target`` argument and ``ValueError`` for an
    empty string: those are caller bugs, distinct from the runtime "tmux/pane absent"
    conditions that :func:`inject` reports as a result.
    """
    if isinstance(target, Target):
        return target
    if not isinstance(target, str):
        raise TypeError(f"target must be a str or Target, got {type(target).__name__}")
    raw = target.strip()
    if not raw:
        raise ValueError("target must be a non-empty string")

    # Pane id: %<digits> (tmux's server-unique pane identifier).
    if raw.startswith("%") and raw[1:].isdigit():
        return Target(raw=raw, is_pane_id=True)

    # session:window.pane — every component optional. Split on the FIRST ':' (session names
    # can't contain ':'); the remainder is window[.pane]; pane is after the LAST '.'.
    session: Optional[str] = None
    rest = raw
    if ":" in raw:
        session_part, rest = raw.split(":", 1)
        session = session_part or None
    else:
        # No ':' — the whole thing is a session (or window) name; tmux resolves it.
        return Target(raw=raw, session=raw or None)

    window: Optional[str] = None
    pane: Optional[str] = None
    if rest:
        if "." in rest:
            window_part, pane_part = rest.rsplit(".", 1)
            window = window_part or None
            pane = pane_part or None
        else:
            window = rest
    return Target(raw=raw, session=session, window=window, pane=pane)


def _run(argv: Sequence[str], *, timeout: float):
    """Run a tmux argv, returning the ``CompletedProcess``. ``subprocess`` lazy-imported."""
    import subprocess  # lazy: heavy-ish, and only needed on the call path

    return subprocess.run(  # noqa: S603 — argv is a fixed list we build, never a shell str
        list(argv),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _is_timeout(exc: BaseException) -> bool:
    """Whether ``exc`` is a ``subprocess.TimeoutExpired`` (tmux hung past the deadline).

    ``subprocess`` is imported lazily, so match by the exception's class name rather than
    importing the module just to reference the type in an ``except`` clause.
    """
    return type(exc).__name__ == "TimeoutExpired"


def _failure(
    target: "Target",
    argv: Sequence[str],
    *,
    error: str,
    message: str,
    returncode: Optional[int] = None,
    stderr: str = "",
) -> "InjectResult":
    """Build a failure :class:`InjectResult` — the single place failures are constructed."""
    return InjectResult(
        ok=False,
        target=target.as_tmux_arg(),
        argv=tuple(argv),
        error=error,
        message=message,
        returncode=returncode,
        stderr=stderr,
    )


def _build_text_argv(
    tmux: str, target: "Target", text: str, *, literal: bool
) -> list[str]:
    """Construct the ``tmux send-keys`` argv for the text portion of an injection.

    ``-l`` makes tmux send the argument(s) literally instead of interpreting key names.
    ``--`` terminates option parsing so a message starting with ``-`` is never mistaken for
    a flag. The target is passed via ``-t``.
    """
    argv = [tmux, "send-keys", "-t", target.as_tmux_arg()]
    if literal:
        argv.append("-l")
    argv.append("--")
    argv.append(text)
    return argv


def _build_enter_argv(tmux: str, target: "Target") -> list[str]:
    """Construct the ``tmux send-keys ... Enter`` argv (interpreted, no ``-l``).

    Issued as a separate call after a literal text send so the receiving program sees a
    real Return keypress rather than a literal LF byte.
    """
    return [tmux, "send-keys", "-t", target.as_tmux_arg(), "Enter"]


def send_keys(
    target: Union[str, "Target"],
    keys: str,
    *,
    literal: bool = True,
    enter: bool = False,
    timeout: float = 5.0,
) -> "InjectResult":
    """Low-level wrapper over ``tmux send-keys``. Never raises on the runtime path.

    This is the primitive :func:`inject` builds on; use :func:`inject` for the common
    "type a message and press Enter" case. Here ``enter`` defaults to ``False`` so the call
    maps 1:1 to a single ``send-keys`` unless you opt in.

    * ``literal=True`` (default) → ``send-keys -l`` (bytes verbatim).
    * ``literal=False`` → keys are interpreted as tmux key *names* (``C-c``, ``Enter``).
    * ``enter=True`` → an extra interpreted ``send-keys Enter`` after the text send.

    Returns an :class:`InjectResult`; ``result.ok`` is ``True`` only when tmux exited 0.
    """
    if not isinstance(keys, str):
        # Programmer error, not an environment condition — surface it.
        raise TypeError(f"keys must be a str, got {type(keys).__name__}")

    # resolve_target raises TypeError/ValueError for a bad target — both are programmer
    # errors (bugs), so let them propagate rather than swallowing into a result.
    tgt = resolve_target(target)

    tmux = _which_tmux()
    text_argv = _build_text_argv(tmux or _tmux_bin(), tgt, keys, literal=literal)

    if tmux is None:
        return _failure(
            tgt,
            text_argv,
            error=ERR_NO_TMUX,
            message=(
                "tmux is not installed / not on PATH; cannot inject keys "
                f"(looked for {_tmux_bin()!r})"
            ),
        )

    try:
        proc = _run(text_argv, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — subprocess failure must not crash the caller
        return _failure(
            tgt,
            text_argv,
            error=ERR_TIMEOUT if _is_timeout(exc) else ERR_SEND_FAILED,
            message=f"failed to run tmux send-keys: {exc}",
        )

    if proc.returncode != 0:
        return _failure(
            tgt,
            text_argv,
            error=_classify_failure(proc.stderr),
            message=(proc.stderr or "tmux send-keys failed").strip(),
            returncode=proc.returncode,
            stderr=proc.stderr or "",
        )

    if not enter:
        return InjectResult(
            ok=True,
            target=tgt.as_tmux_arg(),
            argv=tuple(text_argv),
            returncode=proc.returncode,
            stderr=proc.stderr or "",
        )

    # Press a real Return as a separate, interpreted send-keys call. ``argv`` records BOTH
    # commands, concatenated (text send then Enter send) — see InjectResult.argv.
    enter_argv = _build_enter_argv(tmux, tgt)
    full_argv = text_argv + enter_argv
    try:
        enter_proc = _run(enter_argv, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return _failure(
            tgt,
            full_argv,
            error=ERR_TIMEOUT if _is_timeout(exc) else ERR_SEND_FAILED,
            message=f"text sent but failed to send Enter: {exc}",
        )

    if enter_proc.returncode != 0:
        return _failure(
            tgt,
            full_argv,
            error=_classify_failure(enter_proc.stderr),
            message=(enter_proc.stderr or "Enter failed").strip(),
            returncode=enter_proc.returncode,
            stderr=enter_proc.stderr or "",
        )

    return InjectResult(
        ok=True,
        target=tgt.as_tmux_arg(),
        argv=tuple(full_argv),
        returncode=enter_proc.returncode,
        stderr=enter_proc.stderr or "",
    )


def inject(
    target: Union[str, "Target"],
    text: str,
    *,
    enter: bool = True,
    literal: bool = True,
    timeout: float = 5.0,
) -> "InjectResult":
    """Inject ``text`` into a tmux pane, pressing Enter by default. Never raises at runtime.

    The headline API: a finishing agent calls ``inject("work:1.0", "done, X unblocked")``
    to post a line into another agent's pane. Equivalent to :func:`send_keys` with
    ``enter=True`` — text is typed literally, then a real Return is pressed so the receiving
    REPL/agent processes the line.

    * ``enter=True`` (default) presses Return after the text; ``enter=False`` leaves it on
      the prompt unsent (e.g. to stage a command for the human to review).
    * ``literal=True`` (default) types the text verbatim; ``literal=False`` interprets it as
      tmux key names.

    On any environment failure (no tmux, no server, missing pane, send error) returns an
    :class:`InjectResult` with ``ok=False`` and a populated ``error``/``message`` — it does
    not raise, so it's safe to call from a completion hook without a try/except.
    """
    return send_keys(target, text, literal=literal, enter=enter, timeout=timeout)


# Substrings tmux prints when there is no server to connect to, vs. when the target itself
# can't be resolved. Best-effort heuristics over tmux's human-readable stderr — the exact
# wording varies by tmux version, so these are deliberately broad.
_NO_SERVER_HINTS = ("no server running", "error connecting", "no server")
_BAD_TARGET_HINTS = ("can't find", "no such", "not found", "bad target", "unknown")


def _classify_failure(stderr: str) -> str:
    """Map tmux's stderr to an ``ERR_*`` sentinel (best-effort, for caller branching)."""
    low = (stderr or "").lower()
    if any(hint in low for hint in _NO_SERVER_HINTS):
        return ERR_NO_SERVER
    if any(hint in low for hint in _BAD_TARGET_HINTS):
        return ERR_BAD_TARGET
    return ERR_SEND_FAILED


def has_session(name: str, *, timeout: float = 5.0) -> bool:
    """Whether a tmux session ``name`` exists. ``False`` when tmux/server is absent.

    Wraps ``tmux has-session -t <name>`` (exit 0 = present). Any failure to even run tmux —
    not installed, no server — is reported as ``False`` rather than raised, so it's a safe
    pre-flight guard before :func:`inject`.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("session name must be a non-empty string")
    # Strip before handing to tmux so the validated and the queried name agree — mirrors
    # resolve_target, which strips the target string it passes along.
    name = name.strip()

    tmux = _which_tmux()
    if tmux is None:
        return False
    argv = [tmux, "has-session", "-t", name]
    try:
        proc = _run(argv, timeout=timeout)
    except Exception:  # noqa: BLE001 — absence/error -> "no such session", never crash
        return False
    return proc.returncode == 0


def list_panes(
    target: Optional[Union[str, "Target"]] = None, *, timeout: float = 5.0
) -> list[dict]:
    """List tmux panes as dicts, optionally scoped to a session/window ``target``.

    Returns one dict per pane with keys ``pane_id`` (``%N``), ``session``, ``window_index``,
    ``window_name``, ``pane_index``, ``active`` (bool) and ``title``. Returns an empty list
    when tmux is absent, no server is running, or the target doesn't exist — never raises on
    those runtime conditions, mirroring the rest of the module's best-effort posture.

    Implemented via ``tmux list-panes`` with an explicit ``-F`` format string so the output
    is parsed deterministically (one record per line, tab-separated) rather than scraped.
    """
    tmux = _which_tmux()
    if tmux is None:
        return []

    fmt = "\t".join(
        (
            "#{pane_id}",
            "#{session_name}",
            "#{window_index}",
            "#{window_name}",
            "#{pane_index}",
            "#{pane_active}",
            "#{pane_title}",
        )
    )
    argv = [tmux, "list-panes"]
    if target is not None:
        tgt = resolve_target(target)
        # '-s' scopes to the whole SESSION when only a session was given; for a specific
        # window/pane (or a pane id), the plain '-t' default suffices. Build the flag list
        # explicitly so flag order isn't load-bearing on a positional splice.
        if tgt.window is None and tgt.pane is None and not tgt.is_pane_id:
            argv.append("-s")
        argv += ["-F", fmt, "-t", tgt.as_tmux_arg()]
    else:
        # No target → enumerate EVERY pane on the server. Without '-a', tmux scopes
        # list-panes to the current window only, which silently hides panes in other
        # windows/sessions — the opposite of the documented "omit for all panes" contract.
        argv += ["-a", "-F", fmt]

    try:
        proc = _run(argv, timeout=timeout)
    except Exception:  # noqa: BLE001
        return []
    if proc.returncode != 0:
        return []

    panes: list[dict] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        # Be tolerant of fewer fields than requested (older tmux / odd builds).
        while len(parts) < 7:
            parts.append("")
        pane_id, session, win_idx, win_name, pane_idx, active, title = parts[:7]
        panes.append(
            {
                "pane_id": pane_id,
                "session": session,
                "window_index": win_idx,
                "window_name": win_name,
                "pane_index": pane_idx,
                "active": active == "1",
                "title": title,
            }
        )
    return panes

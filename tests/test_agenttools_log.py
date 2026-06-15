"""Tests for agenttools_log — the shared structured JSONL logger.

Run from the repo root::

    uv run --with pytest python -m pytest tests/ -q
    # or, if agenttools-log is installed:  python -m pytest tests/ -q
"""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import agenttools_log as atl  # noqa: E402
from agenttools_log import configure, get_logger, reset  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env_and_state(monkeypatch):
    """Each test starts from a pristine logger tree and no AGENTTOOLS_* env.

    pytest's logging plugin can leave the global ``logging.disable`` level raised between
    tests; clear it so a later test's ``isEnabledFor`` isn't suppressed by harness state.
    """
    for var in ("AGENTTOOLS_LOG_LEVEL", "AGENTTOOLS_LOG_FILE", "AGENTTOOLS_LOG_FORMAT"):
        monkeypatch.delenv(var, raising=False)
    logging.disable(logging.NOTSET)
    reset()
    yield
    reset()
    logging.disable(logging.NOTSET)


def _capture(level=None, fmt="json"):
    """Configure logging to an in-memory stream and return (stream, logger)."""
    buf = io.StringIO()
    configure(level=level, fmt=fmt, stream=buf)
    return buf, get_logger("test")


def _lines(buf: io.StringIO) -> list[str]:
    return [ln for ln in buf.getvalue().splitlines() if ln.strip()]


# --- JSONL shape ------------------------------------------------------------------------


def test_each_line_is_valid_json_with_required_keys():
    buf, log = _capture()
    log.info("first")
    log.info("second", extra_field=1)
    log.error("third")

    lines = _lines(buf)
    assert len(lines) == 3
    for line in lines:
        rec = json.loads(line)  # raises if not valid JSON -> a hard failure
        assert isinstance(rec, dict)
        for key in ("ts", "level", "logger", "msg"):
            assert key in rec, f"missing required key {key!r} in {rec}"


def test_record_field_values():
    buf, log = _capture()
    log.info("hello world", user="alice", count=3, ok=True)
    rec = json.loads(_lines(buf)[0])

    assert rec["msg"] == "hello world"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "agenttools.test"
    assert rec["user"] == "alice"
    assert rec["count"] == 3
    assert rec["ok"] is True
    # ts is ISO-8601 UTC (has a timezone offset / Z).
    assert rec["ts"].endswith("+00:00") or rec["ts"].endswith("Z")


def test_structured_fields_are_top_level_keys():
    buf, log = _capture()
    log.warn("retry", attempt=2, url="https://x")
    rec = json.loads(_lines(buf)[0])
    assert rec["level"] == "WARNING"
    assert rec["attempt"] == 2
    assert rec["url"] == "https://x"


def test_stdlib_percent_formatting_positional_args():
    # stdlib-style lazy %-formatting must work (and never raise) for migrating callers.
    buf, log = _capture()
    log.info("user %s id=%d", "alice", 42)
    rec = json.loads(_lines(buf)[0])
    assert rec["msg"] == "user alice id=42"


def test_percent_formatting_with_fields_and_exc_info():
    buf, log = _capture()
    try:
        raise ValueError("boom")
    except ValueError:
        # positional args + structured field + exc_info all together
        log.error("op %s failed", "sync", request_id="r9", exc_info=True)
    rec = json.loads(_lines(buf)[0])
    assert rec["msg"] == "op sync failed"
    assert rec["request_id"] == "r9"
    assert "ValueError" in rec["exc"]


def test_bad_percent_format_does_not_crash():
    # Too few args for the format string would raise in stdlib at getMessage time; our
    # formatter swallows it and still emits a valid line.
    buf, log = _capture()
    log.info("needs two: %s %s", "only-one")
    lines = _lines(buf)
    assert len(lines) == 1
    json.loads(lines[0])  # still valid JSON


def test_reserved_keys_cannot_be_clobbered_by_a_field():
    buf, log = _capture()
    # A stray ``msg``/``level`` field must not overwrite the canonical record values.
    log.info("real message", msg="HIJACK", level="HIJACK", logger="HIJACK")
    rec = json.loads(_lines(buf)[0])
    assert rec["msg"] == "real message"
    assert rec["level"] == "INFO"
    assert rec["logger"] == "agenttools.test"


# --- level filtering --------------------------------------------------------------------


def test_level_filtering_default_is_info():
    buf, log = _capture()  # default INFO
    log.debug("debug-should-be-dropped")
    log.info("info-should-show")
    lines = _lines(buf)
    assert len(lines) == 1
    assert json.loads(lines[0])["msg"] == "info-should-show"


def test_level_filtering_debug_lets_everything_through():
    buf, log = _capture(level="debug")
    log.debug("d")
    log.info("i")
    log.warn("w")
    log.error("e")
    assert len(_lines(buf)) == 4


def test_level_filtering_error_only():
    buf, log = _capture(level="error")
    log.info("i")
    log.warn("w")
    log.error("e")
    lines = _lines(buf)
    assert len(lines) == 1
    assert json.loads(lines[0])["level"] == "ERROR"


def test_unknown_level_name_falls_back_to_info():
    buf = io.StringIO()
    configure(level="nonsense", stream=buf)
    log = get_logger("test")
    log.info("shown")
    log.debug("hidden")
    lines = _lines(buf)
    assert len(lines) == 1


# --- env config -------------------------------------------------------------------------


def test_env_level_is_respected(monkeypatch):
    monkeypatch.setenv("AGENTTOOLS_LOG_LEVEL", "error")
    reset()
    # Auto-config from env on first get_logger; redirect handler to a buffer afterwards by
    # reconfiguring with the env level but our stream.
    log = get_logger("test")
    # The auto-config wrote a stderr handler; assert the level took effect via the tree.
    assert logging.getLogger("agenttools").level == logging.ERROR
    # Drop info; keep error.
    buf = io.StringIO()
    configure(stream=buf)  # level still from env (ERROR)
    log = get_logger("test")
    log.info("dropped")
    log.error("kept")
    assert len(_lines(buf)) == 1


def test_env_format_pretty(monkeypatch):
    monkeypatch.setenv("AGENTTOOLS_LOG_FORMAT", "pretty")
    buf = io.StringIO()
    configure(stream=buf)  # fmt from env -> pretty
    log = get_logger("svc")
    log.info("started", port=8080)
    out = buf.getvalue()
    # Pretty mode is NOT JSON; it carries the message and the field as k=v.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip())
    assert "started" in out
    assert "port=8080" in out


def test_explicit_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("AGENTTOOLS_LOG_LEVEL", "error")
    buf = io.StringIO()
    configure(level="debug", stream=buf)  # explicit debug beats env error
    log = get_logger("test")
    log.debug("shown")
    assert len(_lines(buf)) == 1


# --- file sink + 0600 -------------------------------------------------------------------


def test_file_sink_writes_jsonl_and_is_0600(tmp_path):
    log_file = tmp_path / "out.jsonl"
    configure(log_file=str(log_file))
    log = get_logger("filetest")
    log.info("to file", k="v")
    reset()  # close the handler so the file is flushed

    assert log_file.exists()
    mode = stat.S_IMODE(log_file.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    lines = [ln for ln in log_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["msg"] == "to file"
    assert rec["k"] == "v"


def test_pre_existing_file_is_forced_to_0600(tmp_path):
    log_file = tmp_path / "preexisting.jsonl"
    log_file.write_text("")
    os.chmod(log_file, 0o644)  # world-readable to start
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o644

    configure(log_file=str(log_file))
    get_logger("x").info("hi")
    reset()

    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


def test_env_file_sink(monkeypatch, tmp_path):
    log_file = tmp_path / "env.jsonl"
    monkeypatch.setenv("AGENTTOOLS_LOG_FILE", str(log_file))
    reset()
    log = get_logger("envfile")  # auto-config reads AGENTTOOLS_LOG_FILE
    log.info("via env")
    reset()
    rec = json.loads(log_file.read_text().splitlines()[0])
    assert rec["msg"] == "via env"


def test_unwritable_file_falls_back_to_stderr(tmp_path, capsys):
    bad = tmp_path / "nope" / "deeper" / "x.jsonl"  # parent dir does not exist
    # Should not raise; degrades to stderr.
    configure(log_file=str(bad))
    log = get_logger("fallback")
    log.error("still works")
    err = capsys.readouterr().err
    assert "still works" in err
    assert not bad.exists()


def test_file_open_failure_diagnostic_is_json(tmp_path, capsys):
    # The "cannot open log file" diagnostic must NOT contaminate the default JSONL stream:
    # every non-empty stderr line must parse as JSON in default (json) mode.
    bad = tmp_path / "missing-dir" / "x.jsonl"
    configure(log_file=str(bad))
    get_logger("diag").info("payload")
    err = capsys.readouterr().err
    nonempty = [ln for ln in err.splitlines() if ln.strip()]
    assert nonempty, "expected at least the diagnostic + the log line"
    for line in nonempty:
        rec = json.loads(line)  # raises if any line is plain text -> a hard failure
        assert {"ts", "level", "logger", "msg"} <= set(rec)
    # The diagnostic record names the failure explicitly.
    msgs = [json.loads(ln)["msg"] for ln in nonempty]
    assert any("cannot open log file" in m for m in msgs)
    assert "payload" in msgs


# --- never crash on bad input -----------------------------------------------------------


def test_unserializable_field_does_not_crash():
    buf, log = _capture()

    class Unserializable:
        def __repr__(self):
            return "<obj>"

    # An object json can't natively serialize must be stringified, not raised.
    log.info("weird", obj=Unserializable(), s={1, 2, 3})
    line = _lines(buf)[0]
    rec = json.loads(line)  # still valid JSON
    assert rec["msg"] == "weird"
    assert isinstance(rec["obj"], str)


def test_field_whose_str_raises_does_not_crash():
    buf, log = _capture()

    class Hostile:
        def __repr__(self):
            raise RuntimeError("boom")

        def __str__(self):
            raise RuntimeError("boom")

    # Even a value whose str()/repr() raises must not crash logging.
    log.info("hostile", x=Hostile())
    lines = _lines(buf)
    assert len(lines) == 1
    json.loads(lines[0])  # valid JSON regardless


def test_exc_info_attaches_traceback():
    buf, log = _capture()
    try:
        raise ValueError("kaboom")
    except ValueError:
        log.error("caught", exc_info=True)
    rec = json.loads(_lines(buf)[0])
    assert "exc" in rec
    assert "ValueError" in rec["exc"]
    assert "kaboom" in rec["exc"]


def test_exception_helper():
    buf, log = _capture()
    try:
        raise KeyError("missing")
    except KeyError:
        log.exception("oops", request_id="r1")
    rec = json.loads(_lines(buf)[0])
    assert rec["level"] == "ERROR"
    assert rec["request_id"] == "r1"
    assert "KeyError" in rec["exc"]


# --- per-module loggers + misc ----------------------------------------------------------


def test_per_module_logger_names():
    buf = io.StringIO()
    configure(stream=buf)
    get_logger("alpha").info("a")
    get_logger("beta").info("b")
    recs = [json.loads(ln) for ln in _lines(buf)]
    assert {r["logger"] for r in recs} == {"agenttools.alpha", "agenttools.beta"}


def test_root_logger_name():
    buf = io.StringIO()
    configure(stream=buf)
    get_logger().info("root")
    rec = json.loads(_lines(buf)[0])
    assert rec["logger"] == "agenttools"


def test_native_extra_dict_is_surfaced():
    buf, log = _capture()
    # stdlib-native usage: extra={...} on the underlying logger should still surface.
    log.stdlib.info("native", extra={"native_field": 7})
    rec = json.loads(_lines(buf)[0])
    assert rec["native_field"] == 7


def test_public_api_surface():
    for name in ("get_logger", "configure", "reset", "StructuredLogger",
                 "DEBUG", "INFO", "WARNING", "ERROR"):
        assert hasattr(atl, name), f"missing public symbol {name}"


def test_get_logger_autoconfigures_without_explicit_configure():
    # No configure() call — get_logger must still produce a working logger from env.
    log = get_logger("auto")
    assert log.isEnabledFor(atl.INFO)
    # Underlying tree got a handler installed by auto-config.
    assert logging.getLogger("agenttools").handlers


# --- review-board regression fixes ------------------------------------------------------


def test_stack_info_is_emitted(tmp_path):
    buf, log = _capture()
    log.info("with stack", stack_info=True, marker="m")
    rec = json.loads(_lines(buf)[0])
    assert "stack" in rec
    assert "Stack (most recent call last)" in rec["stack"]
    assert rec["marker"] == "m"  # other fields still ride along


def test_force_false_does_not_duplicate_handlers():
    buf = io.StringIO()
    configure(stream=buf)  # first config -> one handler
    n_before = len(logging.getLogger("agenttools").handlers)
    # A second force=False call must be a no-op, not add a duplicate handler.
    configure(stream=buf, force=False)
    configure(force=False)
    assert len(logging.getLogger("agenttools").handlers) == n_before == 1
    # And a single log line is emitted once, not duplicated.
    get_logger("dup").info("once")
    assert len(_lines(buf)) == 1


def test_explicit_stream_overrides_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "env.jsonl"
    monkeypatch.setenv("AGENTTOOLS_LOG_FILE", str(env_file))
    buf = io.StringIO()
    # Explicit stream must beat $AGENTTOOLS_LOG_FILE (README: explicit args win over env).
    configure(stream=buf)
    get_logger("ovr").info("to stream not file")
    assert "to stream not file" in buf.getvalue()
    assert not env_file.exists()  # the env file was NOT written


def test_explicit_log_file_none_forces_stream_despite_env(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / "env.jsonl"
    monkeypatch.setenv("AGENTTOOLS_LOG_FILE", str(env_file))
    # Passing log_file=None EXPLICITLY suppresses the env file -> stderr stream.
    configure(log_file=None)
    get_logger("none").info("stderr please")
    assert "stderr please" in capsys.readouterr().err
    assert not env_file.exists()


def _open_fd_count() -> int:
    """Best-effort count of open file descriptors for this process."""
    # /dev/fd works on macOS and Linux; fall back to a probe if it's unavailable.
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        import resource

        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        count = 0
        for fd in range(min(soft, 4096)):
            try:
                os.fstat(fd)
                count += 1
            except OSError:
                pass
        return count


def test_file_handler_releases_fd_on_reset(tmp_path):
    # Reconfiguring/resetting a file sink must not leak file descriptors. Assert the open-fd
    # count is stable across many open/close cycles (deterministic, not warning-dependent).
    log_file = tmp_path / "leak.jsonl"

    configure(log_file=str(log_file))
    get_logger("leak").info("warmup")
    reset()
    baseline = _open_fd_count()

    for _ in range(50):
        configure(log_file=str(log_file))
        get_logger("leak").info("x")
        reset()  # must close & release the underlying fd

    after = _open_fd_count()
    # Allow a tiny slack for unrelated runtime fds; a real leak would be ~50.
    assert after <= baseline + 2, f"fd leak: baseline={baseline} after={after}"

    # File still well-formed after many open/close cycles.
    lines = [ln for ln in log_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 51  # warmup + 50
    json.loads(lines[0])


# --- second-round review fixes (thread-safety + pretty single-line) ---------------------


def test_concurrent_first_use_installs_one_handler():
    import threading

    reset()
    barrier = threading.Barrier(16)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait()  # release all threads at once to maximize the race window
            get_logger("race").info("hi")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"workers raised: {errors}"
    # The race must NOT have installed duplicate handlers on the shared tree.
    assert len(logging.getLogger("agenttools").handlers) == 1


def test_pretty_mode_message_with_newline_stays_single_line():
    buf = io.StringIO()
    configure(fmt="pretty", stream=buf)
    log = get_logger("svc")
    log.info("line1\nline2\rline3\tend", field="a\nb")
    out = buf.getvalue()
    # Exactly one physical line: the embedded newlines/CRs were escaped, not emitted.
    physical = [ln for ln in out.split("\n") if ln.strip()]
    assert len(physical) == 1, f"forged extra lines: {out!r}"
    assert "\\n" in physical[0]  # the newline was escaped to its backslash form
    assert "line1" in physical[0] and "line2" in physical[0]


def test_pretty_mode_field_with_newline_is_escaped():
    buf = io.StringIO()
    configure(fmt="pretty", stream=buf)
    get_logger("svc").info("ok", danger="a\nb\nc")
    out = buf.getvalue()
    assert len([ln for ln in out.split("\n") if ln.strip()]) == 1
    assert "\n" not in out.rstrip("\n")  # no raw embedded newline survived

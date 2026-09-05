"""Regression test for tests/conftest.py's `_strip_ambient_ship_env`.

The bug this guards (GH-528): a developer/CI shell that happens to export one of
`ci/ship/ship.sh`'s gate-control knobs (e.g. `SHIP_EXTERNAL_REVIEW=0`, set machine-wide via rig's
global env mechanism) silently changed what several `tests/test_ship.py` external-review-gate
tests exercised, because those tests build their subprocess env as `dict(os.environ)` and only pop
a specific known set of ambient vars by name. It was caught live, not by the suite -- nothing
previously asserted the strip itself, so a future edit (e.g. narrowing the prefixes, or converting
the module-level call back into a per-test `monkeypatch.delenv` autouse fixture -- the exact
alternative `tests/conftest.py`'s docstring argues against) could silently reopen the hole while
the rest of the suite stays green.

`tests/` has no `__init__.py`, so `conftest.py` is importable directly by its bare module name
(pytest inserts `tests/` on `sys.path` for a conftest with no package markers) -- this test
exercises the real `_strip_ambient_ship_env` function rather than reimplementing its logic.

IMPORTANT: `_strip_ambient_ship_env` mutates `os.environ` directly (that is its whole job), and by
the time this test runs in a full-suite invocation, `tests/test_ship.py` /
`tests/test_ship_notify_task_cli.py` have already applied their own module-level
`SHIP_REVIEW_QUORUM` / `SHIP_TASK_NOTIFY_ENABLED` defaults to the REAL process environment at
collection time. Calling the function against the real `os.environ` here would permanently delete
those defaults for the rest of the pytest session (an earlier version of this test did exactly
that and broke ~130 unrelated tests later in the run). So every test below first swaps in a throwaway
dict via `monkeypatch.setattr(os, "environ", ...)` -- `conftest._strip_ambient_ship_env` reads
`os.environ` through the same `os` module object, so it operates on the throwaway dict, and
monkeypatch restores the real `os.environ` object at teardown regardless of what the function did
to the substitute.
"""
from __future__ import annotations

import os

import conftest


def test_strip_ambient_ship_env_removes_ship_and_hatch_request_vars(monkeypatch):
    """A `SHIP_*` gate-control var and a `RIG_HATCH_REQUEST_SHIP_*` hatch-request var are both
    removed by the strip, reproducing exactly the ambient `SHIP_EXTERNAL_REVIEW=0` case that broke
    the external-review-gate tests."""
    fake_environ = {
        "SHIP_EXTERNAL_REVIEW": "0",
        "RIG_HATCH_REQUEST_SHIP_SKIP_CI": "some ambient justification",
        "PATH": os.environ.get("PATH", ""),
    }
    monkeypatch.setattr(os, "environ", fake_environ)

    conftest._strip_ambient_ship_env()

    assert "SHIP_EXTERNAL_REVIEW" not in fake_environ
    assert "RIG_HATCH_REQUEST_SHIP_SKIP_CI" not in fake_environ


def test_strip_ambient_ship_env_leaves_unrelated_vars_alone(monkeypatch):
    """A negative control: a var that merely shares a substring with the banned prefixes (but does
    not start with one) must survive the strip, proving this isn't an over-broad wipe of the whole
    environment."""
    fake_environ = {
        "SHIPMENT_TRACKING_ID": "unrelated",
        "RIG_HATCH_REQUEST_PKILL_GUARD": "not a ship gate",
        "PATH": os.environ.get("PATH", ""),
    }
    monkeypatch.setattr(os, "environ", fake_environ)

    conftest._strip_ambient_ship_env()

    assert fake_environ.get("SHIPMENT_TRACKING_ID") == "unrelated"
    assert fake_environ.get("RIG_HATCH_REQUEST_PKILL_GUARD") == "not a ship gate"

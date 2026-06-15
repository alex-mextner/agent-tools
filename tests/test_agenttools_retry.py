"""Tests for agenttools_retry — the shared, dependency-free retry helper.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_retry.py -q
    # or, if agenttools-retry is installed:  python -m pytest tests/ -q

Every test injects a fake sleeper so the suite is instant and asserts the *exact* backoff
schedule rather than wall-clock timing. Jitter is exercised only with a fixed seed, so the
schedule is deterministic and reproducible.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import agenttools_retry as atr  # noqa: E402
from agenttools_retry import (  # noqa: E402
    RetryError,
    RetryPolicy,
    RetryState,
    retries,
    retry,
)


class Recorder:
    """A fake clock: records every delay it is asked to sleep, sleeps zero real time."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class Flaky:
    """A callable that raises ``exc`` for the first ``fail_times`` calls, then returns.

    Tracks ``calls`` so a test can assert how many attempts actually happened.
    """

    def __init__(self, fail_times: int, exc: BaseException, ok: object = "ok") -> None:
        self.fail_times = fail_times
        self.exc = exc
        self.ok = ok
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return self.ok


# --- succeeds first try -----------------------------------------------------------------


def test_succeeds_first_try_calls_once_and_never_sleeps():
    sleeper = Recorder()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return 42

    result = retry(fn, max_attempts=5, sleeper=sleeper)

    assert result == 42
    assert calls["n"] == 1
    assert sleeper.delays == []  # a first-try success sleeps zero times


# --- succeeds after N -------------------------------------------------------------------


def test_succeeds_after_n_failures_then_returns():
    sleeper = Recorder()
    flaky = Flaky(fail_times=2, exc=ValueError("boom"))

    result = retry(flaky, max_attempts=5, retry_on=ValueError, sleeper=sleeper)

    assert result == "ok"
    assert flaky.calls == 3  # failed twice, succeeded on the third
    assert len(sleeper.delays) == 2  # slept once between each failed attempt


def test_decorator_succeeds_after_n():
    sleeper = Recorder()
    flaky = Flaky(fail_times=1, exc=ConnectionError())

    @retries(max_attempts=3, retry_on=ConnectionError, sleeper=sleeper)
    def wrapped():
        return flaky()

    assert wrapped() == "ok"
    assert flaky.calls == 2
    assert len(sleeper.delays) == 1


# --- exhausts and raises ----------------------------------------------------------------


def test_exhausts_and_reraises_original_exception():
    sleeper = Recorder()
    boom = RuntimeError("still broken")
    flaky = Flaky(fail_times=99, exc=boom)

    with pytest.raises(RuntimeError) as ei:
        retry(flaky, max_attempts=3, retry_on=RuntimeError, sleeper=sleeper)

    assert ei.value is boom  # the ORIGINAL exception, not a wrapper
    assert flaky.calls == 3  # exactly max_attempts tries
    assert len(sleeper.delays) == 2  # slept between attempts, NOT after the last one


def test_exhausts_with_reraise_false_wraps_in_retry_error():
    sleeper = Recorder()
    boom = RuntimeError("nope")
    flaky = Flaky(fail_times=99, exc=boom)

    with pytest.raises(RetryError) as ei:
        retry(
            flaky,
            max_attempts=4,
            retry_on=RuntimeError,
            reraise=False,
            sleeper=sleeper,
        )

    err = ei.value
    assert err.attempts == 4
    assert err.last_exception is boom
    assert err.__cause__ is boom  # original preserved as the cause
    assert flaky.calls == 4


# --- backoff schedule is as configured --------------------------------------------------


def test_backoff_schedule_is_exponential_and_capped():
    sleeper = Recorder()
    flaky = Flaky(fail_times=99, exc=ValueError())

    with pytest.raises(ValueError):
        retry(
            flaky,
            max_attempts=6,
            base_delay=1.0,
            factor=2.0,
            max_delay=10.0,
            retry_on=ValueError,
            sleeper=sleeper,
        )

    # 1, 2, 4, 8, then capped at 10 (would have been 16). 5 sleeps for 6 attempts.
    assert sleeper.delays == [1.0, 2.0, 4.0, 8.0, 10.0]


def test_backoff_factor_other_than_two():
    sleeper = Recorder()
    flaky = Flaky(fail_times=99, exc=ValueError())

    with pytest.raises(ValueError):
        retry(
            flaky,
            max_attempts=4,
            base_delay=0.5,
            factor=3.0,
            max_delay=100.0,
            retry_on=ValueError,
            sleeper=sleeper,
        )

    # 0.5 * 3**0, 3**1, 3**2 => 0.5, 1.5, 4.5
    assert sleeper.delays == [0.5, 1.5, 4.5]


def test_jitter_is_deterministic_for_a_fixed_seed():
    """Same seed => byte-identical schedule. This is the property tests rely on."""

    def run():
        sleeper = Recorder()
        flaky = Flaky(fail_times=99, exc=ValueError())
        with pytest.raises(ValueError):
            retry(
                flaky,
                max_attempts=5,
                base_delay=1.0,
                factor=2.0,
                max_delay=1000.0,
                jitter=0.25,
                jitter_seed=1234,
                retry_on=ValueError,
                sleeper=sleeper,
            )
        return sleeper.delays

    first = run()
    second = run()
    assert first == second  # reproducible across runs with the same seed

    # And jitter actually perturbed the schedule away from the bare 1,2,4,8 baseline,
    # while staying within the +-25% band of each base delay.
    baseline = [1.0, 2.0, 4.0, 8.0]
    assert first != baseline
    for slept, base in zip(first, baseline):
        assert base * 0.75 <= slept <= base * 1.25


def test_jitter_accepts_an_injected_random_instance():
    sleeper_a = Recorder()
    sleeper_b = Recorder()
    flaky_a = Flaky(fail_times=99, exc=ValueError())
    flaky_b = Flaky(fail_times=99, exc=ValueError())

    for sleeper, flaky in ((sleeper_a, flaky_a), (sleeper_b, flaky_b)):
        with pytest.raises(ValueError):
            retry(
                flaky,
                max_attempts=4,
                base_delay=1.0,
                jitter=random.Random(7),  # passing an RNG => default +-10% spread
                retry_on=ValueError,
                sleeper=sleeper,
            )

    # Two independently-seeded Random(7) streams produce the identical schedule.
    assert sleeper_a.delays == sleeper_b.delays
    assert len(sleeper_a.delays) == 3


# --- non-retryable exception propagates immediately -------------------------------------


def test_non_retryable_exception_propagates_immediately():
    sleeper = Recorder()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise KeyError("not in retry_on")

    with pytest.raises(KeyError):
        # Only ValueError is retryable; KeyError must escape on the first call.
        retry(fn, max_attempts=5, retry_on=ValueError, sleeper=sleeper)

    assert calls["n"] == 1  # no retries
    assert sleeper.delays == []  # never slept


def test_keyboard_interrupt_is_not_retried_by_default():
    sleeper = Recorder()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise KeyboardInterrupt()

    # Default retry_on=Exception excludes BaseException, so Ctrl-C is never swallowed.
    with pytest.raises(KeyboardInterrupt):
        retry(fn, max_attempts=5, sleeper=sleeper)

    assert calls["n"] == 1
    assert sleeper.delays == []


# --- result-based retry -----------------------------------------------------------------


def test_retry_on_result_predicate_then_success():
    sleeper = Recorder()
    seq = iter([503, 503, 200])

    def call_api():
        return next(seq)

    result = retry(
        call_api,
        max_attempts=5,
        retry_if_result=lambda status: status == 503,
        sleeper=sleeper,
    )

    assert result == 200
    assert len(sleeper.delays) == 2  # retried past the two 503s


def test_retry_on_result_exhausted_returns_last_value_not_raises():
    sleeper = Recorder()

    def always_busy():
        return 503

    # Value-based exhaustion hands back the last VALUE; it does not raise.
    result = retry(
        always_busy,
        max_attempts=3,
        retry_if_result=lambda status: status == 503,
        sleeper=sleeper,
    )

    assert result == 503
    assert len(sleeper.delays) == 2


# --- on_retry hook ----------------------------------------------------------------------


def test_on_retry_hook_receives_state_for_each_retry():
    sleeper = Recorder()
    flaky = Flaky(fail_times=2, exc=ValueError("x"))
    seen: list[RetryState] = []

    retry(
        flaky,
        max_attempts=5,
        base_delay=1.0,
        factor=2.0,
        retry_on=ValueError,
        on_retry=seen.append,
        sleeper=sleeper,
    )

    assert [s.attempt for s in seen] == [1, 2]  # fired before each of the 2 sleeps
    assert [s.delay for s in seen] == [1.0, 2.0]  # the delay about to be slept
    assert all(isinstance(s.exception, ValueError) for s in seen)
    assert all(s.max_attempts == 5 for s in seen)


def test_on_retry_hook_sees_result_on_result_based_retry():
    sleeper = Recorder()
    seq = iter([503, 200])
    seen: list[RetryState] = []

    retry(
        lambda: next(seq),
        max_attempts=3,
        retry_if_result=lambda s: s == 503,
        on_retry=seen.append,
        sleeper=sleeper,
    )

    assert len(seen) == 1
    assert seen[0].result == 503
    assert seen[0].exception is None


def test_on_retry_hook_errors_do_not_break_the_retry_loop():
    sleeper = Recorder()
    flaky = Flaky(fail_times=1, exc=ValueError())

    def bad_hook(_state: RetryState) -> None:
        raise RuntimeError("observer blew up")

    # A misbehaving observer must not take down the operation it observes.
    result = retry(
        flaky,
        max_attempts=3,
        retry_on=ValueError,
        on_retry=bad_hook,
        sleeper=sleeper,
    )
    assert result == "ok"


# --- policy object + validation ---------------------------------------------------------


def test_decorator_preserves_metadata_and_exposes_policy():
    @retries(max_attempts=2)
    def documented():
        """The original docstring."""
        return 1

    assert documented.__name__ == "documented"
    assert documented.__doc__ == "The original docstring."
    assert isinstance(documented.retry_policy, RetryPolicy)
    assert documented.retry_policy.max_attempts == 2


def test_max_attempts_one_means_no_retry():
    sleeper = Recorder()
    flaky = Flaky(fail_times=99, exc=ValueError())

    with pytest.raises(ValueError):
        retry(flaky, max_attempts=1, retry_on=ValueError, sleeper=sleeper)

    assert flaky.calls == 1
    assert sleeper.delays == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_delay": -1.0},
        {"factor": 0.5},
        {"jitter": 1.5},
    ],
)
def test_invalid_policy_parameters_raise_value_error(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_tuple_of_exception_types_is_accepted():
    sleeper = Recorder()
    seq = iter([ConnectionError(), TimeoutError(), "ok"])

    def fn():
        item = next(seq)
        if isinstance(item, BaseException):
            raise item
        return item

    result = retry(
        fn,
        max_attempts=5,
        retry_on=(ConnectionError, TimeoutError),
        sleeper=sleeper,
    )
    assert result == "ok"
    assert len(sleeper.delays) == 2


def test_public_api_surface():
    assert atr.__all__ == [
        "RetryError",
        "RetryPolicy",
        "RetryState",
        "retries",
        "retry",
    ]

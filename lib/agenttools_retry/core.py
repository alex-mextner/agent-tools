"""Core implementation of the shared retry helper.

The public surface (``retry``, ``retries``, ``RetryPolicy``, ``RetryState``,
``RetryError``) is re-exported from the package ``__init__``; import from there.

Design notes
------------
* One :class:`RetryPolicy` value object holds the whole configuration. Both the imperative
  :func:`retry` and the :func:`retries` decorator build a policy and call
  ``policy.call(fn, *args, **kwargs)`` — there is a single execution loop, so the two entry
  points can never drift in behaviour.
* **Deterministic jitter by construction.** The jitter source is a ``random.Random``
  instance the policy *owns*. Pass ``jitter_seed=`` for a reproducible stream (tests), or
  pass your own ``random.Random`` via ``jitter=``. With neither, the policy constructs a
  fresh, unseeded ``Random`` — never the process-global ``random`` module, so one
  misbehaving caller can't perturb another's sequence and tests are never at the mercy of
  global RNG state.
* **The clock is injectable.** ``sleeper`` defaults to ``time.sleep`` but can be any
  ``Callable[[float], None]``; tests pass a recorder so the suite is instant and can assert
  the exact backoff schedule.
* **A return value can be a retryable outcome.** Real APIs signal "try again" with a 503,
  an empty body, ``None`` — not only with an exception. ``retry_if_result`` inspects the
  return value; if it matches, the call is retried exactly like a raised, retryable
  exception. If attempts run out on a result-based retry we return the last result rather
  than raising (the caller asked us to inspect a *value*, not treat it as an error).
"""

from __future__ import annotations

import functools
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Tuple, Type, Union

# An exception spec is one exception type or a tuple of them, the same shape ``except``
# accepts. ``BaseException`` is deliberately excluded as a default — we never want to
# swallow ``KeyboardInterrupt`` / ``SystemExit`` by retrying them.
ExcTypes = Union[Type[BaseException], Tuple[Type[BaseException], ...]]


class RetryError(RuntimeError):
    """Raised when retries are exhausted by *exceptions* and re-raising is suppressed.

    By default the original exception is re-raised when attempts run out (its traceback is
    the useful one). Set ``reraise=False`` to get this wrapper instead — it carries the
    last exception as ``__cause__`` and the attempt count as :attr:`attempts`, handy when a
    caller wants one stable type to catch regardless of the underlying failure.
    """

    def __init__(self, message: str, *, attempts: int, last_exception: BaseException) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_exception = last_exception


@dataclass(frozen=True)
class RetryState:
    """The outcome of one attempt, handed to the ``on_retry`` callback before each sleep.

    Exactly one of :attr:`exception` / :attr:`result` is meaningful: ``exception`` is set
    when the attempt raised a retryable error, ``result`` when it returned a retryable
    value. :attr:`delay` is the backoff (seconds) about to be slept before the next try.
    """

    attempt: int  # 1-based index of the attempt that just finished
    max_attempts: int
    delay: float  # seconds about to be slept before the next attempt
    exception: Optional[BaseException] = None
    result: Any = None


def _normalize_exc(spec: Optional[ExcTypes]) -> Tuple[Type[BaseException], ...]:
    """Turn an exception spec into a tuple usable with ``isinstance``.

    ``None`` means "match nothing" (empty tuple) — used so a result-only policy doesn't
    accidentally retry on exceptions the caller never opted into.
    """
    if spec is None:
        return ()
    if isinstance(spec, tuple):
        return spec
    return (spec,)


@dataclass(frozen=True)
class RetryPolicy:
    """An immutable retry configuration plus the single execution loop that applies it.

    Construct one and reuse it across calls, or let :func:`retry` / :func:`retries` build
    one for you. All fields have safe defaults; see ``lib/retry/README.md`` for the full
    reference and the exact backoff formula.
    """

    max_attempts: int = 3
    base_delay: float = 0.1
    factor: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.0  # fraction (0..1] of the computed delay to randomize, e.g. 0.1=±10%
    retry_on: Optional[ExcTypes] = Exception
    retry_if_result: Optional[Callable[[Any], bool]] = None
    on_retry: Optional[Callable[[RetryState], None]] = None
    sleeper: Callable[[float], None] = time.sleep
    reraise: bool = True
    # The owned RNG. ``jitter_seed`` builds a seeded stream; a caller-supplied ``_rng``
    # (via the ``jitter=`` kwarg of :func:`retry`) wins. ``default_factory`` guarantees a
    # *distinct, unseeded* Random per policy when neither is given.
    jitter_seed: Optional[int] = None
    _rng: random.Random = field(default_factory=random.Random, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay < 0:
            raise ValueError("base_delay must be >= 0")
        if self.factor < 1:
            raise ValueError("factor must be >= 1")
        if not (0.0 <= self.jitter <= 1.0):
            raise ValueError("jitter must be in [0.0, 1.0]")
        # frozen dataclass: bypass the frozen guard to seed the owned RNG once.
        if self.jitter_seed is not None:
            object.__setattr__(self, "_rng", random.Random(self.jitter_seed))

    def compute_delay(self, attempt: int) -> float:
        """Backoff (seconds) to wait *after* the given 1-based ``attempt`` fails.

        ``base_delay * factor ** (attempt - 1)``, capped at ``max_delay``, then jittered.
        The jitter is drawn from the policy's owned RNG, so a seeded policy is fully
        reproducible. Jitter is symmetric: the delay is scaled by a factor uniformly drawn
        from ``[1 - jitter, 1 + jitter]`` (then re-capped at ``max_delay`` and floored at 0).
        """
        raw = self.base_delay * (self.factor ** (attempt - 1))
        capped = min(raw, self.max_delay)
        if self.jitter:
            spread = self._rng.uniform(-self.jitter, self.jitter)
            capped = capped * (1.0 + spread)
        return max(0.0, min(capped, self.max_delay))

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``fn(*args, **kwargs)`` under this policy and return its (eventual) result."""
        retry_exc = _normalize_exc(self.retry_on)
        last_exc: Optional[BaseException] = None

        for attempt in range(1, self.max_attempts + 1):
            is_last = attempt == self.max_attempts
            try:
                result = fn(*args, **kwargs)
            except retry_exc as exc:  # type: ignore[misc]  # empty tuple => never matches
                last_exc = exc
                if is_last:
                    break  # exhausted by exceptions: raise/wrap below
                delay = self.compute_delay(attempt)
                self._notify(attempt, delay, exception=exc)
                self.sleeper(delay)
                continue

            # The call returned. Decide whether the *value* is a retryable outcome.
            if self.retry_if_result is not None and self.retry_if_result(result):
                if is_last:
                    return result  # value-based retry exhausted: hand back the last value
                delay = self.compute_delay(attempt)
                self._notify(attempt, delay, result=result)
                self.sleeper(delay)
                continue

            return result  # success

        # The loop only falls through here via the ``break`` above, which always sets
        # ``last_exc`` — every other path returns. So exhaustion here is always
        # exception-based: re-raise the original, or wrap it when ``reraise`` is off.
        assert last_exc is not None  # noqa: S101 - documents the loop invariant
        if self.reraise:
            raise last_exc
        raise RetryError(
            f"retry exhausted after {self.max_attempts} attempt(s)",
            attempts=self.max_attempts,
            last_exception=last_exc,
        ) from last_exc

    def _notify(
        self,
        attempt: int,
        delay: float,
        *,
        exception: Optional[BaseException] = None,
        result: Any = None,
    ) -> None:
        """Invoke the ``on_retry`` hook, swallowing its errors so a bad hook can't break
        the retry loop (logging/metrics must never take down the operation they observe)."""
        if self.on_retry is None:
            return
        state = RetryState(
            attempt=attempt,
            max_attempts=self.max_attempts,
            delay=delay,
            exception=exception,
            result=result,
        )
        try:
            self.on_retry(state)
        except Exception:  # noqa: BLE001 - an observer must not break the observed call
            pass


def retry(
    fn: Callable[..., Any],
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 0.1,
    factor: float = 2.0,
    max_delay: float = 30.0,
    jitter: Union[float, random.Random] = 0.0,
    jitter_seed: Optional[int] = None,
    retry_on: Optional[ExcTypes] = Exception,
    retry_if_result: Optional[Callable[[Any], bool]] = None,
    on_retry: Optional[Callable[[RetryState], None]] = None,
    sleeper: Callable[[float], None] = time.sleep,
    reraise: bool = True,
    **kwargs: Any,
) -> Any:
    """Call ``fn(*args, **kwargs)`` under a retry policy and return its result.

    The imperative entry point. Positional/keyword arguments after ``fn`` are forwarded to
    it; the policy is configured via keyword-only parameters (documented on
    :class:`RetryPolicy`). To pass an argument to ``fn`` that collides with a policy name,
    build a :class:`RetryPolicy` yourself and call ``policy.call(fn, ...)``.

    ``jitter`` accepts either a float fraction (``0.1`` = ±10%) or a ``random.Random``
    instance to use as the jitter source directly (handy for sharing one seeded RNG across
    calls). When a ``random.Random`` is passed, the jitter fraction defaults to ``0.1``.
    """
    policy = _build_policy(
        max_attempts=max_attempts,
        base_delay=base_delay,
        factor=factor,
        max_delay=max_delay,
        jitter=jitter,
        jitter_seed=jitter_seed,
        retry_on=retry_on,
        retry_if_result=retry_if_result,
        on_retry=on_retry,
        sleeper=sleeper,
        reraise=reraise,
    )
    return policy.call(fn, *args, **kwargs)


def retries(
    *,
    max_attempts: int = 3,
    base_delay: float = 0.1,
    factor: float = 2.0,
    max_delay: float = 30.0,
    jitter: Union[float, random.Random] = 0.0,
    jitter_seed: Optional[int] = None,
    retry_on: Optional[ExcTypes] = Exception,
    retry_if_result: Optional[Callable[[Any], bool]] = None,
    on_retry: Optional[Callable[[RetryState], None]] = None,
    sleeper: Callable[[float], None] = time.sleep,
    reraise: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator form: wrap a function so every call runs under the given retry policy.

    Keyword-only, mirroring :func:`retry`'s policy parameters. The wrapped function keeps
    its name/docstring (via :func:`functools.wraps`) and exposes the policy as
    ``wrapped.retry_policy`` for introspection / per-test overrides.

        @retries(max_attempts=5, retry_on=ConnectionError, base_delay=0.25)
        def fetch(url): ...
    """
    policy = _build_policy(
        max_attempts=max_attempts,
        base_delay=base_delay,
        factor=factor,
        max_delay=max_delay,
        jitter=jitter,
        jitter_seed=jitter_seed,
        retry_on=retry_on,
        retry_if_result=retry_if_result,
        on_retry=on_retry,
        sleeper=sleeper,
        reraise=reraise,
    )

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return policy.call(fn, *args, **kwargs)

        wrapper.retry_policy = policy  # type: ignore[attr-defined]
        return wrapper

    return decorator


def _build_policy(
    *,
    max_attempts: int,
    base_delay: float,
    factor: float,
    max_delay: float,
    jitter: Union[float, random.Random],
    jitter_seed: Optional[int],
    retry_on: Optional[ExcTypes],
    retry_if_result: Optional[Callable[[Any], bool]],
    on_retry: Optional[Callable[[RetryState], None]],
    sleeper: Callable[[float], None],
    reraise: bool,
) -> RetryPolicy:
    """Shared policy builder for both entry points.

    Resolves the dual-typed ``jitter`` argument: a ``random.Random`` becomes the policy's
    owned RNG (with a default ±10% spread), a float is the jitter fraction.
    """
    rng_override: Optional[random.Random] = None
    jitter_fraction: float
    if isinstance(jitter, random.Random):
        rng_override = jitter
        jitter_fraction = 0.1
    else:
        jitter_fraction = float(jitter)

    policy = RetryPolicy(
        max_attempts=max_attempts,
        base_delay=base_delay,
        factor=factor,
        max_delay=max_delay,
        jitter=jitter_fraction,
        jitter_seed=jitter_seed,
        retry_on=retry_on,
        retry_if_result=retry_if_result,
        on_retry=on_retry,
        sleeper=sleeper,
        reraise=reraise,
    )
    if rng_override is not None:
        object.__setattr__(policy, "_rng", rng_override)
    return policy

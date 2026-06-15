"""agenttools_retry — a small, dependency-free retry helper for the agent-tools ecosystem.

A retry loop is the kind of thing every tool reimplements slightly differently (and
slightly wrong). This is the one shared copy: a :func:`retry` callable that runs a function
under a retry policy, and a :func:`retries` decorator that wraps a policy around a function
permanently. Both share one :class:`RetryPolicy` so behaviour is identical whichever entry
point you use.

What it does
------------
* **Configurable attempts** — ``max_attempts`` total tries (1 = no retry).
* **Exponential backoff + jitter** — ``base_delay * factor ** (attempt - 1)``, capped at
  ``max_delay``; jitter spreads the delay so a fleet doesn't retry in lockstep.
* **Deterministic, seedable jitter** — the jitter source is a ``random.Random`` you can
  seed (``jitter_seed=``) or replace (``jitter=``). Tests get a fixed schedule; production
  gets a fresh, unseeded ``Random``. No module-level / unseeded global randomness.
* **Retryable predicate** — decide what counts as a failure worth retrying, by exception
  type (``retry_on=``) and/or by inspecting the *return value* (``retry_if_result=``).
  Anything not matched propagates / returns immediately.
* **on-retry hook** — a callback invoked before each sleep with the attempt outcome, for
  logging / metrics (``on_retry=``).
* **Injectable clock** — the sleeper is ``time.sleep`` by default but can be replaced
  (``sleeper=``), so tests run instantly and can assert the exact backoff schedule.

Why stdlib only (no ``tenacity``, no ``backoff``)
-------------------------------------------------
The ecosystem is stdlib-first by directive. A focused retry loop is ~a hundred lines and
adds zero install/import cost; ``tenacity`` is a large dependency surface for behaviour we
can own outright, and ``backoff``'s decorator-only API can't be driven imperatively the way
``retry(fn, ...)`` can. Keeping it in-house also lets the jitter be *deterministic by
construction*, which the third-party options make awkward.

Quick start
-----------
    from agenttools_retry import retry, retries

    # Imperative: run a callable under a policy.
    result = retry(
        fetch,
        "https://example.com",
        max_attempts=5,
        base_delay=0.2,
        retry_on=(ConnectionError, TimeoutError),
    )

    # Declarative: bake the policy into a function.
    @retries(max_attempts=3, retry_on=ValueError)
    def parse(blob):
        ...

    # Retry on a *return value*, not just an exception:
    @retries(max_attempts=4, retry_if_result=lambda r: r.status == 503)
    def call_api():
        ...

See ``lib/retry/README.md`` for the full reference.
"""

from __future__ import annotations

from .core import (
    RetryError,
    RetryPolicy,
    RetryState,
    retries,
    retry,
)

__all__ = [
    "RetryError",
    "RetryPolicy",
    "RetryState",
    "retries",
    "retry",
]

__version__ = "0.1.0"

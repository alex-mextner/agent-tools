# agenttools-retry

A small, **dependency-free** retry helper for the agent-tools ecosystem — the one shared
copy of the retry loop every tool would otherwise reimplement (slightly differently, and
slightly wrong). **Stdlib only** (no `tenacity`, no `backoff`).

It gives you two entry points over one execution loop, so they can never drift:

- `retry(fn, *args, **policy)` — imperative: run a callable under a policy, get its result.
- `@retries(**policy)` — declarative: bake a policy into a function permanently.

## Features

- **Configurable attempts** — `max_attempts` total tries (`1` = no retry, just call once).
- **Exponential backoff + jitter** — `base_delay * factor ** (attempt - 1)`, capped at
  `max_delay`.
- **Deterministic, seedable jitter** — the jitter source is a `random.Random` the policy
  owns. Seed it (`jitter_seed=`) or pass your own `random.Random` for a reproducible
  schedule in tests; with neither, each policy gets a fresh, unseeded `Random`. **Never the
  process-global `random` module** — one caller can't perturb another's sequence.
- **Retryable predicate** — retry on exception type (`retry_on=`) and/or by inspecting the
  **return value** (`retry_if_result=`). Anything unmatched propagates / returns at once.
- **on-retry hook** — `on_retry=` callback fires before each sleep with a `RetryState`
  (attempt number, the delay about to be slept, and the exception or result), for
  logging / metrics. A failing hook can't break the retry loop.
- **Injectable clock** — `sleeper=` defaults to `time.sleep` but takes any
  `Callable[[float], None]`, so tests run instantly and assert the exact backoff schedule.

## Usage

```python
from agenttools_retry import retry, retries

# Imperative — run a callable under a policy.
data = retry(
    fetch, "https://example.com",
    max_attempts=5,
    base_delay=0.2,
    retry_on=(ConnectionError, TimeoutError),
)

# Declarative — bake the policy into the function.
@retries(max_attempts=3, retry_on=ValueError, base_delay=0.25)
def parse(blob):
    ...

# Retry on a RETURN VALUE, not just an exception (e.g. an HTTP 503):
@retries(max_attempts=4, retry_if_result=lambda resp: resp.status == 503)
def call_api():
    ...
```

Positional/keyword arguments after `fn` are forwarded to it; the policy is configured via
keyword-only parameters. If an argument to `fn` collides with a policy name, build a
`RetryPolicy` yourself and call `policy.call(fn, ...)`.

## Backoff schedule

The delay slept **after** the 1-based `attempt` that just failed is:

```
delay(attempt) = min(base_delay * factor ** (attempt - 1), max_delay)
```

then, if `jitter > 0`, scaled by a factor drawn uniformly from `[1 - jitter, 1 + jitter]`
(re-capped at `max_delay`, floored at `0`). With the defaults (`base_delay=0.1`,
`factor=2.0`) and `jitter=0`, the first three sleeps are `0.1, 0.2, 0.4` seconds. The
sleep happens **between** attempts — a successful (or final) attempt sleeps zero times, so
`max_attempts=N` sleeps at most `N - 1` times.

### Deterministic jitter

Jitter is randomized *delay*, but the randomness is owned and reproducible:

```python
# Two policies seeded identically produce byte-identical schedules:
a = retry(fn, jitter=0.1, jitter_seed=1234, sleeper=record)
b = retry(fn, jitter=0.1, jitter_seed=1234, sleeper=record)  # same delays as a

# Or hand in your own RNG (jitter fraction defaults to ±10%):
import random
rng = random.Random(42)
retry(fn, jitter=rng)
```

In production, pass neither `jitter_seed=` nor a `random.Random` and each policy gets its
own fresh, unseeded `Random`.

## Exception vs. result exhaustion

- **Exhausted by exceptions** — by default the **original** exception is re-raised (its
  traceback is the useful one). Set `reraise=False` to get a `RetryError` wrapper instead,
  carrying `.attempts` and `.last_exception` (and the original as `__cause__`), so a caller
  can catch one stable type.
- **Exhausted by a retryable result** — the **last result is returned**, not raised. You
  asked us to inspect a *value*, so the value comes back; treat it as an error yourself if
  you want to.

`retry_on` excludes `BaseException` by default — `KeyboardInterrupt` / `SystemExit` are
never retried.

## Public API

| Symbol | Purpose |
| --- | --- |
| `retry(fn, *args, **policy, **fn_kwargs) -> Any` | run `fn` under a policy, return its result |
| `retries(**policy) -> decorator` | wrap a function so every call retries; exposes `wrapped.retry_policy` |
| `RetryPolicy` | the immutable policy value object + the single `call(fn, ...)` loop |
| `RetryState` | per-attempt outcome handed to the `on_retry` hook |
| `RetryError` | raised on exception-exhaustion when `reraise=False` |

### Policy parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `max_attempts` | `3` | total attempts (`1` = no retry) |
| `base_delay` | `0.1` | seconds before the first retry |
| `factor` | `2.0` | exponential growth per attempt (`>= 1`) |
| `max_delay` | `30.0` | cap on any single delay (seconds) |
| `jitter` | `0.0` | fraction `[0, 1]` of the delay to randomize (`0.1` = ±10%); or a `random.Random` |
| `jitter_seed` | `None` | seed the owned jitter RNG for a reproducible schedule |
| `retry_on` | `Exception` | exception type / tuple to retry; `None` = retry no exceptions |
| `retry_if_result` | `None` | predicate on the return value; truthy = retry |
| `on_retry` | `None` | `Callable[[RetryState], None]` fired before each sleep |
| `sleeper` | `time.sleep` | injectable clock — replace in tests |
| `reraise` | `True` | re-raise the original exception on exhaustion (else `RetryError`) |

## Installing / importing as a consumer

The package lives under `lib/` in the umbrella repo and builds as the `agenttools-retry`
distribution:

```toml
# pyproject.toml of the consumer
[project]
dependencies = ["agenttools-retry"]
```

For local/dev installs from the umbrella checkout:

```sh
pip install -e /path/to/agent-tools/lib/agenttools_retry   # editable install
# or, ad-hoc, with uv:
uv run --with /path/to/agent-tools/lib/agenttools_retry python -c "from agenttools_retry import retry"
```

## Why stdlib only

The ecosystem is stdlib-first by directive. A focused retry loop is ~a couple hundred lines
and adds zero install/import cost. `tenacity` is a large dependency surface for behaviour we
can own outright; `backoff`'s decorator-only API can't be driven imperatively the way
`retry(fn, ...)` can. Owning it also lets the jitter be **deterministic by construction**,
which the third-party options make awkward.

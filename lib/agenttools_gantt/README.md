# agenttools-gantt

A compact, **deterministic** ASCII Gantt renderer for the agent-tools ecosystem — the one
shared copy of the chart logic `tg-cli`'s `/tasks` uses to turn a session's tasks into a
monospace timeline that drops straight into a Telegram `<pre>` block. **Stdlib only** (no
plotting/charting dependency).

`render_gantt` is a **pure function of its inputs**: the same tasks plus the same `now`
produce a byte-identical string. It reads no system clock, filesystem, or network, so the
output is cacheable, diffable, and snapshot-testable.

## Why it exists

ROADMAP §3.6 ("Gantt-render", shared-lib extraction): `tg-cli /tasks` renders the session's
tasks as a table + Gantt + agent summary. The chart half is this library, lifted out so the
layout lives in exactly one tested place rather than being hand-rolled inside the CLI.

## Quick start

```python
from agenttools_gantt import render_gantt

chart = render_gantt(
    [
        {"id": "spec", "label": "Write spec", "start": 0, "end": 2, "status": "done"},
        {"id": "impl", "label": "Implement", "start": 2, "end": 6,
         "deps": ["spec"], "status": "active"},
        {"id": "test", "label": "Test", "start": 6, "end": 8,
         "deps": ["impl"], "status": "blocked"},
        {"id": "ship", "label": "Ship", "start": 8, "duration": 0,
         "deps": ["test"], "status": "todo"},
    ],
    width=60,
    now=4,  # injected reference time — NEVER read from the system clock
)
print(chart)
```

```
Write spec #############           :
Implement              =========================
Test                               :           xxxxxxxxxxxxx
Ship                               :                       |
           0                       :                       8
```

Wrap that in `<pre>…</pre>` and it renders identically in Telegram (all glyphs are ASCII).

## API

```python
render_gantt(tasks, *, width=80, now=None, label_width=None) -> str
```

| Parameter | Default | Meaning |
| --- | --- | --- |
| `tasks` | — | iterable of `Task` or plain mappings (see *Task shape*) |
| `width` | `80` | **total** line width = label gutter + bar field; wider = finer time resolution |
| `now` | `None` | reference "now" on the task time axis; drawn as a `:` column when inside the window. **Injected**, never read from `datetime.now()` — omit it and no marker is drawn |
| `label_width` | `None` | override the auto-sized label gutter (mostly for fixed-column callers / tests) |

Returns the chart as a newline-joined block (no trailing newline, no trailing spaces on any
line). An empty `tasks` returns the single line `(no tasks)`.

Raises `GanttError` (a `ValueError` subclass) on malformed input: a missing `id`/`start`,
a non-numeric or non-finite coordinate (including an `int` too large for a double, `inf`,
or `nan`), `end` before `start`, an `end`/`duration` pair that disagrees, a negative
`duration`, a dependency on an unknown id, a dependency cycle, or a `width` too small to
lay out.

### Task shape

A task is a `Task` dataclass or a plain mapping with these keys:

| Key | Required | Meaning |
| --- | --- | --- |
| `id` | yes | unique task id (referenced by `deps`) |
| `label` | no (default: `id`) | the row label; truncated with `…` if it exceeds the gutter |
| `start` | yes | start coordinate on the shared time axis |
| `end` | no | end coordinate; **or** give `duration` (`end = start + duration`). Neither => a zero-length milestone. Supplying both is allowed only if they agree (compared with a couple-of-ulp float tolerance, so consistent fractional inputs like `start=0.1, duration=0.2, end=0.3` are accepted) |
| `deps` | no | ids this task waits on; affects row order, validated against the task set |
| `status` | no | keys into the glyph map below; unknown / absent => `.` |

`start`/`end` are **numbers on one shared axis** — epoch seconds, day indices, sprint days,
whatever the caller uses consistently. The renderer never interprets a number as a calendar
date; it only needs an ordering and a span.

### Status glyphs

| Status | Glyph | | Status | Glyph |
| --- | --- | --- | --- | --- |
| `done` | `#` | | `todo` / `pending` | `-` |
| `active` / `in_progress` | `=` | | `cancelled` | `/` |
| `blocked` | `x` | | *unknown / unset* | `.` |
| *milestone* (`start == end`) | `\|` | | *now-marker* | `:` |

The glyph map is exported as `STATUS_GLYPHS`; the unknown/milestone/now characters are
`DEFAULT_GLYPH`, `MILESTONE_GLYPH`, `NOW_GLYPH`.

## Determinism (why there is no argless `datetime.now()`)

The whole value of this renderer is that it is reproducible. If it read the wall clock, the
output would change every second and could never be snapshot-tested or cached. So the
reference time is **injected** via `now=`; the module touches no clock at import or call
time. Omit `now` and the chart simply has no now-marker — we never invent a value. This is
the same discipline the rest of the ecosystem follows (no module-level / unseeded global
state).

## Dependency-aware ordering

Rows are topologically sorted so a task never prints above a dependency it waits on. Ties
(tasks whose dependencies are all already placed) break by `start` ascending, then by
original declaration order — so any permutation of the same task list renders the *same*
chart. A dependency on an unknown id, or a cycle among known ids, raises `GanttError`
instead of producing a silently mis-ordered chart.

## Width scaling

`width` is the **total** line width. The label gutter auto-sizes to the longest label
(capped so the bar field keeps a usable width); the bar field is whatever remains. Every
task's span maps onto that field, so a larger `width` buys **finer time resolution**, not a
wider label column. Below a small floor the layout can't fit and `render_gantt` raises.

## Installing / importing as a consumer

The package lives under `lib/` in the umbrella repo and builds as the `agenttools-gantt`
distribution:

```toml
# pyproject.toml of the consumer
[project]
dependencies = ["agenttools-gantt"]
```

For local/dev installs from the umbrella checkout:

```sh
pip install -e /path/to/agent-tools/lib/agenttools_gantt   # editable install
# or, ad-hoc, with uv:
uv run --with /path/to/agent-tools/lib/agenttools_gantt python -c "from agenttools_gantt import render_gantt"
```

## Tests

```sh
uv run --with pytest python -m pytest tests/test_agenttools_gantt.py -q
```

The suite is deterministic and instant — no `$HOME` isolation, sleeps, or network are
needed, because the renderer is pure. It asserts golden strings for a fixed task set + a
fixed `now`, plus dependency ordering, width scaling, status glyphs, milestones, the
now-marker, empty input, and the input-validation errors.

## Why stdlib only

The ecosystem is stdlib-first by directive, and an ASCII chart is text layout — a few
hundred lines of arithmetic and string joining (`dataclasses` + `typing`, both stdlib). A
plotting/charting dependency would add a large install/import surface (and most of them draw
pixels, not characters) for output we can own outright and keep deterministic.
```

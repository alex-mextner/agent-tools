"""agenttools_gantt — a compact ASCII Gantt renderer for the agent-tools ecosystem.

``tg-cli``'s ``/tasks`` wants to show a session's tasks as a chart inside a Telegram
``<pre>`` block: a monospace timeline of bars with status glyphs, dependency-aware row
order, and a "now" marker. This is that renderer, lifted into one shared, well-tested,
**stdlib-only** library so the chart logic lives in exactly one place.

What it does
------------
* **Pure function, deterministic by construction.** :func:`render_gantt` is a function of
  its inputs alone — same tasks + same ``now`` => byte-identical string. It touches no
  clock, filesystem, network, or global state, so callers can cache and snapshot-test it.
* **No argless ``datetime.now()``.** The reference "now" is *injected* via ``now=``; the
  module reads no system clock at import or call time. Omit ``now`` and no marker is drawn.
* **Tasks as dicts or typed :class:`Task`.** ``/tasks`` passes JSON-ish rows (``id``,
  ``label``, ``start``, ``end``/``duration``, ``deps``, ``status``); both shapes normalise
  through one path.
* **Dependency-aware ordering.** Rows are topologically sorted so a task never sits above a
  dependency it waits on, with stable ``start``-then-declaration tie-breaks. A cycle or a
  dependency on an unknown id is reported, not silently mis-laid-out.
* **Width scaling.** ``width`` is the total line width (label gutter + bar field); a wider
  ``width`` buys finer time resolution, not a wider label column.
* **Status glyphs.** ``done`` ``#``, ``active``/``in_progress`` ``=``, ``blocked`` ``x``,
  ``todo``/``pending`` ``-``, ``cancelled`` ``/``, unknown ``.``, milestone ``|``,
  now-marker ``:`` — all ASCII, so the ``<pre>`` block renders identically everywhere.

Why stdlib only
---------------
The ecosystem is stdlib-first by directive, and an ASCII chart is text layout — a few
hundred lines of arithmetic and string joining. A plotting/charting dependency would add a
large install/import surface (and most draw pixels, not characters) for output we can own
outright and keep deterministic.

Quick start
-----------
    from agenttools_gantt import render_gantt

    chart = render_gantt(
        [
            {"id": "spec", "label": "Write spec", "start": 0, "end": 2, "status": "done"},
            {"id": "impl", "label": "Implement", "start": 2, "end": 6,
             "deps": ["spec"], "status": "active"},
            {"id": "ship", "label": "Ship", "start": 6, "duration": 0,
             "deps": ["impl"], "status": "todo"},
        ],
        width=60,
        now=4,  # injected reference time — never read from the system clock
    )
    print(chart)  # drop straight into a Telegram <pre> block

See ``lib/agenttools_gantt/README.md`` for the full reference.
"""

from __future__ import annotations

from .core import (
    DEFAULT_GLYPH,
    MILESTONE_GLYPH,
    NOW_GLYPH,
    STATUS_GLYPHS,
    GanttError,
    Task,
    render_gantt,
)

__all__ = [
    "DEFAULT_GLYPH",
    "MILESTONE_GLYPH",
    "NOW_GLYPH",
    "STATUS_GLYPHS",
    "GanttError",
    "Task",
    "render_gantt",
]

__version__ = "0.1.0"

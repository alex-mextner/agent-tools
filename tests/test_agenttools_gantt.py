"""Tests for agenttools_gantt — the compact, deterministic ASCII Gantt renderer.

Run from the repo root::

    uv run --with pytest python -m pytest tests/test_agenttools_gantt.py -q
    # or, if agenttools-gantt is installed:  python -m pytest tests/ -q

The renderer is a pure function — no clock, filesystem, or network — so the suite is
deterministic by construction and instant: no monkeypatching of ``$HOME``, no sleeps, no
fixtures touching the developer's machine. The reference "now" is always *injected*, so a
fixed task set plus a fixed ``now`` yields a byte-identical string we assert as a golden.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make ``lib/`` importable without an install, so the suite runs from a bare checkout.
_LIB = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import agenttools_gantt as gantt  # noqa: E402
from agenttools_gantt import (  # noqa: E402
    DEFAULT_GLYPH,
    STATUS_GLYPHS,
    GanttError,
    Task,
    render_gantt,
)

# The canonical fixture: a four-stage pipeline with a dependency chain, mixed statuses, and
# a final milestone (zero-length). Declared deliberately OUT of dependency order so the
# ordering tests prove the topological sort, not just input echo.
PIPELINE = [
    {"id": "ship", "label": "Ship", "start": 8, "duration": 0,
     "deps": ["test"], "status": "todo"},
    {"id": "test", "label": "Test", "start": 6, "end": 8,
     "deps": ["impl"], "status": "blocked"},
    {"id": "spec", "label": "Write spec", "start": 0, "end": 2, "status": "done"},
    {"id": "impl", "label": "Implement", "start": 2, "end": 6,
     "deps": ["spec"], "status": "active"},
]


# --------------------------------------------------------------------------------------
# Golden output — a fixed task set + a fixed reference time => an exact, stable string.
# --------------------------------------------------------------------------------------

def test_golden_canonical_chart():
    """A fixed pipeline at width=60 with now=4 renders byte-for-byte as expected."""
    expected = (
        "Write spec #############           :\n"
        "Implement              =========================\n"
        "Test                               :           xxxxxxxxxxxxx\n"
        "Ship                               :                       |\n"
        "           0                       :                       8"
    )
    assert render_gantt(PIPELINE, width=60, now=4) == expected


def test_golden_is_idempotent():
    """Same inputs => identical output across calls (no hidden state / no clock read)."""
    a = render_gantt(PIPELINE, width=60, now=4)
    b = render_gantt(PIPELINE, width=60, now=4)
    assert a == b


def test_no_trailing_whitespace_in_any_line():
    """Lines are rstripped — friendly to a Telegram <pre> block and to golden diffs."""
    out = render_gantt(PIPELINE, width=72, now=4)
    for line in out.split("\n"):
        assert line == line.rstrip()


# --------------------------------------------------------------------------------------
# Dependency ordering — topological sort, deterministic tie-break, validation.
# --------------------------------------------------------------------------------------

def test_dependency_ordering_overrides_input_order():
    """Rows come out in dependency order regardless of how the input was declared.

    PIPELINE is declared ship→test→spec→impl; the chart must read spec→impl→test→ship.
    """
    out = render_gantt(PIPELINE, width=60, now=4)
    # The label gutter width is constant across rows; slice it off the front of each row
    # (the axis line is the last line, hence ``[:-1]``).
    gutter = len("Write spec")  # longest label drives the auto-sized gutter
    labels = [line[:gutter].strip() for line in out.split("\n")[:-1]]
    assert labels == ["Write spec", "Implement", "Test", "Ship"]


def test_ordering_is_stable_across_input_permutations():
    """Any permutation of the same tasks yields the same chart (stable tie-break)."""
    forward = render_gantt(PIPELINE, width=60, now=4)
    reshuffled = list(reversed(PIPELINE))
    assert render_gantt(reshuffled, width=60, now=4) == forward


def test_independent_tasks_break_ties_by_start_then_declaration():
    """With no deps, rows order by ``start`` ascending, then declaration order on a tie."""
    tasks = [
        {"id": "c", "label": "C", "start": 5, "end": 6},
        {"id": "a", "label": "A", "start": 1, "end": 2},
        {"id": "b1", "label": "B1", "start": 3, "end": 4},
        {"id": "b2", "label": "B2", "start": 3, "end": 4},  # same start as b1, after it
    ]
    out = render_gantt(tasks, width=40)
    labels = [line.split(" ")[0] for line in out.split("\n")[:-1]]
    assert labels == ["A", "B1", "B2", "C"]


def test_cycle_is_reported():
    """A dependency cycle among known ids raises, naming the stuck tasks."""
    cyclic = [
        {"id": "a", "start": 0, "end": 1, "deps": ["b"]},
        {"id": "b", "start": 0, "end": 1, "deps": ["a"]},
    ]
    with pytest.raises(GanttError, match=r"cycle.*a, b"):
        render_gantt(cyclic)


def test_unknown_dependency_is_reported():
    """Depending on an id not in the task set raises, naming both tasks."""
    with pytest.raises(GanttError, match=r"unknown task 'zzz'"):
        render_gantt([{"id": "a", "start": 0, "end": 1, "deps": ["zzz"]}])


# --------------------------------------------------------------------------------------
# Width scaling — the same tasks compress onto fewer bar columns as width shrinks.
# --------------------------------------------------------------------------------------

def test_width_scaling_narrow():
    """At width=40 the same pipeline maps onto a narrower bar field, exactly."""
    expected = (
        "Write spec ########\n"
        "Implement         ===============\n"
        "Test                            xxxxxxxx\n"
        "Ship                                   |\n"
        "           0                           8"
    )
    assert render_gantt(PIPELINE, width=40) == expected


def test_width_scaling_very_narrow():
    """At width=20 bars collapse further but every task still renders a visible cell."""
    expected = (
        "Write spec ###\n"
        "Implement    =====\n"
        "Test             xxx\n"
        "Ship               |\n"
        "           0       8"
    )
    assert render_gantt(PIPELINE, width=20) == expected


def test_wider_width_gives_finer_resolution_not_wider_labels():
    """Growing ``width`` widens the bar field, while the label gutter stays put."""
    narrow = render_gantt(PIPELINE, width=40).split("\n")
    wide = render_gantt(PIPELINE, width=80).split("\n")
    # The label gutter (text before the first bar/space run) is identical width...
    narrow_gutter = narrow[0].index("#")
    wide_gutter = wide[0].index("#")
    assert narrow_gutter == wide_gutter
    # ...but the wide chart's lines are longer (more bar columns).
    assert len(wide[0]) > len(narrow[0])


def test_width_too_small_is_rejected():
    """A width that cannot fit label + separator + min bar field raises."""
    with pytest.raises(GanttError, match=r"too small"):
        render_gantt(PIPELINE, width=4)


# --------------------------------------------------------------------------------------
# Status glyphs — each status maps to its documented character; unknown => DEFAULT_GLYPH.
# --------------------------------------------------------------------------------------

def test_status_glyphs_golden():
    """Every status renders its glyph; an unknown / absent status uses DEFAULT_GLYPH."""
    tasks = [
        {"id": "a", "label": "done", "start": 0, "end": 10, "status": "done"},
        {"id": "b", "label": "active", "start": 0, "end": 10, "status": "active"},
        {"id": "c", "label": "inprog", "start": 0, "end": 10, "status": "in_progress"},
        {"id": "d", "label": "blocked", "start": 0, "end": 10, "status": "blocked"},
        {"id": "e", "label": "todo", "start": 0, "end": 10, "status": "todo"},
        {"id": "f", "label": "pending", "start": 0, "end": 10, "status": "pending"},
        {"id": "g", "label": "cancelled", "start": 0, "end": 10, "status": "cancelled"},
        {"id": "h", "label": "unknown", "start": 0, "end": 10, "status": "weird"},
        {"id": "i", "label": "nostatus", "start": 0, "end": 10},
    ]
    expected = (
        "done       #############################\n"
        "active     =============================\n"
        "inprog     =============================\n"
        "blocked    xxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
        "todo       -----------------------------\n"
        "pending    -----------------------------\n"
        "cancelled  /////////////////////////////\n"
        "unknown    .............................\n"
        "nostatus   .............................\n"
        "           0                          10"
    )
    assert render_gantt(tasks, width=40, label_width=10) == expected


def test_unknown_status_uses_default_glyph_symbol():
    """The DEFAULT_GLYPH constant is what an unrecognised status falls back to."""
    out = render_gantt(
        [{"id": "x", "label": "X", "start": 0, "end": 4, "status": "nonsense"}],
        width=20,
    )
    bar = out.split("\n")[0]
    assert DEFAULT_GLYPH in bar
    assert set(bar.split("X ")[1]) == {DEFAULT_GLYPH}


def test_known_statuses_cover_the_glyph_map():
    """Sanity: each key in STATUS_GLYPHS renders its mapped char as the whole bar fill."""
    for status, glyph in STATUS_GLYPHS.items():
        out = render_gantt(
            [{"id": "t", "label": "t", "start": 0, "end": 5, "status": status}],
            width=20,
        )
        fill = out.split("\n")[0].split("t ")[1]
        assert set(fill) == {glyph}, f"{status!r} should fill with {glyph!r}"


# --------------------------------------------------------------------------------------
# Milestones (zero-length) and the injected now-marker.
# --------------------------------------------------------------------------------------

def test_milestone_renders_a_single_cell():
    """A start==end (or duration 0) task draws exactly one milestone cell, not a bar."""
    out = render_gantt(
        [
            {"id": "a", "label": "work", "start": 0, "end": 8, "status": "done"},
            {"id": "m", "label": "release", "start": 8, "duration": 0, "deps": ["a"]},
        ],
        width=30,
    )
    milestone_line = [ln for ln in out.split("\n") if ln.startswith("release")][0]
    assert milestone_line.count("|") == 1


def test_now_marker_drawn_inside_window_only():
    """``now`` inside the window draws a ':' column; outside the window draws nothing."""
    tasks = [
        {"id": "a", "label": "early", "start": 0, "end": 3, "status": "done"},
        {"id": "b", "label": "late", "start": 7, "end": 10, "status": "todo"},
    ]
    inside = render_gantt(tasks, width=40, now=5)
    assert ":" in inside  # the gap between the two tasks shows the marker

    outside = render_gantt(tasks, width=40, now=999)
    assert ":" not in outside

    no_now = render_gantt(tasks, width=40)
    assert ":" not in no_now


def test_now_marker_appears_on_axis():
    """When drawn, the now-marker sits on the same column in rows and on the axis line."""
    out = render_gantt(
        [
            {"id": "a", "label": "early", "start": 0, "end": 3, "status": "done"},
            {"id": "b", "label": "late", "start": 7, "end": 10, "status": "todo"},
        ],
        width=40,
        now=5,
    )
    lines = out.split("\n")
    axis = lines[-1]
    assert ":" in axis
    marker_col = axis.index(":")
    # Every line that has a ':' has it at the very same column (one full-height marker).
    for ln in lines:
        if ":" in ln:
            assert ln.index(":") == marker_col


def test_now_is_never_read_from_system_clock():
    """Omitting ``now`` must not invent a wall-clock value — output stays marker-free.

    This is the determinism guarantee in test form: two renders without ``now`` are equal,
    so nothing time-varying leaked in.
    """
    a = render_gantt(PIPELINE, width=50)
    b = render_gantt(PIPELINE, width=50)
    assert a == b
    assert ":" not in a


# --------------------------------------------------------------------------------------
# Empty input and degenerate windows.
# --------------------------------------------------------------------------------------

def test_empty_list_returns_placeholder():
    """An empty task list renders a single, stable placeholder line — never an error."""
    assert render_gantt([]) == "(no tasks)"
    assert render_gantt((), width=20, now=5) == "(no tasks)"


def test_all_tasks_at_one_instant_does_not_divide_by_zero():
    """A degenerate window (every task at the same time) collapses safely to column 0."""
    out = render_gantt(
        [
            {"id": "a", "label": "a", "start": 5, "end": 5, "status": "done"},
            {"id": "b", "label": "b", "start": 5, "end": 5, "status": "todo"},
        ],
        width=20,
    )
    assert out.split("\n")[0].startswith("a |")


# --------------------------------------------------------------------------------------
# Input normalisation and validation.
# --------------------------------------------------------------------------------------

def test_duration_is_equivalent_to_end():
    """``start`` + ``duration`` produces the same chart as the equivalent ``end``."""
    via_duration = render_gantt(
        [{"id": "t", "label": "T", "start": 2, "duration": 4, "status": "active"}],
        width=30,
    )
    via_end = render_gantt(
        [{"id": "t", "label": "T", "start": 2, "end": 6, "status": "active"}],
        width=30,
    )
    assert via_duration == via_end


def test_typed_task_and_dict_render_identically():
    """A :class:`Task` instance and the equivalent dict produce the same output."""
    as_dict = render_gantt(
        [{"id": "t", "label": "T", "start": 0, "end": 4, "status": "done"}], width=30
    )
    as_task = render_gantt(
        [Task(id="t", label="T", start=0, end=4, status="done")], width=30
    )
    assert as_dict == as_task


def test_label_truncation_keeps_column_width_exact():
    """An over-long label is ellipsised to the gutter, never overflowing the column."""
    out = render_gantt(
        [
            {"id": "x", "label": "A very long task label indeed",
             "start": 0, "end": 3, "status": "done"},
            {"id": "y", "label": "short", "start": 1, "end": 2, "status": "todo"},
        ],
        width=30,
    )
    first, second = out.split("\n")[0], out.split("\n")[1]
    assert "…" in first  # truncated with an ellipsis
    # The truncated label fills the whole gutter exactly (ellipsis is the last gutter cell),
    # so the gutter width equals the visible label length up to the ellipsis.
    gutter = first.index("…") + 1
    # The other row's label is padded to that same gutter, then a separator space — i.e.
    # the bar field starts at the same column on both rows.
    assert second[:gutter].rstrip() == "short"
    assert first[gutter] == " " and second[gutter] == " "  # the gutter/field separator


@pytest.mark.parametrize(
    "bad, match",
    [
        ({"id": "x", "start": 5, "end": 2}, r"end .* before start"),
        ({"label": "noid", "start": 0, "end": 1}, r"missing required 'id'"),
        ({"id": "x"}, r"missing required 'start'"),
        ({"id": "x", "start": 0, "end": 1, "deps": "spec"}, r"must be a sequence"),
        ({"id": "x", "start": True, "end": 1}, r"must be a number"),
        ({"id": "x", "start": 0, "duration": -2}, r"duration .* is negative"),
        ({"id": "x", "start": 0, "end": 5, "duration": 2}, r"disagree"),
    ],
)
def test_malformed_input_raises_gantterror(bad, match):
    """Each class of malformed row raises a pointed GanttError, not a bare crash."""
    with pytest.raises(GanttError, match=match):
        render_gantt([bad])


def test_non_mapping_task_is_rejected():
    """A task that is neither a Task nor a mapping is a clear error, naming its position."""
    with pytest.raises(GanttError, match=r"#0:.*Task or a mapping"):
        render_gantt([42])


def test_task_end_before_start_rejected_at_construction():
    """The Task dataclass itself guards end >= start, independent of the renderer."""
    with pytest.raises(GanttError, match=r"end .* before start"):
        Task(id="x", label="X", start=5, end=2)


def test_public_api_surface():
    """The documented names are exported and the version is pinned."""
    assert set(gantt.__all__) >= {
        "render_gantt", "Task", "GanttError", "STATUS_GLYPHS", "DEFAULT_GLYPH",
    }
    assert gantt.__version__ == "0.1.0"

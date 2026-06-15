"""Core implementation of the compact ASCII Gantt renderer.

The public surface (:func:`render_gantt`, :class:`Task`, :class:`GanttError`, the
``STATUS_GLYPHS`` map) is re-exported from the package ``__init__``; import from there.

WHAT THIS FILE IS
    The one function ``tg-cli``'s ``/tasks`` reaches for to turn a list of tasks into a
    monospace ASCII Gantt chart that drops straight into a Telegram ``<pre>`` block. It is
    a pure function of its inputs: same tasks + same ``now`` => byte-identical string, with
    no clock, filesystem, network, or global state touched. That determinism is the whole
    point — it lets the caller cache, snapshot-test, and diff the output, and it lets *our*
    tests assert golden strings.

DESIGN NOTES
    * **No argless ``datetime.now()`` — ever.** The reference "now" is injected via the
      keyword-only ``now=`` parameter. At import time the module touches no clock at all
      (the stdlib-lazy-import rule plus a determinism rule). If a caller omits ``now`` we
      simply do not draw the now-marker; we never invent a wall-clock value, because that
      would make the output non-reproducible.
    * **Tasks come in as plain dicts or as :class:`Task`.** ``/tasks`` has its rows as
      JSON-ish dicts; typed callers can pass :class:`Task`. Both are normalised through one
      path so the renderer only ever sees a :class:`Task`.
    * **Time is unit-agnostic.** ``start`` / ``end`` are numbers on a single shared axis —
      epoch seconds, day indices, sprint days, whatever the caller uses, as long as they
      are consistent. The renderer only needs an ordering and a span; it never interprets a
      number as a calendar date. (``duration`` is sugar: ``end = start + duration``.)
    * **Dependency-aware ordering.** Rows are topologically sorted so a task never prints
      above a dependency it waits on, with ties broken by ``start`` then declaration order
      so the result is stable. A dependency cycle is reported, not silently mis-ordered.
    * **Width scaling is the contract.** ``width`` is the *total* line width (label gutter +
      bar field). The bar field is whatever is left after the gutter; every task's span is
      mapped onto that field, so a wider ``width`` means finer time resolution, not a wider
      label column.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

# A status string maps to a single-cell glyph drawn at the bar's leading edge and used to
# fill the bar. Kept as a plain dict (not an Enum) so callers can pass arbitrary status
# strings from a tracker and we degrade gracefully via ``DEFAULT_GLYPH`` rather than raise.
STATUS_GLYPHS: Dict[str, str] = {
    "done": "#",
    "active": "=",
    "in_progress": "=",
    "blocked": "x",
    "todo": "-",
    "pending": "-",
    "cancelled": "/",
}

# Glyph used when a task's status is unknown / unset. A hollow bar reads as "scheduled but
# unclassified" — distinct from the solid/##-filled known states.
DEFAULT_GLYPH = "."

# A zero-width or sub-character span still deserves one visible cell, so a milestone (start
# == end) does not vanish. This is that single-cell marker.
MILESTONE_GLYPH = "|"

# The now-marker drawn as a full-height column across the chart when ``now`` is supplied and
# falls inside the time window.
NOW_GLYPH = ":"


class GanttError(ValueError):
    """Raised for malformed input the renderer cannot lay out.

    Subclasses :class:`ValueError` (the input *is* an invalid value) so existing
    ``except ValueError`` handlers in callers keep working, while ``except GanttError``
    lets a caller distinguish a layout problem from any other ``ValueError``.
    """


# What a task may arrive as: a typed ``Task`` or a plain mapping (the ``/tasks`` JSON row).
TaskLike = Union["Task", Mapping[str, object]]


@dataclass(frozen=True)
class Task:
    """One row of the chart, normalised.

    ``start`` and ``end`` are numbers on the caller's single shared time axis (see the
    module docstring) — ``end`` must be ``>= start``. ``deps`` are the ids of tasks this one
    waits on; they affect row ordering and are validated against the supplied id set.
    ``status`` keys into :data:`STATUS_GLYPHS` (unknown / ``None`` => :data:`DEFAULT_GLYPH`).
    """

    id: str
    label: str
    start: float
    end: float
    deps: Tuple[str, ...] = ()
    status: Optional[str] = None

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise GanttError(
                f"task {self.id!r}: end ({self.end}) is before start ({self.start})"
            )

    @property
    def glyph(self) -> str:
        """The fill character for this task's bar, from its status."""
        if self.status is None:
            return DEFAULT_GLYPH
        return STATUS_GLYPHS.get(self.status, DEFAULT_GLYPH)


def _coerce_task(raw: TaskLike, *, index: int) -> Task:
    """Normalise one dict/``Task`` into a :class:`Task`, resolving ``duration`` => ``end``.

    ``index`` is the task's position in the input and is only used to make error messages
    point at the offending row when it has no usable id yet.
    """
    if isinstance(raw, Task):
        return raw
    if not isinstance(raw, Mapping):
        raise GanttError(
            f"task #{index}: expected a Task or a mapping, got {type(raw).__name__}"
        )

    where = raw.get("id", f"#{index}")
    if "id" not in raw or raw["id"] in (None, ""):
        raise GanttError(f"task {where!r}: missing required 'id'")
    task_id = str(raw["id"])
    label = str(raw.get("label", task_id))

    if "start" not in raw or raw["start"] is None:
        raise GanttError(f"task {task_id!r}: missing required 'start'")
    start = _as_number(raw["start"], field_name="start", task_id=task_id)

    # ``end`` wins if present; otherwise derive it from ``duration`` (default 0 => a
    # milestone). Supplying both is allowed only if they agree, to catch caller mistakes.
    has_end = raw.get("end") is not None
    has_duration = raw.get("duration") is not None
    if has_end:
        end = _as_number(raw["end"], field_name="end", task_id=task_id)
        if has_duration:
            dur = _as_number(raw["duration"], field_name="duration", task_id=task_id)
            if start + dur != end:
                raise GanttError(
                    f"task {task_id!r}: end ({end}) and start+duration "
                    f"({start + dur}) disagree"
                )
    elif has_duration:
        dur = _as_number(raw["duration"], field_name="duration", task_id=task_id)
        if dur < 0:
            raise GanttError(f"task {task_id!r}: duration ({dur}) is negative")
        end = start + dur
    else:
        end = start  # neither end nor duration => a zero-length milestone

    deps_raw = raw.get("deps") or ()
    if isinstance(deps_raw, (str, bytes)):
        raise GanttError(
            f"task {task_id!r}: 'deps' must be a sequence of ids, not a string"
        )
    if not isinstance(deps_raw, Iterable):
        raise GanttError(
            f"task {task_id!r}: 'deps' must be a sequence of ids, "
            f"got {type(deps_raw).__name__}"
        )
    deps = tuple(str(d) for d in deps_raw)

    status = raw.get("status")
    return Task(
        id=task_id,
        label=label,
        start=start,
        end=end,
        deps=deps,
        status=None if status is None else str(status),
    )


def _as_number(value: object, *, field_name: str, task_id: str) -> float:
    """Coerce a start/end/duration cell to ``float`` with a pointed error on failure.

    ``bool`` is rejected explicitly — ``True``/``False`` are ``int`` subclasses in Python,
    and a boolean in a time field is always a caller mistake, not a 0/1 coordinate.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GanttError(
            f"task {task_id!r}: {field_name} must be a number, got {value!r}"
        )
    return float(value)


def _order_tasks(tasks: Sequence[Task]) -> List[Task]:
    """Topologically sort so no task precedes a dependency, deterministically.

    Kahn's algorithm with a stable tie-break: among tasks whose dependencies are all
    already placed, pick the one with the smallest ``start``, then the earliest original
    declaration order. Dependencies pointing at unknown ids are ignored for ordering (they
    are reported separately as a hard error by :func:`_validate_deps`), so ordering never
    deadlocks on a typo. A genuine cycle among *known* ids raises :class:`GanttError`.
    """
    index_of = {t.id: i for i, t in enumerate(tasks)}
    known = set(index_of)
    # Only count edges between tasks we actually have; unknown deps are validated elsewhere.
    remaining_deps: Dict[str, set] = {
        t.id: {d for d in t.deps if d in known} for t in tasks
    }
    dependents: Dict[str, List[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        for d in remaining_deps[t.id]:
            dependents[d].append(t.id)

    def sort_key(task_id: str) -> Tuple[float, int]:
        t = tasks[index_of[task_id]]
        return (t.start, index_of[task_id])

    ready = sorted(
        (tid for tid, deps in remaining_deps.items() if not deps), key=sort_key
    )
    ordered: List[Task] = []
    while ready:
        tid = ready.pop(0)
        ordered.append(tasks[index_of[tid]])
        for child in dependents[tid]:
            remaining_deps[child].discard(tid)
            if not remaining_deps[child]:
                # Insert keeping ``ready`` sorted, so the next pop is the right tie-break.
                _insort(ready, child, sort_key)

    if len(ordered) != len(tasks):
        stuck = sorted(set(known) - {t.id for t in ordered})
        raise GanttError(f"dependency cycle among tasks: {', '.join(stuck)}")
    return ordered


def _insort(seq: List[str], item: str, key) -> None:
    """Insert ``item`` into the already-key-sorted ``seq`` keeping it sorted (stable)."""
    k = key(item)
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        if key(seq[mid]) <= k:
            lo = mid + 1
        else:
            hi = mid
    seq.insert(lo, item)


def _validate_deps(tasks: Sequence[Task]) -> None:
    """Raise if any dependency points at an id not present in the task set."""
    known = {t.id for t in tasks}
    for t in tasks:
        for d in t.deps:
            if d not in known:
                raise GanttError(
                    f"task {t.id!r}: depends on unknown task {d!r}"
                )


def _bar_cells(field_width: int) -> List[str]:
    """A fresh blank bar field (list of single-space cells) of ``field_width`` columns."""
    return [" "] * field_width


def _to_col(value: float, lo: float, span: float, field_width: int) -> int:
    """Map a time ``value`` onto a 0-based column in a ``field_width``-wide field.

    ``lo``/``span`` are the window's start and (end - start). A degenerate window
    (``span == 0`` — every task at one instant) collapses everything to column 0. The
    result is clamped to the field so floating point at the right edge can't overflow.
    """
    if span <= 0:
        return 0
    # Right edge (value == lo + span) must land on the last cell, not one past it, hence
    # ``field_width - 1`` as the multiplier and the clamp.
    raw = (value - lo) / span * (field_width - 1)
    col = int(round(raw))
    return max(0, min(field_width - 1, col))


def render_gantt(
    tasks: Iterable[TaskLike],
    *,
    width: int = 80,
    now: Optional[float] = None,
    label_width: Optional[int] = None,
) -> str:
    """Render ``tasks`` as a compact monospace ASCII Gantt chart.

    Parameters
    ----------
    tasks:
        An iterable of :class:`Task` instances or plain mappings with the keys ``id``
        (required), ``label`` (default: the id), ``start`` (required), ``end`` *or*
        ``duration`` (default: a zero-length milestone), ``deps`` (default: none) and
        ``status`` (default: unknown => :data:`DEFAULT_GLYPH`). ``start``/``end`` are
        numbers on one shared axis; their unit is the caller's business.
    width:
        Total line width — the label gutter plus the bar field. Wider means finer time
        resolution, not a wider label column. Must be large enough to leave at least a few
        bar columns after the gutter.
    now:
        Reference "now" on the same axis, drawn as a vertical marker when it falls inside
        the window. **Injected, never read from the system clock** — omit it (``None``) and
        no marker is drawn; the renderer never calls :func:`datetime.now`.
    label_width:
        Override the auto-sized label gutter (longest label, capped so the bar field keeps a
        usable width). Mostly for tests / fixed-column callers.

    Returns
    -------
    str
        The chart as a newline-joined block (no trailing newline), ready for a Telegram
        ``<pre>`` block. An empty ``tasks`` yields a single ``"(no tasks)"`` line.

    Raises
    ------
    GanttError
        On malformed input: a missing id/start, ``end`` before ``start``, a dependency on an
        unknown id, a dependency cycle, or a ``width`` too small to lay out.
    """
    coerced = [_coerce_task(t, index=i) for i, t in enumerate(tasks)]
    if not coerced:
        return "(no tasks)"

    _validate_deps(coerced)
    ordered = _order_tasks(coerced)

    if width < _MIN_WIDTH:
        raise GanttError(f"width ({width}) too small; need at least {_MIN_WIDTH}")

    # Label gutter: the longest label, but never so wide it starves the bar field. One
    # trailing space separates the gutter from the bars.
    longest = max(len(t.label) for t in ordered)
    if label_width is None:
        max_gutter = width - _MIN_BAR_FIELD - 1
        gutter = min(longest, max(_MIN_LABEL_WIDTH, max_gutter))
    else:
        if label_width < 1:
            raise GanttError(f"label_width ({label_width}) must be >= 1")
        gutter = label_width
    field_width = width - gutter - 1
    if field_width < _MIN_BAR_FIELD:
        raise GanttError(
            f"width ({width}) leaves only {field_width} bar columns; "
            f"need {_MIN_BAR_FIELD} (shrink labels or widen)"
        )

    lo = min(t.start for t in ordered)
    hi = max(t.end for t in ordered)
    span = hi - lo

    now_col: Optional[int] = None
    if now is not None and lo <= now <= hi:
        now_col = _to_col(float(now), lo, span, field_width)

    lines = [
        _render_row(t, lo, span, field_width, gutter, now_col) for t in ordered
    ]
    lines.append(_render_axis(lo, hi, span, field_width, gutter, now_col))
    # Strip trailing blanks: they are invisible in a <pre> block but bloat the payload and
    # make golden-string tests fragile. Internal spacing (alignment) is untouched.
    return "\n".join(line.rstrip() for line in lines)


def _render_row(
    task: Task,
    lo: float,
    span: float,
    field_width: int,
    gutter: int,
    now_col: Optional[int],
) -> str:
    """Render one task as ``"<label padded to gutter> <bar field>"``."""
    cells = _bar_cells(field_width)
    start_col = _to_col(task.start, lo, span, field_width)
    end_col = _to_col(task.end, lo, span, field_width)

    if task.end == task.start:
        cells[start_col] = MILESTONE_GLYPH  # a milestone: one visible cell
    else:
        for c in range(start_col, end_col + 1):
            cells[c] = task.glyph

    # The now-marker overlays the bar but never erases a milestone glyph (the milestone is
    # the more specific signal at that exact column).
    if now_col is not None and cells[now_col] == " ":
        cells[now_col] = NOW_GLYPH

    label = _fit_label(task.label, gutter)
    return f"{label} {''.join(cells)}"


def _render_axis(
    lo: float,
    hi: float,
    span: float,
    field_width: int,
    gutter: int,
    now_col: Optional[int],
) -> str:
    """Render the time-axis footer: ``lo`` left-aligned, ``hi`` right-aligned under bars.

    Numbers are printed as ints when they are whole (the common day-index / epoch-second
    case) so the axis stays compact. The now-marker column, if any, is shown with the
    :data:`NOW_GLYPH` between the endpoints.
    """
    axis = list(" " * field_width)
    if now_col is not None:
        axis[now_col] = NOW_GLYPH
    lo_s = _fmt_num(lo)
    hi_s = _fmt_num(hi)

    # Left endpoint at column 0; right endpoint flush to the field's right edge, but never
    # overwriting the left label if the field is narrow.
    for i, ch in enumerate(lo_s):
        if i < field_width:
            axis[i] = ch
    start_hi = field_width - len(hi_s)
    if start_hi > len(lo_s):  # only if it doesn't collide with the left label
        for i, ch in enumerate(hi_s):
            axis[start_hi + i] = ch

    return f"{' ' * gutter} {''.join(axis)}"


def _fit_label(label: str, gutter: int) -> str:
    """Left-justify ``label`` to ``gutter`` columns, truncating with an ellipsis if needed."""
    if len(label) <= gutter:
        return label.ljust(gutter)
    if gutter <= 1:
        return label[:gutter]
    return label[: gutter - 1] + "…"  # one-char ellipsis keeps the column width exact


def _fmt_num(value: float) -> str:
    """Format an axis number: whole values as ints, otherwise a trimmed float."""
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


# Layout floors. ``_MIN_BAR_FIELD`` keeps a bar field wide enough that start/end map to
# distinguishable columns; ``_MIN_LABEL_WIDTH`` keeps labels from collapsing to nothing;
# ``_MIN_WIDTH`` is the smallest total line that can hold both plus the separator space.
_MIN_BAR_FIELD = 4
_MIN_LABEL_WIDTH = 3
_MIN_WIDTH = _MIN_LABEL_WIDTH + 1 + _MIN_BAR_FIELD

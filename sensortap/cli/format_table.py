"""Plain-text table formatting for the CLI's non-JSON output (Req 12.2,
12.10, 12.11).

``format_sensor_table`` renders the ``list`` command's sensor table:
grouped by ``kind``, ids ordered alphabetically within each group, with
column widths computed across the whole result set before any row is
printed (Req 12.2). Colour is applied to the ``availability`` column only,
through a :class:`Style` object that resolves to empty strings whenever
stdout is not a terminal or ``NO_COLOR`` is set, so callers always call
``style.for_availability(...)`` unconditionally rather than branching on
whether colour is enabled (Req 12.10).

``format_values`` implements the values-truncation rule for a single
Reading (Req 12.11): a `values` sequence with more than 8 elements, or
more than 8 rows when reshaped by `shape`, is trimmed to the first 8
rows/columns, with the full `shape`, the `unit` and an explicit omission
indicator always printed alongside.

Note on Req 12.2's group ordering: the requirement text says only
"grouped by `kind`, ordered alphabetically by Sensor_Id within each
group" -- it does not specify an ordering for the groups themselves.
`KIND_VOCABULARY` (schema/kinds.py) is a `frozenset`, which carries no
defined order, so "vocabulary order" is not a meaningful alternative here.
Groups are therefore ordered alphabetically by `kind`, which is
deterministic and matches the within-group rule already specified.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Sequence

from sensortap.schema.enums import Availability
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo

MAX_ITEMS = 8
MAX_ROWS = 8
MAX_COLS = 8

_RESET = "\x1b[0m"

_AVAILABILITY_COLOURS: dict[Availability, str] = {
    Availability.PRESENT: "\x1b[32m",  # green
    Availability.ABSENT: "\x1b[90m",  # dim/grey
    Availability.UNAVAILABLE: "\x1b[33m",  # yellow
    Availability.IN_USE_BY_OTHER_APP: "\x1b[33m",  # yellow
    Availability.PERMISSION_DENIED: "\x1b[31m",  # red
}


def _no_color_env_set() -> bool:
    """True when the ``NO_COLOR`` environment variable is present, no
    matter its value -- per the NO_COLOR informal standard, "any value,
    including an empty string, indicates that colour should be
    disabled" (https://no-color.org/). Only the variable's *absence*
    means colour may be used.
    """

    return "NO_COLOR" in os.environ


@dataclass(frozen=True, slots=True)
class Style:
    """Resolves ANSI colour codes, deciding once at construction time
    whether colour is active at all (Req 12.10).

    Colour is disabled -- every ``for_*`` method returns ``""`` -- when
    stdout is not a terminal or when ``NO_COLOR`` is set. Callers must
    not branch on whether colour is enabled themselves; they call
    ``style.for_availability(...)`` unconditionally and get either a real
    ANSI code or an empty string, giving one code path regardless of the
    output destination.
    """

    enabled: bool = field(default=False)

    def __init__(self, *, enabled: bool | None = None) -> None:
        if enabled is None:
            enabled = sys.stdout.isatty() and not _no_color_env_set()
        object.__setattr__(self, "enabled", enabled)

    def for_availability(self, availability: Availability) -> str:
        """The ANSI colour-start code for ``availability``, or ``""``
        when colour is disabled or ``availability`` has no assigned
        colour."""

        if not self.enabled:
            return ""
        return _AVAILABILITY_COLOURS.get(availability, "")

    def reset(self) -> str:
        """The ANSI reset code, or ``""`` when colour is disabled."""

        return _RESET if self.enabled else ""

    def colourize(self, text: str, availability: Availability) -> str:
        """``text`` wrapped in the colour for ``availability``, or
        ``text`` unchanged when colour is disabled."""

        code = self.for_availability(availability)
        if not code:
            return text
        return f"{code}{text}{self.reset()}"


def _default_rate(sensor: SensorInfo) -> str:
    default = sensor.rate_hz.default
    return "" if default is None else str(default)


def format_sensor_table(sensors: Sequence[SensorInfo], *, style: Style | None = None) -> str:
    """Render ``sensors`` as a fixed-width table, grouped by ``kind``,
    ids ordered alphabetically within each group (Req 12.2).

    Column widths are computed once across the entire result set before
    any row is printed, so columns stay aligned across group
    boundaries. The ``availability`` column is colourized through
    ``style`` (Req 12.10).
    """

    if style is None:
        style = Style()

    rows = sorted(sensors, key=lambda s: (s.kind, s.id))

    columns = ("id", "kind", "unit", "rate_hz", "availability")
    cell_values: list[tuple[str, str, str, str, str]] = []
    for sensor in rows:
        cell_values.append(
            (
                sensor.id,
                sensor.kind,
                sensor.unit or "",
                _default_rate(sensor),
                str(sensor.availability),
            )
        )

    widths = [len(name) for name in columns]
    for row in cell_values:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    lines: list[str] = []
    current_kind: str | None = None
    for sensor, row in zip(rows, cell_values):
        if sensor.kind != current_kind:
            if current_kind is not None:
                lines.append("")
            current_kind = sensor.kind
        id_cell, kind_cell, unit_cell, rate_cell, avail_cell = row
        coloured_avail = style.colourize(avail_cell.ljust(widths[4]), sensor.availability)
        line = (
            id_cell.ljust(widths[0])
            + "  "
            + kind_cell.ljust(widths[1])
            + "  "
            + unit_cell.ljust(widths[2])
            + "  "
            + rate_cell.ljust(widths[3])
            + "  "
            + coloured_avail
        )
        lines.append(line)

    return "\n".join(lines) + ("\n" if lines else "")


def format_values(
    values: Sequence[float],
    shape: Sequence[int],
    unit: str | None,
    *,
    max_items: int = MAX_ITEMS,
    max_rows: int = MAX_ROWS,
    max_cols: int = MAX_COLS,
) -> str:
    """Render ``values`` for display, truncating per Req 12.11.

    When ``values`` has at most ``max_items`` elements and, if 2D,
    at most ``max_rows`` rows, the full data is printed. Otherwise the
    output is limited to the first ``max_rows`` rows and ``max_cols``
    columns (or the first ``max_items`` elements for 1D data), always
    preceded by ``shape`` and ``unit``, with an explicit indicator of
    what was omitted.
    """

    shape = tuple(shape)
    unit_text = "" if unit is None else unit
    header = f"shape={list(shape)} unit={unit_text!r}"

    is_2d = len(shape) == 2
    if is_2d:
        n_rows, n_cols = shape
        matrix = [
            list(values[r * n_cols : (r + 1) * n_cols]) for r in range(n_rows)
        ]
        truncated_rows = n_rows > max_rows
        truncated_cols = n_cols > max_cols
        if not truncated_rows and not truncated_cols:
            body = "\n".join(" ".join(str(v) for v in row) for row in matrix)
            return f"{header}\n{body}"

        shown_rows = matrix[:max_rows]
        lines = [
            " ".join(str(v) for v in row[:max_cols]) for row in shown_rows
        ]
        omitted_rows = max(0, n_rows - max_rows)
        omitted_cols = max(0, n_cols - max_cols)
        omission_parts = []
        if omitted_rows:
            omission_parts.append(f"{omitted_rows} more row(s)")
        if omitted_cols:
            omission_parts.append(f"{omitted_cols} more column(s)")
        omission = "... (" + ", ".join(omission_parts) + " omitted)"
        return f"{header}\n" + "\n".join(lines) + "\n" + omission

    total = len(values)
    if total <= max_items:
        body = " ".join(str(v) for v in values)
        return f"{header}\n{body}"

    shown = list(values[:max_items])
    omitted = total - max_items
    body = " ".join(str(v) for v in shown)
    omission = f"... ({omitted} more element(s) omitted)"
    return f"{header}\n{body}\n{omission}"


def format_sensor_info_detail(info: SensorInfo) -> str:
    """Render the complete `SensorInfo` record, one field per line
    (Req 12.5). Unlike `format_sensor_table`, which only shows a handful
    of columns suited to scanning many sensors at once, this shows every
    field -- the point of `inspect` is the full record for one sensor.
    """

    lines = [
        f"schema_version: {info.schema_version}",
        f"id: {info.id}",
        f"kind: {info.kind}",
        f"dtype: {info.dtype}",
        f"unit: {info.unit}",
        f"channels: {list(info.channels)}",
        f"shape: {list(info.shape)}",
        f"range: {list(info.range) if info.range is not None else None}",
        f"resolution: {info.resolution}",
        (
            "rate_hz: default="
            f"{info.rate_hz.default} min={info.rate_hz.min} max={info.rate_hz.max} "
            f"supported={list(info.rate_hz.supported)}"
        ),
        f"delivery: {info.delivery}",
        f"derived: {info.derived}",
        f"requires_consent: {info.requires_consent}",
        f"requires_elevation: {info.requires_elevation}",
        f"source: {list(info.source)}",
        f"vendor: {info.vendor}",
        f"part_number: {info.part_number}",
        f"availability: {info.availability}",
    ]
    for key, value in info.extra.items():
        lines.append(f"extra.{key}: {value}")
    return "\n".join(lines) + "\n"


def format_reading(reading: Reading, info: SensorInfo, *, style: Style | None = None) -> str:
    """Render a single :class:`Reading` for the ``read`` command's
    plain-text output, applying the values-truncation rule (Req 12.11).
    """

    if style is None:
        style = Style()

    values_text = format_values(reading.values, info.shape, info.unit)
    status_line = f"id: {reading.id}\nstatus: {reading.status}\nt_mono: {reading.t_mono}\nt_wall: {reading.t_wall}\nseq: {reading.seq}"
    return f"{status_line}\n{values_text}\n"

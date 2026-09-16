"""Tests for the table formatter (Req 12.2, 12.10, 12.11)."""

from __future__ import annotations

import sys

import pytest

from sensortap.cli.format_table import Style, format_sensor_table, format_values
from sensortap.schema.enums import Availability, Delivery, Dtype
from sensortap.schema.rate import RateSpec
from sensortap.schema.sensor_info import SensorInfo


def _sensor(id_, kind, unit="degC", availability=Availability.PRESENT):
    return SensorInfo(
        schema_version="1.0",
        id=id_,
        kind=kind,
        dtype=Dtype.SCALAR,
        unit=unit,
        channels=("x",),
        shape=(1,),
        range=None,
        resolution=None,
        rate_hz=RateSpec(default=10.0, min=None, max=None),
        delivery=Delivery.POLL,
        derived=False,
        requires_consent=False,
        requires_elevation=False,
        source=("ref",),
        vendor=None,
        part_number=None,
        availability=availability,
    )


def test_style_disabled_when_not_a_tty(monkeypatch):
    monkeypatch.delattr(sys.stdout.__class__, "isatty", raising=False) if False else None
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    style = Style()
    assert style.for_availability(Availability.PRESENT) == ""
    assert style.reset() == ""


def test_style_disabled_when_no_color_env_set_even_empty(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "")
    style = Style()
    assert style.for_availability(Availability.PRESENT) == ""


def test_style_enabled_when_tty_and_no_no_color(monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    style = Style()
    assert style.for_availability(Availability.PRESENT) != ""
    assert style.reset() != ""


def test_style_one_code_path_returns_string_always():
    # for_availability must always return a str, never branch differently
    # for callers -- both enabled and disabled styles satisfy the same
    # interface.
    enabled = Style(enabled=True)
    disabled = Style(enabled=False)
    assert isinstance(enabled.for_availability(Availability.ABSENT), str)
    assert isinstance(disabled.for_availability(Availability.ABSENT), str)


def test_grouping_by_kind_and_alphabetical_within_group():
    sensors = [
        _sensor("temp.b.0", "temp"),
        _sensor("accel.a.0", "accel"),
        _sensor("temp.a.0", "temp"),
        _sensor("accel.b.0", "accel"),
    ]
    table = format_sensor_table(sensors, style=Style(enabled=False))
    lines = [l for l in table.split("\n") if l.strip()]
    ids_in_order = [line.split()[0] for line in lines]
    assert ids_in_order == ["accel.a.0", "accel.b.0", "temp.a.0", "temp.b.0"]


def test_column_widths_align_across_groups():
    sensors = [
        _sensor("a.short.0", "accel"),
        _sensor("temp.very-long-id.0", "temp"),
    ]
    table = format_sensor_table(sensors, style=Style(enabled=False))
    lines = [l for l in table.split("\n") if l.strip()]
    # The id column's width is fixed across the whole table (computed
    # over the full result set), so the id field itself is padded to the
    # same width on every row.
    max_id_len = max(len(s.id) for s in sensors)
    id_fields = [line[: max_id_len + 2] for line in lines]
    assert len({len(f) for f in id_fields}) == 1
    assert all(field[max_id_len:] == "  " for field in id_fields)


def test_format_values_no_truncation_for_small_flat_values():
    result = format_values((1, 2, 3), (3,), "degC")
    assert "omitted" not in result
    assert "shape=[3]" in result
    assert "'degC'" in result


def test_format_values_truncates_flat_values_over_eight():
    values = tuple(range(12))
    result = format_values(values, (12,), None)
    assert "shape=[12]" in result
    assert "4 more element(s) omitted" in result
    shown_line = result.split("\n")[1]
    assert shown_line.split() == [str(v) for v in range(8)]


def test_format_values_truncates_matrix_rows_and_cols():
    n_rows, n_cols = 10, 10
    values = tuple(range(n_rows * n_cols))
    result = format_values(values, (n_rows, n_cols), "V")
    assert "shape=[10, 10]" in result
    assert "2 more row(s)" in result
    assert "2 more column(s)" in result
    rows = [
        line for line in result.split("\n") if line and not line.startswith("shape") and "omitted" not in line
    ]
    assert len(rows) == 8
    assert all(len(row.split()) == 8 for row in rows)

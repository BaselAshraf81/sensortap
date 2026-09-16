"""Tests for `WindowsMotionAdapter` (Req 13.1, 13.4, 6.1, 6.3, 6.6).

Uses the real `winsdk` package (confirmed importable/installed, see
`test_winrt_binding_probe.py`). This dev/CI machine has no physical
accelerometer/gyrometer/magnetometer, so these tests exercise the
"WinRT class present, sensor absent" style paths robustly -- that is
exactly what actually runs in CI -- while staying correct if real
hardware happens to be attached.
"""

from __future__ import annotations

import pytest

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION
from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows.winrt_motion import WindowsMotionAdapter
from sensortap.schema.enums import Availability, Dtype


def test_meta() -> None:
    meta = WindowsMotionAdapter.meta
    assert meta.adapter_id == "winrt_motion"
    assert meta.interface_version == ADAPTER_INTERFACE_VERSION
    assert meta.supported_platforms == WINDOWS_PLATFORMS
    assert meta.read_only_declared is True


def test_discover_returns_three_vector3_motion_records() -> None:
    adapter = WindowsMotionAdapter()
    records = adapter.discover()

    assert len(records) == 3
    by_kind = {record.kind: record for record in records}
    assert set(by_kind) == {"accel", "gyro", "magn"}

    expected_units = {"accel": "m/s2", "gyro": "deg/s", "magn": "uT"}
    for kind, record in by_kind.items():
        assert record.dtype is Dtype.VECTOR3
        assert record.shape == (3,)
        assert record.channels == ("x", "y", "z")
        assert record.unit == expected_units[kind]
        assert record.derived is False
        assert record.availability in (Availability.PRESENT, Availability.ABSENT)


def test_discover_ids_are_prefixed_by_kind() -> None:
    adapter = WindowsMotionAdapter()
    records = adapter.discover()
    by_kind = {record.kind: record for record in records}

    assert by_kind["accel"].id.startswith("accel.winrt.")
    assert by_kind["gyro"].id.startswith("gyro.winrt.")
    assert by_kind["magn"].id.startswith("magn.winrt.")


def test_read_unknown_sensor_id_raises_key_error() -> None:
    adapter = WindowsMotionAdapter()
    adapter.discover()

    with pytest.raises(KeyError):
        adapter.read("nonexistent.sensor.id")


def test_configure_rate_unknown_sensor_id_raises_key_error() -> None:
    adapter = WindowsMotionAdapter()
    adapter.discover()

    with pytest.raises(KeyError):
        adapter.configure_rate("nonexistent.sensor.id", 50.0)


def test_configure_rate_on_absent_sensor_raises_key_error() -> None:
    """On this machine the three WinRT classes report no live instance,
    so `configure_rate` against the real (absent) accel id must raise
    rather than touch a `None` sensor.
    """
    adapter = WindowsMotionAdapter()
    records = adapter.discover()
    accel = next(r for r in records if r.kind == "accel")

    if accel.availability is Availability.ABSENT:
        with pytest.raises(KeyError):
            adapter.configure_rate(accel.id, 50.0)
    else:  # pragma: no cover - only exercised on hardware with a real sensor
        applied = adapter.configure_rate(accel.id, 50.0)
        assert applied > 0

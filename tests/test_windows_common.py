"""Tests for the shared Windows/WinRT adapter plumbing (Req 13.1, 13.2,
13.3, 3.5).

Uses a fake sensor-class stand-in rather than real `winsdk` types, since
CI/dev machines may have no sensor hardware and the plumbing under test
does not care about the real WinRT binding -- only about the
`get_default()` -> `None`-or-object contract and the `device_id` attribute
verified interactively against the real `winsdk` package (see
`adapters/windows/_common.py` module docstring).
"""

from __future__ import annotations

import time

import pytest

from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows._common import (
    bounded_read,
    device_instance_hash,
    query_winrt_sensor_class,
)
from sensortap.registry.ids import instance_hash
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading


def test_windows_platforms_is_win32_only() -> None:
    assert WINDOWS_PLATFORMS == frozenset({"win32"})


class _FakeAbsentSensorClass:
    """Stands in for a WinRT sensor class the OS does not expose."""

    @staticmethod
    def get_default() -> object | None:
        return None


class _FakeSensorInstance:
    def __init__(self, device_id: str) -> None:
        self.device_id = device_id


class _FakePresentSensorClass:
    """Stands in for a WinRT sensor class the OS does expose."""

    @staticmethod
    def get_default() -> object | None:
        return _FakeSensorInstance("\\\\?\\ACPI#FAKE0000#1#{fake-guid}")


def _query(sensor_class: object) -> tuple:
    return query_winrt_sensor_class(
        sensor_class,  # type: ignore[arg-type]
        kind="accel",
        source_id="winrt",
        unit="m/s2",
        dtype=Dtype.VECTOR3,
        channels=("x", "y", "z"),
        shape=(3,),
        value_range=(-156.9, 156.9),
        resolution=None,
        rate_hz=RateSpec(default=None, min=None, max=None),
        delivery=Delivery.PUSH,
        derived=False,
    )


def test_absent_class_yields_one_record_with_availability_absent() -> None:
    info, sensor = _query(_FakeAbsentSensorClass)

    assert sensor is None
    assert info.availability is Availability.ABSENT
    assert info.id == "accel.winrt.0"
    # Still a full, schema-valid record -- not merely skipped (Req 13.1).
    assert info.kind == "accel"
    assert info.dtype is Dtype.VECTOR3
    assert info.channels == ("x", "y", "z")


def test_present_class_yields_present_record_with_hashed_instance_id() -> None:
    info, sensor = _query(_FakePresentSensorClass)

    assert sensor is not None
    assert info.availability is Availability.PRESENT
    expected_hash = instance_hash("\\\\?\\ACPI#FAKE0000#1#{fake-guid}")
    assert info.id == f"accel.winrt.{expected_hash}"
    assert len(expected_hash) == 16


def test_device_instance_hash_matches_registry_instance_hash() -> None:
    device_id = "\\\\?\\HID#VID_1234&PID_5678#7&abc#{guid}"
    assert device_instance_hash(device_id) == instance_hash(device_id)


def test_bounded_read_returns_ok_reading_when_fast() -> None:
    def fast_read() -> Reading:
        now = time.monotonic()
        return Reading(
            id="accel.winrt.0",
            t_mono=now,
            t_wall=time.time(),
            values=(0.0, 0.0, 9.8),
            seq=0,
            status=Status.OK,
        )

    reading = bounded_read(fast_read, sensor_id="accel.winrt.0", timeout_ms=2000)
    assert reading.status is Status.OK
    assert reading.values == (0.0, 0.0, 9.8)


def test_bounded_read_times_out_and_reports_unavailable() -> None:
    def slow_read() -> Reading:
        time.sleep(0.5)
        return Reading(
            id="accel.winrt.0",
            t_mono=time.monotonic(),
            t_wall=time.time(),
            values=(1.0,),
            seq=0,
            status=Status.OK,
        )

    reading = bounded_read(slow_read, sensor_id="accel.winrt.0", timeout_ms=50)
    assert reading.status is Status.UNAVAILABLE
    assert reading.values == ()
    assert reading.id == "accel.winrt.0"


def test_bounded_read_degrades_gracefully_on_exception() -> None:
    def raising_read() -> Reading:
        raise RuntimeError("simulated WinRT failure")

    reading = bounded_read(raising_read, sensor_id="accel.winrt.0", timeout_ms=2000)
    assert reading.status is Status.UNAVAILABLE
    assert reading.values == ()

"""Tests for `winrt_orientation.py` (Req 13.1, 13.2, 13.4).

Runs `discover()` against whatever this machine actually reports (present
or absent for each of the three sensors is fine; the properties asserted
below hold either way, per Req 13.1/13.2), plus focused unit tests for the
quaternion-to-Euler conversion against known simple cases.
"""

from __future__ import annotations

import math

from sensortap.adapters.windows.winrt_orientation import (
    WindowsOrientationAdapter,
    quaternion_to_euler_zyx,
)
from sensortap.schema.enums import Availability, Dtype


def test_discover_returns_three_records_regardless_of_hardware() -> None:
    adapter = WindowsOrientationAdapter()
    records = adapter.discover()

    assert len(records) == 3

    by_kind = {record.kind: record for record in records}
    assert set(by_kind) == {"incline", "orientation", "hinge-angle"}

    incline = by_kind["incline"]
    orientation = by_kind["orientation"]
    hinge = by_kind["hinge-angle"]

    assert incline.dtype is Dtype.VECTOR3
    assert incline.shape == (3,)
    assert incline.unit == "deg"
    assert incline.derived is True

    assert orientation.dtype is Dtype.VECTOR3
    assert orientation.shape == (3,)
    assert orientation.unit == "deg"
    assert orientation.derived is True

    assert hinge.dtype is Dtype.SCALAR
    assert hinge.shape == (1,)
    assert hinge.unit == "deg"
    assert hinge.derived is True

    for record in records:
        assert record.availability in (Availability.PRESENT, Availability.ABSENT)
        assert record.schema_version
        assert record.source == ("winrt",)


def test_discover_ids_use_expected_kind_stems() -> None:
    adapter = WindowsOrientationAdapter()
    records = adapter.discover()
    ids = {record.id.split(".")[0] for record in records}
    assert ids == {"incline", "orientation", "hinge-angle"}


def test_quaternion_identity_maps_to_zero_euler() -> None:
    roll, pitch, yaw = quaternion_to_euler_zyx(0.0, 0.0, 0.0, 1.0)
    assert math.isclose(roll, 0.0, abs_tol=1e-9)
    assert math.isclose(pitch, 0.0, abs_tol=1e-9)
    assert math.isclose(yaw, 0.0, abs_tol=1e-9)


def test_quaternion_90_degree_yaw() -> None:
    # 90 degree rotation about Z: (x,y,z,w) = (0, 0, sin(45deg), cos(45deg))
    half = math.radians(90.0) / 2.0
    x, y, z, w = 0.0, 0.0, math.sin(half), math.cos(half)
    roll, pitch, yaw = quaternion_to_euler_zyx(x, y, z, w)
    assert math.isclose(roll, 0.0, abs_tol=1e-6)
    assert math.isclose(pitch, 0.0, abs_tol=1e-6)
    assert math.isclose(yaw, 90.0, abs_tol=1e-6)


def test_quaternion_90_degree_roll() -> None:
    # 90 degree rotation about X: (x,y,z,w) = (sin(45deg), 0, 0, cos(45deg))
    half = math.radians(90.0) / 2.0
    x, y, z, w = math.sin(half), 0.0, 0.0, math.cos(half)
    roll, pitch, yaw = quaternion_to_euler_zyx(x, y, z, w)
    assert math.isclose(roll, 90.0, abs_tol=1e-6)
    assert math.isclose(pitch, 0.0, abs_tol=1e-6)
    assert math.isclose(yaw, 0.0, abs_tol=1e-6)

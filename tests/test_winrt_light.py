"""Tests for the WinRT ambient light adapter (Req 13.1, 13.4)."""

from __future__ import annotations

from sensortap.adapters.windows.winrt_light import WindowsLightAdapter
from sensortap.schema.enums import Dtype


def test_discover_returns_exactly_one_record_regardless_of_hardware() -> None:
    adapter = WindowsLightAdapter()
    records = adapter.discover()

    assert len(records) == 1


def test_discover_record_has_correct_kind_shape_and_unit() -> None:
    adapter = WindowsLightAdapter()
    (info,) = adapter.discover()

    assert info.kind == "light"
    assert info.dtype is Dtype.SCALAR
    assert info.shape == (1,)
    assert info.unit == "lx"
    assert info.channels == ("lux",)
    assert info.derived is False


def test_read_on_absent_sensor_reports_unavailable() -> None:
    adapter = WindowsLightAdapter()
    (info,) = adapter.discover()

    reading = adapter.read(info.id)

    # On CI/dev machines with no ambient light sensor, LightSensor.get_default()
    # returns None, so read() must degrade to UNAVAILABLE rather than raise.
    from sensortap.schema.enums import Status

    if reading.status is Status.UNAVAILABLE:
        assert reading.values == ()
    else:
        assert reading.status is Status.OK
        assert len(reading.values) == 1

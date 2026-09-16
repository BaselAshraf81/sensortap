"""Tests for the Windows battery/power telemetry adapter (Req 13.9).

Runs regardless of whether the machine has a battery: discovery must always
return five full, schema-valid `SensorInfo` records, with voltage and
temperature always `Availability.ABSENT` since `Windows.Devices.Power
.Battery` never exposes either quantity (verified interactively, see
`win_battery.py` module docstring).
"""

from __future__ import annotations

from sensortap.adapters.windows.win_battery import WindowsBatteryAdapter
from sensortap.schema.enums import Availability, Dtype, Status


def test_discover_returns_exactly_five_records() -> None:
    adapter = WindowsBatteryAdapter()
    records = adapter.discover()

    assert len(records) == 5


def test_discover_records_have_correct_kind_dtype_and_unit() -> None:
    adapter = WindowsBatteryAdapter()
    pct, cap, rate, volt, temp = adapter.discover()

    assert pct.kind == "battery" and pct.unit == "%" and pct.derived is True
    assert cap.kind == "battery" and cap.unit == "mW.h" and cap.derived is False
    assert rate.kind == "power" and rate.unit == "mW" and rate.derived is False
    assert volt.kind == "voltage" and volt.unit == "V"
    assert temp.kind == "temp" and temp.unit == "degC"

    for info in (pct, cap, rate, volt, temp):
        assert info.dtype is Dtype.SCALAR
        assert info.shape == (1,)
        assert info.requires_consent is False
        assert info.schema_version


def test_voltage_and_temperature_are_always_absent() -> None:
    """Never fabricated: this API exposes neither quantity on any machine."""
    adapter = WindowsBatteryAdapter()
    _pct, _cap, _rate, volt, temp = adapter.discover()

    assert volt.availability is Availability.ABSENT
    assert temp.availability is Availability.ABSENT


def test_absent_records_are_full_valid_sensor_info_not_omitted() -> None:
    adapter = WindowsBatteryAdapter()
    _pct, _cap, _rate, volt, temp = adapter.discover()

    for info in (volt, temp):
        assert info.id
        assert info.channels
        assert info.kind
        assert info.dtype is Dtype.SCALAR


def test_read_on_voltage_and_temperature_reports_unavailable() -> None:
    adapter = WindowsBatteryAdapter()
    _pct, _cap, _rate, volt, temp = adapter.discover()

    for info in (volt, temp):
        reading = adapter.read(info.id)
        assert reading.status is Status.UNAVAILABLE
        assert reading.values == ()


def test_read_on_percentage_capacity_rate_degrades_gracefully_when_absent() -> None:
    """On a desktop machine with no battery these degrade to UNAVAILABLE;
    on a laptop they succeed. Either outcome must be schema-consistent."""
    adapter = WindowsBatteryAdapter()
    pct, cap, rate, _volt, _temp = adapter.discover()

    for info in (pct, cap, rate):
        reading = adapter.read(info.id)
        if reading.status is Status.UNAVAILABLE:
            assert reading.values == ()
        else:
            assert reading.status is Status.OK
            assert len(reading.values) == 1

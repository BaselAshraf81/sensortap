"""Tests for `win_radio.py` (Req 13.9, 13.11).

These tests exercise `discover()` only and assert schema-valid records
regardless of actual hardware/connectivity state on the machine running the
suite -- no wireless interface, no Bluetooth radio, or both present with an
active Wi-Fi connection must all yield two schema-valid `SensorInfo`
records. This deliberately does not mock the winsdk calls: it runs the real
`discover()` against whatever winsdk reports on this machine, and asserts
only the invariants that must hold in every case.
"""

from __future__ import annotations

import pytest

from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows.win_radio import WindowsRadioAdapter
from sensortap.schema.enums import Availability, Delivery, Dtype
from sensortap.schema.validate import validate_sensor_info


def test_adapter_meta() -> None:
    meta = WindowsRadioAdapter.meta
    assert meta.adapter_id == "win_radio"
    assert meta.supported_platforms == WINDOWS_PLATFORMS
    assert meta.read_only_declared is True


def test_adapter_implements_required_protocol_methods() -> None:
    # `typing.Protocol`'s `runtime_checkable` isinstance check inspects
    # every attribute named on the Protocol, including the six *optional*
    # methods (Req 10.2) -- it cannot distinguish required from optional at
    # runtime. `WindowsRadioAdapter` deliberately implements only the two
    # required methods, so an `isinstance(..., Adapter)` check would fail
    # for a structural reason unrelated to conformance. Check directly for
    # the two required members instead.
    adapter = WindowsRadioAdapter()
    assert hasattr(adapter, "meta")
    assert callable(adapter.discover)
    assert callable(adapter.read)


def test_discover_returns_two_schema_valid_records() -> None:
    adapter = WindowsRadioAdapter()
    records = adapter.discover()

    assert len(records) == 2

    for record in records:
        violations = validate_sensor_info(record, adapter_id="win_radio")
        assert violations == [], f"{record.id}: {violations}"


def test_wifi_signal_record_shape() -> None:
    adapter = WindowsRadioAdapter()
    records = {r.id: r for r in adapter.discover()}
    wifi = records["radio-signal.win-radio.0"]

    assert wifi.kind == "radio-signal"
    assert wifi.dtype is Dtype.SCALAR
    assert wifi.shape == (1,)
    assert wifi.unit == "%"
    assert wifi.range == (0.0, 100.0)
    assert wifi.requires_consent is False
    assert wifi.availability in (Availability.PRESENT, Availability.ABSENT)


def test_bluetooth_presence_record_shape() -> None:
    adapter = WindowsRadioAdapter()
    records = {r.id: r for r in adapter.discover()}
    bt = records["radio-signal.win-radio.1"]

    assert bt.kind == "radio-signal"
    assert bt.dtype is Dtype.SCALAR
    assert bt.shape == (1,)
    assert bt.unit is None
    assert bt.range == (0.0, 1.0)
    assert bt.requires_consent is False
    assert bt.availability in (Availability.PRESENT, Availability.ABSENT)


def test_read_wifi_signal_returns_valid_reading_regardless_of_hardware() -> None:
    adapter = WindowsRadioAdapter()
    reading = adapter.read("radio-signal.win-radio.0")

    assert reading.id == "radio-signal.win-radio.0"
    if reading.values:
        assert len(reading.values) == 1
        assert 0.0 <= reading.values[0] <= 100.0


def test_read_bluetooth_presence_returns_valid_reading() -> None:
    adapter = WindowsRadioAdapter()
    reading = adapter.read("radio-signal.win-radio.1")

    assert reading.id == "radio-signal.win-radio.1"
    assert len(reading.values) == 1
    assert reading.values[0] in (0.0, 1.0)


def test_read_unknown_sensor_raises_key_error() -> None:
    adapter = WindowsRadioAdapter()
    with pytest.raises(KeyError):
        adapter.read("radio-signal.win-radio.99")

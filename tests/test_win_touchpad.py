"""Tests for `win_touchpad.py` (Req 7.1, 9.4, 13.9, 13.10).

Runs the real `discover()` against whatever winsdk reports on this
machine; asserts only the invariants that must hold regardless of actual
touchpad presence.
"""

from __future__ import annotations

import pytest

from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows.win_touchpad import WindowsTouchpadAdapter
from sensortap.schema.enums import Availability, Dtype
from sensortap.schema.validate import validate_sensor_info


def test_adapter_meta() -> None:
    meta = WindowsTouchpadAdapter.meta
    assert meta.adapter_id == "win_touchpad"
    assert meta.supported_platforms == WINDOWS_PLATFORMS
    assert meta.read_only_declared is True


def test_adapter_implements_required_protocol_methods() -> None:
    adapter = WindowsTouchpadAdapter()
    assert hasattr(adapter, "meta")
    assert callable(adapter.discover)
    assert callable(adapter.read)


def test_discover_returns_three_schema_valid_records() -> None:
    adapter = WindowsTouchpadAdapter()
    records = adapter.discover()

    assert len(records) == 3
    for record in records:
        violations = validate_sensor_info(record, adapter_id="win_touchpad")
        assert violations == [], f"{record.id}: {violations}"


def test_capacitive_image_sensor_is_always_absent_and_requires_consent() -> None:
    adapter = WindowsTouchpadAdapter()
    records = {r.id: r for r in adapter.discover()}
    capimg = records["touchpad.win-capimg.0"]

    assert capimg.availability is Availability.ABSENT
    assert capimg.requires_consent is True
    assert capimg.dtype is Dtype.MATRIX
    assert capimg.kind == "touchpad"


def test_ptp_presence_and_contacts_availability_reflect_the_machine() -> None:
    adapter = WindowsTouchpadAdapter()
    records = {r.id: r for r in adapter.discover()}
    ptp = records["touchpad.win-ptp.0"]
    contacts = records["touchpad.win-contacts.0"]

    assert ptp.kind == "touchpad"
    assert ptp.requires_consent is False
    assert ptp.availability in (Availability.PRESENT, Availability.ABSENT)

    assert contacts.kind == "touchpad"
    assert contacts.requires_consent is False
    assert contacts.availability in (Availability.PRESENT, Availability.ABSENT)

    # The two capability sensors share one discovery-time signal, so their
    # availability must agree.
    assert ptp.availability == contacts.availability


def test_read_ptp_presence_returns_valid_reading() -> None:
    adapter = WindowsTouchpadAdapter()
    adapter.discover()
    reading = adapter.read("touchpad.win-ptp.0")

    assert reading.id == "touchpad.win-ptp.0"
    assert len(reading.values) == 1
    assert reading.values[0] in (0.0, 1.0)


def test_read_contacts_returns_valid_reading_regardless_of_hardware() -> None:
    adapter = WindowsTouchpadAdapter()
    adapter.discover()
    reading = adapter.read("touchpad.win-contacts.0")

    assert reading.id == "touchpad.win-contacts.0"
    if reading.values:
        assert len(reading.values) == 1
        assert reading.values[0] >= 0.0


def test_read_capacitive_image_is_always_unavailable() -> None:
    adapter = WindowsTouchpadAdapter()
    adapter.discover()
    reading = adapter.read("touchpad.win-capimg.0")

    assert reading.id == "touchpad.win-capimg.0"
    assert reading.values == ()


def test_read_unknown_sensor_raises_key_error() -> None:
    adapter = WindowsTouchpadAdapter()
    with pytest.raises(KeyError):
        adapter.read("touchpad.win-unknown.0")

"""Tests for `win_touchpad.py` (Req 7.1, 9.4, 13.9, 13.10).

Runs the real `discover()` against whatever winsdk reports on this
machine; asserts only the invariants that must hold regardless of actual
touchpad presence.
"""

from __future__ import annotations

import pytest

from sensortap.adapters.windows import WINDOWS_PLATFORMS, win_touchpad
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

    # The two sensors carry *different* facts and must not be assumed to
    # agree: a touchpad found through the HID digitizer path has no
    # reachable contact count, so presence can be PRESENT while contacts
    # is ABSENT. Assuming they agreed is what hid the Dell G3 false
    # negative. The only direction that holds is the implication below --
    # a contact count cannot exist without a touchpad to count on.
    if contacts.availability is Availability.PRESENT:
        assert ptp.availability is Availability.PRESENT


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


# --- detection paths -------------------------------------------------------
#
# These run against injected fakes rather than the host's real devices, so
# they assert the same behaviour on a machine with a touchpad, a machine
# without one, and CI with no input hardware at all.


class _FakeTouchPointerDevice:
    """Stand-in for an integrated touch `PointerDevice` (Path 2)."""

    def __init__(self, max_contacts: int = 5) -> None:
        self.max_contacts = max_contacts


class _FakeHidCollection:
    """Stand-in for a `DeviceInformation` record from HID enumeration."""

    def __init__(self, is_enabled: bool = True) -> None:
        self.is_enabled = is_enabled

    @property
    def name(self) -> str:
        raise AssertionError(
            "DeviceInformation.name must never be read: Windows reports the "
            "machine hostname there for system-claimed HID collections"
        )


def test_hid_usage_pair_is_the_standard_touchpad_digitizer() -> None:
    # Usage page 0x0D / usage 0x05 is "Digitizer / Touch Pad" in the
    # USB-IF HID usage tables. Pinned because drifting to Touch Screen
    # (0x04) or Pen (0x02) would silently reclassify other digitizers as
    # touchpads.
    assert win_touchpad._HID_USAGE_PAGE_DIGITIZER == 0x0D
    assert win_touchpad._HID_USAGE_TOUCH_PAD == 0x05


def test_touchpad_found_only_through_hid_reports_presence_without_contacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Dell G3 3779 case: PointerDevice surfaces only the touchpad's
    # Generic Desktop/Mouse collection, so Path 2 finds nothing, while the
    # Digitizer/Touch Pad collection is enumerable through Path 1.
    monkeypatch.setattr(
        win_touchpad, "_find_integrated_touch_pointer_device", lambda: None
    )
    monkeypatch.setattr(
        win_touchpad,
        "_enumerate_touchpad_hid_collections",
        lambda: [_FakeHidCollection()],
    )

    present, max_contacts = win_touchpad._detect_touchpad()

    assert present is True
    assert max_contacts is None

    records = {r.id: r for r in WindowsTouchpadAdapter().discover()}
    assert records["touchpad.win-ptp.0"].availability is Availability.PRESENT
    # Honest: a touchpad exists, its contact count does not come for free.
    assert records["touchpad.win-contacts.0"].availability is Availability.ABSENT


def test_touchpad_found_through_pointer_device_reports_a_contact_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        win_touchpad,
        "_find_integrated_touch_pointer_device",
        lambda: _FakeTouchPointerDevice(max_contacts=5),
    )

    present, max_contacts = win_touchpad._detect_touchpad()

    assert present is True
    assert max_contacts == 5

    adapter = WindowsTouchpadAdapter()
    records = {r.id: r for r in adapter.discover()}
    assert records["touchpad.win-ptp.0"].availability is Availability.PRESENT
    assert records["touchpad.win-contacts.0"].availability is Availability.PRESENT
    assert adapter.read("touchpad.win-contacts.0").values == (5.0,)


def test_no_touchpad_on_either_path_reports_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        win_touchpad, "_find_integrated_touch_pointer_device", lambda: None
    )
    monkeypatch.setattr(win_touchpad, "_enumerate_touchpad_hid_collections", lambda: [])

    assert win_touchpad._detect_touchpad() == (False, None)

    adapter = WindowsTouchpadAdapter()
    records = {r.id: r for r in adapter.discover()}
    assert records["touchpad.win-ptp.0"].availability is Availability.ABSENT
    assert records["touchpad.win-contacts.0"].availability is Availability.ABSENT
    assert adapter.read("touchpad.win-ptp.0").values == (0.0,)


def test_a_disabled_hid_collection_is_not_a_present_touchpad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        win_touchpad, "_find_integrated_touch_pointer_device", lambda: None
    )
    monkeypatch.setattr(
        win_touchpad,
        "_enumerate_touchpad_hid_collections",
        lambda: [_FakeHidCollection(is_enabled=False)],
    )

    assert win_touchpad._detect_touchpad() == (False, None)


def test_hid_detection_never_reads_a_device_name() -> None:
    # `_FakeHidCollection.name` raises. Windows puts the hostname there for
    # system-claimed HID collections, so reading it would leak a machine
    # identifier into output meant to be pasted into public bug reports.
    assert win_touchpad._any_enabled([_FakeHidCollection()]) is True


def test_one_failing_detection_path_does_not_break_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode():
        raise OSError("WinRT surface unavailable on this build")

    monkeypatch.setattr(
        win_touchpad, "_find_integrated_touch_pointer_device", _explode
    )
    monkeypatch.setattr(
        win_touchpad,
        "_enumerate_touchpad_hid_collections",
        lambda: [_FakeHidCollection()],
    )

    assert win_touchpad._detect_touchpad() == (True, None)


def test_both_detection_paths_failing_degrades_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*_args):
        raise OSError("WinRT surface unavailable on this build")

    monkeypatch.setattr(
        win_touchpad, "_find_integrated_touch_pointer_device", _explode
    )
    monkeypatch.setattr(win_touchpad, "_enumerate_touchpad_hid_collections", _explode)

    assert win_touchpad._detect_touchpad() == (False, None)

    # Discovery still returns three schema-valid records: one adapter's
    # broken backend never removes its sensors from the schema surface.
    records = WindowsTouchpadAdapter().discover()
    assert len(records) == 3
    for record in records:
        assert validate_sensor_info(record, adapter_id="win_touchpad") == []

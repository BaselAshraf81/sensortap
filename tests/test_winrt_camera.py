"""Tests for `WindowsCameraAdapter` (Req 7.1, 7.2, 7.8, 7.9, 9.4, 9.7, 13.9,
15.6).

Uses the real `winsdk` package. This dev/CI machine has at least one
physical webcam attached, so `discover()` is exercised against a real
`DeviceInformation` record; `read()` against a nonexistent id is checked
without touching any real device.
"""

from __future__ import annotations

import inspect

import pytest

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION
from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows.winrt_camera import WindowsCameraAdapter
from sensortap.schema.enums import Availability, Dtype
from sensortap.schema.validate import validate_sensor_info


def test_meta() -> None:
    meta = WindowsCameraAdapter.meta
    assert meta.adapter_id == "winrt_camera"
    assert meta.interface_version == ADAPTER_INTERFACE_VERSION
    assert meta.supported_platforms == WINDOWS_PLATFORMS
    assert meta.read_only_declared is True


def test_discover_does_not_reference_media_capture() -> None:
    """Structural guard for Req 9.4: `discover()`'s own source must not
    import or construct `MediaCapture` -- only `_find_all_video_capture_devices`
    (which uses `DeviceInformation` alone) may be called.
    """

    source = inspect.getsource(WindowsCameraAdapter.discover)
    body = source.split('"""', 2)[-1]  # drop the docstring, check only code
    assert "MediaCapture" not in body
    assert "_find_all_video_capture_devices" in body


def test_discover_returns_reachable_frame_geometry_records() -> None:
    """The camera reports frame geometry as a `matrix` of shape (1, 2).

    Regression: this previously declared `dtype=buffer` with
    `shape=(1080, 1920)` while implementing only `read()` and no
    `open_stream()`, which made the sensor unreachable by any caller --
    the registry routes `buffer` exclusively through `stream()`, so
    `read()` raised `BlockPathRequiredError` and `stream()` raised
    `UnsupportedOperationError`. The shape also promised 2,073,600 values
    against a `read()` that returns 2.
    """

    adapter = WindowsCameraAdapter()
    records = adapter.discover()

    for record in records:
        assert record.kind == "camera"
        assert record.dtype is Dtype.MATRIX
        assert record.requires_consent is True
        assert record.shape == (1, 2)
        assert record.channels == ("height", "width")
        assert record.availability in (
            Availability.PRESENT,
            Availability.PERMISSION_DENIED,
        )
        assert record.id.startswith("camera.winrt.")


def test_camera_is_reachable_through_read() -> None:
    """`read()` on a discovered camera must be reachable: it may degrade
    to `Status.UNAVAILABLE` when the device refuses a frame (documented as
    happening on real hardware), but it must not raise a routing error
    that leaves the sensor unreadable by every path."""

    from sensortap.schema.enums import Status

    adapter = WindowsCameraAdapter()
    records = adapter.discover()

    if not records:
        pytest.skip("no camera present on this machine")

    reading = adapter.read(records[0].id)
    assert reading.id == records[0].id
    assert reading.status in (Status.OK, Status.UNAVAILABLE)
    if reading.status is Status.OK:
        # Declared shape (1, 2) has product 2; the implementation must agree.
        assert len(reading.values) == 2


def test_discover_completes_quickly() -> None:
    import time

    adapter = WindowsCameraAdapter()
    start = time.monotonic()
    adapter.discover()
    elapsed = time.monotonic() - start
    # Metadata enumeration only; should be well under the 2000ms WinRT
    # read bound used for actual device access.
    assert elapsed < 2.0


def test_read_unknown_sensor_id_raises_key_error() -> None:
    adapter = WindowsCameraAdapter()
    adapter.discover()

    with pytest.raises(KeyError):
        adapter.read("nonexistent.sensor.id")


def test_discover_records_pass_schema_validation() -> None:
    """Regression: the placeholder shape (1080, 1920) was declared
    against an empty `channels` tuple, which the validator's channel-count
    rule (shape's last dimension must equal len(channels)) rejects. Every
    camera sensor was silently dropped from the registry's final output
    as a result -- discover() itself never raised, and no reason was ever
    surfaced anywhere a caller could see it (Req 2.10 drops invalid
    records without raising). Confirmed live on real hardware, not just
    in this synthetic check."""

    adapter = WindowsCameraAdapter()
    records = adapter.discover()

    for record in records:
        violations = validate_sensor_info(record, adapter_id="winrt_camera")
        assert violations == [], f"{record.id}: {violations}"

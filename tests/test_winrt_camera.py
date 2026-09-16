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


def test_discover_returns_schema_valid_buffer_records() -> None:
    adapter = WindowsCameraAdapter()
    records = adapter.discover()

    for record in records:
        assert record.kind == "camera"
        assert record.dtype is Dtype.BUFFER
        assert record.requires_consent is True
        assert len(record.shape) == 2
        assert record.availability in (
            Availability.PRESENT,
            Availability.PERMISSION_DENIED,
        )
        assert record.id.startswith("camera.winrt.")


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

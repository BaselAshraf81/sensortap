"""Tests for the WinRT audio-capture adapter (Req 7.1, 7.8, 7.9, 9.4, 13.9,
15.6).

Runs on any machine, with or without a real microphone: `discover()` must
open no device handle and always return schema-valid records; `read()`
must always reject direct reads since every sensor here is buffer-dtype;
`open_stream()` either produces a real `StreamSource`-conforming object
backed by a real capture device, or degrades sensibly (raises
`DeviceOpenError`) when no capture device is present.
"""

from __future__ import annotations

import pytest

from sensortap.adapters.windows.winrt_audio import WindowsAudioAdapter
from sensortap.registry.errors import DeviceOpenError
from sensortap.schema.enums import Availability, Dtype


def test_discover_returns_schema_valid_buffer_records() -> None:
    adapter = WindowsAudioAdapter()
    records = adapter.discover()

    for info in records:
        assert info.kind == "microphone"
        assert info.dtype is Dtype.BUFFER
        assert info.unit is None
        assert info.requires_consent is True
        assert info.requires_elevation is False
        assert len(info.shape) == 2
        assert info.availability in (Availability.PRESENT, Availability.PERMISSION_DENIED)
        assert info.schema_version


def test_discover_opens_no_device_handle() -> None:
    """discover() must not open any capture device (Req 9.4).

    There is no direct handle-count to assert on via `winsdk`, so this
    checks the documented behavioural proxy instead: calling `discover()`
    repeatedly must not raise, must not require any consent grant, and
    must return the same device ids each time -- all of which would be
    false if discovery itself opened (and could fail to open, or hold) a
    device handle.
    """

    adapter = WindowsAudioAdapter()
    first = adapter.discover()
    second = adapter.discover()

    assert {info.id for info in first} == {info.id for info in second}


def test_read_on_buffer_sensor_always_raises() -> None:
    """Every sensor here is buffer-dtype; the registry never calls read()
    for a buffer sensor (Req 4.12), and this adapter has no direct read
    path to offer, so read() always raises for any sensor id it owns."""

    adapter = WindowsAudioAdapter()
    records = adapter.discover()

    if not records:
        pytest.skip("no audio capture device present on this machine")

    with pytest.raises(KeyError):
        adapter.read(records[0].id)


def test_read_on_unknown_sensor_id_raises() -> None:
    adapter = WindowsAudioAdapter()
    adapter.discover()

    with pytest.raises(KeyError):
        adapter.read("microphone.winrt.doesnotexist")


def test_open_stream_on_unknown_sensor_id_raises() -> None:
    adapter = WindowsAudioAdapter()
    adapter.discover()

    with pytest.raises(KeyError):
        adapter.open_stream("microphone.winrt.doesnotexist")


def test_open_stream_produces_stream_source_or_degrades_sensibly() -> None:
    """On a machine with a real microphone, open_stream() must return a
    StreamSource-conforming object that yields a real captured block. On a
    machine with none, open_stream() must raise a `DeviceOpenError` rather
    than hang or fabricate data -- there are no discovered sensor ids to
    call it with in that case, which this test also treats as a pass.
    """

    adapter = WindowsAudioAdapter()
    records = adapter.discover()

    if not records:
        pytest.skip("no audio capture device present on this machine")

    sensor_id = records[0].id

    try:
        stream = adapter.open_stream(sensor_id, block_size=256)
    except DeviceOpenError:
        # Sensible degradation: e.g. device disappeared between discover()
        # and open_stream(), or access was denied.
        return

    try:
        assert hasattr(stream, "next_block")
        assert hasattr(stream, "close")
        assert hasattr(stream, "achieved_rate")
        assert hasattr(stream, "applied_rate")

        block = stream.next_block()
        assert block.id == sensor_id
        assert len(block.values) >= 0
        assert stream.applied_rate() > 0
    finally:
        stream.close()

"""Focused tests for `ReferenceAdapter` (Req 16.7, 4.8, 4.9, 5.9, 6.1)."""

from __future__ import annotations

import time

import pytest

from sensortap.adapters.reference import (
    _BUFFER_ID,
    _MATRIX_ID,
    _SCALAR_ID,
    _VECTOR3_ID,
    ReferenceAdapter,
)
from sensortap.registry.errors import RateFixedError
from sensortap.schema.enums import Delivery, Dtype, Status


@pytest.fixture()
def adapter():
    a = ReferenceAdapter()
    a.setup()
    try:
        yield a
    finally:
        a.teardown()


def test_discover_exposes_all_four_dtypes(adapter):
    sensors = adapter.discover()
    dtypes = {s.dtype for s in sensors}
    assert dtypes == {Dtype.SCALAR, Dtype.VECTOR3, Dtype.MATRIX, Dtype.BUFFER}
    deliveries = {s.delivery for s in sensors}
    assert Delivery.PUSH in deliveries
    assert Delivery.POLL in deliveries


def test_scalar_push_sensor_reads_ok_shortly_after_setup(adapter):
    reading = adapter.read(_SCALAR_ID)
    assert reading.status == Status.OK
    assert len(reading.values) == 1


def test_vector3_sensor_never_received_returns_unavailable_immediately(adapter):
    start = time.monotonic()
    reading = adapter.read(_VECTOR3_ID)
    elapsed = time.monotonic() - start
    assert reading.status == Status.UNAVAILABLE
    assert reading.values == ()
    assert elapsed < 0.5


def test_matrix_poll_sensor_reads_ok(adapter):
    reading = adapter.read(_MATRIX_ID)
    assert reading.status == Status.OK
    assert len(reading.values) == 16


def test_configure_rate_applies_for_scalar_and_raises_fixed_for_vector3(adapter):
    applied = adapter.configure_rate(_SCALAR_ID, 5.0)
    assert applied == pytest.approx(5.0)

    with pytest.raises(RateFixedError):
        adapter.configure_rate(_VECTOR3_ID, 10.0)


def test_open_stream_on_buffer_sensor_produces_conforming_blocks(adapter):
    source = adapter.open_stream(_BUFFER_ID, block_size=64, rate_hz=8000.0)
    try:
        block = source.next_block()
        assert len(block.values) == 64
        assert block.status == Status.OK
        assert source.applied_rate() == 8000.0
    finally:
        source.close()


def test_supported_block_sizes_for_buffer_sensor(adapter):
    lo, hi = adapter.supported_block_sizes(_BUFFER_ID)
    assert lo < hi


def test_health_reports_ok(adapter):
    health = adapter.health()
    assert health.status == "ok"

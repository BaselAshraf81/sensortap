"""Focused tests for registry/streaming.py's `Stream` and ring buffer
(Req 5.1-5.5, 5.7-5.10, 5.12, 6.9).

Uses a fake `StreamSource` conforming to the adapter-side streaming
protocol, so these tests exercise `Stream` in isolation from real
adapters.
"""

from __future__ import annotations

import gc
import threading
import time

import pytest

from sensortap.registry.errors import ClosedStreamError
from sensortap.registry.streaming import Stream, _release_resources
from sensortap.schema.enums import Status
from sensortap.schema.reading import Reading


class FakeStreamSource:
    """A fake adapter-side `StreamSource`.

    `next_block()` returns readings from a caller-supplied generator,
    optionally gated by an `Event` so a test can control production
    pace relative to consumption pace. `seq` on the returned readings is
    irrelevant to `Stream` -- `Stream`'s producer thread reassigns `seq`
    at production time regardless.
    """

    def __init__(self, *, block_gate: threading.Event | None = None):
        self._i = 0
        self._closed = False
        self._block_gate = block_gate
        self.close_calls = 0

    def next_block(self) -> Reading:
        if self._closed:
            raise RuntimeError("source closed")
        if self._block_gate is not None:
            self._block_gate.wait()
        i = self._i
        self._i += 1
        return Reading(
            id="temp.fake.0", t_mono=float(i), t_wall=float(i), values=(1.0,), seq=i,
            status=Status.OK,
        )

    def close(self) -> None:
        self._closed = True
        self.close_calls += 1

    def achieved_rate(self):
        return None

    def applied_rate(self) -> float:
        return 10.0


def test_buffer_blocks_validation_bounds():
    with pytest.raises(ValueError):
        Stream(FakeStreamSource(), sensor_id="temp.fake.0", buffer_blocks=1)
    with pytest.raises(ValueError):
        Stream(FakeStreamSource(), sensor_id="temp.fake.0", buffer_blocks=1025)

    s = Stream(FakeStreamSource(), sensor_id="temp.fake.0", buffer_blocks=2)
    try:
        assert s.buffer_blocks == 2
    finally:
        s.close()


def test_discard_on_backpressure_sets_discarded_and_degraded_status():
    # A tiny buffer and an unthrottled fake producer: the producer thread
    # will quickly outrun a consumer that does not read for a moment.
    source = FakeStreamSource()
    s = Stream(source, sensor_id="temp.fake.0", buffer_blocks=2)
    try:
        # Give the producer thread a chance to overrun the 2-slot buffer.
        deadline = time.monotonic() + 2.0
        while s.discarded_blocks == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert s.discarded_blocks > 0

        reading = next(s)
        assert reading.status == Status.DEGRADED
        # One-shot: the following delivered block should not also be
        # marked degraded (unless another discard happened in between,
        # which is possible but unlikely given we just drained one).
    finally:
        s.close()


def test_seq_gaps_survive_a_discard_rather_than_being_renumbered():
    source = FakeStreamSource()
    s = Stream(source, sensor_id="temp.fake.0", buffer_blocks=2)
    try:
        deadline = time.monotonic() + 2.0
        while s.discarded_blocks == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert s.discarded_blocks > 0

        first = next(s)
        second = next(s)
        # A discard occurred between production and this delivery, so the
        # delivered seq values must show a gap rather than being 0, 1.
        assert second.seq > first.seq + 1 or first.seq > 0
    finally:
        s.close()


def test_achieved_rate_hz_none_below_two_samples_and_value_at_two_or_more():
    source = FakeStreamSource()
    s = Stream(source, sensor_id="temp.fake.0", buffer_blocks=8)
    try:
        assert s.achieved_rate_hz() is None
        next(s)
        assert s.achieved_rate_hz() is None
        next(s)
        rate = s.achieved_rate_hz()
        assert rate is not None
        assert rate > 0
    finally:
        s.close()


def test_close_rejects_further_block_requests_with_closed_stream_error():
    source = FakeStreamSource()
    s = Stream(source, sensor_id="temp.fake.0", buffer_blocks=4)
    next(s)
    s.close()

    with pytest.raises(ClosedStreamError):
        next(s)

    assert source.close_calls == 1


def test_close_is_idempotent():
    source = FakeStreamSource()
    s = Stream(source, sensor_id="temp.fake.0", buffer_blocks=4)
    s.close()
    s.close()
    assert source.close_calls == 1


def test_iterator_and_context_manager_usage_pattern():
    source = FakeStreamSource()
    collected = []
    with Stream(source, sensor_id="temp.fake.0", buffer_blocks=4) as s:
        for reading in s:
            collected.append(reading)
            if len(collected) >= 3:
                break

    assert len(collected) == 3
    seqs = [r.seq for r in collected]
    # The background producer may race ahead of the consumer (that is the
    # whole point of the ring buffer), so seq need not start at 0, but it
    # must be strictly increasing.
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 3
    # Exiting the context manager closed the stream.
    with pytest.raises(ClosedStreamError):
        next(s)


def test_discarded_blocks_readable_after_close():
    source = FakeStreamSource()
    s = Stream(source, sensor_id="temp.fake.0", buffer_blocks=2)
    deadline = time.monotonic() + 2.0
    while s.discarded_blocks == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    discarded_before_close = s.discarded_blocks
    assert discarded_before_close > 0
    s.close()
    # discarded_blocks stays readable after close and does not reset;
    # the producer may have pushed (and possibly discarded) one more
    # block in the brief window before it observed the stop signal.
    assert s.discarded_blocks >= discarded_before_close


def test_applied_rate_hz_property():
    source = FakeStreamSource()
    s = Stream(source, sensor_id="temp.fake.0", buffer_blocks=4, applied_rate_hz=48000.0)
    try:
        assert s.applied_rate_hz == 48000.0
    finally:
        s.close()


def test_gc_finalization_releases_resources_without_explicit_close():
    source = FakeStreamSource()
    s = Stream(source, sensor_id="temp.fake.0", buffer_blocks=4)
    next(s)
    del s
    gc.collect()

    deadline = time.monotonic() + 2.0
    while source.close_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert source.close_calls == 1

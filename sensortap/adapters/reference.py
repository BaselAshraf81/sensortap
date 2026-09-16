"""`ReferenceAdapter`: the synthetic, no-hardware worked example (Req 16.7).

This adapter exists purely so a contributor writing a new backend adapter
has a small, complete, runnable example of the full `Adapter` Protocol
(`adapters/protocol.py`) to read alongside the design doc, without needing
any physical sensor or platform-specific SDK. It never touches real
hardware and is safe to load and exercise on any OS.

It exposes exactly one sensor per `Dtype`, chosen to also demonstrate the
two things the interface cares most about besides shape:

- ``temp.reference.0`` (`scalar`, `push`, kind ``"temp"``): the "normal"
  push sensor. Demonstrates the *stale-value* read path (Req 4.8) -- once
  more than two sampling intervals have passed since a value was last
  produced, `read()` reports `Status.STALE` instead of quietly returning
  an old value as `Status.OK`.
- ``accel.reference.0`` (`vector3`, `push`, kind ``"accel"``): a second
  push sensor that demonstrates the *never-received-value* path (Req 4.9)
  -- it never produces a value at all, so `read()` must return
  immediately with `Status.UNAVAILABLE` and empty `values` rather than
  blocking forever waiting for a first sample.
- ``touchpad.reference.0`` (`matrix`, `poll`, kind ``"touchpad"``):
  demonstrates the plain synchronous poll path -- `read()` just computes
  and returns a value on demand, no "last received" bookkeeping needed.
- ``microphone.reference.0`` (`buffer`, `push`, kind ``"microphone"``):
  demonstrates the Block/Stream path (Req 5.9, 5.6) -- `open_stream()`
  and `supported_block_sizes()` -- since `buffer`-dtype sensors can only
  be read that way, never through `read()` (Req 4.12).

All four sensor values are deterministic functions of elapsed time (a
sine wave or a simple counter), specifically so runs and tests are
reproducible rather than exercising real randomness.

The adapter also implements every optional method (`open_stream`,
`supported_block_sizes`, `configure_rate`, `setup`, `teardown`, `health`)
so a contributor can see the full interface exercised in one file, and it
declares ``read_only_declared = True`` -- required for the registry to
load *any* adapter at all (Req 15.8).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import ClassVar

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION, AdapterMeta
from sensortap.registry.errors import RateFixedError
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec, nearest_supported_rate
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo
from sensortap.schema.version import SCHEMA_VERSION

#: Sensor_Ids exposed by this adapter, one per dtype.
_SCALAR_ID = "temp.reference.0"
_VECTOR3_ID = "accel.reference.0"
_MATRIX_ID = "touchpad.reference.0"
_BUFFER_ID = "microphone.reference.0"

#: Matrix sensor shape.
_MATRIX_SHAPE = (4, 4)

#: Buffer sensor shape: (samples-per-block, channels).
_BUFFER_SHAPE = (256, 1)

#: Sampling rate for the scalar push sensor. Used both to compute the
#: "last received" cadence and the staleness threshold (2x the sampling
#: interval, per Req 4.8's rule).
_SCALAR_RATE_HZ = 10.0

#: Sampling rate for the vector3 push sensor (never actually produces a
#: value, but still declares a plausible rate).
_VECTOR3_RATE_HZ = 50.0

#: Nominal rate for the buffer/streaming sensor.
_BUFFER_RATE_HZ = 8000.0


@dataclass(frozen=True, slots=True)
class _ReferenceHealth:
    """Minimal `AdapterHealth`-conforming value."""

    status: str
    detail: str | None = None


class _BufferStreamSource:
    """`StreamSource`-conforming producer for `microphone.reference.0`.

    Produces deterministic synthetic blocks (a sine wave) at a fixed
    cadence derived from `rate_hz`, one block per `next_block()` call.
    """

    def __init__(self, sensor_id: str, *, block_size: int, rate_hz: float) -> None:
        self._sensor_id = sensor_id
        self._block_size = block_size
        self._rate_hz = rate_hz
        self._seq = 0
        self._closed = False
        self._lock = threading.Lock()
        self._sample_index = 0
        # Interval between blocks, so a slow consumer sees realistic
        # pacing rather than the producer spinning as fast as possible.
        self._block_interval_s = block_size / rate_hz if rate_hz > 0 else 0.0

    def next_block(self) -> Reading:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"stream for sensor {self._sensor_id!r} is closed")
        if self._block_interval_s > 0:
            time.sleep(self._block_interval_s)
        with self._lock:
            if self._closed:
                raise RuntimeError(f"stream for sensor {self._sensor_id!r} is closed")
            values = tuple(
                math.sin(2 * math.pi * 440.0 * (self._sample_index + i) / self._rate_hz)
                for i in range(self._block_size)
            )
            self._sample_index += self._block_size
            now = time.monotonic()
            seq = self._seq
            self._seq += 1
            return Reading(
                id=self._sensor_id,
                t_mono=now,
                t_wall=time.time(),
                values=values,
                seq=seq,
                status=Status.OK,
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def achieved_rate(self) -> float | None:
        return self._rate_hz

    def applied_rate(self) -> float:
        return self._rate_hz


class ReferenceAdapter:
    """The synthetic, no-hardware reference adapter (Req 16.7)."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="reference",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=frozenset({"win32", "linux", "darwin"}),
        priority=None,
        requires_elevation_optin=False,
        read_only_declared=True,
    )

    def __init__(self) -> None:
        self._start_mono: float | None = None
        self._last_scalar_update_mono: float | None = None
        self._lock = threading.Lock()
        # configure_rate() applies to the scalar sensor only, to keep the
        # example small; the vector3 sensor's rate is intentionally fixed
        # so `configure_rate` has a `RateFixedError` path to demonstrate.
        self._scalar_rate_hz = _SCALAR_RATE_HZ
        self._push_stop_event = threading.Event()
        self._push_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # required
    # ------------------------------------------------------------------

    def discover(self) -> tuple[SensorInfo, ...]:
        return (
            SensorInfo(
                schema_version=SCHEMA_VERSION,
                id=_SCALAR_ID,
                kind="temp",
                dtype=Dtype.SCALAR,
                unit="degC",
                channels=("temp",),
                shape=(1,),
                range=(-40.0, 125.0),
                resolution=0.1,
                rate_hz=RateSpec(default=_SCALAR_RATE_HZ, min=1.0, max=100.0),
                delivery=Delivery.PUSH,
                derived=False,
                requires_consent=False,
                requires_elevation=False,
                source=("reference",),
                vendor=None,
                part_number=None,
                availability=Availability.PRESENT,
            ),
            SensorInfo(
                schema_version=SCHEMA_VERSION,
                id=_VECTOR3_ID,
                kind="accel",
                dtype=Dtype.VECTOR3,
                unit="m/s2",
                channels=("x", "y", "z"),
                shape=(3,),
                range=(-156.9, 156.9),
                resolution=None,
                rate_hz=RateSpec(default=_VECTOR3_RATE_HZ, min=None, max=None),
                delivery=Delivery.PUSH,
                derived=False,
                requires_consent=False,
                requires_elevation=False,
                source=("reference",),
                vendor=None,
                part_number=None,
                availability=Availability.PRESENT,
            ),
            SensorInfo(
                schema_version=SCHEMA_VERSION,
                id=_MATRIX_ID,
                kind="touchpad",
                dtype=Dtype.MATRIX,
                unit=None,
                channels=("c0", "c1", "c2", "c3"),
                shape=_MATRIX_SHAPE,
                range=(0.0, 1.0),
                resolution=None,
                rate_hz=RateSpec(default=60.0, min=1.0, max=60.0),
                delivery=Delivery.POLL,
                derived=False,
                requires_consent=False,
                requires_elevation=False,
                source=("reference",),
                vendor=None,
                part_number=None,
                availability=Availability.PRESENT,
            ),
            SensorInfo(
                schema_version=SCHEMA_VERSION,
                id=_BUFFER_ID,
                kind="microphone",
                dtype=Dtype.BUFFER,
                unit=None,
                channels=("mono",),
                shape=_BUFFER_SHAPE,
                range=(-1.0, 1.0),
                resolution=None,
                rate_hz=RateSpec(default=_BUFFER_RATE_HZ, min=8000.0, max=10000.0),
                delivery=Delivery.PUSH,
                derived=False,
                requires_consent=True,
                requires_elevation=False,
                source=("reference",),
                vendor=None,
                part_number=None,
                availability=Availability.PRESENT,
            ),
        )

    def read(self, sensor_id: str) -> Reading:
        now = time.monotonic()

        if sensor_id == _SCALAR_ID:
            with self._lock:
                if self._start_mono is None:
                    self._start_mono = now
                start_mono = self._start_mono
                last_update = self._last_scalar_update_mono
                interval = 1.0 / self._scalar_rate_hz if self._scalar_rate_hz else None

            elapsed_since_start = now - start_mono
            value = 20.0 + 2.0 * math.sin(2 * math.pi * 0.05 * elapsed_since_start)

            age = now - last_update if last_update is not None else None
            # Stale-value path (Req 4.8): age exceeds 2x the reciprocal
            # of the sampling rate.
            if interval is not None and age is not None and age > 2 * interval:
                status = Status.STALE
            else:
                status = Status.OK

            return Reading(
                id=sensor_id,
                t_mono=now,
                t_wall=time.time(),
                values=(value,),
                seq=0,
                status=status,
            )

        if sensor_id == _VECTOR3_ID:
            # Never-received-value path (Req 4.9): this sensor never
            # actually produces a value. `read()` must return
            # immediately with UNAVAILABLE and empty values, not block.
            return Reading(
                id=sensor_id,
                t_mono=now,
                t_wall=time.time(),
                values=(),
                seq=0,
                status=Status.UNAVAILABLE,
            )

        if sensor_id == _MATRIX_ID:
            values = tuple(
                float((row + col) % 2)
                for row in range(_MATRIX_SHAPE[0])
                for col in range(_MATRIX_SHAPE[1])
            )
            return Reading(
                id=sensor_id,
                t_mono=now,
                t_wall=time.time(),
                values=values,
                seq=0,
                status=Status.OK,
            )

        raise KeyError(f"unknown sensor id for ReferenceAdapter: {sensor_id!r}")

    # ------------------------------------------------------------------
    # optional (all six implemented, per task 3.1)
    # ------------------------------------------------------------------

    def open_stream(
        self,
        sensor_id: str,
        *,
        block_size: int | None = None,
        rate_hz: float | None = None,
        buffer_blocks: int = 64,
    ) -> _BufferStreamSource:
        if sensor_id != _BUFFER_ID:
            raise KeyError(f"sensor {sensor_id!r} does not support streaming")
        min_size, max_size = self.supported_block_sizes(sensor_id)
        resolved_block_size = block_size if block_size is not None else _BUFFER_SHAPE[0]
        resolved_block_size = max(min_size, min(max_size, resolved_block_size))
        resolved_rate_hz = rate_hz if rate_hz is not None else _BUFFER_RATE_HZ
        return _BufferStreamSource(
            sensor_id, block_size=resolved_block_size, rate_hz=resolved_rate_hz
        )

    def supported_block_sizes(self, sensor_id: str) -> tuple[int, int]:
        if sensor_id != _BUFFER_ID:
            raise KeyError(f"sensor {sensor_id!r} has no block-size configuration")
        return (64, 4096)

    def configure_rate(self, sensor_id: str, rate_hz: float) -> float:
        if sensor_id == _SCALAR_ID:
            spec = RateSpec(default=_SCALAR_RATE_HZ, min=1.0, max=100.0)
            applied = nearest_supported_rate(spec, rate_hz, sensor_id=sensor_id)
            with self._lock:
                self._scalar_rate_hz = applied
            return applied

        if sensor_id == _VECTOR3_ID:
            # Demonstrates the fixed-rate contract (Req 6.7): this
            # sensor's backend supports no rate configuration at all.
            raise RateFixedError(sensor_id=sensor_id)

        if sensor_id == _MATRIX_ID:
            spec = RateSpec(default=60.0, min=1.0, max=60.0)
            return nearest_supported_rate(spec, rate_hz, sensor_id=sensor_id)

        if sensor_id == _BUFFER_ID:
            spec = RateSpec(default=_BUFFER_RATE_HZ, min=8000.0, max=10000.0)
            return nearest_supported_rate(spec, rate_hz, sensor_id=sensor_id)

        raise KeyError(f"unknown sensor id for ReferenceAdapter: {sensor_id!r}")

    def setup(self) -> None:
        self._start_mono = time.monotonic()
        with self._lock:
            self._last_scalar_update_mono = self._start_mono
        # A background thread simulates a push backend delivering fresh
        # scalar values at its configured cadence, updating
        # `_last_scalar_update_mono` each time. If this thread falls
        # behind (or is stopped), `read()`'s staleness check (Req 4.8)
        # has something real to observe.
        self._push_stop_event.clear()
        self._push_thread = threading.Thread(
            target=self._push_loop, name="sensortap-reference-push", daemon=True
        )
        self._push_thread.start()

    def teardown(self) -> None:
        self._push_stop_event.set()
        if self._push_thread is not None and self._push_thread.is_alive():
            self._push_thread.join(timeout=1.0)
        self._push_thread = None
        with self._lock:
            self._start_mono = None
            self._last_scalar_update_mono = None

    def _push_loop(self) -> None:
        while not self._push_stop_event.is_set():
            with self._lock:
                rate = self._scalar_rate_hz
            interval = 1.0 / rate if rate else 1.0
            if self._push_stop_event.wait(timeout=interval):
                break
            with self._lock:
                self._last_scalar_update_mono = time.monotonic()

    def health(self) -> _ReferenceHealth:
        return _ReferenceHealth(status="ok", detail="synthetic adapter, no hardware")

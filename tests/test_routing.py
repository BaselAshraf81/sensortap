"""Focused tests for registry/routing.py's Registry (Req 3.9, 3.10, 4.1,
4.2, 4.6, 4.7, 4.12, 4.13, 7.3, 10.3, 15.2).

Uses a fake in-process adapter conforming to the Adapter Protocol, built
directly against `Registry` internals (bypassing entry-point discovery,
which is already covered by test_loading.py) so these tests stay focused
on routing/dispatch behaviour.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import ClassVar

import pytest

from sensortap.adapters.protocol import AdapterMeta
from sensortap.registry.errors import (
    BlockPathRequiredError,
    ConsentError,
    MalformedSensorIdError,
    UnknownSensorError,
    UnsupportedOperationError,
)
from sensortap.registry.loading import LoadedAdapter
from sensortap.registry.routing import Registry
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo


def _make_info(sensor_id: str, *, dtype: Dtype = Dtype.SCALAR, requires_consent: bool = False,
                shape: tuple[int, ...] = (1,)) -> SensorInfo:
    return SensorInfo(
        schema_version="1.0",
        id=sensor_id,
        kind="temp" if dtype != Dtype.BUFFER else "microphone",
        dtype=dtype,
        unit="degC" if dtype != Dtype.BUFFER else None,
        channels=("value",),
        shape=shape,
        range=None,
        resolution=None,
        rate_hz=RateSpec(default=1.0, min=0.1, max=10.0),
        delivery=Delivery.POLL,
        derived=False,
        requires_consent=requires_consent,
        requires_elevation=False,
        source=("fake",),
        vendor=None,
        part_number=None,
        availability=Availability.PRESENT,
    )


@dataclass
class FakeAdapter:
    """A minimal in-process Adapter for routing tests. Supports only the
    two required methods unless `with_stream` is set, so it can exercise
    the UnsupportedOperationError path.
    """

    sensors: list[SensorInfo]
    with_stream: bool = False
    read_calls: list[str] = field(default_factory=list)
    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="fake",
        interface_version="1.0",
        supported_platforms=frozenset({"win32", "linux"}),
        read_only_declared=True,
    )

    def discover(self):
        return self.sensors

    def read(self, sensor_id: str) -> Reading:
        self.read_calls.append(sensor_id)
        return Reading(id=sensor_id, t_mono=1.0, t_wall=1.0, values=(1.0,), seq=0, status=Status.OK)


class FakeAdapterWithStream(FakeAdapter):
    def open_stream(self, sensor_id, *, block_size=None, rate_hz=None, buffer_blocks=64):
        raise AssertionError("not exercised in these tests")


def _registry_with_adapter(adapter, *, sensors=None) -> Registry:
    """Build a Registry without running real adapter loading, then inject
    one fake adapter's sensors directly into the routing caches, exactly
    as list_sensors() would have populated them.
    """
    registry = Registry.__new__(Registry)
    Registry.__init__(registry, discovery_timeout_ms=2000)
    # Replace the loaded-adapter set that __init__'s real load_adapters()
    # call would have populated from entry points (there are none in the
    # test environment) with our fake adapter.
    registry._loaded_adapters = [
        LoadedAdapter(entry_point_name="fake", distribution_name=None, instance=adapter)
    ]

    sensors = sensors if sensors is not None else adapter.sensors
    registry._sensor_owner = {s.id: adapter for s in sensors}
    registry._sensor_info = {s.id: s for s in sensors}
    for s in sensors:
        registry._last_availability[s.id] = str(s.availability)
    return registry


@pytest.fixture(autouse=True)
def _no_atexit_leak():
    # Registry.__init__ registers shutdown() with atexit; call it here
    # too so tests don't accumulate handlers across the session (harmless
    # either way, but keeps things tidy).
    yield


def test_read_grammar_error_raised_before_any_lookup():
    adapter = FakeAdapter(sensors=[])
    registry = _registry_with_adapter(adapter)

    with pytest.raises(MalformedSensorIdError):
        registry.read("not-three-segments")

    assert adapter.read_calls == []


def test_read_unknown_sensor_raises_without_invoking_adapter():
    adapter = FakeAdapter(sensors=[])
    registry = _registry_with_adapter(adapter)

    with pytest.raises(UnknownSensorError):
        registry.read("temp.fake.0")

    assert adapter.read_calls == []


def test_read_blocks_until_consent_granted():
    info = _make_info("temp.fake.0", requires_consent=True)
    adapter = FakeAdapter(sensors=[info])
    registry = _registry_with_adapter(adapter)

    with pytest.raises(ConsentError):
        registry.read("temp.fake.0")
    assert adapter.read_calls == []

    with registry._consent_gate.grant(["temp.fake.0"]):
        reading = registry.read("temp.fake.0")
        assert reading.id == "temp.fake.0"
    assert adapter.read_calls == ["temp.fake.0"]


def test_read_rejects_buffer_dtype_without_consuming_seq():
    info = _make_info("microphone.fake.0", dtype=Dtype.BUFFER, shape=(1024, 1))
    adapter = FakeAdapter(sensors=[info])
    registry = _registry_with_adapter(adapter)

    with pytest.raises(BlockPathRequiredError):
        registry.read("microphone.fake.0")

    assert adapter.read_calls == []
    # No seq was allocated for this sensor id.
    assert "microphone.fake.0" not in registry._next_seq


def test_concurrent_reads_of_one_sensor_get_distinct_seq_values():
    info = _make_info("temp.fake.0")
    adapter = FakeAdapter(sensors=[info])
    registry = _registry_with_adapter(adapter)

    seqs: list[int] = []
    lock = threading.Lock()

    def _do_read():
        reading = registry.read("temp.fake.0")
        with lock:
            seqs.append(reading.seq)

    threads = [threading.Thread(target=_do_read) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(seqs) == 20
    assert len(set(seqs)) == 20  # all distinct
    assert sorted(seqs) == list(range(20))


def test_t_mono_is_clamped_to_previous_plus_one_tick_when_non_increasing():
    info = _make_info("temp.fake.0")

    class NonMonotonicAdapter(FakeAdapter):
        def __init__(self, sensors):
            super().__init__(sensors=sensors)
            self._t_mono_values = [5.0, 5.0, 3.0]

        def read(self, sensor_id):
            self.read_calls.append(sensor_id)
            t_mono = self._t_mono_values.pop(0)
            return Reading(id=sensor_id, t_mono=t_mono, t_wall=1.0, values=(1.0,), seq=0, status=Status.OK)

    adapter = NonMonotonicAdapter(sensors=[info])
    registry = _registry_with_adapter(adapter)

    r1 = registry.read("temp.fake.0")
    r2 = registry.read("temp.fake.0")
    r3 = registry.read("temp.fake.0")

    assert r1.t_mono == 5.0
    assert r2.t_mono > r1.t_mono  # clamped from 5.0 to 5.0 + tick
    assert r3.t_mono > r2.t_mono  # clamped from 3.0 forward again


def test_list_sensors_routes_dispatch_when_source_qualifier_differs_from_adapter_id():
    """A record's `source` tuple is each adapter's own Sensor_Id
    qualifier segment, which is not guaranteed to equal `meta.adapter_id`
    (e.g. `hwmon_bridge` reports `source=("hwmon",)`). Before this fix,
    `list_sensors()`'s owner lookup matched `source` directly against
    `adapter_id`, so any adapter using a shorter qualifier never got an
    entry in `_sensor_owner` -- read()/stream() on such a sensor always
    raised UnknownSensorError, real live case found: no sensor from a
    C# helper hardware-monitoring adapter (~65 sensors on real hardware)
    was ever readable despite list_sensors() enumerating it correctly."""

    class ShortQualifierAdapter(FakeAdapter):
        meta: ClassVar[AdapterMeta] = AdapterMeta(
            adapter_id="short_qualifier_adapter",
            interface_version="1.0",
            supported_platforms=frozenset({"win32", "linux"}),
            read_only_declared=True,
        )

    from dataclasses import replace

    info = replace(_make_info("temp.short.0"), source=("short",))
    adapter = ShortQualifierAdapter(sensors=[info])

    registry = Registry.__new__(Registry)
    Registry.__init__(registry, discovery_timeout_ms=2000)
    registry._loaded_adapters = [
        LoadedAdapter(entry_point_name="short", distribution_name=None, instance=adapter)
    ]

    sensors = registry.list_sensors()
    assert [s.id for s in sensors] == ["temp.short.0"]

    reading = registry.read("temp.short.0")
    assert reading.id == "temp.short.0"
    assert adapter.read_calls == ["temp.short.0"]


def test_stream_raises_unsupported_operation_when_adapter_lacks_open_stream():
    info = _make_info("temp.fake.0")
    other_info = _make_info("temp.fake.1")
    adapter = FakeAdapter(sensors=[info, other_info])
    registry = _registry_with_adapter(adapter)

    with pytest.raises(UnsupportedOperationError):
        registry.stream("temp.fake.0")

    # The adapter's other sensors remain readable through read().
    reading = registry.read("temp.fake.1")
    assert reading.id == "temp.fake.1"

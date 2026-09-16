"""Tests for the post-collection pipeline in registry/dedup.py
(Req 1.1, 1.2, 1.3, 1.6, 1.7, 2.10, 3.7, 3.8, 10.10).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from sensortap.adapters.protocol import AdapterMeta
from sensortap.registry.dedup import run_pipeline
from sensortap.registry.loading import LoadedAdapter
from sensortap.schema.enums import Availability, Delivery, Dtype
from sensortap.schema.rate import RateSpec
from sensortap.schema.sensor_info import SensorInfo


def make_info(sensor_id: str, *, kind: str = "temp", source=()) -> SensorInfo:
    return SensorInfo(
        schema_version="1.0",
        id=sensor_id,
        kind=kind,
        dtype=Dtype.SCALAR,
        unit="degC",
        channels=("value",),
        shape=(1,),
        range=(-40.0, 125.0),
        resolution=0.1,
        rate_hz=RateSpec(default=1.0, min=0.1, max=10.0),
        delivery=Delivery.POLL,
        derived=False,
        requires_consent=False,
        requires_elevation=False,
        source=source,
        vendor=None,
        part_number=None,
        availability=Availability.PRESENT,
    )


def _adapter(adapter_id: str, *, priority: int | None = None) -> LoadedAdapter:
    class _Instance:
        meta: ClassVar[AdapterMeta] = AdapterMeta(
            adapter_id=adapter_id,
            interface_version="1.0",
            supported_platforms=frozenset({"win32", "linux"}),
            priority=priority,
            read_only_declared=True,
        )

    return LoadedAdapter(
        entry_point_name=adapter_id,
        distribution_name=None,
        instance=_Instance(),
    )


def test_validation_drops_bad_record_but_keeps_good_ones_from_same_adapter():
    good = make_info("temp.hwmon.0")
    # kind outside the closed vocabulary -> invalid.
    bad = make_info("temp.hwmon.1", kind="not-a-real-kind")

    adapters = [_adapter("hwmon")]
    raw = {"hwmon": [good, bad]}

    result = run_pipeline(raw, adapters)

    assert [s.id for s in result.sensors] == ["temp.hwmon.0"]
    assert len(result.validation_errors) == 1
    assert result.validation_errors[0].adapter_id == "hwmon"


def test_dedup_and_conflict_resolution_picks_highest_priority_and_merges_source():
    low = make_info("temp.hwmon.0")
    high = make_info("temp.hwmon.0")

    adapters = [_adapter("low_prio", priority=1), _adapter("high_prio", priority=5)]
    raw = {"low_prio": [low], "high_prio": [high]}

    result = run_pipeline(raw, adapters)

    assert len(result.sensors) == 1
    winner = result.sensors[0]
    assert winner.source == ("low_prio", "high_prio")

    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.id == "temp.hwmon.0"
    assert conflict.winning_adapter == "high_prio"
    assert conflict.competitors == ("low_prio", "high_prio")


def test_conflict_resolution_ties_go_to_first_loaded_adapter():
    first = make_info("temp.hwmon.0")
    second = make_info("temp.hwmon.0")

    adapters = [_adapter("first"), _adapter("second")]  # both priority None
    raw = {"first": [first], "second": [second]}

    result = run_pipeline(raw, adapters)

    assert len(result.sensors) == 1
    assert result.conflicts[0].winning_adapter == "first"


def test_within_adapter_collision_gets_disambiguation_suffix():
    # One adapter emits two records that happen to share an id, but they
    # are distinct physical sensors (a naive id-derivation bug).
    sensor_a = make_info("temp.hwmon.0")
    sensor_b = make_info("temp.hwmon.0")

    adapters = [_adapter("hwmon")]
    raw = {"hwmon": [sensor_a, sensor_b]}

    result = run_pipeline(raw, adapters)

    assert len(result.sensors) == 2
    ids = sorted(s.id for s in result.sensors)
    assert ids == ["temp.hwmon.0", "temp.hwmon.0-0"]
    assert len(result.collision_warnings) == 1
    assert result.collision_warnings[0].sensor_id == "temp.hwmon.0"
    assert result.collision_warnings[0].affected_count == 2
    # No cross-adapter conflict recorded: this is a within-adapter collision.
    assert result.conflicts == []


def test_ordering_by_load_order_then_ascending_id_and_filters_are_conjunctive():
    a1 = make_info("temp.hwmon.9", kind="temp")
    a2 = make_info("temp.hwmon.1", kind="temp")
    b1 = make_info("accel.builtin.0", kind="accel")

    adapters = [_adapter("second"), _adapter("first")]
    raw = {"second": [a1, a2], "first": [b1]}

    result = run_pipeline(raw, adapters)

    # Ordered by adapter load order (second, then first), then ascending
    # id within an adapter.
    assert [s.id for s in result.sensors] == [
        "temp.hwmon.1",
        "temp.hwmon.9",
        "accel.builtin.0",
    ]

    # Conjunctive filtering: kind=temp AND id=temp.hwmon.1 -> exactly one.
    filtered = run_pipeline(raw, adapters, kind="temp", id="temp.hwmon.1")
    assert [s.id for s in filtered.sensors] == ["temp.hwmon.1"]

    # No match -> empty list.
    empty = run_pipeline(raw, adapters, kind="temp", id="accel.builtin.0")
    assert empty.sensors == []

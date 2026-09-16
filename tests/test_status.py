"""Minimal tests for the per-adapter BackendStatus tracker (Req 11)."""

from __future__ import annotations

import time

from sensortap.registry.status import (
    DependencySpec,
    NotLoadedReason,
    StatusTracker,
)


def test_loaded_zero_sensors_distinct_from_not_loaded() -> None:
    tracker = StatusTracker()
    tracker.record_loaded("adapter_a", discovered_count=0)
    tracker.record_not_loaded(
        "adapter_b",
        reason=NotLoadedReason.NOT_OPTED_IN,
        remediation="opt in via config to enable this adapter",
        unsatisfied_deps=(DependencySpec("libfoo", ">=1.0"),),
    )

    records = {s.adapter_id: s for s in tracker.snapshot()}

    loaded = records["adapter_a"]
    assert loaded.state == "loaded"
    assert loaded.discovered_count == 0
    assert loaded.reason is None

    not_loaded = records["adapter_b"]
    assert not_loaded.state == "not_loaded"
    assert not_loaded.discovered_count == 0
    assert not_loaded.reason is NotLoadedReason.NOT_OPTED_IN
    assert not_loaded.unsatisfied_deps == (DependencySpec("libfoo", ">=1.0"),)


def test_mark_unknown_keeps_other_records() -> None:
    tracker = StatusTracker()
    tracker.record_loaded("adapter_a", discovered_count=3)
    tracker.mark_unknown("adapter_b")

    records = {s.adapter_id: s for s in tracker.snapshot()}

    assert records["adapter_a"].state == "loaded"
    assert records["adapter_a"].discovered_count == 3

    assert records["adapter_b"].state == "unknown"
    assert records["adapter_b"].introspection_failed is True


def test_snapshot_is_fast_with_no_enumeration() -> None:
    tracker = StatusTracker()
    for i in range(50):
        tracker.record_not_loaded(
            f"adapter_{i}",
            reason=NotLoadedReason.NOT_OPTED_IN,
            remediation="opt in via config to enable this adapter",
        )

    start = time.monotonic()
    snapshot = tracker.snapshot()
    elapsed_ms = (time.monotonic() - start) * 1000

    assert len(snapshot) == 50
    assert elapsed_ms < 500

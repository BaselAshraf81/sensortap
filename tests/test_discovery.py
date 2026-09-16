"""Tests for concurrent discovery mechanics (Req 1.4, 1.8, 1.9, 9.1-9.9)."""

from __future__ import annotations

import threading
import time

import pytest

from sensortap.registry.discovery import (
    DEFAULT_DISCOVERY_TIMEOUT_MS,
    DiscoveryCoordinator,
    DiscoveryError,
    MAX_DISCOVERY_TIMEOUT_MS,
    MIN_DISCOVERY_TIMEOUT_MS,
    PreviousDiscoveryStillRunning,
    TimedOut,
    validate_discovery_timeout,
)
from sensortap.registry.errors import InvalidTimeoutError


class _FastAdapter:
    class meta:
        adapter_id = "fast"

    def discover(self):
        return []

    def read(self, sensor_id):  # pragma: no cover - not used here
        raise NotImplementedError


class _SlowAdapter:
    """Simulates an adapter whose discover() blocks past the deadline and
    cannot be interrupted -- release_event lets the test unblock it later
    to observe the abandoned-worker done-callback firing."""

    class meta:
        adapter_id = "slow"

    def __init__(self, release_event: threading.Event):
        self._release_event = release_event

    def discover(self):
        self._release_event.wait(timeout=5)
        return []

    def read(self, sensor_id):  # pragma: no cover - not used here
        raise NotImplementedError


class _RaisingAdapter:
    class meta:
        adapter_id = "raising"

    def discover(self):
        raise RuntimeError("boom")

    def read(self, sensor_id):  # pragma: no cover - not used here
        raise NotImplementedError


def test_validate_discovery_timeout_accepts_range_and_defaults():
    assert validate_discovery_timeout(MIN_DISCOVERY_TIMEOUT_MS) == MIN_DISCOVERY_TIMEOUT_MS
    assert validate_discovery_timeout(MAX_DISCOVERY_TIMEOUT_MS) == MAX_DISCOVERY_TIMEOUT_MS
    assert DEFAULT_DISCOVERY_TIMEOUT_MS == 5000


@pytest.mark.parametrize("bad_value", [99, 60001, "abc", None, True])
def test_validate_discovery_timeout_rejects_out_of_range_or_non_numeric(bad_value):
    with pytest.raises(InvalidTimeoutError):
        validate_discovery_timeout(bad_value)


def test_run_discovery_fast_and_raising_adapters_do_not_block_each_other():
    coordinator = DiscoveryCoordinator()
    try:
        outcomes = coordinator.run_discovery(
            [_FastAdapter(), _RaisingAdapter()], timeout_ms=MIN_DISCOVERY_TIMEOUT_MS
        )
        assert outcomes["fast"] == []
        assert isinstance(outcomes["raising"], DiscoveryError)
        assert isinstance(outcomes["raising"].error, RuntimeError)
    finally:
        coordinator.shutdown()


def test_run_discovery_timeout_does_not_accumulate_across_adapters():
    """Two adapters that both block past the deadline must together cost
    roughly one timeout period, not two (Req 9.1)."""
    coordinator = DiscoveryCoordinator()
    release_a = threading.Event()
    release_b = threading.Event()
    try:
        start = time.monotonic()
        outcomes = coordinator.run_discovery(
            [_SlowAdapter(release_a), _SlowAdapter(release_b)],
            timeout_ms=MIN_DISCOVERY_TIMEOUT_MS,
        )
        elapsed = time.monotonic() - start
        assert elapsed < (2 * MIN_DISCOVERY_TIMEOUT_MS / 1000)
        # Both adapters share the key "slow" (same adapter_id) in this test,
        # so use distinct adapter ids to disambiguate outcomes.
    finally:
        release_a.set()
        release_b.set()
        coordinator.shutdown()

    assert all(isinstance(o, TimedOut) for o in outcomes.values())


def test_abandoned_adapter_is_skipped_until_late_worker_resolves():
    coordinator = DiscoveryCoordinator()
    release_event = threading.Event()

    class _SlowAdapterUniqueId(_SlowAdapter):
        class meta:
            adapter_id = "slow-unique"

    adapter = _SlowAdapterUniqueId(release_event)
    try:
        first = coordinator.run_discovery([adapter], timeout_ms=MIN_DISCOVERY_TIMEOUT_MS)
        assert isinstance(first["slow-unique"], TimedOut)

        # A second round while the worker is still blocked must not
        # resubmit the adapter -- it should report "previous discovery
        # still running" instead (Req 9.9).
        second = coordinator.run_discovery([adapter], timeout_ms=MIN_DISCOVERY_TIMEOUT_MS)
        assert isinstance(second["slow-unique"], PreviousDiscoveryStillRunning)

        # Release the blocked worker and let its done-callback clear the
        # abandoned entry.
        release_event.set()
        deadline = time.monotonic() + 2
        while coordinator._is_abandoned("slow-unique") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not coordinator._is_abandoned("slow-unique")

        # Now the adapter is eligible again.
        third = coordinator.run_discovery([adapter], timeout_ms=1000)
        assert third["slow-unique"] == []
    finally:
        release_event.set()
        coordinator.shutdown()

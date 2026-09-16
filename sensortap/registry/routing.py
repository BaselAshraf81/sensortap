"""Routing and the public `Registry` API (Req 3.9, 3.10, 4.1, 4.2, 4.6, 4.7,
4.12, 4.13, 7.3, 10.3, 10.11, 14.4, 15.2).

`Registry` is the only stateful object in the core (design.md, "The
Registry"). It assembles adapter loading, concurrent discovery, the
dedup/validation pipeline, per-adapter status, the consent gate and
Sensor_Id routing behind the five public methods plus `shutdown()`.

Nothing here branches on `sys.platform`: all platform variance already
lives inside adapters (Req 14.4), so the public surface is identical on
every platform by construction.
"""

from __future__ import annotations

import atexit
import re
import threading
from time import monotonic, time
from typing import Any, Callable

from sensortap.adapters.protocol import Adapter
from sensortap.registry.consent import AuditEvent, ConsentGate, ConsentGrant
from sensortap.registry.dedup import run_pipeline
from sensortap.registry.discovery import (
    DEFAULT_DISCOVERY_TIMEOUT_MS,
    DiscoveryCoordinator,
    DiscoveryError,
    PreviousDiscoveryStillRunning,
    TimedOut,
    validate_discovery_timeout,
)
from sensortap.registry.errors import (
    BlockPathRequiredError,
    ConsentError,
    UnknownSensorError,
    UnsupportedOperationError,
)
from sensortap.registry.ids import validate_sensor_id_grammar
from sensortap.registry.loading import LoadedAdapter, load_adapters
from sensortap.registry.status import NotLoadedReason, StatusTracker
from sensortap.registry.streaming import Stream
from sensortap.schema.enums import Dtype
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo

#: The smallest increment `t_mono` is bumped by when clamping a
#: non-increasing value from an adapter (Req 4.7).
_T_MONO_TICK = 1e-6


class Registry:
    """The orchestrator: adapter loading, concurrent discovery, dedup,
    per-adapter status, consent, and Sensor_Id routing/dispatch.

    One instance backs the module-level convenience functions in
    `sensortap/__init__.py`; consumers wanting isolation construct their
    own (design.md, "The Registry").
    """

    def __init__(
        self,
        *,
        discovery_timeout_ms: int = DEFAULT_DISCOVERY_TIMEOUT_MS,
        include_elevated: bool = False,
        audit_hook: Callable[[AuditEvent], None] | None = None,
    ) -> None:
        # Validate before touching any adapter (Req 9.8).
        self._discovery_timeout_ms = validate_discovery_timeout(discovery_timeout_ms)

        load_result = load_adapters(include_elevated=include_elevated)
        self._loaded_adapters: list[LoadedAdapter] = load_result.loaded

        self._status_tracker = StatusTracker()
        for loaded in self._loaded_adapters:
            self._status_tracker.record_loaded(loaded.instance.meta.adapter_id)
        for skipped in load_result.skipped:
            reason = (
                NotLoadedReason.NOT_OPTED_IN
                if skipped.reason == "not_opted_in"
                else NotLoadedReason.UNSUPPORTED_PLATFORM
            )
            self._status_tracker.record_not_loaded(
                skipped.entry_point_name,
                reason=reason,
                remediation=skipped.detail or "adapter was not loaded",
            )
        for failure in load_result.failures:
            self._status_tracker.record_not_loaded(
                failure.entry_point_name,
                reason=NotLoadedReason.LOAD_ERROR,
                remediation=failure.detail or "adapter failed to load",
            )

        self._consent_gate = ConsentGate(audit_hook=audit_hook)
        self._discovery = DiscoveryCoordinator()

        # Sensor_Id -> owning adapter instance, and the latest cached
        # SensorInfo per id, populated by list_sensors() so read()/stream()
        # can route without re-running discovery.
        self._sensor_owner: dict[str, Adapter] = {}
        self._sensor_info: dict[str, SensorInfo] = {}
        self._last_availability: dict[str, str] = {}

        # Per-sensor-id lock + monotonic seq counter + last t_mono, so
        # concurrent readers of one sensor get distinct seq values and
        # t_mono never decreases (Req 4.6, 4.7, 4.13).
        self._seq_lock = threading.Lock()
        self._read_locks: dict[str, threading.Lock] = {}
        self._next_seq: dict[str, int] = {}
        self._last_t_mono: dict[str, float] = {}

        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

        atexit.register(self.shutdown)

    # ------------------------------------------------------------------
    # list_sensors
    # ------------------------------------------------------------------

    def list_sensors(
        self,
        *,
        kind: str | None = None,
        source: str | None = None,
        id: str | None = None,
    ) -> list[SensorInfo]:
        adapters = [loaded.instance for loaded in self._loaded_adapters]
        outcomes = self._discovery.run_discovery(adapters, self._discovery_timeout_ms)

        raw_results: dict[str, list[SensorInfo]] = {}
        for loaded in self._loaded_adapters:
            adapter_id = loaded.instance.meta.adapter_id
            outcome = outcomes.get(adapter_id)

            if isinstance(outcome, list):
                raw_results[adapter_id] = outcome
                self._status_tracker.update_enumeration(
                    adapter_id, discovered_count=len(outcome), duration_ms=0.0
                )
            elif isinstance(outcome, TimedOut):
                # Timeout: raise nothing to the caller, this adapter
                # simply contributes no records this round (Req 1.8, 9.2).
                raw_results[adapter_id] = []
                self._status_tracker.update_timeout(
                    adapter_id, timeout_ms=outcome.limit_ms
                )
            elif isinstance(outcome, DiscoveryError):
                raw_results[adapter_id] = []
                self._status_tracker.record_degraded(
                    adapter_id,
                    discovered_count=0,
                    remediation=f"discover() raised: {outcome.error!r}",
                )
            elif isinstance(outcome, PreviousDiscoveryStillRunning):
                raw_results[adapter_id] = []
            else:
                raw_results[adapter_id] = []

        result = run_pipeline(
            raw_results, self._loaded_adapters, kind=kind, source=source, id=id
        )

        # Cache the latest full enumeration for read()/stream() routing.
        # Re-run unfiltered so the cache always reflects the full set,
        # regardless of the filters this particular call used.
        full_result = (
            result
            if kind is None and source is None and id is None
            else run_pipeline(raw_results, self._loaded_adapters)
        )

        owner_by_adapter_id = {
            loaded.instance.meta.adapter_id: loaded.instance
            for loaded in self._loaded_adapters
        }

        new_sensor_owner: dict[str, Adapter] = {}
        new_sensor_info: dict[str, SensorInfo] = {}
        for sensor in full_result.sensors:
            new_sensor_info[sensor.id] = sensor
            self._last_availability[sensor.id] = str(sensor.availability)
            # A deduped/merged record's `source` lists every reporting
            # adapter; the owning adapter for dispatch is the first one
            # in that tuple that is actually loaded (load order winner).
            owner: Adapter | None = None
            for adapter_id in sensor.source:
                if adapter_id in owner_by_adapter_id:
                    owner = owner_by_adapter_id[adapter_id]
                    break
            if owner is None:
                # `source` is each adapter's own qualifier string, which
                # is not guaranteed to equal its `adapter_id` (e.g.
                # hwmon_bridge reports source=("hwmon",) while its
                # adapter_id is "hwmon_bridge"). Fall back to the
                # raw_results this record actually came from: the first
                # loaded adapter whose discover() output contains this
                # record's id is the real owner for dispatch. A record
                # that went through the collision-suffix step (Req 3.8)
                # has an id that no longer matches any raw record
                # verbatim, so also try stripping a trailing `-<digits>`
                # disambiguation suffix before giving up.
                candidate_raw_ids = {sensor.id}
                stripped = re.sub(r"-\d+$", "", sensor.id)
                if stripped != sensor.id:
                    candidate_raw_ids.add(stripped)
                for loaded in self._loaded_adapters:
                    adapter_id = loaded.instance.meta.adapter_id
                    if any(
                        r.id in candidate_raw_ids
                        for r in raw_results.get(adapter_id, [])
                    ):
                        owner = loaded.instance
                        break
            if owner is not None:
                new_sensor_owner[sensor.id] = owner

        self._sensor_owner = new_sensor_owner
        self._sensor_info = new_sensor_info

        return result.sensors

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------

    def read(self, sensor_id: str) -> Reading:
        # 1. Grammar check first, no lookup performed (Req 3.10).
        validate_sensor_id_grammar(sensor_id)

        # 2. Existence check against the most recent enumeration. No
        # adapter is invoked (Req 3.9, 4.2).
        info = self._sensor_info.get(sensor_id)
        if info is None:
            last_known = self._last_availability.get(sensor_id)
            raise UnknownSensorError(sensor_id=sensor_id, last_known_availability=last_known)

        # 3. Consent check before dispatch; the device is left closed on
        # denial (Req 7.3).
        if info.requires_consent and not self._consent_gate.is_granted(sensor_id):
            raise ConsentError(sensor_id=sensor_id)

        # 4. dtype check: buffer sensors must use the Block/stream path,
        # and `seq` is never consumed for a rejected read (Req 4.12).
        if info.dtype == Dtype.BUFFER:
            raise BlockPathRequiredError(sensor_id=sensor_id)

        adapter = self._sensor_owner.get(sensor_id)
        if adapter is None:
            raise UnknownSensorError(sensor_id=sensor_id, last_known_availability=None)

        # 5. Dispatch. Adapter errors (e.g. SensorUnavailableError,
        # ReadTimeoutError from a poll adapter) are never swallowed --
        # they propagate to the caller unchanged (Req 4.6's dispatch step
        # explicitly does not hide adapter failures).
        reading = adapter.read(sensor_id)

        return self._finalize_reading(sensor_id, reading)

    def _finalize_reading(self, sensor_id: str, reading: Reading) -> Reading:
        """Allocate a distinct `seq` and a non-decreasing `t_mono` for one
        sensor under a per-sensor-id lock, so concurrent readers of the
        same sensor never observe a repeated `seq` or a `t_mono` that goes
        backwards -- even if the adapter itself is not internally
        thread-safe or occasionally reports a non-increasing clock
        reading (Req 4.6, 4.7, 4.13).
        """
        lock = self._get_read_lock(sensor_id)
        with lock:
            seq = self._next_seq.get(sensor_id, 0)
            self._next_seq[sensor_id] = seq + 1

            t_mono = reading.t_mono
            previous = self._last_t_mono.get(sensor_id)
            if previous is not None and t_mono <= previous:
                t_mono = previous + _T_MONO_TICK
            self._last_t_mono[sensor_id] = t_mono

        if t_mono == reading.t_mono and seq == reading.seq:
            return reading
        return Reading(
            id=reading.id,
            t_mono=t_mono,
            t_wall=reading.t_wall,
            values=reading.values,
            seq=seq,
            status=reading.status,
        )

    def _get_read_lock(self, sensor_id: str) -> threading.Lock:
        with self._seq_lock:
            lock = self._read_locks.get(sensor_id)
            if lock is None:
                lock = threading.Lock()
                self._read_locks[sensor_id] = lock
            return lock

    # ------------------------------------------------------------------
    # stream
    # ------------------------------------------------------------------

    def stream(
        self,
        sensor_id: str,
        *,
        block_size: int | None = None,
        rate_hz: float | None = None,
        buffer_blocks: int = 64,
    ) -> Stream:
        # Same grammar/existence/consent checks as read().
        validate_sensor_id_grammar(sensor_id)

        info = self._sensor_info.get(sensor_id)
        if info is None:
            last_known = self._last_availability.get(sensor_id)
            raise UnknownSensorError(sensor_id=sensor_id, last_known_availability=last_known)

        if info.requires_consent and not self._consent_gate.is_granted(sensor_id):
            raise ConsentError(sensor_id=sensor_id)

        adapter = self._sensor_owner.get(sensor_id)
        if adapter is None:
            raise UnknownSensorError(sensor_id=sensor_id, last_known_availability=None)

        # Configuration keys are limited, by signature, to exactly
        # `rate_hz` and `block_size` (Req 15.2); there is no `**kwargs`
        # here for a caller to smuggle an arbitrary key through.
        open_stream = getattr(adapter, "open_stream", None)
        if not callable(open_stream):
            # Missing optional method: raise UnsupportedOperationError,
            # while this adapter's other sensors stay readable via
            # read() (Req 10.3) -- nothing here disables the adapter or
            # its other sensors.
            raise UnsupportedOperationError(sensor_id=sensor_id, operation="stream")

        source = open_stream(
            sensor_id, block_size=block_size, rate_hz=rate_hz, buffer_blocks=buffer_blocks
        )
        applied_rate_hz = None
        applied_rate = getattr(source, "applied_rate", None)
        if callable(applied_rate):
            try:
                applied_rate_hz = applied_rate()
            except Exception:
                applied_rate_hz = None
        return Stream(
            source,
            sensor_id=sensor_id,
            buffer_blocks=buffer_blocks,
            applied_rate_hz=applied_rate_hz,
        )

    # ------------------------------------------------------------------
    # consent / backend_status
    # ------------------------------------------------------------------

    def consent(self, sensor_ids: Any) -> ConsentGrant:
        return self._consent_gate.grant(sensor_ids)

    def backend_status(self) -> list[Any]:
        return self._status_tracker.snapshot()

    # ------------------------------------------------------------------
    # shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Call `teardown()` on every loaded adapter exactly once (Req
        10.11), catching and recording (not re-raising) any exception so
        the loop continues through the rest. Idempotent: safe to call
        twice, e.g. once explicitly and once via `atexit`.
        """
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True

            for loaded in self._loaded_adapters:
                teardown = getattr(loaded.instance, "teardown", None)
                if not callable(teardown):
                    continue
                try:
                    teardown()
                except Exception:
                    # Recorded, not re-raised: one adapter's failing
                    # teardown must not stop the rest from tearing down.
                    pass

            self._discovery.shutdown(wait=False)

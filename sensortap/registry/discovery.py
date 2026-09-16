"""Concurrent discovery with a shared deadline (Req 1.4, 1.8, 1.9, 9.1-9.9).

This module owns the discovery executor mechanics only: submitting every
eligible adapter's `discover()` call to a persistent thread pool, waiting
against one absolute shared deadline so per-adapter timeouts do not
accumulate, and tracking "abandoned" workers left behind by a timeout so a
later result from one of them is never merged into a subsequent
enumeration.

It intentionally does not implement dedup, validation, ordering (registry
task for `dedup.py`), per-adapter `BackendStatus` bookkeeping (`status.py`),
or the registry's public API (`registry.py`). It also does not call into
adapter loading -- it accepts an already-loaded `adapters` sequence.

Note on device handles (Req 9.4, 9.7): `discover()` on camera, microphone
and HID adapters must not open the underlying device handle -- that is an
*adapter* responsibility (the handle opens lazily on first `read()` or
`open_stream()`). This module only runs whatever `discover()` an adapter
supplies; it does not enforce that obligation.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable, Hashable, Sequence

from sensortap.adapters.protocol import Adapter
from sensortap.registry.errors import InvalidTimeoutError
from sensortap.schema.sensor_info import SensorInfo

#: Default per-adapter Discovery_Timeout in milliseconds (Req 9.6).
#:
#: Raised from an original 2000ms after live testing against the real
#: compiled Windows helper (hwmon_bridge.py). Even with the helper
#: process pre-warmed during adapter setup() -- so the child spawn and
#: handshake cost is already paid before discovery ever runs -- a single
#: `list` round-trip over the named pipe to LibreHardwareMonitorLib on
#: real hardware took ~1.4s on its own, and that time competes with every
#: other adapter running concurrently in the same shared discovery
#: window. 2000ms was observed to time out in practice on a real machine;
#: 5000ms gives comfortable headroom while callers can still override it
#: lower or higher via the CLI's --discovery-timeout / Registry's
#: discovery_timeout_ms.
DEFAULT_DISCOVERY_TIMEOUT_MS = 5000

#: Permitted Discovery_Timeout range in milliseconds (Req 9.5, 9.8).
MIN_DISCOVERY_TIMEOUT_MS = 100
MAX_DISCOVERY_TIMEOUT_MS = 60000


def validate_discovery_timeout(timeout_ms: Any) -> int:
    """Validate a caller-supplied Discovery_Timeout.

    Raises `InvalidTimeoutError` naming the permitted range when
    `timeout_ms` is non-numeric or outside 100..60000 ms, without
    mutating any configured value (this function is pure). Returns the
    validated value as an `int` otherwise.

    Requirements: 9.5, 9.8
    """
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
        raise InvalidTimeoutError(
            supplied_value=timeout_ms,
            min_ms=MIN_DISCOVERY_TIMEOUT_MS,
            max_ms=MAX_DISCOVERY_TIMEOUT_MS,
        )
    if not (MIN_DISCOVERY_TIMEOUT_MS <= timeout_ms <= MAX_DISCOVERY_TIMEOUT_MS):
        raise InvalidTimeoutError(
            supplied_value=timeout_ms,
            min_ms=MIN_DISCOVERY_TIMEOUT_MS,
            max_ms=MAX_DISCOVERY_TIMEOUT_MS,
        )
    return int(timeout_ms)


@dataclass(frozen=True, slots=True)
class TimedOut:
    """Outcome recorded when an adapter's discovery call missed the
    shared deadline. Its future is not cancelled -- a thread blocked in a
    driver call cannot be interrupted -- and is instead tracked as
    abandoned until it eventually completes.

    Requirements: 9.2
    """

    limit_ms: int


@dataclass(frozen=True, slots=True)
class PreviousDiscoveryStillRunning:
    """Outcome recorded when an adapter is skipped for this round because
    a worker abandoned by an earlier timeout has not yet finished.

    Requirements: 9.9
    """


@dataclass(frozen=True, slots=True)
class DiscoveryError:
    """Outcome recorded when an adapter's `discover()` call raised.

    Requirements: 1.4
    """

    error: BaseException


#: One discovery outcome per adapter: either a successful list of
#: SensorInfo records, or one of the failure/abandonment markers above.
DiscoveryOutcome = "list[SensorInfo] | TimedOut | PreviousDiscoveryStillRunning | DiscoveryError"

#: Key type used to identify an adapter across rounds. Adapters are keyed
#: by `adapter.meta.adapter_id` when available (stable, declared by the
#: adapter itself per the Adapter Protocol), falling back to the adapter
#: instance itself otherwise. Using `adapter_id` rather than the instance
#: keeps abandoned-worker bookkeeping correct even if the registry were to
#: reload/replace an adapter instance between rounds.
AdapterKey = Hashable


def _adapter_key(adapter: Adapter) -> AdapterKey:
    meta = getattr(adapter, "meta", None)
    adapter_id = getattr(meta, "adapter_id", None) if meta is not None else None
    if adapter_id is not None:
        return adapter_id
    return adapter


class DiscoveryCoordinator:
    """Owns the persistent discovery thread pool and abandoned-worker
    bookkeeping across successive `run_discovery` rounds.

    Requirements: 1.4, 1.8, 1.9, 9.1, 9.2, 9.3, 9.5, 9.6, 9.8, 9.9
    """

    def __init__(self, *, max_workers: int | None = None) -> None:
        # `max_workers=None` lets ThreadPoolExecutor pick a default sized
        # for I/O-bound work; callers with many adapters may want to pass
        # an explicit value so 64 adapters do not queue behind a small pool.
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="sensortap-discovery"
        )
        self._lock = threading.Lock()
        # adapter key -> set of futures abandoned by a previous timeout,
        # not yet resolved by their done-callback.
        self._abandoned: dict[AdapterKey, set[Future]] = {}

    def shutdown(self, *, wait: bool = True) -> None:
        """Shut down the persistent executor. Not part of the per-round
        discovery contract; provided for orderly process/registry teardown."""
        self._pool.shutdown(wait=wait)

    def _is_abandoned(self, key: AdapterKey) -> bool:
        with self._lock:
            pending = self._abandoned.get(key)
            return bool(pending)

    def _mark_abandoned(self, key: AdapterKey, future: Future) -> None:
        with self._lock:
            self._abandoned.setdefault(key, set()).add(future)

    def _make_done_callback(self, key: AdapterKey, future: Future) -> Callable[[Future], None]:
        def _on_done(fut: Future) -> None:
            # Discard the late result/exception unconditionally -- it must
            # never be merged into any enumeration result. Just clear the
            # bookkeeping entry so the adapter becomes eligible again once
            # every abandoned future for it has resolved.
            with self._lock:
                pending = self._abandoned.get(key)
                if pending is not None:
                    pending.discard(fut)
                    if not pending:
                        del self._abandoned[key]

        return _on_done

    def run_discovery(
        self, adapters: Sequence[Adapter], timeout_ms: int
    ) -> dict[AdapterKey, Any]:
        """Run `discover()` on every eligible adapter under one shared
        absolute deadline, so per-adapter timeouts do not accumulate
        (Req 9.1). Returns a mapping of adapter key -> outcome, where an
        outcome is either a `list[SensorInfo]`, `TimedOut`,
        `PreviousDiscoveryStillRunning`, or `DiscoveryError`.

        `timeout_ms` must already be validated (see
        `validate_discovery_timeout`) by the caller before it reaches
        here; this method does not re-validate it.

        Requirements: 1.4, 1.8, 1.9, 9.1, 9.2, 9.3, 9.9
        """
        outcomes: dict[AdapterKey, Any] = {}
        eligible: list[tuple[AdapterKey, Adapter]] = []

        for adapter in adapters:
            key = _adapter_key(adapter)
            if self._is_abandoned(key):
                outcomes[key] = PreviousDiscoveryStillRunning()
                continue
            eligible.append((key, adapter))

        # Submit every eligible adapter first, then wait against one
        # shared absolute deadline -- this is what keeps N adapters'
        # timeouts from accumulating into N * timeout total.
        deadline = monotonic() + timeout_ms / 1000
        futures: dict[Future, AdapterKey] = {
            self._pool.submit(adapter.discover): key for key, adapter in eligible
        }

        for future, key in futures.items():
            remaining = deadline - monotonic()
            try:
                outcomes[key] = list(future.result(timeout=max(remaining, 0)))
            except FuturesTimeout:
                # Never cancel: a thread blocked in a driver call cannot be
                # interrupted. Discard the partial result and track the
                # future as abandoned until it eventually completes.
                outcomes[key] = TimedOut(limit_ms=timeout_ms)
                self._mark_abandoned(key, future)
                future.add_done_callback(self._make_done_callback(key, future))
            except Exception as exc:  # noqa: BLE001 - adapter-raised error
                outcomes[key] = DiscoveryError(error=exc)

        return outcomes

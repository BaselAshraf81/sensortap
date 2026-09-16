"""Per-adapter backend status tracking (Req 11.1-11.9).

`BackendStatus` records are maintained continuously as adapters are
loaded, enumerated, timed out, or fail introspection, rather than being
computed on demand. This lets `backend_status()` (Req 11.6) answer within
500 ms even when no enumeration has ever run: it is a plain dict lookup
with no I/O and no adapter interaction.

This module owns only the status data structure and its update API. It is
not wired into loading.py / discovery.py / the dedup pipeline yet — later
registry tasks call into `StatusTracker` as adapters are loaded,
enumerated and torn down.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

#: Maximum number of unsatisfied dependencies reported per adapter (Req 11.3).
MAX_UNSATISFIED_DEPS = 20

#: Bounds on the remediation hint string length (Req 11.2).
MIN_REMEDIATION_LENGTH = 1
MAX_REMEDIATION_LENGTH = 500

BackendState = Literal["loaded", "not_loaded", "degraded", "unknown"]
HelperState = Literal["running", "not_running", "failed_to_start"]


class NotLoadedReason(StrEnum):
    """Closed set of reasons an Adapter is `not_loaded` (Req 11.2)."""

    UNSUPPORTED_PLATFORM = "unsupported_platform"
    MISSING_DEPENDENCY = "missing_dependency"
    LOAD_ERROR = "load_error"
    ELEVATION_REQUIRED = "elevation_required"
    NOT_OPTED_IN = "not_opted_in"


@dataclass(frozen=True, slots=True)
class DependencySpec:
    """One unsatisfied dependency named in a `missing_dependency` status
    (Req 11.3).
    """

    name: str
    version_constraint: str


@dataclass(frozen=True, slots=True)
class BackendStatus:
    """One adapter's continuously-maintained availability record.

    Loaded-with-zero-sensors is `state="loaded"` with `discovered_count=0`,
    deliberately distinct from every `not_loaded` + `reason` record
    (Req 11.7): the two never collapse into the same shape because
    `reason` is only meaningful (non-None) when `state == "not_loaded"`.
    """

    adapter_id: str
    state: BackendState
    discovered_count: int = 0
    reason: NotLoadedReason | None = None
    remediation: str | None = None
    unsatisfied_deps: tuple[DependencySpec, ...] = ()
    last_timeout_ms: int | None = None
    helper_state: HelperState | None = None
    introspection_failed: bool = False
    last_enumeration_ms: float | None = None

    def __post_init__(self) -> None:
        if self.remediation is not None:
            length = len(self.remediation)
            if not (MIN_REMEDIATION_LENGTH <= length <= MAX_REMEDIATION_LENGTH):
                raise ValueError(
                    "remediation hint must be 1..500 characters, "
                    f"got {length} for adapter {self.adapter_id!r}"
                )
        if len(self.unsatisfied_deps) > MAX_UNSATISFIED_DEPS:
            raise ValueError(
                f"at most {MAX_UNSATISFIED_DEPS} unsatisfied dependencies "
                f"may be reported, got {len(self.unsatisfied_deps)} "
                f"for adapter {self.adapter_id!r}"
            )


class StatusTracker:
    """Continuously-maintained map of `adapter_id -> BackendStatus`.

    All mutating methods replace one adapter's record under a single lock
    held only for the dict update (mirrors the discovery locking strategy
    described in the design for concurrent enumerations). `snapshot()`
    performs no I/O and no adapter interaction, so it stays well under the
    500 ms bound of Req 11.6 regardless of adapter count.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, BackendStatus] = {}

    def record_loaded(self, adapter_id: str, *, discovered_count: int = 0) -> None:
        """Record `adapter_id` as `loaded`.

        Zero sensors discovered is a normal, distinct-from-not_loaded
        outcome (Req 11.7), so `discovered_count=0` is a valid default.
        """

        status = BackendStatus(
            adapter_id=adapter_id,
            state="loaded",
            discovered_count=discovered_count,
        )
        self._set(status)

    def record_not_loaded(
        self,
        adapter_id: str,
        *,
        reason: NotLoadedReason,
        remediation: str,
        unsatisfied_deps: tuple[DependencySpec, ...] = (),
    ) -> None:
        """Record `adapter_id` as `not_loaded` with exactly one reason
        (Req 11.2) and, when the reason is `missing_dependency`, up to 20
        unsatisfied dependencies (Req 11.3).
        """

        status = BackendStatus(
            adapter_id=adapter_id,
            state="not_loaded",
            discovered_count=0,
            reason=reason,
            remediation=remediation,
            unsatisfied_deps=unsatisfied_deps,
        )
        self._set(status)

    def record_degraded(
        self,
        adapter_id: str,
        *,
        discovered_count: int,
        remediation: str | None = None,
    ) -> None:
        """Record `adapter_id` as `degraded`: loaded, but with a recorded
        discovery error or partial failure.
        """

        status = BackendStatus(
            adapter_id=adapter_id,
            state="degraded",
            discovered_count=discovered_count,
            remediation=remediation,
        )
        self._set(status)

    def update_enumeration(
        self, adapter_id: str, *, discovered_count: int, duration_ms: float
    ) -> None:
        """Update `discovered_count` and `last_enumeration_ms` after an
        enumeration completes for `adapter_id` (Req 11.8).
        """

        self._update(
            adapter_id,
            discovered_count=discovered_count,
            last_enumeration_ms=duration_ms,
        )

    def update_timeout(self, adapter_id: str, *, timeout_ms: int) -> None:
        """Record the exceeded Discovery_Timeout limit for the most recent
        enumeration of `adapter_id` (Req 11.4).
        """

        self._update(adapter_id, last_timeout_ms=timeout_ms)

    def update_helper_state(self, adapter_id: str, *, helper_state: HelperState) -> None:
        """Update the Helper_Process state reported for a helper-dependent
        adapter (Req 11.5).
        """

        self._update(adapter_id, helper_state=helper_state)

    def mark_unknown(self, adapter_id: str) -> None:
        """Mark `adapter_id`'s state as `unknown` with
        `introspection_failed=True` when it cannot be determined, without
        dropping any other adapter's record (Req 11.9).
        """

        with self._lock:
            existing = self._records.get(adapter_id)
            if existing is None:
                self._records[adapter_id] = BackendStatus(
                    adapter_id=adapter_id,
                    state="unknown",
                    introspection_failed=True,
                )
            else:
                self._records[adapter_id] = replace(
                    existing, state="unknown", introspection_failed=True
                )

    def snapshot(self) -> list[BackendStatus]:
        """Return all current records. Fast and side-effect-free: no I/O,
        no adapter interaction (Req 11.6).
        """

        with self._lock:
            return list(self._records.values())

    def _set(self, status: BackendStatus) -> None:
        with self._lock:
            self._records[status.adapter_id] = status

    def _update(self, adapter_id: str, **changes: object) -> None:
        with self._lock:
            existing = self._records.get(adapter_id)
            if existing is None:
                existing = BackendStatus(adapter_id=adapter_id, state="unknown")
            self._records[adapter_id] = replace(existing, **changes)

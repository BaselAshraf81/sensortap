"""Public API re-exports only.

A single module-level `Registry` backs the convenience functions below,
created lazily on first use (rather than eagerly at import time) so that
importing `sensortap` never has the side effect of loading adapters,
running `setup()`, or spawning helper processes -- those only happen once
a caller actually asks for sensors. The singleton's `shutdown()` is
registered with `atexit` so teardown runs once per adapter, exactly once,
regardless of whether the caller ever calls it explicitly.
"""

from __future__ import annotations

import atexit
import threading
from typing import Any, Callable

from sensortap.registry.consent import AuditEvent, ConsentGrant
from sensortap.registry.routing import Registry
from sensortap.registry.streaming import Stream
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo

__all__ = [
    "Registry",
    "list_sensors",
    "read",
    "stream",
    "consent",
    "backend_status",
]

_registry: Registry | None = None
_registry_lock = threading.Lock()


def _get_registry() -> Registry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = Registry()
            atexit.register(_registry.shutdown)
        return _registry


def list_sensors(
    *, kind: str | None = None, source: str | None = None, id: str | None = None
) -> list[SensorInfo]:
    return _get_registry().list_sensors(kind=kind, source=source, id=id)


def read(sensor_id: str) -> Reading:
    return _get_registry().read(sensor_id)


def stream(
    sensor_id: str,
    *,
    block_size: int | None = None,
    rate_hz: float | None = None,
    buffer_blocks: int = 64,
) -> Stream:
    return _get_registry().stream(
        sensor_id, block_size=block_size, rate_hz=rate_hz, buffer_blocks=buffer_blocks
    )


def consent(sensor_ids: Any) -> ConsentGrant:
    return _get_registry().consent(sensor_ids)


def backend_status() -> list[Any]:
    return _get_registry().backend_status()

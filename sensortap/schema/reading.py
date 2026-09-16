"""`Reading`: a single value observation from a sensor (Req 4.3)."""

from __future__ import annotations

from dataclasses import dataclass

from sensortap.schema.enums import Status


@dataclass(frozen=True, slots=True)
class Reading:
    """One observation emitted by a sensor.

    `values` is a flat sequence whose element count must match the shape
    declared by the corresponding `SensorInfo` (last dimension varying
    fastest). Empty `values` is legal only when `status` is `unavailable`.
    """

    id: str
    t_mono: float
    t_wall: float
    values: tuple[float, ...]
    seq: int
    status: Status

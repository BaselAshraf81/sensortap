"""`SensorInfo`: the immutable description of one discovered sensor (Req 2.1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sensortap.schema.enums import Availability, Delivery, Dtype
from sensortap.schema.rate import RateSpec


@dataclass(frozen=True, slots=True)
class SensorInfo:
    """Describes one sensor discovered by an adapter.

    Frozen and slotted: `SensorInfo` records are values shared across
    threads during concurrent enumeration (Req 1.9), and immutability
    removes any question of a consumer mutating a record the registry
    still holds.
    """

    schema_version: str
    id: str
    kind: str
    dtype: Dtype
    unit: str | None
    channels: tuple[str, ...]
    shape: tuple[int, ...]
    range: tuple[float, float] | None
    resolution: float | None
    rate_hz: RateSpec
    delivery: Delivery
    derived: bool
    requires_consent: bool
    requires_elevation: bool
    source: tuple[str, ...]
    vendor: str | None
    part_number: str | None
    availability: Availability
    extra: Mapping[str, object] = field(default_factory=dict)

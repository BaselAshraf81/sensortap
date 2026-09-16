"""Sample-rate specification for a sensor (Req 2.3, 2.9)."""

from __future__ import annotations

from dataclasses import dataclass

from sensortap.registry.errors import RateOutOfRangeError


@dataclass(frozen=True, slots=True)
class RateSpec:
    """Describes the sample rate(s) a sensor supports.

    `default`, `min` and `max` are in Hz and may be `None` when unknown or
    inapplicable. `supported` lists the discrete rates the sensor can be
    configured to; an empty tuple means the sensor supports a continuous
    range between `min` and `max` rather than a fixed set of rates.
    """

    default: float | None
    min: float | None
    max: float | None
    supported: tuple[float, ...] = ()


def nearest_supported_rate(
    spec: RateSpec, requested_hz: float, *, sensor_id: str
) -> float:
    """Resolve a requested sampling rate against `spec`.

    Raises `RateOutOfRangeError` when `requested_hz` falls outside the
    interval bounded by `spec.min` and `spec.max` (Req 6.5). Otherwise, if
    `spec.supported` is non-empty and `requested_hz` is not itself a
    member, returns the nearest supported rate by absolute distance,
    resolving exact ties to the lower rate (Req 6.4). When `supported` is
    empty (a continuous range), returns `requested_hz` unchanged.
    """

    if (spec.min is not None and requested_hz < spec.min) or (
        spec.max is not None and requested_hz > spec.max
    ):
        raise RateOutOfRangeError(
            sensor_id=sensor_id,
            requested_rate_hz=requested_hz,
            min_rate_hz=spec.min,
            max_rate_hz=spec.max,
            supported_rates_hz=spec.supported,
        )

    if not spec.supported:
        return requested_hz

    best: float | None = None
    best_distance: float | None = None
    for candidate in spec.supported:
        distance = abs(candidate - requested_hz)
        if (
            best_distance is None
            or distance < best_distance
            or (distance == best_distance and candidate < best)
        ):
            best = candidate
            best_distance = distance

    assert best is not None
    return best

"""Closed value sets used across the schema.

`Dtype`, `Delivery`, `Availability` and `Status` are `enum.StrEnum` members so
that they compare and serialize as their plain string values. This gives JSON
output "for free" (Req 12.6) and guarantees identical value sets with
identical meanings across platforms by construction (Req 14.5).
"""

from __future__ import annotations

import enum


class Dtype(enum.StrEnum):
    """The shape discriminator of a sensor's values (Req 2.2, 2.4)."""

    SCALAR = "scalar"
    VECTOR3 = "vector3"
    MATRIX = "matrix"
    BUFFER = "buffer"


class Delivery(enum.StrEnum):
    """How a Backend hands values to its Adapter (Req 2.4)."""

    PUSH = "push"
    POLL = "poll"


class Availability(enum.StrEnum):
    """The availability state of a sensor reported in `SensorInfo` (Req 2.5).

    - PRESENT: the sensor is discovered and currently readable.
    - ABSENT: the sensor class is not exposed by the platform/backend.
    - UNAVAILABLE: the sensor was discovered but currently fails to read.
    - IN_USE_BY_OTHER_APP: another process holds exclusive access.
    - PERMISSION_DENIED: the operating system denied access.
    """

    PRESENT = "present"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"
    IN_USE_BY_OTHER_APP = "in_use_by_other_app"
    PERMISSION_DENIED = "permission_denied"


class Status(enum.StrEnum):
    """The status of a single `Reading` (Req 4.4)."""

    OK = "ok"
    STALE = "stale"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"

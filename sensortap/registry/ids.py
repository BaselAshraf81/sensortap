"""Sensor_Id construction toolkit (Req 3.1-3.8, 3.10).

This module is pure and stateless: it builds and validates the Sensor_Id
grammar, computes the one-way instance hash, assigns fallback indices for
backends with no persistent identifier, and resolves collisions between
distinct sensors that land on the same candidate id. It holds no registry
state and is not wired into discovery/dedup/loading — that happens in
later registry tasks.

Grammar (Req 3.1, 3.2): exactly three dot-separated segments,
`<kind>.<source-qualifier>.<instance-qualifier>`, each segment 1-64
characters drawn only from lowercase `a`-`z`, digits `0`-`9` and hyphen,
with a total length of at most 200 characters, lowercase only.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Sequence, TypeVar

from sensortap.registry.errors import MalformedSensorIdError

#: Regex for one grammar-legal segment: 1-64 chars from [a-z0-9-].
_SEGMENT_PATTERN = r"[a-z0-9-]{1,64}"

#: Regex for a full Sensor_Id: exactly three dot-separated segments, each
#: matching `_SEGMENT_PATTERN`. Total length is checked separately since a
#: regex quantifier bound on the whole string is less precise to report
#: against a max of 200 for the sum of three independently bounded parts.
SENSOR_ID_PATTERN = re.compile(
    rf"^(?P<kind>{_SEGMENT_PATTERN})\.(?P<source>{_SEGMENT_PATTERN})\."
    rf"(?P<instance>{_SEGMENT_PATTERN})$"
)

#: Maximum total length of a Sensor_Id (Req 3.1).
MAX_SENSOR_ID_LENGTH = 200

#: Maximum length of a single segment (Req 3.1).
MAX_SEGMENT_LENGTH = 64

_SEGMENT_NAMES = ("kind", "source-qualifier", "instance-qualifier")


def validate_sensor_id_grammar(sensor_id: str) -> None:
    """Validate a Sensor_Id against the grammar and length limits (Req
    3.1, 3.10).

    Raises :class:`MalformedSensorIdError` naming the first offending
    segment. No lookup of any kind is performed here or implied by this
    function; callers must invoke this before ever attempting a lookup.
    """

    if len(sensor_id) > MAX_SENSOR_ID_LENGTH:
        raise MalformedSensorIdError(
            sensor_id=sensor_id, offending_segment="<total length>"
        )

    segments = sensor_id.split(".")
    if len(segments) != 3:
        # Name the first missing/extra segment position we can identify.
        offending = _SEGMENT_NAMES[min(len(segments), 3) - 1] if segments else "kind"
        raise MalformedSensorIdError(sensor_id=sensor_id, offending_segment=offending)

    for name, segment in zip(_SEGMENT_NAMES, segments):
        if not re.fullmatch(r"[a-z0-9-]{1,64}", segment):
            raise MalformedSensorIdError(sensor_id=sensor_id, offending_segment=name)


def instance_hash(persistent_id: str) -> str:
    """Derive the instance qualifier from a persistent hardware identifier
    (Req 3.5).

    Uses BLAKE2b with an 8-byte digest, producing exactly 16 lowercase hex
    characters. The digest is unsalted and unkeyed by design: this is what
    keeps ids stable and reproducible across runs and across shared bug
    reports (Req 3.3), at the deliberate cost that a local attacker who
    already has a candidate persistent id could confirm it by hashing.
    """

    digest = hashlib.blake2b(persistent_id.encode("utf-8"), digest_size=8)
    hex_digest = digest.hexdigest()
    assert len(hex_digest) == 16
    assert hex_digest == hex_digest.lower()
    return hex_digest


_T = TypeVar("_T")


def fallback_index(
    items: Sequence[_T], sort_key: Callable[[_T], object]
) -> dict[int, _T]:
    """Assign zero-based fallback indices to items with no persistent
    hardware identifier (Req 3.6).

    `sort_key` must be an adapter-declared function of only run-invariant
    attributes (e.g. `(chip_name, attribute_index)` for hwmon, never a
    bus-probe-order-dependent identifier). Sorting on that key is what
    makes the assigned index stable across runs.

    Returns a mapping from assigned index to the original item, in sorted
    order.
    """

    ordered = sorted(items, key=sort_key)
    return {index: item for index, item in enumerate(ordered)}


@dataclass(frozen=True, slots=True)
class CollisionWarning:
    """Reports that two or more distinct sensors collided on one
    candidate Sensor_Id (Req 3.8).

    This is distinct from a dedup conflict (two adapters describing one
    sensor): here every affected sensor is retained, disambiguated with a
    suffix.
    """

    sensor_id: str
    affected_count: int

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"sensor id {self.sensor_id!r} collided across "
            f"{self.affected_count} distinct sensors; disambiguation "
            "suffixes were applied"
        )


def resolve_collisions(
    candidate_ids: Sequence[str],
) -> tuple[list[str], list[CollisionWarning]]:
    """Resolve collisions between distinct sensors sharing a candidate
    Sensor_Id (Req 3.8).

    `candidate_ids` must already be in deterministic order (e.g. the
    registry's load-order/id ordering). The first occurrence of a
    colliding id keeps the bare id; each subsequent occurrence gets a
    zero-based disambiguation suffix (`-0`, `-1`, ...) appended to its
    instance segment. Every sensor is retained; none are dropped.

    Returns the resolved list (same length and order as `candidate_ids`)
    plus one :class:`CollisionWarning` per colliding id, naming the id and
    the count of sensors affected (including the one that kept the bare
    id).
    """

    seen_counts: dict[str, int] = {}
    total_counts: dict[str, int] = {}
    for candidate_id in candidate_ids:
        total_counts[candidate_id] = total_counts.get(candidate_id, 0) + 1

    resolved: list[str] = []
    for candidate_id in candidate_ids:
        occurrence = seen_counts.get(candidate_id, 0)
        seen_counts[candidate_id] = occurrence + 1

        if total_counts[candidate_id] == 1:
            resolved.append(candidate_id)
            continue

        if occurrence == 0:
            resolved.append(candidate_id)
        else:
            suffix = f"-{occurrence - 1}"
            kind, source, instance = candidate_id.split(".")
            new_instance = instance[: MAX_SEGMENT_LENGTH - len(suffix)] + suffix
            new_id = f"{kind}.{source}.{new_instance}"
            # A 16-char hash (or a small fallback index) plus a short
            # numeric suffix cannot exceed 64 chars or 200 total, per the
            # design's stated invariant, but truncation above keeps the
            # segment bound even if that invariant is ever violated.
            resolved.append(new_id)

    warnings = [
        CollisionWarning(sensor_id=sensor_id, affected_count=count)
        for sensor_id, count in total_counts.items()
        if count > 1
    ]

    return resolved, warnings

"""The post-collection pipeline: validate, dedup, resolve conflicts,
disambiguate collisions, order, and filter (Req 1.1, 1.2, 1.3, 1.6, 1.7,
2.10, 3.7, 3.8, 10.10).

Six steps, in order (see design.md "Validation, dedup, conflict
resolution, ordering"):

1. Validate every record. Invalid records are dropped individually; the
   rest of that adapter's records survive (Req 2.10).
2. Dedup on byte-for-byte `id` equality only -- no fuzzy matching.
3. Resolve conflicts by declared `priority` among records from
   *different* adapters sharing an `id` (the "two adapters describing
   one physical sensor" case). The winner's `source` lists every
   reporting adapter in load order; a `ConflictEntry` names the id and
   every competing adapter.
4. Assign collision suffixes for records that land on the same `id` but
   are *not* a cross-adapter same-sensor report -- concretely, when one
   adapter itself emits two or more records sharing an `id` for what are
   actually distinct physical sensors (Req 3.7, 3.8). This is the
   `resolve_collisions` case from `registry.ids`, and it is deliberately
   distinct from step 3: step 3 merges reports of one sensor; step 4
   disambiguates genuinely different sensors that collided on one
   string.
5. Order by adapter load order (the winning/owning adapter for a deduped
   record), then ascending `id` within that adapter (Req 1.7).
6. Filter on `kind`, `source`, `id` -- conjunctive, exact equality,
   empty list when nothing matches (Req 1.6). `source` matches when the
   filter value is present anywhere in the record's `source` tuple,
   since a record's `source` may list several reporting adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Sequence

from sensortap.registry.errors import SchemaVersionError, ValidationError
from sensortap.registry.ids import CollisionWarning, resolve_collisions
from sensortap.registry.loading import LoadedAdapter
from sensortap.schema.sensor_info import SensorInfo
from sensortap.schema.validate import validate_sensor_info


@dataclass(frozen=True, slots=True)
class ConflictEntry:
    """Records that two or more *different* adapters reported the same
    `id` for what is treated as one physical sensor (Req 10.10).

    `competitors` lists every reporting adapter identity, in load order,
    including the winner.
    """

    id: str
    winning_adapter: str
    competitors: tuple[str, ...]


@dataclass(slots=True)
class PipelineResult:
    """The bundled outcome of :func:`run_pipeline`."""

    sensors: list[SensorInfo] = field(default_factory=list)
    validation_errors: list[ValidationError | SchemaVersionError] = field(default_factory=list)
    conflicts: list[ConflictEntry] = field(default_factory=list)
    collision_warnings: list[CollisionWarning] = field(default_factory=list)


def _adapter_priority(loaded_adapters: Sequence[LoadedAdapter], adapter_id: str) -> int | None:
    for loaded in loaded_adapters:
        if loaded.instance.meta.adapter_id == adapter_id:
            return loaded.instance.meta.priority
    return None


def _priority_sort_key(priority: int | None) -> tuple[int, int]:
    """Undeclared priority (`None`) sorts lowest (Req 10.10)."""
    return (0, 0) if priority is None else (1, priority)


def run_pipeline(
    raw_results: dict[str, list[SensorInfo]],
    loaded_adapters: Sequence[LoadedAdapter],
    *,
    kind: str | None = None,
    source: str | None = None,
    id: str | None = None,
) -> PipelineResult:
    """Run the full post-collection pipeline.

    `raw_results` maps each loaded adapter's identity (`meta.adapter_id`)
    to the list of `SensorInfo` records its `discover()` call returned.
    `loaded_adapters` supplies both the load order (its sequence order)
    and each adapter's declared `priority`, via `instance.meta`.
    """

    result = PipelineResult()
    load_order: list[str] = [loaded.instance.meta.adapter_id for loaded in loaded_adapters]
    order_index: dict[str, int] = {adapter_id: i for i, adapter_id in enumerate(load_order)}

    # --- Step 1: validate every record; drop invalid ones individually ---
    # Each entry: (global_sequence, adapter_id, record)
    validated: list[tuple[int, str, SensorInfo]] = []
    seq = 0
    for adapter_id in load_order:
        for record in raw_results.get(adapter_id, []):
            violations = validate_sensor_info(record, adapter_id=adapter_id)
            if violations:
                result.validation_errors.extend(violations)
                continue
            validated.append((seq, adapter_id, record))
            seq += 1
    # Records from adapters not present in load_order (defensive: ignore,
    # since they have no defined position/priority to resolve against).

    # --- Step 2/3/4 prep: group by exact id ---
    groups: dict[str, list[tuple[int, str, SensorInfo]]] = {}
    for entry in validated:
        groups.setdefault(entry[2].id, []).append(entry)

    # candidates: one entry per surviving record after dedup/conflict
    # resolution, each tagged with the adapter it should be ordered under
    # (the winning adapter for a merged record, or its own adapter
    # otherwise) and its earliest sequence number for stable ordering.
    candidates: list[tuple[int, str, SensorInfo]] = []

    for shared_id, entries in groups.items():
        adapters_seen: dict[str, list[tuple[int, str, SensorInfo]]] = {}
        for entry in entries:
            adapters_seen.setdefault(entry[1], []).append(entry)

        if len(adapters_seen) == 1:
            # Only one adapter reports this id. If it reported it more
            # than once, these are distinct sensors that happened to
            # collide within one adapter (Req 3.7/3.8) -- no conflict
            # resolution applies (there is nothing to arbitrate between
            # different adapters), each passes through as its own
            # candidate and step 4 will disambiguate the ids.
            for entry in entries:
                candidates.append(entry)
            continue

        # Two or more *different* adapters reported this id: this is the
        # "same physical sensor, multiple adapters" case (Req 1.2, 1.3,
        # 10.10). Resolve by highest declared priority among one
        # representative record per adapter (the first that adapter
        # contributed for this id); ties go to the first-loaded adapter.
        representatives: list[tuple[int, str, SensorInfo]] = [
            recs[0] for recs in adapters_seen.values()
        ]
        representatives.sort(key=lambda e: order_index.get(e[1], len(load_order)))

        winner_entry = max(
            representatives,
            key=lambda e: _priority_sort_key(_adapter_priority(loaded_adapters, e[1])),
        )
        # `max` returns the first maximal element for equal keys given a
        # stable sort input, so ties already resolve to the first-loaded
        # adapter because `representatives` is sorted by load order.
        winner_seq, winner_adapter, winner_record = winner_entry

        competitor_adapters = tuple(
            adapter_id for adapter_id in load_order if adapter_id in adapters_seen
        )
        merged_record = replace(winner_record, source=competitor_adapters)
        result.conflicts.append(
            ConflictEntry(
                id=shared_id,
                winning_adapter=winner_adapter,
                competitors=competitor_adapters,
            )
        )
        candidates.append((winner_seq, winner_adapter, merged_record))

        # Any additional records an adapter contributed for this id beyond
        # its first are within-adapter collisions on distinct sensors
        # (Req 3.7/3.8): they are not merged into the cross-adapter
        # conflict resolution above, and pass through as their own
        # candidates so step 4 disambiguates them.
        for adapter_id, recs in adapters_seen.items():
            for extra_entry in recs[1:]:
                candidates.append(extra_entry)

    # Stable order for feeding the collision resolver: by earliest
    # sequence number, which reflects load order and within-adapter order.
    candidates.sort(key=lambda e: e[0])

    # --- Step 4: collision suffixes for distinct sensors sharing an id ---
    candidate_ids = [entry[2].id for entry in candidates]
    resolved_ids, collision_warnings = resolve_collisions(candidate_ids)
    result.collision_warnings.extend(collision_warnings)

    resolved_candidates: list[tuple[str, SensorInfo]] = []
    for (_, owning_adapter, record), resolved_id in zip(candidates, resolved_ids):
        if resolved_id != record.id:
            record = replace(record, id=resolved_id)
        resolved_candidates.append((owning_adapter, record))

    # --- Step 5: order by adapter load order, then ascending id ---
    resolved_candidates.sort(
        key=lambda pair: (order_index.get(pair[0], len(load_order)), pair[1].id)
    )

    # --- Step 6: filter on kind/source/id, conjunctive, exact equality ---
    final: list[SensorInfo] = []
    for _, record in resolved_candidates:
        if kind is not None and record.kind != kind:
            continue
        if source is not None and source not in record.source:
            continue
        if id is not None and record.id != id:
            continue
        final.append(record)

    result.sensors = final
    return result

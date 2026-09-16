"""JSON output formatting for the CLI's ``--json`` option (Req 2.14, 12.6).

Kept as a separate module from the table formatter (task 10.2) so that
padding, colour and grouping logic built for human-readable output cannot
leak into machine-readable output. This module produces plain,
schema-conforming JSON: no colour, no grouping, no column padding, no
summary line.

Output is compact (``json.dumps`` with default separators, no ``indent``)
since this output is meant for machine consumption (piping to ``jq`` or
another program) rather than direct human reading -- a human who wants a
readable rendering should omit ``--json`` and get the table formatter's
output instead.
"""

from __future__ import annotations

import json

from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo


def _rate_hz_to_dict(rate_hz: RateSpec) -> dict:
    return {
        "default": rate_hz.default,
        "min": rate_hz.min,
        "max": rate_hz.max,
        "supported": list(rate_hz.supported),
    }


def sensor_info_to_dict(info: SensorInfo) -> dict:
    """Serialize a `SensorInfo` record to a plain, JSON-ready dict.

    All 18 required fields are included. Enum fields (`dtype`, `delivery`,
    `availability`) serialize as their plain string value (Req 2.14 note:
    these are `StrEnum` members, so `.value` and `str(...)` agree, but
    `.value` is used explicitly to avoid ever depending on `__str__`).
    `rate_hz` serializes as a nested object; `range` as a 2-element array
    or `null`. `extra`'s keys are flattened directly at the top level of
    the resulting dict, per Req 2.14, rather than nested under an
    `"extra"` key.
    """

    result = {
        "schema_version": info.schema_version,
        "id": info.id,
        "kind": info.kind,
        "dtype": info.dtype.value,
        "unit": info.unit,
        "channels": list(info.channels),
        "shape": list(info.shape),
        "range": list(info.range) if info.range is not None else None,
        "resolution": info.resolution,
        "rate_hz": _rate_hz_to_dict(info.rate_hz),
        "delivery": info.delivery.value,
        "derived": info.derived,
        "requires_consent": info.requires_consent,
        "requires_elevation": info.requires_elevation,
        "source": list(info.source),
        "vendor": info.vendor,
        "part_number": info.part_number,
        "availability": info.availability.value,
    }
    result.update(info.extra)
    return result


def reading_to_dict(reading: Reading) -> dict:
    """Serialize a `Reading` record to a plain, JSON-ready dict."""

    return {
        "id": reading.id,
        "t_mono": reading.t_mono,
        "t_wall": reading.t_wall,
        "values": list(reading.values),
        "seq": reading.seq,
        "status": reading.status.value,
    }


def format_sensor_list_json(sensors: list[SensorInfo]) -> str:
    """Format a full sensor enumeration as a compact JSON array.

    No colour, no grouping, no column padding, no summary line (Req 12.6)
    -- just the flat, complete array of sensor records.
    """

    return json.dumps([sensor_info_to_dict(s) for s in sensors])


def format_reading_json(reading: Reading) -> str:
    """Format a single `Reading` as compact, schema-conforming JSON."""

    return json.dumps(reading_to_dict(reading))


def format_sensor_info_json(info: SensorInfo) -> str:
    """Format a single `SensorInfo` record as compact, schema-conforming JSON."""

    return json.dumps(sensor_info_to_dict(info))


def backend_status_to_dict(status: object) -> dict:
    """Serialize one `BackendStatus` record to a plain, JSON-ready dict.

    Accepts the `BackendStatus` dataclass (`registry/status.py`) by duck
    typing rather than importing it, avoiding a dependency from the CLI's
    JSON formatter module onto the registry's status module for what is
    otherwise a self-contained field-list.
    """

    reason = getattr(status, "reason", None)
    unsatisfied_deps = getattr(status, "unsatisfied_deps", ())
    return {
        "adapter_id": status.adapter_id,
        "state": status.state,
        "discovered_count": status.discovered_count,
        "reason": reason.value if reason is not None else None,
        "remediation": getattr(status, "remediation", None),
        "unsatisfied_deps": [
            {"name": d.name, "version_constraint": d.version_constraint}
            for d in unsatisfied_deps
        ],
        "last_timeout_ms": getattr(status, "last_timeout_ms", None),
        "helper_state": getattr(status, "helper_state", None),
        "introspection_failed": getattr(status, "introspection_failed", False),
        "last_enumeration_ms": getattr(status, "last_enumeration_ms", None),
    }


def format_list_result_json(
    sensors: list[SensorInfo], backend_status: list[object], summary: dict
) -> str:
    """Format the `list` command's richer JSON shape: the sensor array,
    backend status records, and the summary object, all under one
    top-level object (Req 12.7).

    Kept separate from `format_sensor_list_json`, which stays a bare
    array for any other caller that just wants the flat sensor list.
    """

    return json.dumps(
        {
            "sensors": [sensor_info_to_dict(s) for s in sensors],
            "backend_status": [backend_status_to_dict(s) for s in backend_status],
            "summary": summary,
        }
    )

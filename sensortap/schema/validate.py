"""Error-collecting validation for `SensorInfo` and `Reading` (Req 2, 4).

`validate_sensor_info` and `validate_reading` never raise. They return a
list of violations -- `ValidationError` for ordinary field violations and
`SchemaVersionError` for a MAJOR schema-version mismatch, which Req 2.13
requires to be reported as a distinct violation type rather than a generic
field violation. This is what lets the Registry exclude one bad record
while retaining the rest of an adapter's results (Req 2.10).
"""

from __future__ import annotations

import math

from sensortap.registry.errors import SchemaVersionError, ValidationError
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.kinds import KIND_VOCABULARY
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo
from sensortap.schema.version import (
    SCHEMA_VERSION,
    MalformedSchemaVersionError,
    is_compatible,
    parse_major,
)

#: The 18 required fields of `SensorInfo` (Req 2.1). `extra` is excluded:
#: it is the namespaced-field bag, not a required field itself.
_REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "id",
    "kind",
    "dtype",
    "unit",
    "channels",
    "shape",
    "range",
    "resolution",
    "rate_hz",
    "delivery",
    "derived",
    "requires_consent",
    "requires_elevation",
    "source",
    "vendor",
    "part_number",
    "availability",
)

#: Required fields that may be null (Req 2.9).
_NULLABLE_FIELDS: frozenset[str] = frozenset(
    {"unit", "range", "resolution", "vendor", "part_number"}
)

_RATE_MIN_HZ = 0.001
#: Raised from 10000.0: real audio capture reports its actual device
#: sample rate here (44100/48000 Hz WASAPI defaults, both above the old
#: bound), which silently failed validation and dropped every microphone
#: sensor from the registry's output on every machine, never surfacing as
#: an error anywhere a caller could see it (Req 2.10 drops invalid records
#: without raising). 192000 Hz covers WASAPI's common high-res audio rates
#: with headroom; nothing else in the shipped adapters reports a rate
#: anywhere near this high.
_RATE_MAX_HZ = 192000.0
_SHAPE_DIM_MIN = 1
_SHAPE_DIM_MAX = 1_048_576
_SHAPE_LEN_MIN = 1
_SHAPE_LEN_MAX = 4
_CHANNEL_NAME_MAX_LEN = 64
_ID_MAX_LEN = 200
_UNIT_MAX_LEN = 32


def validate_sensor_info(
    record: SensorInfo, *, adapter_id: str
) -> list[ValidationError | SchemaVersionError]:
    """Validate `record` and return every violation found.

    Never raises. Returns an empty list when `record` is fully valid.
    """
    violations: list[ValidationError | SchemaVersionError] = []
    record_id = record.id if isinstance(record.id, str) and record.id else None

    def add(field_name: str, detail: str) -> None:
        violations.append(
            ValidationError(
                adapter_id=adapter_id,
                record_id=record_id,
                field=field_name,
                detail=detail,
            )
        )

    # --- required-field presence: null permitted only on the nullable set (Req 2.9) ---
    for field_name in _REQUIRED_FIELDS:
        value = getattr(record, field_name)
        if value is None and field_name not in _NULLABLE_FIELDS:
            add(field_name, "required field is null")

    # --- id: non-empty, <=200 chars (Req 2.1) ---
    if not isinstance(record.id, str) or not record.id:
        add("id", "id must be a non-empty string")
    elif len(record.id) > _ID_MAX_LEN:
        add("id", f"id length {len(record.id)} exceeds {_ID_MAX_LEN} characters")

    # --- unit: <=32 chars, non-empty when non-null (Req 2.1) ---
    if record.unit is not None:
        if not isinstance(record.unit, str) or not record.unit:
            add("unit", "unit must be a non-empty string when not null")
        elif len(record.unit) > _UNIT_MAX_LEN:
            add("unit", f"unit length {len(record.unit)} exceeds {_UNIT_MAX_LEN} characters")

    # --- dtype: closed set (Req 2.2). Enforced by type in practice, but guard anyway. ---
    dtype = record.dtype
    if not isinstance(dtype, Dtype):
        add("dtype", f"dtype {dtype!r} is not a member of Dtype")
        dtype = None

    # --- shape: 1..4 ints, each 1..1048576, agreeing with dtype (Req 2.6) ---
    shape = record.shape
    shape_ok = True
    if not isinstance(shape, (tuple, list)) or not (
        _SHAPE_LEN_MIN <= len(shape) <= _SHAPE_LEN_MAX
    ):
        add(
            "shape",
            f"shape must be a sequence of {_SHAPE_LEN_MIN}..{_SHAPE_LEN_MAX} integers",
        )
        shape_ok = False
    else:
        for dim in shape:
            if not isinstance(dim, int) or isinstance(dim, bool) or not (
                _SHAPE_DIM_MIN <= dim <= _SHAPE_DIM_MAX
            ):
                add(
                    "shape",
                    f"shape dimension {dim!r} outside "
                    f"[{_SHAPE_DIM_MIN}, {_SHAPE_DIM_MAX}]",
                )
                shape_ok = False
                break

    if dtype is not None and shape_ok:
        shape_t = tuple(shape)
        if dtype == Dtype.SCALAR and shape_t != (1,):
            add("shape", "dtype scalar requires shape [1]")
        elif dtype == Dtype.VECTOR3 and shape_t != (3,):
            add("shape", "dtype vector3 requires shape [3]")
        elif dtype == Dtype.MATRIX and len(shape_t) != 2:
            add("shape", "dtype matrix requires exactly 2 integers")
        elif dtype == Dtype.BUFFER and len(shape_t) not in (1, 2):
            add("shape", "dtype buffer requires 1 or 2 integers")

    # --- channels: non-empty strings <=64 chars, count rule (Req 2.7) ---
    channels = record.channels
    if not isinstance(channels, (tuple, list)):
        add("channels", "channels must be a sequence of strings")
    else:
        for name in channels:
            if not isinstance(name, str) or not name:
                add("channels", f"channel name {name!r} must be a non-empty string")
                break
            if len(name) > _CHANNEL_NAME_MAX_LEN:
                add(
                    "channels",
                    f"channel name {name!r} exceeds {_CHANNEL_NAME_MAX_LEN} characters",
                )
                break
        if shape_ok:
            shape_t = tuple(shape)
            expected_count = shape_t[-1] if len(shape_t) == 2 else (
                shape_t[0] if shape_t else None
            )
            if expected_count is not None and len(channels) != expected_count:
                add(
                    "channels",
                    f"channel count {len(channels)} does not match expected "
                    f"count {expected_count} from shape {shape_t}",
                )

    # --- range: min <= max (Req 2.8) ---
    range_ = record.range
    if range_ is not None:
        if (
            not isinstance(range_, (tuple, list))
            or len(range_) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in range_)
        ):
            add("range", "range must be a (min, max) numeric pair")
            range_ = None
        elif range_[0] > range_[1]:
            add("range", f"range min {range_[0]} exceeds max {range_[1]}")

    # --- resolution: >0 and <= (max - min) when not null (Req 2.8) ---
    resolution = record.resolution
    if resolution is not None:
        if not isinstance(resolution, (int, float)) or isinstance(resolution, bool):
            add("resolution", "resolution must be numeric")
        elif resolution <= 0:
            add("resolution", f"resolution {resolution} must be > 0")
        elif range_ is not None and range_[0] <= range_[1]:
            span = range_[1] - range_[0]
            if resolution > span:
                add(
                    "resolution",
                    f"resolution {resolution} exceeds range span {span}",
                )

    # --- rate_hz: bounds and ordering (Req 2.3) ---
    rate = record.rate_hz
    if rate is None:
        add("rate_hz", "rate_hz is required")
    else:
        for bound_name in ("default", "min", "max"):
            bound_value = getattr(rate, bound_name, None)
            if bound_value is not None and not (
                _RATE_MIN_HZ <= bound_value <= _RATE_MAX_HZ
            ):
                add(
                    "rate_hz",
                    f"rate_hz.{bound_name} {bound_value} outside "
                    f"[{_RATE_MIN_HZ}, {_RATE_MAX_HZ}]",
                )
        if (
            rate.min is not None
            and rate.default is not None
            and rate.max is not None
            and not (rate.min <= rate.default <= rate.max)
        ):
            add(
                "rate_hz",
                f"rate_hz ordering violated: min={rate.min} default={rate.default} "
                f"max={rate.max}",
            )
        supported = getattr(rate, "supported", ())
        if supported:
            for value in supported:
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not (
                    _RATE_MIN_HZ <= value <= _RATE_MAX_HZ
                ):
                    add(
                        "rate_hz",
                        f"rate_hz.supported value {value!r} outside "
                        f"[{_RATE_MIN_HZ}, {_RATE_MAX_HZ}]",
                    )
                    break

    # --- delivery: closed set (Req 2.4) ---
    if not isinstance(record.delivery, Delivery):
        add("delivery", f"delivery {record.delivery!r} is not a member of Delivery")

    # --- availability: closed set (Req 2.5) ---
    if not isinstance(record.availability, Availability):
        add(
            "availability",
            f"availability {record.availability!r} is not a member of Availability",
        )

    # --- kind: closed vocabulary (Req 2.12) ---
    if record.kind not in KIND_VOCABULARY:
        add("kind", f"kind {record.kind!r} is not in the closed kind vocabulary")

    # --- schema_version: MAJOR.MINOR form; MAJOR mismatch is a distinct violation (Req 2.13) ---
    schema_version = record.schema_version
    if not isinstance(schema_version, str):
        add("schema_version", "schema_version must be a string")
    else:
        try:
            parse_major(schema_version)
        except MalformedSchemaVersionError as exc:
            add("schema_version", str(exc))
        else:
            if not is_compatible(schema_version, against=SCHEMA_VERSION):
                violations.append(
                    SchemaVersionError(
                        adapter_id=adapter_id,
                        record_id=record_id,
                        declared_version=schema_version,
                        expected_major=str(parse_major(SCHEMA_VERSION)),
                    )
                )

    # --- extra: any namespaced key valid; collision with a required field name is a violation (Req 2.14) ---
    extra = record.extra
    if extra:
        for key in extra:
            if key in _REQUIRED_FIELDS:
                add(
                    "extra",
                    f"extra key {key!r} collides with a required field name",
                )

    return violations


def validate_reading(
    reading: Reading, info: SensorInfo
) -> list[ValidationError]:
    """Validate `reading` against the shape declared by `info`.

    Enforces `len(values) == prod(info.shape)`, with empty `values` legal
    only when `status` is `unavailable` (Req 4.5, 4.9, 4.11).
    """
    violations: list[ValidationError] = []
    record_id = reading.id if isinstance(reading.id, str) and reading.id else None

    def add(field_name: str, detail: str) -> None:
        violations.append(
            ValidationError(
                adapter_id=info.id if isinstance(info.id, str) else "<unknown>",
                record_id=record_id,
                field=field_name,
                detail=detail,
            )
        )

    if not isinstance(reading.status, Status):
        add("status", f"status {reading.status!r} is not a member of Status")

    values = reading.values
    values_len = len(values) if values is not None else 0

    if values_len == 0:
        if reading.status != Status.UNAVAILABLE:
            add(
                "values",
                "empty values is only legal when status is 'unavailable'",
            )
    else:
        expected = math.prod(info.shape) if info.shape else 0
        if values_len != expected:
            add(
                "values",
                f"values length {values_len} does not match product of shape "
                f"{tuple(info.shape)} ({expected})",
            )

    return violations

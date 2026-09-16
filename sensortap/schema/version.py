"""Schema version and MAJOR-only compatibility comparison (Req 2.13).

Each adapter stamps `schema_version` on the records it emits. The registry
compares MAJOR only: an equal MAJOR is accepted regardless of MINOR (forward
and backward compatible); a differing MAJOR excludes the record with a
version-incompatibility error naming the adapter and record. MINOR is
advisory metadata for diagnostics only.
"""

from __future__ import annotations

#: The schema version stamped onto records emitted by adapters shipped with
#: sensortap.
SCHEMA_VERSION = "1.0"


class MalformedSchemaVersionError(ValueError):
    """Raised when a `schema_version` string is not `MAJOR.MINOR`."""


def parse_major(schema_version: str) -> int:
    """Extract the MAJOR component from a `MAJOR.MINOR` version string.

    Raises `MalformedSchemaVersionError` if `schema_version` is not exactly
    two dot-separated non-negative integers.
    """
    parts = schema_version.split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise MalformedSchemaVersionError(
            f"schema_version {schema_version!r} is not of the form MAJOR.MINOR"
        )
    return int(parts[0])


def is_compatible(schema_version: str, *, against: str = SCHEMA_VERSION) -> bool:
    """Return True when `schema_version` has the same MAJOR as `against`.

    MINOR differences never affect compatibility (Req 2.13): a record whose
    schema_version differs only in MINOR is accepted.
    """
    return parse_major(schema_version) == parse_major(against)

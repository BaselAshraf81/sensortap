"""Exception hierarchy for sensortap.

One hierarchy rooted at :class:`SensortapError`, so a consumer can catch
everything with one clause. Each concrete class carries the structured
fields the CLI (and any other caller) needs to build its message and pick
an exit code, without parsing strings.

`ValidationError` and `SchemaVersionError` are special: the Registry never
raises them to a caller. It only constructs them and records them (e.g. in
per-adapter status / diagnostics) while excluding the offending record and
continuing with the rest (Req 2.10, 2.13).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


class SensortapError(Exception):
    """Base class for every error raised by sensortap."""


@dataclass(eq=False)
class UnknownSensorError(SensortapError):
    """Raised when a caller requests a Sensor_Id absent from the current
    enumeration.

    Requirements: 3.9, 4.2
    """

    sensor_id: str
    last_known_availability: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.last_known_availability is not None:
            return (
                f"unknown sensor id {self.sensor_id!r} "
                f"(last known availability: {self.last_known_availability})"
            )
        return f"unknown sensor id {self.sensor_id!r} (no recorded availability)"


@dataclass(eq=False)
class MalformedSensorIdError(SensortapError):
    """Raised when a requested Sensor_Id violates the id grammar or length
    limits. No lookup is performed.

    Requirements: 3.10
    """

    sensor_id: str
    offending_segment: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"malformed sensor id {self.sensor_id!r}: "
            f"offending segment {self.offending_segment!r}"
        )


@dataclass(eq=False)
class BlockPathRequiredError(SensortapError):
    """Raised when a caller requests a single read of a `buffer` dtype
    sensor, which must instead be read via the Block/Stream path.

    Requirements: 4.12
    """

    sensor_id: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"sensor {self.sensor_id!r} is a buffer sensor; "
            "use the Block/stream path instead of a single read"
        )


@dataclass(eq=False)
class BlockSizeOutOfRangeError(SensortapError):
    """Raised when a caller requests a Block size outside the range the
    Backend supports. No Backend resources are allocated.

    Requirements: 5.6
    """

    sensor_id: str
    requested_block_size: int
    min_block_size: int
    max_block_size: int

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"block size {self.requested_block_size} for sensor "
            f"{self.sensor_id!r} is outside the supported range "
            f"[{self.min_block_size}, {self.max_block_size}]"
        )


@dataclass(eq=False)
class ClosedStreamError(SensortapError):
    """Raised when a Block request is made against a Stream that has
    already been closed.

    Requirements: 5.7
    """

    sensor_id: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"stream for sensor {self.sensor_id!r} is closed"


@dataclass(eq=False)
class StreamBusyError(SensortapError):
    """Raised when a Backend permits only one concurrent Stream for a
    Sensor_Id and a second Stream is requested. The existing Stream is
    left open and unaffected.

    Requirements: 5.11
    """

    sensor_id: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"a stream is already open for sensor {self.sensor_id!r}"


@dataclass(eq=False)
class RateOutOfRangeError(SensortapError):
    """Raised when a caller requests a sampling rate outside the interval
    bounded by `min` and `max`.

    Requirements: 6.5
    """

    sensor_id: str
    requested_rate_hz: float
    min_rate_hz: float | None
    max_rate_hz: float | None
    supported_rates_hz: Sequence[float] = field(default_factory=tuple)

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = (
            f"requested rate {self.requested_rate_hz} Hz for sensor "
            f"{self.sensor_id!r} is outside the supported interval "
            f"[{self.min_rate_hz}, {self.max_rate_hz}]"
        )
        if self.supported_rates_hz:
            base += f"; supported rates: {list(self.supported_rates_hz)}"
        return base


@dataclass(eq=False)
class RateFixedError(SensortapError):
    """Raised when a caller requests rate configuration on a sensor whose
    Backend supports no rate configuration.

    Requirements: 6.7
    """

    sensor_id: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"sensor {self.sensor_id!r} has a fixed sampling rate"


@dataclass(eq=False)
class RateConflictError(SensortapError):
    """Raised when a caller requests a rate change on an already-open
    Stream, or a second Stream on the same Sensor_Id requests a rate
    different from the rate applied to the open Stream. The applied rate
    is left unchanged.

    Requirements: 6.8
    """

    sensor_id: str
    applied_rate_hz: float | None
    requested_rate_hz: float

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"rate conflict for sensor {self.sensor_id!r}: "
            f"applied rate is {self.applied_rate_hz} Hz, "
            f"requested {self.requested_rate_hz} Hz"
        )


@dataclass(eq=False)
class InvalidTimeoutError(SensortapError):
    """Raised when a caller supplies a Discovery_Timeout that is
    non-numeric or outside the permitted range. The configured timeout is
    left unchanged.

    Requirements: 9.8
    """

    supplied_value: object
    min_ms: int = 100
    max_ms: int = 60000

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"invalid discovery timeout {self.supplied_value!r}; "
            f"must be numeric and within [{self.min_ms}, {self.max_ms}] ms"
        )


@dataclass(eq=False)
class UnsupportedOperationError(SensortapError):
    """Raised when a caller requests an optional Adapter operation that
    the owning Adapter did not implement.

    Requirements: 10.3
    """

    sensor_id: str
    operation: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"operation {self.operation!r} is not supported for sensor "
            f"{self.sensor_id!r}"
        )


@dataclass(eq=False)
class UnsupportedConfigKeyError(SensortapError):
    """Raised when a caller supplies an acquisition-configuration key other
    than sampling rate or block size. The existing configuration is left
    unchanged.

    Requirements: 15.2
    """

    sensor_id: str
    key: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"unsupported configuration key {self.key!r} for sensor "
            f"{self.sensor_id!r}"
        )


@dataclass(eq=False)
class ConsentError(SensortapError):
    """Raised when a caller requests a read or Stream of a
    Privacy_Sensitive_Sensor without a current Consent_Gate grant for that
    Sensor_Id. The underlying device is left closed.

    Requirements: 7.3
    """

    sensor_id: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"no consent grant for sensor {self.sensor_id!r}"


@dataclass(eq=False)
class InvalidConsentRequestError(SensortapError):
    """Raised when a caller requests a Consent_Gate grant using a
    wildcard, a pattern, or an empty Sensor_Id set. No grant is created.

    Requirements: 7.7
    """

    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"invalid consent request: {self.reason}"


@dataclass(eq=False)
class SensorUnavailableError(SensortapError):
    """Raised when a sensor is discovered but cannot currently be opened
    or read (e.g. a device-open failure)."""

    sensor_id: str
    reason: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.reason:
            return f"sensor {self.sensor_id!r} is unavailable: {self.reason}"
        return f"sensor {self.sensor_id!r} is unavailable"


@dataclass(eq=False)
class DeviceOpenError(SensortapError):
    """Raised when the underlying device handle for a sensor fails to
    open."""

    sensor_id: str
    reason: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.reason:
            return f"failed to open device for sensor {self.sensor_id!r}: {self.reason}"
        return f"failed to open device for sensor {self.sensor_id!r}"


@dataclass(eq=False)
class ReadTimeoutError(SensortapError):
    """Raised when a poll-delivery read does not complete within the
    permitted time budget."""

    sensor_id: str
    timeout_ms: int

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"read of sensor {self.sensor_id!r} timed out after {self.timeout_ms} ms"


@dataclass(eq=False)
class ValidationError(SensortapError):
    """Represents a SensorInfo record that violates the Schema.

    IMPORTANT: this exception is never raised to a caller. The Registry
    only constructs and records instances of it (e.g. attaching them to
    per-adapter diagnostics) while excluding the offending record and
    retaining all other valid records from that Adapter.

    Requirements: 2.10
    """

    adapter_id: str
    record_id: str | None
    field: str
    detail: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        record = self.record_id if self.record_id is not None else "<no id>"
        base = (
            f"validation error from adapter {self.adapter_id!r}, "
            f"record {record!r}: field {self.field!r} violated"
        )
        if self.detail:
            base += f" ({self.detail})"
        return base


@dataclass(eq=False)
class SchemaVersionError(SensortapError):
    """Represents a record whose declared schema MAJOR version differs
    from the core Schema's MAJOR version.

    IMPORTANT: this exception is never raised to a caller. The Registry
    only constructs and records instances of it while excluding the
    offending record.

    Requirements: 2.13
    """

    adapter_id: str
    record_id: str | None
    declared_version: str
    expected_major: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        record = self.record_id if self.record_id is not None else "<no id>"
        return (
            f"schema version error from adapter {self.adapter_id!r}, "
            f"record {record!r}: declared {self.declared_version!r}, "
            f"expected MAJOR {self.expected_major!r}"
        )

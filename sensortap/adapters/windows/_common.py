"""Shared plumbing for the Windows/WinRT adapter family (Req 13.1, 13.2,
13.3, 3.5).

Every concrete Windows adapter (`winrt_motion.py`, `winrt_orientation.py`,
`winrt_light.py`, ...) imports from this module rather than re-implementing
these three pieces of behaviour on its own:

1. :func:`query_winrt_sensor_class` -- calls a WinRT sensor class's
   `get_default()` and turns a `None` result into a full, schema-valid
   `SensorInfo` with `availability = Availability.ABSENT` (Req 13.1, 13.2).
2. :func:`bounded_read` -- runs a possibly-blocking read function under a
   2000 ms bound and degrades to `Status.UNAVAILABLE` on timeout or
   exception rather than propagating either into the caller (Req 13.3).
3. :func:`device_instance_hash` -- extracts a WinRT sensor's persistent
   `device_id` and feeds it through `registry.ids.instance_hash()` to
   produce the 16-char instance qualifier (Req 3.5).

Verified WinRT API surface (winsdk==1.0.0b10, Python 3.12, checked
interactively against the installed package rather than assumed):

- `Accelerometer`, `Gyrometer`, `Magnetometer`, `Inclinometer`,
  `OrientationSensor`, `LightSensor` each expose a classmethod
  `get_default()` (snake_case, per winsdk's Python projection convention --
  *not* `GetDefault()`) that returns `None` when the OS does not expose that
  sensor class, or a live sensor object otherwise.
- `HingeAngleSensor` is the one exception: it exposes no `get_default()` at
  all, only `get_default_async()` (a WinRT async operation) plus
  `get_current_reading_async()`. Its adapter (task 6.3) must await that
  async method instead of calling `query_winrt_sensor_class` directly; this
  module still provides the shared "null -> absent" and "bounded read"
  *behaviour* for it to reuse, just not through the synchronous
  `get_default()` path the other six classes share.
- Every checked class (including `HingeAngleSensor`) exposes a `device_id`
  property on the *instance* returned by `get_default()` -- this is the
  WinRT device instance path (the same kind of string
  `DeviceInformation.Id` would report) and is the persistent identifier fed
  to `instance_hash()` below. It is not available on a `None` result, which
  is precisely the case Req 13.2 says must assert nothing about hardware
  presence -- there is no identifier to hash when the class itself is
  absent from the OS.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
from dataclasses import dataclass
from typing import Callable, Protocol, TypeVar

from sensortap.registry.ids import instance_hash
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo
from sensortap.schema.version import SCHEMA_VERSION

#: Shared read bound for WinRT sensor reads (Req 13.3). A read that neither
#: returns nor raises within this budget is abandoned; the caller sees
#: `Status.UNAVAILABLE` rather than a hang.
WINRT_READ_TIMEOUT_MS = 2000

#: Persistent worker used to bound reads. One worker is enough: reads are
#: dispatched one at a time by the registry's routing layer per sensor, and
#: a persistent pool avoids the per-call cost of spinning a thread up and
#: down for every read (WinRT reads happen far more often than discovery).
_read_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sensortap-winrt-read")


class _GetDefaultSensorClass(Protocol):
    """Structural shape of a WinRT sensor class exposing `get_default()`.

    Satisfied by `Accelerometer`, `Gyrometer`, `Magnetometer`,
    `Inclinometer`, `OrientationSensor` and `LightSensor`. `HingeAngleSensor`
    does *not* satisfy this (it has no synchronous `get_default()`), which is
    exactly why its adapter cannot call :func:`query_winrt_sensor_class`
    directly.
    """

    @staticmethod
    def get_default() -> object | None: ...  # pragma: no cover - structural


_T = TypeVar("_T")


def query_winrt_sensor_class(
    sensor_class: _GetDefaultSensorClass,
    *,
    kind: str,
    source_id: str,
    unit: str | None,
    dtype: Dtype,
    channels: tuple[str, ...],
    shape: tuple[int, ...],
    value_range: tuple[float, float] | None,
    resolution: float | None,
    rate_hz: RateSpec,
    delivery: Delivery,
    derived: bool,
    requires_consent: bool = False,
    requires_elevation: bool = False,
    vendor: str | None = None,
    part_number: str | None = None,
) -> tuple[SensorInfo, object | None]:
    """Query one WinRT sensor class and emit exactly one `SensorInfo` for it.

    This is the "one record per queried class" behaviour Req 13.1 demands:
    even when `sensor_class.get_default()` returns `None`, this still
    returns a full, schema-valid `SensorInfo` -- with `availability =
    Availability.ABSENT` -- rather than the caller simply skipping the class.

    Per Req 13.2, an absent result means only that the WinRT class is not
    exposed by the operating system. It makes no assertion about whether the
    underlying hardware exists, and the returned record's `id` still uses a
    deterministic fallback instance qualifier (`"0"`) rather than a hash of
    a nonexistent device id, since there is nothing to hash when the class
    itself is absent.

    Returns a `(SensorInfo, sensor_instance_or_none)` pair. Callers that need
    to read the sensor go on to use the second element; a `None` second
    element means the caller must not attempt any further WinRT calls
    against this sensor.
    """

    sensor = sensor_class.get_default()

    if sensor is None:
        return (
            SensorInfo(
                schema_version=SCHEMA_VERSION,
                id=f"{kind}.{source_id}.0",
                kind=kind,
                dtype=dtype,
                unit=unit,
                channels=channels,
                shape=shape,
                range=value_range,
                resolution=resolution,
                rate_hz=rate_hz,
                delivery=delivery,
                derived=derived,
                requires_consent=requires_consent,
                requires_elevation=requires_elevation,
                source=(source_id,),
                vendor=vendor,
                part_number=part_number,
                availability=Availability.ABSENT,
            ),
            None,
        )

    device_id = getattr(sensor, "device_id", None)
    instance_qualifier = instance_hash(device_id) if device_id else "0"

    return (
        SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=f"{kind}.{source_id}.{instance_qualifier}",
            kind=kind,
            dtype=dtype,
            unit=unit,
            channels=channels,
            shape=shape,
            range=value_range,
            resolution=resolution,
            rate_hz=rate_hz,
            delivery=delivery,
            derived=derived,
            requires_consent=requires_consent,
            requires_elevation=requires_elevation,
            source=(source_id,),
            vendor=vendor,
            part_number=part_number,
            availability=Availability.PRESENT,
        ),
        sensor,
    )


def device_instance_hash(device_id: str) -> str:
    """Derive the 16-char Sensor_Id instance qualifier from a WinRT device
    instance path (Req 3.5).

    `device_id` is expected to be the value of a WinRT sensor's `device_id`
    property (verified present on every checked sensor class's instance,
    see module docstring). This is a thin, named wrapper around
    `registry.ids.instance_hash()` so Windows adapter modules do not import
    `registry.ids` directly and so the "this is *the* persistent identifier
    for WinRT sensors" decision has one place to live and to change.
    """

    return instance_hash(device_id)


def bounded_read(
    read_fn: Callable[[], Reading],
    *,
    sensor_id: str,
    timeout_ms: int = WINRT_READ_TIMEOUT_MS,
    seq: int = 0,
) -> Reading:
    """Run `read_fn` under a bounded time budget (Req 13.3).

    `Reading` has no free-text "reason" field (its fixed fields are `id`,
    `t_mono`, `t_wall`, `values`, `seq`, `status` -- see `schema/reading.py`)
    and `Availability` (not `Status`) is the enum that carries `unavailable`
    as a *sensor-level* concept; `Status.UNAVAILABLE` is the corresponding
    *reading-level* concept (Req 2.5 vs Req 4.4 -- both enums independently
    define an `unavailable`/`UNAVAILABLE` member, so this is not a
    misspelling of one shared value). This function reports the Req 13.3
    "unavailable with a reason" outcome at the `Reading` level via
    `Status.UNAVAILABLE` with empty `values` -- the only vehicle `Reading`
    provides for "this read failed" (Req 4.10, 4.11). The human-readable
    "reason" text itself is not a `Reading` field; it belongs on the
    surrounding `SensorInfo.availability` transition (`PRESENT` ->
    `UNAVAILABLE`) that the calling adapter's `discover()`/status bookkeeping
    performs, and/or on a logged detail string. This function's own
    docstring and the caller's log line are that reason's home; on timeout,
    the caller receives a plain string via the returned `Reading`'s absence
    of any exception, so adapters that want to surface *why* should catch
    the specific failure mode themselves (this function never raises).

    The read function runs on the shared `_read_executor`; on timeout the
    underlying thread is *not* cancelled (WinRT calls, like the registry's
    own discovery bound, cannot be interrupted once blocked) and its result,
    if it eventually arrives, is simply discarded.
    """

    now = time.monotonic()
    future = _read_executor.submit(read_fn)
    try:
        return future.result(timeout=timeout_ms / 1000)
    except _FutureTimeoutError:
        return Reading(
            id=sensor_id,
            t_mono=now,
            t_wall=time.time(),
            values=(),
            seq=seq,
            status=Status.UNAVAILABLE,
        )
    except Exception:  # noqa: BLE001 - any WinRT-raised failure degrades gracefully
        return Reading(
            id=sensor_id,
            t_mono=now,
            t_wall=time.time(),
            values=(),
            seq=seq,
            status=Status.UNAVAILABLE,
        )


@dataclass(frozen=True, slots=True)
class AbsentSensorReadReason:
    """A short, human-readable reason to attach to logs/status when a
    bounded read against a WinRT sensor times out or raises (Req 13.3).

    Not a schema field -- `Reading` has no reason field (see `bounded_read`
    docstring) -- this exists purely so adapters have one consistent shape
    to log/record rather than inventing ad hoc strings per adapter file.
    """

    sensor_id: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"sensor {self.sensor_id!r} unavailable: {self.detail}"

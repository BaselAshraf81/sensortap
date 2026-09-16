"""`WindowsMotionAdapter`: accelerometer, gyrometer and magnetometer via WinRT
(Req 13.1, 13.4, 6.1, 6.3, 6.6).

Verified WinRT API surface (winsdk==1.0.0b10, Python 3.12, checked
interactively against the installed package -- see the findings recorded
here since guessing these wrong fails silently):

- `Accelerometer`, `Gyrometer`, `Magnetometer` each expose, on the *class*
  itself (not just the instance returned by `get_default()`):
  `report_interval` (a settable `getset_descriptor` -- readable AND
  writable) and `minimum_report_interval` (read-only). Both are plain
  `int` counts of **milliseconds**, per WinRT's `ReportInterval`
  convention -- not a rate. `rate_hz = 1000.0 / interval_ms`.
- Neither class exposes any "maximum report interval" or "minimum rate"
  concept -- there is no `maximum_report_interval` attribute on any of the
  three classes. This adapter therefore always reports `RateSpec.max =
  None` for all three sensors; there is nothing in the WinRT surface to
  populate it with, and inventing a ceiling would be a guess this module
  is explicitly written to avoid.
- Each class exposes a synchronous `get_current_reading()` on the live
  instance (no `_async` suffix needed), returning an
  `AccelerometerReading` / `GyrometerReading` / `MagnetometerReading`.
  Their field names are `acceleration_x/y/z`, `angular_velocity_x/y/z` and
  `magnetic_field_x/y/z` respectively (verified via `dir()` against the
  installed package) -- not e.g. generic `x/y/z`.
- `report_interval = 0` is WinRT's documented sentinel for "use the
  sensor's default interval" on some WinRT sensor classes; this adapter
  never writes `0` itself (it always writes a computed millisecond count)
  but does not special-case a `0` read-back either, since none of the
  three classes examined here return `0` from `report_interval` for a
  live device in the case exercised in testing (no physical sensor
  present in the dev/CI environment, only the absent-class path was
  exercised end-to-end).

Design choice for populating `RateSpec.default`/`min` in `discover()`
(documented per the task's instructions): `query_winrt_sensor_class`
requires a `RateSpec` up front, before it knows whether `get_default()`
will return `None`. Building an accurate `RateSpec` from the live
instance would require querying `get_default()` a second time inside this
adapter. Instead, this module always passes the placeholder
`RateSpec(default=None, min=None, max=None)` into
`query_winrt_sensor_class`, and once it has the returned
`(SensorInfo, sensor_or_none)` pair, uses `dataclasses.replace()` to
rebuild the `SensorInfo` with an accurate `RateSpec` computed from the
live `sensor` object when one was returned. This avoids the double
`get_default()` call (option (a) from the task) at the cost of one extra
`dataclasses.replace()` per present sensor -- cheaper than a second WinRT
round trip.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import ClassVar

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION, AdapterMeta
from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows._common import bounded_read, query_winrt_sensor_class
from sensortap.schema.enums import Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo

_ACCEL_ID = "accel.winrt.0"
_GYRO_ID = "gyro.winrt.0"
_MAGN_ID = "magn.winrt.0"

# NB: these are the *fallback* `id`s used only to key the dispatch tables
# below by kind; the real per-sensor `id` (with its hashed or "0" instance
# qualifier) comes from whatever `query_winrt_sensor_class` returns, which
# is the value actually placed in the record and used as the dispatch key
# at runtime (see `_sensor_ids` built in `__init__`).


def _report_interval_rate_spec(sensor: object) -> RateSpec:
    """Build an accurate `RateSpec` from a live WinRT motion sensor instance.

    `report_interval` and `minimum_report_interval` are both millisecond
    interval counts (Req 6.1, 6.6); `rate_hz = 1000.0 / interval_ms`.
    `max` has no WinRT counterpart on these three classes and is always
    `None` (see module docstring).
    """

    current_interval_ms = getattr(sensor, "report_interval", None)
    minimum_interval_ms = getattr(sensor, "minimum_report_interval", None)

    default_hz = (
        1000.0 / current_interval_ms
        if current_interval_ms is not None and current_interval_ms > 0
        else None
    )
    # The *minimum interval* the sensor supports corresponds to its
    # *maximum* rate, which is not what `RateSpec.min` means -- `min` here
    # is the slowest configurable rate, for which no WinRT concept exists.
    # `minimum_report_interval` is used instead to derive `RateSpec.max`... but
    # since this adapter chooses not to guess a `max` (see module
    # docstring), the minimum-interval value is not used for `max` either.
    # It is retained only as documentation of what was checked; nothing
    # from it is surfaced here beyond the `default`.
    del minimum_interval_ms

    return RateSpec(default=default_hz, min=None, max=None)


class WindowsMotionAdapter:
    """Accelerometer, gyrometer and magnetometer WinRT adapter (Req 13.1)."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="winrt_motion",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=WINDOWS_PLATFORMS,
        priority=None,
        requires_elevation_optin=False,
        read_only_declared=True,
    )

    def __init__(self) -> None:
        self._accel_id = _ACCEL_ID
        self._gyro_id = _GYRO_ID
        self._magn_id = _MAGN_ID
        self._accel_sensor: object | None = None
        self._gyro_sensor: object | None = None
        self._magn_sensor: object | None = None

    # ------------------------------------------------------------------
    # required
    # ------------------------------------------------------------------

    def discover(self) -> tuple[SensorInfo, ...]:
        from winsdk.windows.devices.sensors import Accelerometer, Gyrometer, Magnetometer

        accel_info, accel_sensor = query_winrt_sensor_class(
            Accelerometer,
            kind="accel",
            source_id="winrt",
            unit="m/s2",
            dtype=Dtype.VECTOR3,
            channels=("x", "y", "z"),
            shape=(3,),
            value_range=None,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.PUSH,
            derived=False,
        )
        gyro_info, gyro_sensor = query_winrt_sensor_class(
            Gyrometer,
            kind="gyro",
            source_id="winrt",
            unit="deg/s",
            dtype=Dtype.VECTOR3,
            channels=("x", "y", "z"),
            shape=(3,),
            value_range=None,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.PUSH,
            derived=False,
        )
        magn_info, magn_sensor = query_winrt_sensor_class(
            Magnetometer,
            kind="magn",
            source_id="winrt",
            unit="uT",
            dtype=Dtype.VECTOR3,
            channels=("x", "y", "z"),
            shape=(3,),
            value_range=None,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.PUSH,
            derived=False,
        )

        if accel_sensor is not None:
            accel_info = replace(accel_info, rate_hz=_report_interval_rate_spec(accel_sensor))
        if gyro_sensor is not None:
            gyro_info = replace(gyro_info, rate_hz=_report_interval_rate_spec(gyro_sensor))
        if magn_sensor is not None:
            magn_info = replace(magn_info, rate_hz=_report_interval_rate_spec(magn_sensor))

        self._accel_id = accel_info.id
        self._gyro_id = gyro_info.id
        self._magn_id = magn_info.id
        self._accel_sensor = accel_sensor
        self._gyro_sensor = gyro_sensor
        self._magn_sensor = magn_sensor

        return (accel_info, gyro_info, magn_info)

    def read(self, sensor_id: str) -> Reading:
        if sensor_id == self._accel_id:
            sensor = self._accel_sensor
            if sensor is None:
                # No physical accelerometer on this machine (Req 13.2's
                # "absent" case) -- report unavailable rather than
                # raising, matching winrt_light.py/winrt_orientation.py's
                # established pattern for a sensor discover() already
                # reported as absent.
                return _unavailable_reading(sensor_id)

            def _read() -> Reading:
                import time

                reading = sensor.get_current_reading()
                return Reading(
                    id=sensor_id,
                    t_mono=time.monotonic(),
                    t_wall=time.time(),
                    values=(
                        reading.acceleration_x,
                        reading.acceleration_y,
                        reading.acceleration_z,
                    ),
                    seq=0,
                    status=Status.OK,
                )

            return bounded_read(_read, sensor_id=sensor_id)

        if sensor_id == self._gyro_id:
            sensor = self._gyro_sensor
            if sensor is None:
                return _unavailable_reading(sensor_id)

            def _read() -> Reading:
                import time

                reading = sensor.get_current_reading()
                return Reading(
                    id=sensor_id,
                    t_mono=time.monotonic(),
                    t_wall=time.time(),
                    values=(
                        reading.angular_velocity_x,
                        reading.angular_velocity_y,
                        reading.angular_velocity_z,
                    ),
                    seq=0,
                    status=Status.OK,
                )

            return bounded_read(_read, sensor_id=sensor_id)

        if sensor_id == self._magn_id:
            sensor = self._magn_sensor
            if sensor is None:
                return _unavailable_reading(sensor_id)

            def _read() -> Reading:
                import time

                reading = sensor.get_current_reading()
                return Reading(
                    id=sensor_id,
                    t_mono=time.monotonic(),
                    t_wall=time.time(),
                    values=(
                        reading.magnetic_field_x,
                        reading.magnetic_field_y,
                        reading.magnetic_field_z,
                    ),
                    seq=0,
                    status=Status.OK,
                )

            return bounded_read(_read, sensor_id=sensor_id)

        raise KeyError(f"unknown sensor id for WindowsMotionAdapter: {sensor_id!r}")

    # ------------------------------------------------------------------
    # optional
    # ------------------------------------------------------------------

    def configure_rate(self, sensor_id: str, rate_hz: float) -> float:
        """Set `report_interval` (ms) from a requested rate and return what
        WinRT actually applied, converted back to Hz (Req 6.3, 6.6).

        WinRT coerces a requested interval to a value the sensor supports;
        the applied rate returned here is always the read-back value, not
        the requested one.
        """

        if sensor_id == self._accel_id:
            sensor = self._accel_sensor
        elif sensor_id == self._gyro_id:
            sensor = self._gyro_sensor
        elif sensor_id == self._magn_id:
            sensor = self._magn_sensor
        else:
            raise KeyError(f"unknown sensor id for WindowsMotionAdapter: {sensor_id!r}")

        if sensor is None:
            raise KeyError(f"sensor {sensor_id!r} has no live WinRT instance")

        requested_interval_ms = round(1000.0 / rate_hz)
        sensor.report_interval = requested_interval_ms
        applied_interval_ms = sensor.report_interval
        return 1000.0 / applied_interval_ms


def _unavailable_reading(sensor_id: str) -> Reading:
    return Reading(
        id=sensor_id,
        t_mono=time.monotonic(),
        t_wall=time.time(),
        values=(),
        seq=0,
        status=Status.UNAVAILABLE,
    )

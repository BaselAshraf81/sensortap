"""`WindowsOrientationAdapter`: inclinometer, orientation sensor, hinge
angle sensor (Req 13.1, 13.2, 13.4).

Three sensors, all `derived = True` (Req 13.4): each one is either a fusion
of multiple physical sensors (inclinometer, orientation sensor -- both
built by Windows from the raw accelerometer/gyrometer/magnetometer) or a
value computed from more than one physical IMU (hinge angle, fused from
the lid and base IMUs on a 2-in-1/foldable device). None of the three is a
direct hardware reading, which is exactly what `derived = True` means (Req
2.11).

Verified interactively against the installed `winsdk==1.0.0b10` package
(Python 3.12, no sensor hardware present on the checking machine -- every
`get_default()`/`get_default_async()` call below returned `None`, which is
the expected "class not exposed" case Req 13.2 requires this module to
still report as one full record with `availability = absent`):

- `Inclinometer` and `OrientationSensor` both expose a synchronous
  `get_default()` classmethod (confirmed present, per task 6.1's finding),
  so both use :func:`query_winrt_sensor_class` from `_common.py` exactly
  like `winrt_motion.py` (task 6.2) does for accel/gyro/magn.
- `InclinometerReading` exposes `pitch_degrees`, `roll_degrees` and
  `yaw_degrees` directly, already in degrees, already three separate axes
  -- no conversion math needed. Channel order chosen here is
  `("pitch", "roll", "yaw")`, matching the reading object's own attribute
  order; this is documented on `_INCLINE_CHANNELS` below since there is no
  canonical ordering imposed by WinRT itself.
- `OrientationSensorReading` exposes `.quaternion` (a `SensorQuaternion`
  with `.x`, `.y`, `.z`, `.w`) and `.rotation_matrix`, but **no** direct
  Euler angle properties. This module converts the quaternion to Euler
  angles itself (see :func:`quaternion_to_euler_zyx` below) rather than
  using the rotation matrix, since the quaternion is the more compact and
  numerically direct source and the conversion formula is standard.
- `HingeAngleSensor` has **no** synchronous `get_default()` at all (Req
  13.1/13.2, confirmed by task 6.1 and re-confirmed here: `hasattr(
  HingeAngleSensor, "get_default")` is `False`), only `get_default_async()`
  and `get_current_reading_async()`. Both are real WinRT `IAsyncOperation`s.
  `winsdk` makes an `IAsyncOperation` *awaitable* (it implements
  `__await__`), but it is **not** itself a `coroutine` object, and
  `asyncio.run()` insists on one -- calling
  `asyncio.run(HingeAngleSensor.get_default_async())` directly was tried
  first and fails with `ValueError: a coroutine was expected, got
  <IAsyncOperation ...>`. The fix verified interactively (see
  :func:`_run_async`) is to wrap the awaitable in a small local
  `async def` shim, `await` it there, and hand that coroutine to
  `asyncio.run()` instead -- this works cleanly with no additional
  event-loop wiring. `HingeAngleReading` exposes `angle_in_degrees`
  (already the scalar value this module needs) plus `timestamp` and
  `properties`.

Quaternion convention chosen: **intrinsic Z-Y-X (yaw-pitch-roll)**, applied
in the order yaw (Z) then pitch (Y) then roll (X), which is the convention
most commonly associated with "yaw/pitch/roll" terminology for a device
held in the hand or resting on a surface. `values` for the `orientation`
sensor are emitted in `(roll, pitch, yaw)` channel order to keep visual
parity with the `incline` sensor's `(pitch, roll, yaw)`... note the
difference is intentional and documented per-sensor below via each
sensor's own `channels` tuple, since there is no requirement forcing the
two `vector3` sensors onto one shared channel order and WinRT itself does
not impose one.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import ClassVar

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION, AdapterMeta
from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows._common import (
    bounded_read,
    device_instance_hash,
    query_winrt_sensor_class,
)
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo
from sensortap.schema.version import SCHEMA_VERSION

#: Source qualifier segment for every Sensor_Id this adapter emits.
_SOURCE_ID = "winrt"

#: `incline` sensor: (pitch, roll, yaw), matching `InclinometerReading`'s
#: own attribute order (`pitch_degrees`, `roll_degrees`, `yaw_degrees`).
_INCLINE_ID_STEM = "incline"
_INCLINE_CHANNELS = ("pitch", "roll", "yaw")

#: `orientation` sensor: (roll, pitch, yaw), the conventional Euler
#: reporting order for the intrinsic Z-Y-X convention used by
#: `quaternion_to_euler_zyx` below.
_ORIENTATION_ID_STEM = "orientation"
_ORIENTATION_CHANNELS = ("roll", "pitch", "yaw")

#: `hinge-angle` sensor: one scalar channel.
_HINGE_ID_STEM = "hinge-angle"
_HINGE_CHANNELS = ("angle",)

#: Degrees range shared by all three sensors' `vector3`/`scalar` channels.
#: Inclinometer/orientation report a full rotation; hinge angle is
#: typically 0-360 on modern 2-in-1 hardware.
_DEGREES_RANGE = (0.0, 360.0)


def quaternion_to_euler_zyx(x: float, y: float, z: float, w: float) -> tuple[float, float, float]:
    """Convert a unit quaternion to intrinsic Z-Y-X Euler angles, in degrees.

    Returns `(roll, pitch, yaw)` in degrees, using the standard
    quaternion-to-Euler formula for the yaw-pitch-roll (Z-Y-X) convention.
    Pitch is clamped into [-90, 90] via `asin` (gimbal lock at +/-90 degrees
    is an inherent property of this convention, not a bug in this
    function). The identity quaternion `(0, 0, 0, 1)` maps to `(0, 0, 0)`.
    """

    # roll (x-axis rotation)
    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # pitch (y-axis rotation)
    sinp = 2.0 * (w * y - z * x)
    sinp = max(-1.0, min(1.0, sinp))
    pitch = math.asin(sinp)

    # yaw (z-axis rotation)
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


def _run_async(async_operation_factory) -> object:
    """Bridge a WinRT async operation to synchronous code via `asyncio.run`.

    `winsdk` projects a WinRT `IAsyncOperation` as an *awaitable*, not as a
    `coroutine` object -- `asyncio.run()` insists on a genuine coroutine
    (confirmed interactively: calling `asyncio.run(x.get_default_async())`
    directly raises ``ValueError: a coroutine was expected, got
    <IAsyncOperation ...>``). The fix verified interactively is to wrap the
    awaitable in a tiny local `async def` shim and hand *that* coroutine to
    `asyncio.run()`; `await`-ing an `IAsyncOperation` from inside a real
    coroutine works directly, since `winsdk` implements `__await__` on it.

    `async_operation_factory` is a zero-argument callable returning a fresh
    `IAsyncOperation` (e.g. `HingeAngleSensor.get_default_async`), not the
    operation itself, so a fresh one is obtained on each call.
    """

    async def _shim() -> object:
        return await async_operation_factory()

    return asyncio.run(_shim())


class WindowsOrientationAdapter:
    """Inclinometer, orientation sensor, hinge angle sensor (Req 13.1,
    13.2, 13.4)."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="winrt_orientation",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=WINDOWS_PLATFORMS,
        priority=None,
        requires_elevation_optin=False,
        read_only_declared=True,
    )

    def __init__(self) -> None:
        self._incline_id: str | None = None
        self._orientation_id: str | None = None
        self._hinge_id: str | None = None
        self._seq = 0

    # ------------------------------------------------------------------
    # required
    # ------------------------------------------------------------------

    def discover(self) -> tuple[SensorInfo, ...]:
        incline_info, _ = query_winrt_sensor_class(
            _InclinometerClass,
            kind="incline",
            source_id=_SOURCE_ID,
            unit="deg",
            dtype=Dtype.VECTOR3,
            channels=_INCLINE_CHANNELS,
            shape=(3,),
            value_range=_DEGREES_RANGE,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=True,
        )
        self._incline_id = incline_info.id

        orientation_info, _ = query_winrt_sensor_class(
            _OrientationSensorClass,
            kind="orientation",
            source_id=_SOURCE_ID,
            unit="deg",
            dtype=Dtype.VECTOR3,
            channels=_ORIENTATION_CHANNELS,
            shape=(3,),
            value_range=_DEGREES_RANGE,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=True,
        )
        self._orientation_id = orientation_info.id

        hinge_info = self._discover_hinge_angle()
        self._hinge_id = hinge_info.id

        return (incline_info, orientation_info, hinge_info)

    def _discover_hinge_angle(self) -> SensorInfo:
        """Discover `HingeAngleSensor` by hand (Req 13.1, 13.2).

        `HingeAngleSensor` has no synchronous `get_default()`
        (`_common.query_winrt_sensor_class` cannot be used directly), so
        this replicates that function's "one record per queried class,
        absent if None, present if not" logic inline, using the
        async-bridged instance in place of a sync `get_default()` result.
        """

        from winsdk.windows.devices.sensors import HingeAngleSensor

        sensor = _run_async(HingeAngleSensor.get_default_async)

        if sensor is None:
            return SensorInfo(
                schema_version=SCHEMA_VERSION,
                id=f"{_HINGE_ID_STEM}.{_SOURCE_ID}.0",
                kind=_HINGE_ID_STEM,
                dtype=Dtype.SCALAR,
                unit="deg",
                channels=_HINGE_CHANNELS,
                shape=(1,),
                range=_DEGREES_RANGE,
                resolution=None,
                rate_hz=RateSpec(default=None, min=None, max=None),
                delivery=Delivery.POLL,
                derived=True,
                requires_consent=False,
                requires_elevation=False,
                source=(_SOURCE_ID,),
                vendor=None,
                part_number=None,
                availability=Availability.ABSENT,
            )

        device_id = getattr(sensor, "device_id", None)
        instance_qualifier = device_instance_hash(device_id) if device_id else "0"

        return SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=f"{_HINGE_ID_STEM}.{_SOURCE_ID}.{instance_qualifier}",
            kind=_HINGE_ID_STEM,
            dtype=Dtype.SCALAR,
            unit="deg",
            channels=_HINGE_CHANNELS,
            shape=(1,),
            range=_DEGREES_RANGE,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=True,
            requires_consent=False,
            requires_elevation=False,
            source=(_SOURCE_ID,),
            vendor=None,
            part_number=None,
            availability=Availability.PRESENT,
        )

    def read(self, sensor_id: str) -> Reading:
        if sensor_id == self._incline_id:
            return bounded_read(
                lambda: self._read_incline(sensor_id), sensor_id=sensor_id, seq=self._next_seq()
            )
        if sensor_id == self._orientation_id:
            return bounded_read(
                lambda: self._read_orientation(sensor_id),
                sensor_id=sensor_id,
                seq=self._next_seq(),
            )
        if sensor_id == self._hinge_id:
            return bounded_read(
                lambda: self._read_hinge_angle_sync(sensor_id),
                sensor_id=sensor_id,
                seq=self._next_seq(),
            )
        raise KeyError(f"unknown sensor id for WindowsOrientationAdapter: {sensor_id!r}")

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def _read_incline(self, sensor_id: str) -> Reading:
        sensor = _InclinometerClass.get_default()
        if sensor is None:
            return _unavailable_reading(sensor_id)
        reading = sensor.get_current_reading()
        if reading is None:
            return _unavailable_reading(sensor_id)
        return Reading(
            id=sensor_id,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            values=(reading.pitch_degrees, reading.roll_degrees, reading.yaw_degrees),
            seq=0,
            status=Status.OK,
        )

    def _read_orientation(self, sensor_id: str) -> Reading:
        sensor = _OrientationSensorClass.get_default()
        if sensor is None:
            return _unavailable_reading(sensor_id)
        reading = sensor.get_current_reading()
        if reading is None:
            return _unavailable_reading(sensor_id)
        quat = reading.quaternion
        roll, pitch, yaw = quaternion_to_euler_zyx(quat.x, quat.y, quat.z, quat.w)
        return Reading(
            id=sensor_id,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            values=(roll, pitch, yaw),
            seq=0,
            status=Status.OK,
        )

    def _read_hinge_angle_sync(self, sensor_id: str) -> Reading:
        """Plain sync wrapper around the hinge angle's async read path.

        `bounded_read` accepts a sync callable; this function is that
        callable, and internally bridges the async
        `get_current_reading_async()` call via `asyncio.run` (see
        `_run_async`).
        """

        from winsdk.windows.devices.sensors import HingeAngleSensor

        sensor = _run_async(HingeAngleSensor.get_default_async)
        if sensor is None:
            return _unavailable_reading(sensor_id)

        reading = _run_async(sensor.get_current_reading_async)
        if reading is None:
            return _unavailable_reading(sensor_id)

        return Reading(
            id=sensor_id,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            values=(reading.angle_in_degrees,),
            seq=0,
            status=Status.OK,
        )


def _unavailable_reading(sensor_id: str) -> Reading:
    now = time.monotonic()
    return Reading(
        id=sensor_id,
        t_mono=now,
        t_wall=time.time(),
        values=(),
        seq=0,
        status=Status.UNAVAILABLE,
    )


class _InclinometerClass:
    """Thin indirection over `winsdk`'s `Inclinometer`, imported lazily so
    this module stays importable on any platform (Req 10.9's "skip, don't
    fail to import" expectation for platform-gated adapters)."""

    @staticmethod
    def get_default() -> object | None:
        from winsdk.windows.devices.sensors import Inclinometer

        return Inclinometer.get_default()


class _OrientationSensorClass:
    """Thin indirection over `winsdk`'s `OrientationSensor`, imported
    lazily for the same reason as `_InclinometerClass`."""

    @staticmethod
    def get_default() -> object | None:
        from winsdk.windows.devices.sensors import OrientationSensor

        return OrientationSensor.get_default()

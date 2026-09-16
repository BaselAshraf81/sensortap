"""Ambient light sensor adapter for Windows via WinRT `LightSensor` (Req 13.1,
13.4).

Verified WinRT API surface (winsdk==1.0.0b10, Python 3.12, checked
interactively against the installed package):

- `winsdk.windows.devices.sensors.LightSensor` exposes a synchronous
  classmethod `get_default()` (per `_common.py`'s established list), so
  this adapter uses `query_winrt_sensor_class` directly rather than the
  async-only path `HingeAngleSensor` requires.
- `LightSensor.get_current_reading()` returns a `LightSensorReading` whose
  illuminance value is exposed as `illuminance_in_lux` -- a single float
  -- not `illuminance_lux` as WinRT's usual Python-projection convention
  might suggest at a glance. This was confirmed by inspecting
  `LightSensorReading`'s attributes on the installed `winsdk` package
  rather than assumed.

The sensor is reported as `scalar`/`shape=(1,)`/`unit="lx"` with
`derived=False` (Req 13.4): ambient light is a single raw measurement from
one physical photodiode, not a value computed from other sensors.
"""

from __future__ import annotations

import time
from typing import ClassVar

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION, AdapterMeta
from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows._common import bounded_read, query_winrt_sensor_class
from sensortap.schema.enums import Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo

try:  # pragma: no cover - only importable on Windows with winsdk installed
    from winsdk.windows.devices.sensors import LightSensor
except ImportError:  # pragma: no cover - non-Windows / winsdk absent
    LightSensor = None  # type: ignore[assignment, misc]

#: Sensor_Id source segment for this adapter (Req 3.5).
_SOURCE_ID = "winrt"

#: `kind` string, taken verbatim from `schema.kinds.KIND_VOCABULARY`.
_KIND = "light"


class WindowsLightAdapter:
    """Reports the WinRT `LightSensor` as one ambient-light sensor."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="winrt_light",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=WINDOWS_PLATFORMS,
        read_only_declared=True,
    )

    def __init__(self) -> None:
        self._sensor: object | None = None

    def discover(self) -> tuple[SensorInfo, ...]:
        info, sensor = query_winrt_sensor_class(
            LightSensor,
            kind=_KIND,
            source_id=_SOURCE_ID,
            unit="lx",
            dtype=Dtype.SCALAR,
            channels=("lux",),
            shape=(1,),
            value_range=None,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=False,
        )
        self._sensor = sensor
        return (info,)

    def read(self, sensor_id: str) -> Reading:
        sensor = self._sensor

        def _read() -> Reading:
            reading = sensor.get_current_reading()  # type: ignore[union-attr]
            return Reading(
                id=sensor_id,
                t_mono=time.monotonic(),
                t_wall=time.time(),
                values=(float(reading.illuminance_in_lux),),
                seq=0,
                status=Status.OK,
            )

        if sensor is None:
            return Reading(
                id=sensor_id,
                t_mono=time.monotonic(),
                t_wall=time.time(),
                values=(),
                seq=0,
                status=Status.UNAVAILABLE,
            )

        return bounded_read(_read, sensor_id=sensor_id)

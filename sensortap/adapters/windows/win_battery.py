"""Battery/power telemetry adapter for Windows via WinRT
`Windows.Devices.Power.Battery` (Req 13.9).

Verified WinRT API surface (winsdk==1.0.0b10, Python 3.12, checked
interactively against the installed package rather than assumed):

- `Windows.Devices.Power.Battery` is a **different API shape** than the
  `Windows.Devices.Sensors` classes `_common.query_winrt_sensor_class`
  handles. It has **no** `get_default()` classmethod at all. Its actual
  surface (confirmed via `dir()` on the installed package) is:
  `add_report_updated`, `device_id`, `from_id_async`,
  `get_device_selector`, `get_report`, `remove_report_updated`.
- The system's aggregate battery is not obtained via a static
  `aggregate_battery` property (that does not exist on this `winsdk`
  version) -- it must be **enumerated** like any other WinRT device:
  `Battery.get_device_selector()` returns an AQS selector string, which is
  passed to `DeviceInformation.find_all_async(selector, [])` (the second,
  empty-list "additional properties" argument is required -- calling
  `find_all_async(selector)` with one argument raises
  ``TypeError: 'str' object cannot be interpreted as an integer`` because
  `winsdk` resolves the single-argument overload as the *count*-based
  `IVectorView` indexer instead). Each resulting `DeviceInformation.id` is
  then passed to `Battery.from_id_async(id)` to obtain the `Battery`
  instance itself.
- Confirmed on the machine used to verify this module: it reports exactly
  one battery device (a laptop). `Battery.get_report()` returns a
  `BatteryReport` synchronously (not async) with exactly these properties:
  `charge_rate_in_milliwatts`, `design_capacity_in_milliwatt_hours`,
  `full_charge_capacity_in_milliwatt_hours`,
  `remaining_capacity_in_milliwatt_hours`, `status`
  (`Windows.System.Power.BatteryStatus`: `NOT_PRESENT`, `DISCHARGING`,
  `IDLE`, `CHARGING`).
- **Voltage and temperature are NOT exposed by this API at all.**
  `BatteryReport` has no voltage or temperature property of any kind --
  this was checked for real via `dir(BatteryReport)`, not assumed from
  memory of the historical UWP API surface. Per the project's "degrade,
  never guess" philosophy, this module does not fabricate either reading:
  both are emitted as full, schema-valid `SensorInfo` records with
  `availability = Availability.ABSENT` and a clear reason, rather than
  being silently omitted or invented from some other source (e.g. WMI's
  `MSAcpi_ThermalZoneTemperature`, which this module deliberately does not
  reach for, since that would be a different backend making an unrelated
  claim about "battery temperature" this API cannot itself support).

Five records are emitted, all `requires_consent = False` (Req 13.9 groups
battery telemetry with the other non-privacy-sensitive hardware sensors --
Sensor_Id itself carries no personal information):

- ``battery.win-pct.<instance>``: charge percentage, `kind="battery"`,
  `unit="%"`, `derived=True` -- computed as
  `remaining_capacity_in_milliwatt_hours /
  full_charge_capacity_in_milliwatt_hours * 100`, i.e. from two raw values,
  not a single hardware register.
- ``battery.win-cap.<instance>``: remaining capacity, `kind="battery"`,
  `unit="mW.h"` (the exact token pinned in `schema/kinds.py`'s unit
  vocabulary), `derived=False` -- a single raw WinRT property.
- ``power.win-rate.<instance>``: charge/discharge rate, `kind="power"`,
  `unit="mW"`, `derived=False` -- `charge_rate_in_milliwatts` verbatim
  (WinRT's own sign convention: positive while charging, negative while
  discharging).
- ``voltage.win-volt.<instance>``: `kind="voltage"`, `unit="V"`,
  `availability=Availability.ABSENT` always -- this API exposes no
  voltage reading on any machine.
- ``temp.win-temp.<instance>``: `kind="temp"`, `unit="degC"`,
  `availability=Availability.ABSENT` always -- this API exposes no
  temperature reading on any machine.

Percentage/capacity/rate share one `<instance>` qualifier: the
`device_instance_hash()` of the enumerated `Battery.device_id`, or the
deterministic fallback ``"0"`` when no battery device is enumerated at all
(desktop machines with no battery). Voltage/temp use the same instance
qualifier when a battery device exists (there is something to hash) and
``"0"`` otherwise, since both are ABSENT unconditionally regardless of
hardware presence -- Req 13.2's "absent means the API doesn't expose this,
not an assertion about hardware" applies identically here even though this
is `Windows.Devices.Power`, not `Windows.Devices.Sensors`.
"""

from __future__ import annotations

import asyncio
import time
from typing import ClassVar

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION, AdapterMeta
from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows._common import bounded_read, device_instance_hash
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo
from sensortap.schema.version import SCHEMA_VERSION

#: `kind` strings, taken verbatim from `schema.kinds.KIND_VOCABULARY`.
_KIND_BATTERY = "battery"
_KIND_POWER = "power"
_KIND_VOLTAGE = "voltage"
_KIND_TEMP = "temp"

#: Source-qualifier segments (Req 3.5's second Sensor_Id segment): fixed
#: literals in code, one per distinct quantity, so five records sharing at
#: most one `kind` still land on five distinct Sensor_Ids.
_SOURCE_PCT = "win-pct"
_SOURCE_CAP = "win-cap"
_SOURCE_RATE = "win-rate"
_SOURCE_VOLT = "win-volt"
_SOURCE_TEMP = "win-temp"


def _run_async(async_operation_factory):
    """Bridge a WinRT async operation to synchronous code via `asyncio.run`.

    Same bridging technique as `winrt_orientation.py`'s `_run_async`: a
    `winsdk` `IAsyncOperation` is awaitable but not itself a coroutine, so
    `asyncio.run()` needs a small local `async def` shim wrapped around it.
    """

    async def _shim():
        return await async_operation_factory()

    return asyncio.run(_shim())


def _find_battery_device_id() -> str | None:
    """Enumerate WinRT battery devices and return the first one's
    `DeviceInformation.id`, or `None` if none are enumerated.

    Uses `Battery.get_device_selector()` plus
    `DeviceInformation.find_all_async(selector, [])` -- the two-argument
    call verified interactively (see module docstring); a desktop machine
    with no battery enumerates zero devices rather than raising.
    """

    from winsdk.windows.devices.enumeration import DeviceInformation
    from winsdk.windows.devices.power import Battery

    selector = Battery.get_device_selector()

    async def _enumerate():
        infos = await DeviceInformation.find_all_async(selector, [])
        return infos

    infos = asyncio.run(_enumerate())
    if infos.size == 0:
        return None
    return infos.get_at(0).id


def _get_battery(device_id: str):
    """Resolve a `Battery` instance from a `DeviceInformation.id` (Req 13.9)."""

    from winsdk.windows.devices.power import Battery

    return _run_async(lambda: Battery.from_id_async(device_id))


class WindowsBatteryAdapter:
    """Reports WinRT `Windows.Devices.Power.Battery` telemetry (Req 13.9)."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="win_battery",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=WINDOWS_PLATFORMS,
        read_only_declared=True,
    )

    def __init__(self) -> None:
        self._device_id: str | None = None
        self._pct_id: str | None = None
        self._cap_id: str | None = None
        self._rate_id: str | None = None
        self._volt_id: str | None = None
        self._temp_id: str | None = None
        self._seq = 0

    # ------------------------------------------------------------------
    # required
    # ------------------------------------------------------------------

    def discover(self) -> tuple[SensorInfo, ...]:
        device_id = _find_battery_device_id()
        self._device_id = device_id

        instance_qualifier = device_instance_hash(device_id) if device_id else "0"
        present = device_id is not None

        pct_info = SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=f"{_KIND_BATTERY}.{_SOURCE_PCT}.{instance_qualifier}",
            kind=_KIND_BATTERY,
            dtype=Dtype.SCALAR,
            unit="%",
            channels=("charge",),
            shape=(1,),
            range=(0.0, 100.0),
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=True,
            requires_consent=False,
            requires_elevation=False,
            source=(_SOURCE_PCT,),
            vendor=None,
            part_number=None,
            availability=Availability.PRESENT if present else Availability.ABSENT,
        )
        self._pct_id = pct_info.id

        cap_info = SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=f"{_KIND_BATTERY}.{_SOURCE_CAP}.{instance_qualifier}",
            kind=_KIND_BATTERY,
            dtype=Dtype.SCALAR,
            unit="mW.h",
            channels=("remaining_capacity",),
            shape=(1,),
            range=None,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=False,
            requires_consent=False,
            requires_elevation=False,
            source=(_SOURCE_CAP,),
            vendor=None,
            part_number=None,
            availability=Availability.PRESENT if present else Availability.ABSENT,
        )
        self._cap_id = cap_info.id

        rate_info = SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=f"{_KIND_POWER}.{_SOURCE_RATE}.{instance_qualifier}",
            kind=_KIND_POWER,
            dtype=Dtype.SCALAR,
            unit="mW",
            channels=("charge_rate",),
            shape=(1,),
            range=None,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=False,
            requires_consent=False,
            requires_elevation=False,
            source=(_SOURCE_RATE,),
            vendor=None,
            part_number=None,
            availability=Availability.PRESENT if present else Availability.ABSENT,
        )
        self._rate_id = rate_info.id

        # Voltage and temperature are never exposed by
        # `Windows.Devices.Power.Battery`/`BatteryReport` on any machine
        # (verified: `BatteryReport` has no voltage/temperature property at
        # all). Always ABSENT, with or without a battery device present --
        # "degrade, never guess" means these are never fabricated.
        volt_info = SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=f"{_KIND_VOLTAGE}.{_SOURCE_VOLT}.{instance_qualifier}",
            kind=_KIND_VOLTAGE,
            dtype=Dtype.SCALAR,
            unit="V",
            channels=("voltage",),
            shape=(1,),
            range=None,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=False,
            requires_consent=False,
            requires_elevation=False,
            source=(_SOURCE_VOLT,),
            vendor=None,
            part_number=None,
            availability=Availability.ABSENT,
        )
        self._volt_id = volt_info.id

        temp_info = SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=f"{_KIND_TEMP}.{_SOURCE_TEMP}.{instance_qualifier}",
            kind=_KIND_TEMP,
            dtype=Dtype.SCALAR,
            unit="degC",
            channels=("temperature",),
            shape=(1,),
            range=None,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=False,
            requires_consent=False,
            requires_elevation=False,
            source=(_SOURCE_TEMP,),
            vendor=None,
            part_number=None,
            availability=Availability.ABSENT,
        )
        self._temp_id = temp_info.id

        return (pct_info, cap_info, rate_info, volt_info, temp_info)

    def read(self, sensor_id: str) -> Reading:
        if sensor_id in (self._volt_id, self._temp_id):
            # Never exposed by this API; every read degrades identically
            # to the discovery-time ABSENT record's implication.
            return _unavailable_reading(sensor_id, self._next_seq())

        if sensor_id == self._pct_id:
            return bounded_read(
                lambda: self._read_percentage(sensor_id),
                sensor_id=sensor_id,
                seq=self._next_seq(),
            )
        if sensor_id == self._cap_id:
            return bounded_read(
                lambda: self._read_capacity(sensor_id),
                sensor_id=sensor_id,
                seq=self._next_seq(),
            )
        if sensor_id == self._rate_id:
            return bounded_read(
                lambda: self._read_rate(sensor_id),
                sensor_id=sensor_id,
                seq=self._next_seq(),
            )
        raise KeyError(f"unknown sensor id for WindowsBatteryAdapter: {sensor_id!r}")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def _get_report(self):
        if self._device_id is None:
            return None
        battery = _get_battery(self._device_id)
        if battery is None:
            return None
        return battery.get_report()

    def _read_percentage(self, sensor_id: str) -> Reading:
        report = self._get_report()
        if report is None:
            return _unavailable_reading(sensor_id, 0)
        full = report.full_charge_capacity_in_milliwatt_hours
        remaining = report.remaining_capacity_in_milliwatt_hours
        if not full:
            return _unavailable_reading(sensor_id, 0)
        percentage = (remaining / full) * 100.0
        return Reading(
            id=sensor_id,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            values=(float(percentage),),
            seq=0,
            status=Status.OK,
        )

    def _read_capacity(self, sensor_id: str) -> Reading:
        report = self._get_report()
        if report is None:
            return _unavailable_reading(sensor_id, 0)
        remaining = report.remaining_capacity_in_milliwatt_hours
        return Reading(
            id=sensor_id,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            values=(float(remaining),),
            seq=0,
            status=Status.OK,
        )

    def _read_rate(self, sensor_id: str) -> Reading:
        report = self._get_report()
        if report is None:
            return _unavailable_reading(sensor_id, 0)
        rate = report.charge_rate_in_milliwatts
        return Reading(
            id=sensor_id,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            values=(float(rate),),
            seq=0,
            status=Status.OK,
        )


def _unavailable_reading(sensor_id: str, seq: int) -> Reading:
    return Reading(
        id=sensor_id,
        t_mono=time.monotonic(),
        t_wall=time.time(),
        values=(),
        seq=seq,
        status=Status.UNAVAILABLE,
    )

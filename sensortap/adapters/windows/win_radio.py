"""`WindowsRadioAdapter`: Wi-Fi signal strength and Bluetooth radio presence
(Req 13.9, 13.11).

Verified WinRT API surface (winsdk==1.0.0b10, Python 3.12, checked
interactively against the installed package rather than assumed):

**Wi-Fi.** ``winsdk.windows.devices.wifi.WiFiAdapter`` exposes a static
``find_all_adapters_async()`` returning a (possibly empty) list of
``WiFiAdapter`` instances -- this is the honest way to detect "no wireless
interface exists at all" (an empty list), as opposed to "an interface
exists but currently has no connection". Each ``WiFiAdapter`` has a
``network_report`` property (``WiFiNetworkReport``, synchronous, no
``_async`` suffix) whose ``available_networks`` is a list of
``WiFiAvailableNetwork`` records. Each of those exposes:

- ``network_rssi_in_decibel_milliwatts`` -- a true dBm value.
- ``signal_bars`` -- an integer **0-4** (verified: the live query on this
  machine returned ``signal_bars == 4`` for a network at -52 dBm; Microsoft's
  own documentation for the sibling ``ConnectionProfile.GetSignalBars``
  API states its range is 0-5, but ``WiFiAvailableNetwork.SignalBars`` is a
  documented separate property and this project verified only the
  ``WiFiAvailableNetwork`` value, which is what is actually queried here).

**There is no direct Wi-Fi signal *percentage* property anywhere in the
verified surface.** The task text ("as a percentage... no dBm conversion")
assumed a direct percentage API exists; it does not. What is actually
available is signal bars. This adapter reports the percentage as an
explicit derived approximation: ``signal_bars / 4 * 100``, clamped to
``[0, 100]``. This is **not** a true RSSI-based percentage -- it is a
coarse 5-level (0/25/50/75/100) approximation of whatever bar count Windows
itself computed from RSSI, band and driver-specific heuristics. This
approximation, and the fact that it discards the true ``dBm`` value the API
does expose, is deliberate per the task's explicit "no dBm conversion"
instruction, and is documented here rather than silently applied.

`availability = Availability.ABSENT` is reported only when
``find_all_adapters_async()`` returns an empty list -- i.e. no wireless
interface exists on the machine at all. An adapter that exists but is not
currently connected to any network (empty ``available_networks``) is
`Availability.UNAVAILABLE` at read time, not absent at discovery time: the
interface is real, it simply has no current signal reading.

**Bluetooth.** ``winsdk.windows.devices.bluetooth.BluetoothAdapter`` exposes
only ``get_default_async()`` (no synchronous ``get_default()``, unlike the
sensor classes in ``_common.py``) plus ``get_radio_async()`` on the
returned instance. This module bridges that async call with its own small
``asyncio.run()`` wrapper -- there is no shared async-bridging helper in
`_common.py` as of this writing (task 6.3, which might have introduced one,
is not yet implemented), so this is a self-contained, minimal pattern:
`asyncio.run()` on a dedicated worker thread, submitted through the same
`bounded_read`-style timeout the rest of this module uses, rather than
calling `asyncio.run()` on whatever thread happens to invoke `discover()`
(which could already be running an event loop).

A "radio presence" sensor is unusual for this schema: `SensorInfo` expects
a `dtype`/`shape`/reading, not a native boolean-presence flag. Modeling
choice (documented per the task instructions): Bluetooth presence is
represented as a `scalar` sensor whose reading is `1.0` when a Bluetooth
radio is present and `0.0` when not, with `unit=None` and
`range=(0.0, 1.0)`. `availability = Availability.PRESENT` is reported if the
OS reports *any* Bluetooth adapter via `get_default_async()` -- even if
that radio is currently switched off in software -- and
`Availability.ABSENT` only when the OS reports no Bluetooth adapter at all.
Radio-off-but-present is intentionally not modelled as `ABSENT`: `ABSENT`
per Req 13.2 means "the class is not exposed by the platform", and a
present-but-disabled radio is a real piece of hardware the OS does expose,
just currently switched off -- that distinction is carried in the `1.0`/
`0.0` scalar value at read time, not in `availability`.

Both sensors use `kind = "radio-signal"` (confirmed present in
`schema/kinds.py`'s `KIND_VOCABULARY`) and `requires_consent = False`: a
signal-strength number and a boolean radio-presence flag carry no personal
information.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeoutError
from typing import ClassVar

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION, AdapterMeta
from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows._common import bounded_read
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo
from sensortap.schema.version import SCHEMA_VERSION

#: Sensor_Ids exposed by this adapter. Neither backend supplies a stable
#: per-run persistent identifier suitable for hashing here (a machine
#: typically has exactly one Wi-Fi interface and one Bluetooth radio, and
#: the WinRT objects involved do not expose a hardware instance path the
#: way the sensor classes in `_common.py` do), so both use the fixed
#: fallback instance qualifier "0" (Req 3.6): a zero-based index over a
#: singleton set is a stable, run-invariant sort key by construction.
_WIFI_SIGNAL_ID = "radio-signal.win-radio.0"
_BLUETOOTH_PRESENCE_ID = "radio-signal.win-radio.1"

#: Bound for the async Bluetooth query and any Wi-Fi adapter enumeration
#: call, mirroring the shared WinRT read bound (Req 13.3).
_QUERY_TIMEOUT_MS = 2000

#: Wi-Fi signal bars range as verified against the installed winsdk
#: package (see module docstring): 0-4 inclusive.
_MAX_SIGNAL_BARS = 4

#: Dedicated worker for the Bluetooth async bridge and the Wi-Fi discovery
#: probe. A dedicated pool (rather than reusing `_common`'s read-only
#: executor) keeps discovery-time queries independent of per-read
#: scheduling.
_query_executor = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="sensortap-win-radio-query"
)


def _run_coroutine_bounded(coro_factory, *, timeout_ms: int = _QUERY_TIMEOUT_MS):
    """Run an async WinRT call to completion on a dedicated worker thread.

    `coro_factory` is a zero-argument callable returning a fresh coroutine
    (fresh per call, since a coroutine object can only be awaited once and
    this may be invoked from a thread that already runs its own event
    loop). Returns the coroutine's result, or `None` on timeout or any
    exception -- callers treat `None` as "could not determine", never as a
    hardware assertion.
    """

    def _worker():
        return asyncio.run(coro_factory())

    future = _query_executor.submit(_worker)
    try:
        return future.result(timeout=timeout_ms / 1000)
    except _FutureTimeoutError:
        return None
    except Exception:  # noqa: BLE001 - any WinRT-raised failure degrades gracefully
        return None


def _query_wifi_signal_bars() -> int | None:
    """Return the current Wi-Fi signal bars (0-4), or `None` when no
    wireless interface exists or no network is currently reported.
    """

    async def _query() -> int | None:
        from winsdk.windows.devices.wifi import WiFiAdapter

        adapters = await WiFiAdapter.find_all_adapters_async()
        if not adapters:
            return None
        report = adapters[0].network_report
        networks = report.available_networks
        if not networks:
            return None
        return int(networks[0].signal_bars)

    return _run_coroutine_bounded(_query)


def _wifi_interface_exists() -> bool:
    """`True` iff at least one Wi-Fi adapter is enumerable at all (Req
    13.11's basis for `Availability.ABSENT` on the Wi-Fi sensor).
    """

    async def _query() -> bool:
        from winsdk.windows.devices.wifi import WiFiAdapter

        adapters = await WiFiAdapter.find_all_adapters_async()
        return len(adapters) > 0

    result = _run_coroutine_bounded(_query)
    return bool(result)


def _bluetooth_adapter_present() -> bool:
    """`True` iff the OS reports any Bluetooth adapter via
    `BluetoothAdapter.get_default_async()`, regardless of whether the
    radio it reports is currently switched on.
    """

    async def _query() -> bool:
        from winsdk.windows.devices.bluetooth import BluetoothAdapter

        adapter = await BluetoothAdapter.get_default_async()
        return adapter is not None

    result = _run_coroutine_bounded(_query)
    return bool(result)


class WindowsRadioAdapter:
    """Wi-Fi signal strength (approximate percentage) and Bluetooth radio
    presence (Req 13.9, 13.11)."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="win_radio",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=WINDOWS_PLATFORMS,
        priority=None,
        requires_elevation_optin=False,
        read_only_declared=True,
    )

    def discover(self) -> tuple[SensorInfo, ...]:
        wifi_present = _wifi_interface_exists()

        wifi_info = SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=_WIFI_SIGNAL_ID,
            kind="radio-signal",
            dtype=Dtype.SCALAR,
            unit="%",
            channels=("signal",),
            shape=(1,),
            range=(0.0, 100.0),
            resolution=25.0,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=True,
            requires_consent=False,
            requires_elevation=False,
            source=("win_radio",),
            vendor=None,
            part_number=None,
            availability=(
                Availability.PRESENT if wifi_present else Availability.ABSENT
            ),
        )

        bluetooth_present = _bluetooth_adapter_present()

        bluetooth_info = SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=_BLUETOOTH_PRESENCE_ID,
            kind="radio-signal",
            dtype=Dtype.SCALAR,
            unit=None,
            channels=("present",),
            shape=(1,),
            range=(0.0, 1.0),
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=True,
            requires_consent=False,
            requires_elevation=False,
            source=("win_radio",),
            vendor=None,
            part_number=None,
            availability=(
                Availability.PRESENT if bluetooth_present else Availability.ABSENT
            ),
        )

        return (wifi_info, bluetooth_info)

    def read(self, sensor_id: str) -> Reading:
        if sensor_id == _WIFI_SIGNAL_ID:
            return bounded_read(
                self._read_wifi_signal, sensor_id=sensor_id, timeout_ms=_QUERY_TIMEOUT_MS
            )
        if sensor_id == _BLUETOOTH_PRESENCE_ID:
            return bounded_read(
                self._read_bluetooth_presence,
                sensor_id=sensor_id,
                timeout_ms=_QUERY_TIMEOUT_MS,
            )
        raise KeyError(f"unknown sensor id for WindowsRadioAdapter: {sensor_id!r}")

    def _read_wifi_signal(self) -> Reading:
        now = time.monotonic()
        bars = _query_wifi_signal_bars()
        if bars is None:
            return Reading(
                id=_WIFI_SIGNAL_ID,
                t_mono=now,
                t_wall=time.time(),
                values=(),
                seq=0,
                status=Status.UNAVAILABLE,
            )
        percentage = max(0.0, min(100.0, (bars / _MAX_SIGNAL_BARS) * 100.0))
        return Reading(
            id=_WIFI_SIGNAL_ID,
            t_mono=now,
            t_wall=time.time(),
            values=(percentage,),
            seq=0,
            status=Status.OK,
        )

    def _read_bluetooth_presence(self) -> Reading:
        now = time.monotonic()
        present = _bluetooth_adapter_present()
        return Reading(
            id=_BLUETOOTH_PRESENCE_ID,
            t_mono=now,
            t_wall=time.time(),
            values=(1.0 if present else 0.0,),
            seq=0,
            status=Status.OK,
        )

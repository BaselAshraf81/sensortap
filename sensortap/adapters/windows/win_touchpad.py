"""`WindowsTouchpadAdapter`: touchpad-adjacent sensors on Windows (Req 7.1,
9.4, 13.9, 13.10).

Verified WinRT API surface (winsdk==1.0.0b10, Python 3.12, checked
interactively against the installed package rather than assumed):

- ``winsdk.windows.devices.input`` (**not** ``humaninterfacedevice`` --
  that module only exposes generic ``HidDevice``/report classes with no
  pointer-classification surface at all, so it was not used here) exposes
  ``PointerDevice``, ``PointerDeviceType`` (``MOUSE``, ``PEN``, ``TOUCH``)
  and ``TouchCapabilities``.
- ``PointerDevice.get_pointer_devices()`` returns a **synchronous**, plain
  list of `PointerDevice` device objects -- this is `DeviceInformation`-
  style metadata enumeration (each object exposes only properties already
  known to the OS: `pointer_device_type`, `max_contacts`,
  `is_integrated`, `physical_device_rect`, `screen_rect`,
  `supported_usages`), not a device handle. No `open()`/`activate()`-style
  call exists on `PointerDevice` at all, so calling this during `discover()`
  does not violate the "no device handles during discovery" rule (Req 9.4).
- **There is no precision-touchpad-specific capability flag anywhere in
  this surface.** Windows' own "is this a Precision Touchpad" distinction
  (as shown in Settings > Devices > Touchpad) is a Human Input Device
  top-level-collection / driver capability historically surfaced only via
  registry (`HKLM\\SYSTEM\\CurrentControlSet\\Services\\...\\Parameters\\
  PrecisionTouchPad`) or a vendor/PTP-specific WMI class -- not via any
  WinRT API. This was verified by inspecting `dir()` on every class in
  `winsdk.windows.devices.input` and `winsdk.windows.devices.
  humaninterfacedevice`: no member of either module distinguishes a
  precision touchpad from any other pointing device.
- What **is** available and load-bearing here: a `PointerDevice` whose
  `pointer_device_type == PointerDeviceType.TOUCH` **and**
  `is_integrated is True` is, on every laptop this was checked against,
  the built-in touchpad's digitizer (a discrete external touchscreen also
  reports `TOUCH` but `is_integrated == False`; a mouse reports `MOUSE`).
  This is a documented best-effort heuristic, not a verified "this is a
  PTP-certified touchpad" signal -- Windows does not expose that distinction
  through any API reachable from `winsdk`. The two touchpad-family sensors
  below are built from this heuristic and are honest about its limits in
  their `id`s and this docstring; they do not claim to detect "precision"
  touchpad capability specifically, only "an integrated touch pointer
  device exists," which this module's constants and comments call the
  proxy it actually is.
- `TouchCapabilities` (a different, older, non-`PointerDevice` class) has
  only two properties, `touch_present` and `contacts` (the max simultaneous
  contact count across *all* touch input on the system, not scoped to the
  touchpad specifically) -- confirmed via `dir()`. `PointerDevice.
  max_contacts` is the equivalent value scoped to one specific device, and
  is preferred here for that reason when an integrated touch pointer
  device is found.

Three sensors are emitted, all `kind = "touchpad"` (confirmed present in
`schema/kinds.py`'s `KIND_VOCABULARY`):

1. ``touchpad.win-ptp.0``: the "integrated touch pointer device found"
   proxy described above, modelled as a `scalar` 1.0/0.0 presence flag
   (same convention as `win_radio.py`'s Bluetooth-presence sensor, reused
   here for consistency across the Windows adapter family).
   `Availability.PRESENT` when at least one such device is enumerated,
   `Availability.ABSENT` otherwise. `requires_consent = False`: a
   yes/no capability flag carries no personal information.
2. ``touchpad.win-contacts.0``: the enumerated integrated touch device's
   `max_contacts` as a `scalar` reading, unitless (a count).
   `Availability.PRESENT` only when the same integrated touch device is
   found (its `max_contacts` is metadata, not a live finger count -- no
   HID input report is opened or read); `Availability.ABSENT` otherwise.
   `requires_consent = False`: a static maximum-contacts capability number,
   not a live gesture/finger-position stream.
3. ``touchpad.win-capimg.0``: the raw capacitive touch image (the 2D
   pressure/contact grid a touchpad's controller senses before Windows'
   own digitizer stack reduces it to point/gesture events). This is
   **never** obtainable through any standard Windows API -- it is exposed,
   if at all, only through vendor-proprietary SDKs (e.g. Synaptics' or
   Elan's own driver interfaces), never through WinRT, Win32 HID reports,
   or any other documented OS surface. Per Req 13.10 and the project's
   "degrade, never guess" philosophy, this is reported as a full,
   schema-valid `SensorInfo` record with `availability =
   Availability.ABSENT` -- rather than omitted -- naming the reason in
   this docstring and the `_CAPACITIVE_IMAGE_ABSENT_REASON` constant below
   (`SensorInfo`/`Reading` have no free-text reason field, so, consistent
   with `_common.py`'s `bounded_read` and `win_battery.py`'s voltage/temp
   handling, the reason lives here in code comments, not in a schema
   field). `dtype = Dtype.MATRIX` with an illustrative placeholder
   `shape = (15, 10)` (a plausible small touchpad sensing grid resolution;
   documented as illustrative since real hardware specifics are never
   obtainable through this path, and `ABSENT` sensors still need a
   schema-valid shape declared). `requires_consent = True` (Req 7.1): a
   capacitive image is a raw biometric/gesture-adjacent signal, unlike the
   two capability sensors above.

All three share the fixed fallback instance qualifier ``"0"`` (Req 3.6):
this module reports at most one touchpad-family record set per machine
(mirroring `win_radio.py`'s singleton-device reasoning) rather than
enumerating multiple pointer devices per sensor.
"""

from __future__ import annotations

import time
from typing import ClassVar

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION, AdapterMeta
from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows._common import bounded_read
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo
from sensortap.schema.version import SCHEMA_VERSION

#: `kind` string, taken verbatim from `schema.kinds.KIND_VOCABULARY`.
_KIND_TOUCHPAD = "touchpad"

#: Sensor_Ids exposed by this adapter (Req 3.5's fixed fallback instance
#: qualifier "0" -- see module docstring).
_PTP_PRESENCE_ID = "touchpad.win-ptp.0"
_CONTACTS_ID = "touchpad.win-contacts.0"
_CAPACITIVE_IMAGE_ID = "touchpad.win-capimg.0"

#: Bound for the pointer-device enumeration query, mirroring the shared
#: WinRT read bound (Req 13.3).
_QUERY_TIMEOUT_MS = 2000

#: Illustrative-only placeholder shape for the never-obtainable capacitive
#: image (see module docstring): a plausible small touchpad sensing grid
#: resolution, not a measurement of any real hardware.
_CAPACITIVE_IMAGE_SHAPE = (15, 10)

#: Channel names for the capacitive image's matrix columns (Req 2.7's
#: channel-count rule: a `matrix` dtype's channel count must equal the
#: shape's last dimension). Named generically since no real per-column
#: semantics exist for an illustrative placeholder shape.
_CAPACITIVE_IMAGE_CHANNELS = tuple(
    f"col-{i}" for i in range(_CAPACITIVE_IMAGE_SHAPE[-1])
)

#: Human-readable reason recorded here (not on the schema, which has no
#: free-text field) for why the capacitive image sensor is always ABSENT
#: (Req 13.10): the raw capacitive image is exposed, if at all, only
#: through vendor-proprietary interfaces (e.g. Synaptics/Elan SDKs), never
#: through any standard Windows API reachable from this adapter.
_CAPACITIVE_IMAGE_ABSENT_REASON = (
    "raw capacitive touch image access requires a vendor-specific "
    "interface (e.g. a Synaptics or Elan proprietary SDK); no standard "
    "Windows API (WinRT, Win32 HID reports, or otherwise) exposes it"
)


def _find_integrated_touch_pointer_device():
    """Return the first enumerated `PointerDevice` whose type is `TOUCH`
    and which is marked integrated, or `None` if none is found.

    Metadata-only enumeration (Req 9.4): `PointerDevice.
    get_pointer_devices()` returns already-known OS device metadata, never
    a device handle -- see module docstring for the verification detail.
    """

    from winsdk.windows.devices.input import PointerDevice, PointerDeviceType

    devices = PointerDevice.get_pointer_devices()
    for device in devices:
        if (
            device.pointer_device_type == PointerDeviceType.TOUCH
            and device.is_integrated
        ):
            return device
    return None


class WindowsTouchpadAdapter:
    """Touchpad-family sensors on Windows: integrated-touch-pointer
    presence, its maximum contact count, and the (always unobtainable)
    raw capacitive image (Req 7.1, 9.4, 13.9, 13.10)."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="win_touchpad",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=WINDOWS_PLATFORMS,
        priority=None,
        requires_elevation_optin=False,
        read_only_declared=True,
    )

    def discover(self) -> tuple[SensorInfo, ...]:
        device = _find_integrated_touch_pointer_device()
        present = device is not None
        max_contacts = getattr(device, "max_contacts", None) if device else None

        ptp_info = SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=_PTP_PRESENCE_ID,
            kind=_KIND_TOUCHPAD,
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
            source=("win-ptp",),
            vendor=None,
            part_number=None,
            availability=Availability.PRESENT if present else Availability.ABSENT,
        )

        contacts_info = SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=_CONTACTS_ID,
            kind=_KIND_TOUCHPAD,
            dtype=Dtype.SCALAR,
            unit=None,
            channels=("max_contacts",),
            shape=(1,),
            range=None,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=False,
            requires_consent=False,
            requires_elevation=False,
            source=("win-contacts",),
            vendor=None,
            part_number=None,
            availability=Availability.PRESENT if present else Availability.ABSENT,
        )

        capacitive_image_info = SensorInfo(
            schema_version=SCHEMA_VERSION,
            id=_CAPACITIVE_IMAGE_ID,
            kind=_KIND_TOUCHPAD,
            dtype=Dtype.MATRIX,
            unit=None,
            channels=_CAPACITIVE_IMAGE_CHANNELS,
            shape=_CAPACITIVE_IMAGE_SHAPE,
            range=None,
            resolution=None,
            rate_hz=RateSpec(default=None, min=None, max=None),
            delivery=Delivery.POLL,
            derived=False,
            requires_consent=True,
            requires_elevation=False,
            source=("win-capimg",),
            vendor=None,
            part_number=None,
            availability=Availability.ABSENT,
        )

        self._max_contacts = max_contacts

        return (ptp_info, contacts_info, capacitive_image_info)

    def read(self, sensor_id: str) -> Reading:
        if sensor_id == _PTP_PRESENCE_ID:
            return bounded_read(
                self._read_ptp_presence, sensor_id=sensor_id, timeout_ms=_QUERY_TIMEOUT_MS
            )
        if sensor_id == _CONTACTS_ID:
            return bounded_read(
                self._read_contacts, sensor_id=sensor_id, timeout_ms=_QUERY_TIMEOUT_MS
            )
        if sensor_id == _CAPACITIVE_IMAGE_ID:
            # Never exposed by any standard Windows API (see module
            # docstring) -- every read degrades to unavailable, consistent
            # with the discovery-time ABSENT record.
            return Reading(
                id=sensor_id,
                t_mono=time.monotonic(),
                t_wall=time.time(),
                values=(),
                seq=0,
                status=Status.UNAVAILABLE,
            )
        raise KeyError(f"unknown sensor id for WindowsTouchpadAdapter: {sensor_id!r}")

    def _read_ptp_presence(self) -> Reading:
        device = _find_integrated_touch_pointer_device()
        return Reading(
            id=_PTP_PRESENCE_ID,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            values=(1.0 if device is not None else 0.0,),
            seq=0,
            status=Status.OK,
        )

    def _read_contacts(self) -> Reading:
        device = _find_integrated_touch_pointer_device()
        if device is None:
            return Reading(
                id=_CONTACTS_ID,
                t_mono=time.monotonic(),
                t_wall=time.time(),
                values=(),
                seq=0,
                status=Status.UNAVAILABLE,
            )
        return Reading(
            id=_CONTACTS_ID,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            values=(float(device.max_contacts),),
            seq=0,
            status=Status.OK,
        )

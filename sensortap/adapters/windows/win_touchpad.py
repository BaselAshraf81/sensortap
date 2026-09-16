"""`WindowsTouchpadAdapter`: touchpad-adjacent sensors on Windows (Req 7.1,
9.4, 13.9, 13.10).

Verified WinRT API surface (winsdk==1.0.0b10, Python 3.12, checked
interactively against the installed package rather than assumed):

- ``winsdk.windows.devices.input`` exposes ``PointerDevice``,
  ``PointerDeviceType`` (``MOUSE``, ``PEN``, ``TOUCH``) and
  ``TouchCapabilities``.
- ``winsdk.windows.devices.humaninterfacedevice`` exposes ``HidDevice``,
  whose ``get_device_selector(usage_page, usage_id)`` builds an AQS query
  for a HID **usage page / usage** pair. This *is* a device-classification
  surface, and it is the standardised one: an earlier version of this
  docstring claimed the module had "no pointer-classification surface at
  all", which was wrong and cost this adapter a real bug (see below).
- ``PointerDevice.get_pointer_devices()`` returns a **synchronous**, plain
  list of `PointerDevice` device objects -- this is `DeviceInformation`-
  style metadata enumeration (each object exposes only properties already
  known to the OS: `pointer_device_type`, `max_contacts`,
  `is_integrated`, `physical_device_rect`, `screen_rect`,
  `supported_usages`), not a device handle. No `open()`/`activate()`-style
  call exists on `PointerDevice` at all, so calling this during `discover()`
  does not violate the "no device handles during discovery" rule (Req 9.4).
Detection uses two independent paths, unioned, because neither is
sufficient alone.

**Path 1 (primary): HID usage page.** A Windows Precision Touchpad is
*defined* by the HID specification as a device exposing usage page
``0x0D`` (Digitizer) with usage ``0x05`` (Touch Pad).
``HidDevice.get_device_selector(0x0D, 0x05)`` fed to
``DeviceInformation.find_all_async`` asks the OS for exactly that, with no
vendor knowledge involved. Usage ``0x04`` is Touch Screen and ``0x02`` is
Pen, so the same mechanism *separates* a touchpad from a touchscreen or a
pen digitizer rather than conflating them. This is enumeration, not
acquisition: `find_all_async` returns `DeviceInformation` metadata the OS
already holds and opens no device handle, so it is legal during
`discover()` (Req 9.4).

**Path 2 (fallback, retained): integrated touch `PointerDevice`.** A
`PointerDevice` whose `pointer_device_type == PointerDeviceType.TOUCH`
**and** `is_integrated is True`. This was the original sole detection
path. It is kept because it is the only path that also yields a real
`max_contacts` value, and because it may succeed on a machine where HID
interface enumeration is restricted.

**Why both, and what the union fixes.** Path 2 alone produced a false
negative on real hardware (verified on a Dell G3 3779). That laptop's
touchpad exposes *two* HID top-level collections, and `PointerDevice`
surfaced only the first:

- ``HID\\DELL0886&Col01`` -- Generic Desktop / Mouse (usage page 0x01,
  usage 0x02)
- ``HID\\DELL0886&Col02`` -- Digitizer / Touch Pad (usage page 0x0D,
  usage 0x05)

`PointerDevice.get_pointer_devices()` returned exactly one device on that
machine: `type=MOUSE`, `is_integrated=False`, `max_contacts=1`, advertising
Generic Desktop X/Y usages only -- i.e. Col01, indistinguishable by any
field from an actual USB mouse. The touchpad's *identity* lives in Col02,
which `PointerDevice` never exposed at all. So the touchpad was
undiscoverable through Path 2 by construction, not by heuristic weakness.
Path 1 finds Col02 directly.

Two paths deliberately *not* used, and why:

- **The ``PrecisionTouchPad`` registry key.** It records that the
  touchpad settings UI is available, which is a driver/settings fact, not
  a device-presence fact. It survives the device being disabled or
  removed, so it reports touchpads that are not there.
- **A PnP hardware-id list (``ACPI\\DELL0886`` and friends).** That is a
  vendor allowlist. It would work on the maintainer's laptop and on no
  other model, which is the precise failure mode this fix exists to
  remove.
- `TouchCapabilities` (a different, older, non-`PointerDevice` class) has
  only two properties, `touch_present` and `contacts` (the max simultaneous
  contact count across *all* touch input on the system, not scoped to the
  touchpad specifically) -- confirmed via `dir()`. It is not used as a
  contact-count source: on the Dell G3 above it reports
  `touch_present=0, contacts=0` while a touchpad is demonstrably present,
  so it tracks touchscreen-style touch input rather than touchpads.
  `PointerDevice.max_contacts` is the equivalent value scoped to one
  specific device, and is the only contact-count source used here.
- **`max_contacts` is not reachable through Path 1.** The
  `DeviceInformation` property set returned for a HID interface carries
  only shell metadata (`System.ItemNameDisplay`,
  `System.Devices.DeviceInstanceId`, icons, `InterfaceEnabled`,
  `IsDefault`) -- enumerated and confirmed. A maximum-contacts figure lives
  in the HID report descriptor, which requires *opening* the device to
  parse, and opening a device during `discover()` is forbidden (Req 9.4).
  So when only Path 1 finds the touchpad, the contact count is genuinely
  unknown and is reported `ABSENT` rather than guessed at -- presence and
  contact count are separate sensors precisely so one can be known while
  the other is not.
- **Device names from HID enumeration are never read.** On the machine
  above, `DeviceInformation.name` for the touchpad's HID collections is the
  *hostname* (`WIN-D9FBFB1Q4IF`), not a product name. This adapter counts
  matching devices and reads no name or instance-path field, so no machine
  identifier can reach a `SensorInfo`, a `Reading`, or `sensortap doctor`
  output.

Three sensors are emitted, all `kind = "touchpad"` (confirmed present in
`schema/kinds.py`'s `KIND_VOCABULARY`):

1. ``touchpad.win-ptp.0``: touchpad presence, modelled as a `scalar`
   1.0/0.0 flag (same convention as `win_radio.py`'s Bluetooth-presence
   sensor, reused here for consistency across the Windows adapter family).
   `Availability.PRESENT` when *either* detection path finds a touchpad,
   `Availability.ABSENT` when neither does. `requires_consent = False`: a
   yes/no capability flag carries no personal information.
2. ``touchpad.win-contacts.0``: the maximum simultaneous contact count as a
   `scalar` reading, unitless (a count). This is device metadata, not a
   live finger count -- no HID input report is opened or read.
   `Availability.PRESENT` only when Path 2 supplied a real `max_contacts`;
   `Availability.ABSENT` when the touchpad was found only through Path 1
   (count unknown, see above) or not found at all. A touchpad that is
   `PRESENT` on sensor 1 while sensor 2 is `ABSENT` is therefore an
   expected, meaningful state: "there is a touchpad, and its contact
   count is not obtainable without opening it."
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

import asyncio
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

#: HID usage page 0x0D is "Digitizer" and usage 0x05 within it is
#: "Touch Pad", per the USB-IF HID Usage Tables. This pair is the
#: standardised definition of a Windows Precision Touchpad and carries no
#: vendor knowledge. Usage 0x04 (Touch Screen) and 0x02 (Pen) are
#: deliberately *not* matched, so a touchscreen or pen digitizer is not
#: mistaken for a touchpad.
_HID_USAGE_PAGE_DIGITIZER = 0x0D
_HID_USAGE_TOUCH_PAD = 0x05

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


def _run_async(coro_factory):
    """Bridge a `winsdk` awaitable to synchronous code via `asyncio.run`,
    bounded at `_QUERY_TIMEOUT_MS`.

    Mirrors the identical shim in `winrt_audio.py`: `winsdk` types a WinRT
    `IAsyncOperation` as *awaitable* but not as a `coroutine`, so
    `asyncio.run()` needs a small local `async def` wrapper.
    """

    async def _shim():
        return await asyncio.wait_for(
            coro_factory(), timeout=_QUERY_TIMEOUT_MS / 1000.0
        )

    return asyncio.run(_shim())


def _enumerate_touchpad_hid_collections():
    """Return the `DeviceInformation` records for every HID
    Digitizer/Touch Pad collection the OS knows about (detection Path 1).

    Enumeration only (Req 9.4): `DeviceInformation.find_all_async` returns
    metadata the OS already holds and opens no device handle.
    """

    from winsdk.windows.devices.enumeration import DeviceInformation
    from winsdk.windows.devices.humaninterfacedevice import HidDevice

    selector = HidDevice.get_device_selector(
        _HID_USAGE_PAGE_DIGITIZER, _HID_USAGE_TOUCH_PAD
    )

    def _enumerate():
        return DeviceInformation.find_all_async(selector, [])

    return _run_async(_enumerate)


def _any_enabled(devices) -> bool:
    """Return whether any enumerated device is enabled.

    Reads `is_enabled` and nothing else. In particular it never reads
    `DeviceInformation.name`: for these system-claimed HID collections
    Windows reports the machine's *hostname* there rather than a product
    name (see module docstring), and no machine identifier may reach a
    `SensorInfo`, a `Reading`, or `sensortap doctor` output. Kept as a
    separate pure function so that obligation is directly testable.
    """

    return any(device.is_enabled for device in devices)


def _touchpad_hid_collection_present() -> bool:
    """Return whether at least one enabled HID Digitizer/Touch Pad
    collection exists (detection Path 1)."""

    return _any_enabled(_enumerate_touchpad_hid_collections())


def _detect_touchpad() -> tuple[bool, int | None]:
    """Return `(present, max_contacts)` for this machine's touchpad.

    Unions the two detection paths described in the module docstring, in
    the order that yields the most information: Path 2 first, because it is
    the only one that also carries a real `max_contacts`; Path 1 second,
    because it is the only one that finds a touchpad whose digitizer
    collection `PointerDevice` does not surface.

    `max_contacts` is `None` when a touchpad was found but its contact
    count is not obtainable without opening the device.

    Each path is guarded independently: a WinRT surface missing or raising
    on some Windows build must not make the other path unreachable, and
    must not fail discovery for the whole adapter.
    """

    try:
        device = _find_integrated_touch_pointer_device()
    except Exception:  # noqa: BLE001 - a broken path degrades, never propagates
        device = None
    if device is not None:
        max_contacts = getattr(device, "max_contacts", None)
        return True, max_contacts

    try:
        if _touchpad_hid_collection_present():
            # Found through the digitizer collection, which carries no
            # contact count -- honest None rather than a guessed number.
            return True, None
    except Exception:  # noqa: BLE001 - see above
        pass

    return False, None


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
        present, max_contacts = _detect_touchpad()

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
            # PRESENT only when a real count was obtained. A touchpad found
            # through the HID digitizer path has no reachable contact count
            # (see module docstring), so this stays ABSENT while the
            # presence sensor above is PRESENT.
            availability=(
                Availability.PRESENT
                if max_contacts is not None
                else Availability.ABSENT
            ),
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
        present, _ = _detect_touchpad()
        return Reading(
            id=_PTP_PRESENCE_ID,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            values=(1.0 if present else 0.0,),
            seq=0,
            status=Status.OK,
        )

    def _read_contacts(self) -> Reading:
        _, max_contacts = _detect_touchpad()
        if max_contacts is None:
            # Either no touchpad, or one found through the HID digitizer
            # path whose contact count is not obtainable without opening
            # it. Both are honestly "unavailable", never a guessed number.
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
            values=(float(max_contacts),),
            seq=0,
            status=Status.OK,
        )

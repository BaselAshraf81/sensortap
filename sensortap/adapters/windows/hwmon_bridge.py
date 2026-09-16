"""Hardware-monitoring adapter bridging the Windows Helper_Process's
sensor tree onto `SensorInfo`/`Reading` (Req 13.5, 13.12, 15.4, 15.5).

This is the final piece of the Windows helper family: `helper_src/` (the
.NET helper, task 9.1) reports a flat list of `HelperSensorRecord`s over
the named pipe; `protocol.py` (task 9.2) encodes/decodes the wire format;
`client.py` (task 9.3) owns the pipe lifecycle and exposes `HelperClient`
with a `state` property and `send_list()`/`send_read()`. This module maps
that helper sensor tree onto the closed `kind` vocabulary and the
Sensor_Id grammar, and implements the `Adapter` Protocol so the registry
can load it like any other adapter.

Sensor_Id instance qualifier -- hashing the helper's identifier (Req 3.6,
13.5)
--------------------------------------------------------------------------
Task 9.4's own instructions distinguish "hashing the helper-reported
persistent hardware identifier" from "a bus-probe-order index". The
helper's `HelperSensorRecord.id` mirrors LibreHardwareMonitorLib's own
`ISensor.Identifier` string (per task 9.1/9.2, e.g.
``/amdcpu/0/temperature/0``). That string does contain index-like
components (the `0` after `amdcpu`, the `0` after `temperature`), but
those indices are LibreHardwareMonitorLib's own stable enumeration
positions for a given hardware topology on a given machine, not an index
sensortap itself derives from probing a bus in whatever order the OS
happens to return devices this run. The concern Req 3.6 raises is about
*sensortap* manufacturing its own run-dependent ordering key; it is not a
claim that any identifier containing a digit is disqualified. Adapters
elsewhere in this codebase (e.g. `_common.py`'s `device_instance_hash`)
already hash a backend-reported "device_id"/"identifier" string of
exactly this shape without re-deriving it from ordering.

Conclusion: `HelperSensorRecord.id` is treated as the persistent hardware
identifier for this backend, and is fed directly to
`registry.ids.instance_hash()`. This is consistent with task 9.1/9.2's own
choice to mirror LibreHardwareMonitorLib's `Identifier` field rather than
inventing a different identifier, and satisfies "never a bus-probe-order
index" because sensortap performs no probing or ordering of its own here
-- it hashes the string the helper already reports, whole.

If LibreHardwareMonitorLib's internal identifier assignment ever turned
out not to be stable across runs on unchanged hardware, that would be a
correctness gap in the upstream library's own contract, not something
this adapter could fix by deriving a different key -- there is no
alternative persistent identifier available from the helper to fall back
to. `HelperSensorRecord.hardware_id` is the same kind of index-bearing
string (e.g. ``/amdcpu/0``) and offers no additional stability guarantee,
so it is not preferred over `id`.

Elevation gating (Req 13.12, 15.4, 15.5)
--------------------------------------------------------------------------
Task 9.1's `HardwareMonitor.cs` deliberately left `IsMotherboardEnabled =
false`, specifically to avoid ever reaching LibreHardwareMonitorLib's
internal Ring0 driver-install code path. CPU, GPU, storage and memory
sensors -- the categories the helper as built actually reports -- do not
require the WinRng0 driver; only motherboard/Super-IO sensors typically
do, and that whole category is currently disabled at the source. The
practical consequence: with today's helper, **no** sensor this adapter
sees ever needs elevation, so `requires_elevation` is `False` for every
record it currently emits.

This module still implements the gate generically rather than hardcoding
`False` with no path to ever being `True`: `_requires_elevation()` below
inspects `HelperSensorRecord.hardware_type` for the one category
(`"Motherboard"`, LibreHardwareMonitorLib's own `HardwareType` enum
member name) that would require Ring0 access if that category were ever
re-enabled upstream. Since the C# helper does not currently report that
category at all, this check is a no-op in practice today -- it exists so
that if `HardwareMonitor.cs` is later revised to re-enable motherboard
sensors behind a real Ring0 presence check, this Python side requires no
change. No shim-detection mechanism is fabricated: there is currently
nothing on the wire that would ever make this check evaluate `True`, and
that honest limitation is documented rather than hidden. Per Req 13.12 /
15.5, if this check ever does trip and no Ring0_Shim is present, the
correct behaviour is `availability = Availability.UNAVAILABLE` with a
reason naming the missing shim rather than installing anything --
implemented in `_ring0_unavailable_reason()`, also currently unreachable
given today's helper but present for the same forward-compatibility
reason.

Adapter-level `requires_elevation_optin` (Req 8.3 vs 13.12)
--------------------------------------------------------------------------
`AdapterMeta.requires_elevation_optin` gates the *whole adapter* behind an
explicit opt-in at load time (Req 8.3); `SensorInfo.requires_elevation`
gates individual *sensors* (Req 13.12). This adapter does not need
elevation to load or to enumerate the sensors it currently reports (the
helper process itself runs unprivileged), so `requires_elevation_optin`
is left at its default `False` -- this adapter stays in the default
adapter set. Only if/when individual sensors start requiring the Ring0
shim would their per-sensor `requires_elevation` flip to `True`, per the
reasoning above.

`discover()` when the helper is not running
--------------------------------------------------------------------------
Unlike the WinRT sensor-class pattern in `_common.py` (which always emits
one record per queried class, even absent), this adapter has no fixed set
of sensor classes to enumerate ahead of contacting the helper -- the
entire sensor list is dynamic, reported by the helper at `list` time.
When `HelperClient.state != "running"`, there is nothing to iterate to
build placeholder records from, so `discover()` returns an empty tuple.
The registry's `BackendStatus`/`StatusTracker` (task 2.7), fed from
`HelperClient.state`/`failure_reason`, is how "the helper isn't
available" surfaces -- not a per-sensor `unavailable` record, since no
sensor identities are known yet.

`read()` bounding
--------------------------------------------------------------------------
`bounded_read()` from `_common.py` is *not* reused here. It exists to
bound WinRT calls that can block indefinitely with no timeout of their
own. The helper transport already has its own bound: `HelperClient`'s
pipe I/O (`_read_pipe_chunk`) is itself timeout-bounded, and
`send_read()` either returns a `HelperResponse` or raises
(`HelperProtocolError`/`HelperUnavailableError`) within that bound.
Wrapping a second, independent timeout around an already-bounded,
synchronous call would only add a redundant thread hop with no behaviour
difference in the common case, and would *reduce* clarity in the failure
case (two different timeout values could disagree with no benefit). This
adapter instead calls `HelperClient.send_read()` directly and maps any
exception, or an `ok=False`/missing-value response, straight to
`Status.UNAVAILABLE`.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, ClassVar

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION, AdapterMeta
from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows.helper.protocol import HelperSensorRecord

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sensortap.adapters.windows.helper.client import HelperClient
from sensortap.registry.ids import instance_hash
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.kinds import FAN_SPEED_UNIT
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo
from sensortap.schema.version import SCHEMA_VERSION

#: Source-qualifier segment for every Sensor_Id this adapter mints (Req
#: 3.1's second grammar segment). One fixed literal: every sensor this
#: adapter reports comes through the one Helper_Process backend, so there
#: is no finer per-sensor-family source split the way the WinRT family
#: needs one per sensor class.
_SOURCE_QUALIFIER = "hwmon"

#: Maps the helper's `sensor_type` string (LibreHardwareMonitorLib's own
#: `SensorType` enum member name, verified against `helper_src`'s sensor
#: enumeration in task 9.1) onto the closed `kind` vocabulary
#: (`schema/kinds.py`). Only categories the C# helper actually enumerates
#: (Req 9.1: "temperature, fan, voltage, clock and load", plus current and
#: power per this task's own mapping list) are listed; an unrecognised
#: `sensor_type` is skipped rather than guessed at (see `_map_kind`).
_SENSOR_TYPE_TO_KIND: dict[str, str] = {
    "Temperature": "temp",
    "Fan": "fan",
    "Voltage": "voltage",
    "Current": "current",
    "Power": "power",
    "Clock": "clock",
    "Load": "load",
}

#: Fixed unit per `kind`, using the exact tokens pinned in
#: `schema/kinds.py` (Open Decisions 5 and 6). Clock is reported in MHz by
#: LibreHardwareMonitorLib (verified against `helper_src`'s sensor
#: enumeration, task 9.1); load has no physical unit and is a percentage.
_UNIT_BY_KIND: dict[str, str] = {
    "temp": "degC",
    "fan": FAN_SPEED_UNIT,
    "voltage": "V",
    "current": "A",
    "power": "W",
    "clock": "MHz",
    "load": "%",
}

#: LibreHardwareMonitorLib `HardwareType` member naming the one category
#: that requires the Ring0 (WinRing0) kernel driver -- motherboard/Super-IO
#: chip sensors. `HardwareMonitor.cs` (task 9.1) currently disables this
#: category entirely (`IsMotherboardEnabled = false`), so the helper never
#: reports a record with this `hardware_type` today; this constant exists
#: so the elevation gate below has a concrete, named condition to check
#: rather than an always-false stub, per this task's instruction not to
#: fabricate a fake detection mechanism while still wiring the gate
#: correctly and generically.
_RING0_DEPENDENT_HARDWARE_TYPE = "Motherboard"


def _map_kind(sensor_type: str) -> str | None:
    """Map a helper `sensor_type` string onto the closed `kind`
    vocabulary, or `None` for a category this adapter does not (yet)
    understand -- such a record is skipped rather than guessed at."""

    return _SENSOR_TYPE_TO_KIND.get(sensor_type)


def _requires_elevation(record: HelperSensorRecord) -> bool:
    """Whether `record` is reachable only through an already-installed
    Ring0_Shim (Req 13.12).

    See the module docstring's "Elevation gating" section: this is
    currently always `False` given the helper's disabled motherboard
    category, but the check is written against the record's reported
    `hardware_type` rather than hardcoded, so it activates correctly if
    the upstream helper ever starts reporting that category.
    """

    return record.hardware_type == _RING0_DEPENDENT_HARDWARE_TYPE


def _ring0_unavailable_reason(record: HelperSensorRecord) -> str:
    """Reason text for a Ring0-dependent sensor when no shim is present
    (Req 13.12, 15.4, 15.5). Names the missing shim; installs nothing."""

    return (
        f"sensor {record.id!r} requires an already-installed Ring0_Shim "
        "(WinRing0 kernel driver) that sensortap does not install, "
        "register or start; no such shim was detected"
    )


def _sensor_id_for(record: HelperSensorRecord, kind: str) -> str:
    """Build the 3-segment Sensor_Id for one helper record, hashing the
    helper's persistent identifier per this module's documented
    reasoning (never a sensortap-derived probe-order index)."""

    qualifier = instance_hash(record.id)
    return f"{kind}.{_SOURCE_QUALIFIER}.{qualifier}"


def _range_for(record: HelperSensorRecord) -> tuple[float, float] | None:
    if record.min is not None and record.max is not None:
        return (record.min, record.max)
    return None


class WindowsHardwareMonitorAdapter:
    """Hardware-monitoring adapter serving `temp`, `fan`, `voltage`,
    `current`, `power`, `clock` and `load` sensors through the
    Helper_Process (Req 13.5, 13.12, 15.4, 15.5)."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="hwmon_bridge",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=WINDOWS_PLATFORMS,
        read_only_declared=True,
        # Left at the default `False`: this adapter needs no elevation to
        # load or to enumerate the sensors it currently reports -- see the
        # module docstring's "Adapter-level requires_elevation_optin"
        # section. Individual sensors, not the whole adapter, are what
        # Req 13.12 gates.
        requires_elevation_optin=False,
    )

    def __init__(self, *, helper_client: "HelperClient | None" = None) -> None:
        # `helper_client` is injectable for tests (a fake/mock in place of
        # a real `HelperClient`), matching task 9.3's own lazy-start
        # pattern rather than reimplementing pipe lifecycle here. The
        # real `HelperClient` is imported lazily (not at module scope) so
        # that this module -- and tests exercising it with a fake client
        # -- stay importable on a machine without pywin32 installed;
        # `client.py` itself imports pywin32 unconditionally at module
        # scope since it is Windows-only and guarded by the package's
        # platform gate.
        if helper_client is not None:
            self._client = helper_client
        else:
            from sensortap.adapters.windows.helper.client import HelperClient

            self._client = HelperClient()
        self._setup_called = False
        # Sensor_Id -> helper-reported id, populated by `discover()`, same
        # pattern as `winrt_camera.py`/`winrt_audio.py`'s device maps.
        self._helper_id_by_sensor_id: dict[str, str] = {}
        self._seq_by_sensor_id: dict[str, int] = {}

    # ------------------------------------------------------------------
    # required
    # ------------------------------------------------------------------

    def discover(self) -> tuple[SensorInfo, ...]:
        # `_ensure_setup()` is also called from `setup()` at adapter-load
        # time (registry/loading.py's bounded instantiate+setup() step),
        # so the helper handshake -- which can take a couple of seconds on
        # a cold start, spawning a real child process -- happens once,
        # outside the per-call 2000 ms Discovery_Timeout window this
        # method runs under. Without that pre-warming, the first
        # `discover()` call after a fresh process start would race the
        # helper's own readiness budget against the registry's much
        # shorter discovery timeout and lose, reporting a timed-out
        # adapter even though the helper eventually comes up fine --
        # confirmed live: discovery reported `last_timeout_ms=2000` while
        # the adapter's `setup()` on its own took longer than that to
        # reach `state == "running"`.
        self._ensure_setup()

        if self._client.state != "running":
            # Nothing to iterate; BackendStatus carries the "why" (see
            # module docstring).
            return ()

        try:
            response = self._client.send_list()
        except Exception:  # noqa: BLE001 - degrade, never raise from discover()
            return ()

        if not response.ok or response.sensors is None:
            return ()

        self._helper_id_by_sensor_id = {}
        records: list[SensorInfo] = []
        for record in response.sensors:
            kind = _map_kind(record.sensor_type)
            if kind is None:
                continue

            sensor_id = _sensor_id_for(record, kind)
            elevated = _requires_elevation(record)
            no_shim_present = elevated  # see module docstring: currently
            # this branch is unreachable given today's helper, since
            # `_requires_elevation` never returns True with the current
            # C# helper's disabled motherboard category. It is written
            # this way -- rather than assuming a shim is present whenever
            # `elevated` is True -- because this adapter has no actual
            # shim-presence probe to run (there is nothing to probe for
            # today); per Req 13.12/15.4/15.5 the safe default for a
            # Ring0-dependent sensor with no confirmed shim is
            # `unavailable`, never `present`.

            availability = (
                Availability.UNAVAILABLE if no_shim_present else Availability.PRESENT
            )

            info = SensorInfo(
                schema_version=SCHEMA_VERSION,
                id=sensor_id,
                kind=kind,
                dtype=Dtype.SCALAR,
                unit=_UNIT_BY_KIND[kind],
                channels=("value",),
                shape=(1,),
                range=_range_for(record),
                resolution=None,
                rate_hz=RateSpec(default=None, min=None, max=None),
                delivery=Delivery.POLL,
                derived=False,
                requires_consent=False,
                requires_elevation=elevated,
                source=(_SOURCE_QUALIFIER,),
                vendor=None,
                part_number=None,
                availability=availability,
            )
            records.append(info)
            self._helper_id_by_sensor_id[sensor_id] = record.id
            self._seq_by_sensor_id.setdefault(sensor_id, 0)

        return tuple(records)

    def read(self, sensor_id: str) -> Reading:
        seq = self._next_seq(sensor_id)

        if self._client.state != "running":
            return _unavailable_reading(sensor_id, seq)

        helper_id = self._helper_id_by_sensor_id.get(sensor_id)
        if helper_id is None:
            raise KeyError(
                f"unknown sensor id for WindowsHardwareMonitorAdapter: {sensor_id!r}"
            )

        try:
            response = self._client.send_read([helper_id])
        except Exception:  # noqa: BLE001 - transport failure degrades, never raises
            return _unavailable_reading(sensor_id, seq)

        if not response.ok or not response.sensors:
            return _unavailable_reading(sensor_id, seq)

        value = response.sensors[0].value
        if value is None:
            return _unavailable_reading(sensor_id, seq)

        return Reading(
            id=sensor_id,
            t_mono=time.monotonic(),
            t_wall=time.time(),
            values=(float(value),),
            seq=seq,
            status=Status.OK,
        )

    # ------------------------------------------------------------------
    # optional
    # ------------------------------------------------------------------

    def setup(self) -> None:
        self._ensure_setup()

    def teardown(self) -> None:
        self._client.shutdown()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _ensure_setup(self) -> None:
        if not self._setup_called:
            self._setup_called = True
            self._client.setup()

    def _next_seq(self, sensor_id: str) -> int:
        seq = self._seq_by_sensor_id.get(sensor_id, 0)
        self._seq_by_sensor_id[sensor_id] = seq + 1
        return seq


def _unavailable_reading(sensor_id: str, seq: int) -> Reading:
    return Reading(
        id=sensor_id,
        t_mono=time.monotonic(),
        t_wall=time.time(),
        values=(),
        seq=seq,
        status=Status.UNAVAILABLE,
    )

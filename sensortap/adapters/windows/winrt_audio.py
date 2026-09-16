"""Audio capture device adapter for Windows via WinRT (`Windows.Media.Audio`
+ `Windows.Devices.Enumeration`) (Req 7.1, 7.8, 7.9, 9.4, 13.9, 15.6).

This is the audio sibling of `winrt_camera.py` (task 6.8): same
architecture, same consent/permission-denial pattern, same Req 9.4 "no
device handle during discovery" constraint, same Req 9.7 "open lazily on
first read/stream" constraint.

Verified interactively against the installed `winsdk==1.0.0b10` package
(Python 3.12, one real microphone present on the checking machine -- this
is *not* a `@pytest.mark.hardware` module, but the interactive checks used
to write it did exercise a real device):

- `winsdk.windows.devices.enumeration.DeviceInformation.find_all_async`
  accepts `DeviceClass.AUDIO_CAPTURE` (confirmed:
  `winsdk.windows.devices.enumeration.DeviceClass.AUDIO_CAPTURE` exists as
  a member alongside `VIDEO_CAPTURE`) and returns a
  `DeviceInformationCollection` of `DeviceInformation` objects exposing
  `.id` (the persistent device instance path fed to `device_instance_hash`)
  and `.name`. This enumeration touches no device handle -- it is pure
  metadata (Req 9.4).
- Getting the *real* supported sample rate/channel count for a capture
  device, in contrast to the camera's `MediaCapture` profile enumeration,
  is not obtainable from metadata alone via `winsdk`: there is no
  `DeviceInformation`-level "supported audio formats" property, and the
  only way found to learn a device's actual format
  (`AudioEncodingProperties.channel_count` / `.sample_rate`) is to open an
  `AudioGraph` device-input-node against it, which *is* an actual device
  open. This is the same Req 9.4 tension task 6.8 hit with `MediaCapture`
  profiles, resolved the same way: `discover()` reports each device from
  enumeration metadata only, with a documented illustrative default rate
  and channel count (`44100.0` Hz mono, chosen as a widely-supported
  WASAPI default -- Windows' shared-mode audio engine commonly runs at
  44100 or 48000 Hz; the true applied rate is discovered for real only
  once `open_stream()` actually opens the device). `shape` is reported as
  `(1024, 1)` as a documented placeholder block shape; the real shape used
  by a given open stream is whatever `AudioGraph` actually negotiates
  (verified below to commonly be far larger and stereo on this checking
  machine's default microphone -- e.g. `(2, 192000, 32-bit float)` device
  properties were observed -- so the placeholder is explicitly
  illustrative, not a guarantee, exactly mirroring the camera adapter's
  documented resolution placeholder).
- Real WASAPI-style capture *is* implemented for `open_stream()`, verified
  working against the real microphone on the checking machine:
  `AudioGraph.create_async(AudioGraphSettings(AudioRenderCategory.OTHER))`
  creates a graph, `graph.create_device_input_node_async(MediaCategory.OTHER,
  graph.encoding_properties, device_information)` opens the actual capture
  device (this is the real device-open, deferred to here per Req 9.7,
  never performed by `discover()`), a `create_frame_output_node()` +
  `add_outgoing_connection()` pair lets the code pull `AudioFrame`s via
  `frame_output_node.get_frame()`, and each frame's `AudioBuffer` (locked
  via `frame.lock_buffer(...)` then `.create_reference()`) supports
  Python's buffer protocol directly -- `memoryview(reference)` works and
  was confirmed to yield real float32 PCM samples (`ep.subtype` reported
  `"Float"`). This module decodes those bytes with `array.array("f", ...)`
  into the `Reading.values` tuple. No mocking, no synthetic tone: this is
  real captured audio, sample count and channel layout determined by
  whatever `AudioGraph` actually negotiates for the opened device (its
  `AudioEncodingProperties.channel_count`), not the `discover()`-time
  placeholder.
- `AudioDeviceNodeCreationStatus` has an explicit `ACCESS_DENIED` member
  (confirmed alongside `SUCCESS`, `DEVICE_NOT_AVAILABLE`,
  `FORMAT_NOT_SUPPORTED`, `UNKNOWN_FAILURE`), which is the concrete OS
  permission-denial signal this module watches for on
  `create_device_input_node_async()`'s result, mirroring how the camera
  adapter watches its own `MediaCapture` initialization failure mode.
- `winsdk`'s async operations (`create_async`,
  `create_device_input_node_async`, `find_all_async`) are awaited via
  `asyncio.run()` wrapped in a small local shim, following the same
  `_run_async` bridging pattern `winrt_orientation.py` (task 6.3)
  established for `HingeAngleSensor`.

Permission-denial-surfaces-on-next-discover() mechanism (mirrors task
6.8): `Reading.status` has no `permission_denied` member -- only
`SensorInfo.availability` does (`Availability.PERMISSION_DENIED`,
confirmed against `schema/enums.py`). An OS denial encountered while
actually opening the device (at `open_stream()` time, never at
`discover()` time) is therefore surfaced to the *caller of that failing
open* as `Status.UNAVAILABLE` via a raised `Reading`-shaped failure (see
`open_stream()` below, which raises `DeviceOpenError` since `open_stream`
has no `Reading` return value to degrade in-place the way `read()` does),
and is *cached* on the adapter instance so the *next* `discover()` call
reports that device's `SensorInfo.availability` as
`Availability.PERMISSION_DENIED` instead of `Availability.PRESENT`.

Everything this device is is `buffer`-dtype (Req 4.12): the registry never
calls `read()` for a `buffer` sensor, so this adapter's `read()` always
raises `KeyError` for any sensor id it owns, documented below at the
method. `open_stream()` is the sole real data path.
"""

from __future__ import annotations

import array
import asyncio
import threading
import time
from typing import ClassVar

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION, AdapterMeta
from sensortap.adapters.windows import WINDOWS_PLATFORMS
from sensortap.adapters.windows._common import device_instance_hash
from sensortap.registry.errors import DeviceOpenError
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo
from sensortap.schema.version import SCHEMA_VERSION

#: Sensor_Id source segment for this adapter (Req 3.5).
_SOURCE_ID = "winrt"

#: `kind` string, taken verbatim from `schema.kinds.KIND_VOCABULARY`.
_KIND = "microphone"

#: Illustrative, documented-placeholder block shape reported at discovery
#: time: (frames-per-block, channels). The real shape used by an opened
#: stream is whatever the device actually negotiates -- see module
#: docstring. 1024 frames, mono, is a common minimal default.
_DISCOVERY_SHAPE = (1024, 1)

#: Illustrative, documented-placeholder default sample rate reported at
#: discovery time, before any device is actually opened (Req 9.4). Chosen
#: as a widely-supported WASAPI shared-mode default; the real applied rate
#: is only known once `open_stream()` actually opens the device.
_DISCOVERY_DEFAULT_RATE_HZ = 44100.0

#: Default block size (in frames) requested from the audio graph's frame
#: output node when a caller does not specify one to `open_stream()`.
_DEFAULT_BLOCK_SIZE = 1024


def _run_async(coro_factory):
    """Bridge a `winsdk` awaitable to synchronous code via `asyncio.run`.

    Mirrors `winrt_orientation.py`'s `_run_async`: `winsdk` projects a
    WinRT `IAsyncOperation` as *awaitable* but not as a `coroutine`
    object, so `asyncio.run()` needs a small local `async def` shim
    wrapping it.
    """

    async def _shim():
        return await coro_factory()

    return asyncio.run(_shim())


class _AudioCaptureStreamSource:
    """`StreamSource`-conforming producer backed by a real WinRT
    `AudioGraph` device-input node (Req 5.1, 9.7).

    Opens the actual capture device lazily -- specifically, at
    construction time of *this* object, which itself is only constructed
    from `open_stream()`, never from `discover()` (Req 9.4, 9.7). Produces
    real captured PCM float samples per `next_block()` call, decoded from
    the `AudioFrame`/`AudioBuffer` WinRT hands back.
    """

    def __init__(
        self,
        *,
        sensor_id: str,
        device_id: str,
        device_name: str,
        block_size_frames: int,
        on_permission_denied: "callable[[], None]",
    ) -> None:
        self._sensor_id = sensor_id
        self._device_id = device_id
        self._device_name = device_name
        self._block_size_frames = block_size_frames
        self._on_permission_denied = on_permission_denied
        self._lock = threading.Lock()
        self._closed = False
        self._seq = 0

        self._graph = None
        self._device_input_node = None
        self._frame_output_node = None
        self._channel_count = 1
        self._sample_rate_hz = _DISCOVERY_DEFAULT_RATE_HZ

        self._open()

    def _open(self) -> None:
        """Actually open the capture device (Req 9.7: this only runs once,
        lazily, on first construction of this stream source -- never
        during `discover()`)."""

        import winsdk.windows.devices.enumeration as de
        import winsdk.windows.media.audio as ma
        import winsdk.windows.media.capture as mc
        import winsdk.windows.media.render as mr

        async def _open_async():
            devices = await de.DeviceInformation.find_all_async(de.DeviceClass.AUDIO_CAPTURE)
            device_info = next((d for d in devices if d.id == self._device_id), None)
            if device_info is None:
                raise DeviceOpenError(
                    sensor_id=self._sensor_id,
                    reason="capture device no longer present",
                )

            settings = ma.AudioGraphSettings(mr.AudioRenderCategory.OTHER)
            graph_result = await ma.AudioGraph.create_async(settings)
            if graph_result.status != ma.AudioGraphCreationStatus.SUCCESS:
                raise DeviceOpenError(
                    sensor_id=self._sensor_id,
                    reason=f"AudioGraph creation failed: {graph_result.status!r}",
                )
            graph = graph_result.graph

            input_result = await graph.create_device_input_node_async(
                mc.MediaCategory.OTHER, graph.encoding_properties, device_info
            )
            if input_result.status == ma.AudioDeviceNodeCreationStatus.ACCESS_DENIED:
                graph.close()
                self._on_permission_denied()
                raise DeviceOpenError(
                    sensor_id=self._sensor_id,
                    reason="OS denied microphone access permission",
                )
            if input_result.status != ma.AudioDeviceNodeCreationStatus.SUCCESS:
                graph.close()
                raise DeviceOpenError(
                    sensor_id=self._sensor_id,
                    reason=f"audio device input node creation failed: {input_result.status!r}",
                )

            device_input_node = input_result.device_input_node
            frame_output_node = graph.create_frame_output_node()
            device_input_node.add_outgoing_connection(frame_output_node)

            return graph, device_input_node, frame_output_node

        graph, device_input_node, frame_output_node = _run_async(_open_async)

        encoding_properties = device_input_node.encoding_properties
        self._channel_count = int(encoding_properties.channel_count) or 1
        self._sample_rate_hz = float(encoding_properties.sample_rate) or _DISCOVERY_DEFAULT_RATE_HZ

        self._graph = graph
        self._device_input_node = device_input_node
        self._frame_output_node = frame_output_node
        graph.start()

    def next_block(self) -> Reading:
        with self._lock:
            if self._closed:
                raise RuntimeError(f"stream for sensor {self._sensor_id!r} is closed")
            frame_output_node = self._frame_output_node
            channel_count = self._channel_count

        # AudioGraph delivers frames on its own quantum cadence; poll until
        # a non-empty frame is available or the stream is closed, rather
        # than blocking forever on a single `get_frame()` call.
        samples: array.array = array.array("f")
        deadline = time.monotonic() + 5.0
        while len(samples) < self._block_size_frames * channel_count:
            with self._lock:
                if self._closed:
                    raise RuntimeError(f"stream for sensor {self._sensor_id!r} is closed")
            frame = frame_output_node.get_frame()
            audio_buffer = frame.lock_buffer(0)
            reference = audio_buffer.create_reference()
            frame_bytes = memoryview(reference).tobytes()
            audio_buffer.close()
            if frame_bytes:
                chunk = array.array("f")
                chunk.frombytes(frame_bytes)
                samples.extend(chunk)
            if time.monotonic() > deadline:
                break

        frame_count = len(samples) // channel_count if channel_count else 0
        trimmed = samples[: frame_count * channel_count]

        now = time.monotonic()
        with self._lock:
            seq = self._seq
            self._seq += 1

        return Reading(
            id=self._sensor_id,
            t_mono=now,
            t_wall=time.time(),
            values=tuple(trimmed),
            seq=seq,
            status=Status.OK,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            graph = self._graph
            self._graph = None
        if graph is not None:
            graph.stop()
            graph.close()

    def achieved_rate(self) -> float | None:
        return None

    def applied_rate(self) -> float:
        return self._sample_rate_hz


class WindowsAudioAdapter:
    """Reports each WinRT audio-capture device as one `buffer`-dtype
    sensor (Req 13.9)."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="winrt_audio",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=WINDOWS_PLATFORMS,
        priority=None,
        requires_elevation_optin=False,
        read_only_declared=True,
    )

    def __init__(self) -> None:
        self._devices_by_id: dict[str, object] = {}
        # Sensor_Ids for which the OS has previously denied permission,
        # cached here so the *next* discover() call reports
        # Availability.PERMISSION_DENIED instead of PRESENT (see module
        # docstring: Reading.status has no permission_denied member, so
        # the denial has nowhere to surface except on the next
        # SensorInfo).
        self._permission_denied_ids: set[str] = set()

    # ------------------------------------------------------------------
    # required
    # ------------------------------------------------------------------

    def discover(self) -> tuple[SensorInfo, ...]:
        """Enumerate audio capture devices from metadata only (Req 9.4).

        Opens no device handle: `DeviceInformation.find_all_async` is a
        pure metadata query. Real format/rate discovery is deferred to
        `open_stream()` (Req 9.7).
        """

        import winsdk.windows.devices.enumeration as de

        async def _enumerate():
            return await de.DeviceInformation.find_all_async(de.DeviceClass.AUDIO_CAPTURE)

        devices = _run_async(_enumerate)

        records: list[SensorInfo] = []
        self._devices_by_id.clear()
        for device_info in devices:
            instance_qualifier = device_instance_hash(device_info.id)
            sensor_id = f"{_KIND}.{_SOURCE_ID}.{instance_qualifier}"
            self._devices_by_id[sensor_id] = device_info

            availability = (
                Availability.PERMISSION_DENIED
                if sensor_id in self._permission_denied_ids
                else Availability.PRESENT
            )

            records.append(
                SensorInfo(
                    schema_version=SCHEMA_VERSION,
                    id=sensor_id,
                    kind=_KIND,
                    dtype=Dtype.BUFFER,
                    unit=None,
                    channels=("mono",),
                    shape=_DISCOVERY_SHAPE,
                    range=(-1.0, 1.0),
                    resolution=None,
                    rate_hz=RateSpec(default=_DISCOVERY_DEFAULT_RATE_HZ, min=None, max=None),
                    delivery=Delivery.PUSH,
                    derived=False,
                    requires_consent=True,
                    requires_elevation=False,
                    source=(_SOURCE_ID,),
                    vendor=None,
                    part_number=device_info.name,
                    availability=availability,
                )
            )

        return tuple(records)

    def read(self, sensor_id: str) -> Reading:
        """Always raises for this adapter's sensors (Req 4.12).

        Every sensor this adapter exposes is `buffer`-dtype. The registry
        never calls `read()` for a `buffer` sensor -- it rejects a single
        `read` of a buffer sensor before dispatch and directs the caller
        to the Block/stream path (`BlockPathRequiredError`, Req 4.12).
        This method therefore has no reachable non-error behaviour to
        implement; it raises `KeyError` naming the sensor, exactly the
        same "unknown/unsupported for direct read" signal
        `winrt_light.py` and friends raise for a sensor id they do not
        own, so a caller that somehow reaches this method regardless
        (e.g. a test exercising the adapter directly, bypassing the
        registry) gets an unambiguous failure rather than silent
        fabricated data.
        """

        raise KeyError(
            f"sensor {sensor_id!r} is buffer-dtype; WindowsAudioAdapter has no "
            "direct read() path, use open_stream() instead"
        )

    # ------------------------------------------------------------------
    # optional
    # ------------------------------------------------------------------

    def open_stream(
        self,
        sensor_id: str,
        *,
        block_size: int | None = None,
        rate_hz: float | None = None,
        buffer_blocks: int = 64,
    ) -> _AudioCaptureStreamSource:
        device_info = self._devices_by_id.get(sensor_id)
        if device_info is None:
            raise KeyError(f"unknown sensor id for WindowsAudioAdapter: {sensor_id!r}")

        resolved_block_size = block_size if block_size is not None else _DEFAULT_BLOCK_SIZE

        def _mark_permission_denied() -> None:
            self._permission_denied_ids.add(sensor_id)

        return _AudioCaptureStreamSource(
            sensor_id=sensor_id,
            device_id=device_info.id,
            device_name=device_info.name,
            block_size_frames=resolved_block_size,
            on_permission_denied=_mark_permission_denied,
        )

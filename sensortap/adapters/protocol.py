"""The Adapter Protocol: the contract every backend plug-in implements.

Two required methods, at most six optional (Req 10.1, 10.2). This uses
`typing.Protocol` with `@runtime_checkable` rather than a base class:
structural typing means an adapter needs no import-time dependency on a
sensortap base class, which keeps third-party adapters loosely coupled
from the core.

The interface defines no actuation path (Req 15.1): every method here
either reports metadata/readings or configures the two acquisition
parameters that exist anywhere in the interface (`rate_hz`, `block_size`).
There is no method that transmits a setpoint, control value or firmware
payload.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo

#: The adapter interface version implemented by this core. An adapter's
#: `AdapterMeta.interface_version` is compared against this by MAJOR only
#: (Req 10.9): a differing MAJOR is treated as an unsupported-version skip
#: at load time, not a load failure. MINOR is advisory.
ADAPTER_INTERFACE_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class AdapterMeta:
    """Static, class-level metadata an adapter declares about itself.

    The registry reads this off the class *without instantiating* it,
    so the platform/version gate at load time costs no side effects.
    """

    adapter_id: str
    """Stable, lowercase identifier, e.g. ``"winrt_motion"``."""

    interface_version: str
    """``"MAJOR.MINOR"``; the registry requires MAJOR to match the core's."""

    supported_platforms: frozenset[str]
    """Platform tags this adapter supports, e.g. ``{"win32"}``, ``{"linux"}``."""

    priority: int | None = None
    """Used to resolve `Sensor_Id` conflicts (Req 10.10). `None` sorts lowest."""

    requires_elevation_optin: bool = False
    """If `True`, the registry skips loading unless the caller opts in (Req 8.3)."""

    read_only_declared: bool = False
    """The adapter's declared read-only obligation.

    Requirement 15.8 requires this to be `True` for the registry to load the
    adapter at all -- an adapter that has not made this declaration is refused,
    not merely warned about. Declaring `True` is a promise, not a guarantee:
    the interface itself defines no write/actuation path (Req 15.1), but the
    registry cannot verify an adapter's internals never reach outside that
    surface. See `adapters/conformance.py` for the reusable check that comes
    closest to verifying this mechanically.
    """


class AdapterHealth(Protocol):
    """Return shape of the optional `health()` method.

    Kept intentionally minimal: a short machine-readable status plus an
    optional human-readable detail string, for adapters that can report
    something more granular than the registry's own `BackendStatus`
    (e.g. a helper process reporting itself as degraded).
    """

    status: str
    detail: str | None


@runtime_checkable
class StreamSource(Protocol):
    """The adapter-side half of streaming (Req 5.1).

    A consumer-facing `Stream` (defined in `registry`) wraps one
    `StreamSource` per open stream. The adapter is responsible only for
    producing blocks and reporting rate; buffering, discard-on-backpressure,
    and the iterator/context-manager surface belong to the registry's
    `Stream`, not to the adapter.
    """

    def next_block(self) -> Reading:
        """Return the next Block as a `Reading` whose `values` holds the block.

        Blocks until the next block is available or the stream is closed.
        """
        ...

    def close(self) -> None:
        """Release the underlying backend resources for this stream.

        Must complete within 1000 ms (Req 5.7). Subsequent calls to
        `next_block` on a closed `StreamSource` are rejected by the
        registry before they reach the adapter.
        """
        ...

    def achieved_rate(self) -> float | None:
        """The measured delivery rate over the most recent samples.

        Returns `None` until at least 2 samples have been delivered
        (Req 6.9).
        """
        ...

    def applied_rate(self) -> float:
        """The sampling rate actually applied to this stream (Req 6.3, 6.6).

        May differ from the rate requested at `open_stream` time.
        """
        ...


@runtime_checkable
class Adapter(Protocol):
    """The backend plug-in contract.

    Exactly two required methods (`discover`, `read`) and up to six
    optional methods. A missing optional method is not an error at load
    time -- the registry raises `UnsupportedOperationError` only when a
    caller actually invokes the corresponding operation on that adapter's
    sensors, and the adapter's sensors otherwise stay readable through the
    single-read path (Req 10.3).
    """

    meta: ClassVar[AdapterMeta]

    # --- required (2) ---

    def discover(self) -> Sequence[SensorInfo]:
        """Enumerate every sensor this adapter can currently report.

        Must not open device handles for camera, microphone or HID
        sensors (Req 9.4); those open lazily on first `read` or
        `open_stream` (Req 9.7).
        """
        ...

    def read(self, sensor_id: str) -> Reading:
        """Return the current value of one sensor this adapter owns.

        Never called by the registry for a `buffer`-dtype sensor (Req 4.12);
        the registry rejects that before dispatch.
        """
        ...

    # --- optional (6) ---

    def open_stream(
        self,
        sensor_id: str,
        *,
        block_size: int | None = None,
        rate_hz: float | None = None,
        buffer_blocks: int = 64,
    ) -> StreamSource:
        """Open a Block/streaming path for one sensor. See Requirement 5."""
        ...

    def supported_block_sizes(self, sensor_id: str) -> tuple[int, int]:
        """Return the (minimum, maximum) block size this backend supports."""
        ...

    def configure_rate(self, sensor_id: str, rate_hz: float) -> float:
        """Request a sampling rate change and return the rate actually applied.

        Raises `RateFixedError` for a sensor with no rate configuration
        (Req 6.7).
        """
        ...

    def setup(self) -> None:
        """One-time initialization run after instantiation, before use."""
        ...

    def teardown(self) -> None:
        """Release adapter-level resources. Called at most once per adapter."""
        ...

    def health(self) -> AdapterHealth:
        """Report adapter-specific health detail beyond `BackendStatus`."""
        ...

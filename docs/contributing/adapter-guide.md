# Writing a sensortap Adapter

This guide is for anyone adding a new sensor backend to sensortap, whether
that's a new adapter living inside this repository or an adapter shipped
from your own, separate pip package. It covers the `Adapter` Protocol,
the one-file-per-family rule, entry point registration, and how to run the
Conformance_Check against your adapter before you submit it.

## 1. What an Adapter is, and the one-file-per-family rule

An **Adapter** is a backend plug-in that reports one *sensor family* to
sensortap: WinRT motion sensors, a vendor's battery API, a webcam, and so
on. The convention this codebase follows, and that you should follow too,
is:

> one adapter = one sensor family = one source file = one registered
> entry point.

Don't fold two unrelated families (say, light and battery) into one
adapter class or one file just because they happen to share a platform.
Each family gets its own module and its own class.

Two adapters worth reading before you write your own:

- [`sensortap/adapters/reference.py`](../../sensortap/adapters/reference.py)
  — a synthetic, no-hardware adapter that exercises the *entire*
  interface (all four dtypes, all six optional methods). This is the
  adapter to read line-by-line; it's the worked example the rest of this
  guide walks through.
- [`sensortap/adapters/windows/winrt_light.py`](../../sensortap/adapters/windows/winrt_light.py)
  — the simplest *real* adapter in the tree. One sensor, one dtype, a
  handful of lines. Read this second, once `reference.py`'s shape makes
  sense, to see how little a minimal real-hardware adapter actually
  needs.

## 2. The Adapter Protocol

sensortap defines the adapter contract as a `typing.Protocol`
(`sensortap/adapters/protocol.py`), not a base class. You don't import or
subclass anything from sensortap core to implement one — you just need a
class whose shape matches. That's deliberate: it keeps third-party
adapters loosely coupled from sensortap.

An adapter declares class-level metadata via `AdapterMeta`:

```python
meta: ClassVar[AdapterMeta] = AdapterMeta(
    adapter_id="winrt_light",          # stable, lowercase identifier
    interface_version=ADAPTER_INTERFACE_VERSION,  # "MAJOR.MINOR"; registry checks MAJOR only
    supported_platforms=frozenset({"win32"}),      # platform tags
    priority=None,                      # used to resolve Sensor_Id conflicts; None sorts lowest
    requires_elevation_optin=False,     # True => registry skips unless caller opts in
    read_only_declared=True,            # REQUIRED — see section 6
)
```

`read_only_declared=True` is not optional flavor text: **the registry
refuses to load an adapter at all unless this is `True`** (Req 15.8).

### Required methods (2)

```python
def discover(self) -> Sequence[SensorInfo]:
    """Enumerate every sensor this adapter can currently report.

    Must not open device handles for camera, microphone or HID
    sensors; those open lazily on first read()/open_stream().
    """

def read(self, sensor_id: str) -> Reading:
    """Return the current value of one sensor this adapter owns.

    Never called by the registry for a buffer-dtype sensor.
    """
```

### Optional methods (up to 6)

```python
def open_stream(
    self,
    sensor_id: str,
    *,
    block_size: int | None = None,
    rate_hz: float | None = None,
    buffer_blocks: int = 64,
) -> StreamSource: ...

def supported_block_sizes(self, sensor_id: str) -> tuple[int, int]: ...

def configure_rate(self, sensor_id: str, rate_hz: float) -> float: ...

def setup(self) -> None: ...

def teardown(self) -> None: ...

def health(self) -> AdapterHealth: ...
```

A missing optional method isn't an error at load time. The registry only
raises `UnsupportedOperationError` if a caller invokes the corresponding
operation against a sensor from an adapter that doesn't implement it —
the adapter's other sensors stay readable through the plain `read()`
path regardless.

`open_stream()` returns a `StreamSource`, the adapter-side half of
streaming: `next_block()`, `close()`, `achieved_rate()`,
`applied_rate()`. The registry's own `Stream` wraps one `StreamSource`
per open stream and owns buffering/backpressure — your adapter only needs
to produce blocks and report rate.

## 3. Worked example: `reference.py`

`ReferenceAdapter` exposes exactly one sensor per `Dtype`, so it's the
best single file to copy structure from:

| Sensor_Id | dtype | delivery | what it demonstrates |
|---|---|---|---|
| `temp.reference.0` | `scalar` | push | the stale-value read path — once more than 2 sampling intervals pass since a value was last produced, `read()` reports `Status.STALE` |
| `accel.reference.0` | `vector3` | push | the never-received-value path — `read()` returns immediately with `Status.UNAVAILABLE` and empty `values`, rather than blocking forever |
| `touchpad.reference.0` | `matrix` | poll | the plain synchronous poll path — no "last received" bookkeeping, `read()` just computes and returns |
| `microphone.reference.0` | `buffer` | push | the Block/Stream path — `open_stream()` + `supported_block_sizes()`, since `buffer`-dtype sensors are never read through `read()` |

**Building `SensorInfo` records.** `discover()` returns a tuple of four
`SensorInfo` dataclass instances, one per sensor above, each filling in
`schema_version`, `id`, `kind`, `dtype`, `unit`, `channels`, `shape`,
`range`, `resolution`, `rate_hz` (a `RateSpec`), `delivery`, `derived`,
`requires_consent`, `requires_elevation`, `source`, `vendor`,
`part_number`, and `availability`. Copy one of these blocks verbatim as
your starting point and change the fields that describe your sensor.

**Dispatching `read()` by sensor_id.** `read()` is one method with an
`if sensor_id == ...: return Reading(...)` chain per sensor, ending in a
`raise KeyError(...)` for anything it doesn't recognize. For the scalar
sensor it computes elapsed time since `setup()`, checks how long it's
been since the background thread last updated `_last_scalar_update_mono`,
and sets `Status.STALE` if that gap exceeds twice the sampling interval.
For the vector3 sensor it always returns `Status.UNAVAILABLE` with empty
`values` — no bookkeeping at all, since it never has a value to give.

**`setup()`/`teardown()` and the background thread.** `setup()` records a
start time and spawns a daemon thread (`_push_loop`) that just updates
`_last_scalar_update_mono` at the sensor's configured rate — simulating a
push backend delivering fresh values. `teardown()` sets a
`threading.Event` to stop that loop and joins the thread with a timeout,
then clears the bookkeeping fields. If your real backend has a
persistent connection, subscription, or handle to release, `setup()` and
`teardown()` are where that lifecycle belongs.

## 4. Entry point registration (third-party package)

Adapters register through the `sensortap.adapters` entry point group so a
third-party package can add an adapter **without editing sensortap's own
source** (Req 10.4). If you're maintaining your own pip package (say,
`my-sensortap-widgets`) that depends on `sensortap` and ships an adapter,
your package's `pyproject.toml` needs:

```toml
[project]
name = "my-sensortap-widgets"
dependencies = ["sensortap"]

[project.entry-points."sensortap.adapters"]
my_widget = "my_sensortap_widgets.adapter:MyWidgetAdapter"
```

The key on the left (`my_widget`) is the entry point name — pick
something unique to your package, not necessarily the same as
`meta.adapter_id`. The value on the right is `module.path:ClassName`,
pointing at your adapter class. Once your package is installed alongside
sensortap, the registry discovers it automatically at load time; you
never touch sensortap's own `pyproject.toml`.

For reference, this is the same pattern sensortap's own core adapters use
internally (`sensortap/pyproject.toml`), just with all entries pointing
into the `sensortap` package itself:

```toml
[project.entry-points."sensortap.adapters"]
reference = "sensortap.adapters.reference:ReferenceAdapter"
winrt_light = "sensortap.adapters.windows.winrt_light:WindowsLightAdapter"
# ... one line per adapter
```

## 5. Running the Conformance_Check

Before submitting an adapter, run the Conformance_Check against it. It's
shipped inside the `sensortap` package (not under `tests/`) specifically
so you can run it from your own package without needing sensortap's
source checked out.

**Importable form**, from your own test suite:

```python
from sensortap.adapters.conformance import run_conformance_check

adapter = MyWidgetAdapter()
adapter.setup()  # if your adapter defines setup()
try:
    report = run_conformance_check(adapter)
    assert report.passed
finally:
    adapter.teardown()  # if your adapter defines teardown()
```

**CLI form**:

```
python -m sensortap.adapters.conformance my_sensortap_widgets.adapter:MyWidgetAdapter
```

The CLI form constructs the adapter for you, calls `setup()`/`teardown()`
if present, and exits `0` on pass / `1` on failure.

Here's real output from running it against `reference.py` in this
repository:

```
Conformance_Check for adapter 'reference':
  [PASS] schema compliance of discovered records -- 4 record(s) validated
  [PASS] Sensor_Id stability across consecutive discover() calls -- 4 Sensor_Id(s) stable across 2 discover() calls
  [PASS] adapter-level error behaviour -- read() on a known sensor id succeeded; read() on an unknown sensor id raised, as expected
  [PASS] read-only obligation declared (meta.read_only_declared) -- declared True; honouring this obligation in practice is a documented, unverifiable contractual obligation on the adapter author (Req 15.7, 15.8), not checked mechanically here
Overall: PASSED
```

The checker validates:

1. **Schema compliance** — every `SensorInfo` from `discover()` passes
   `validate_sensor_info()`, and a `read()` from at least one non-`buffer`
   sensor passes `validate_reading()` against its `SensorInfo`.
2. **Sensor_Id stability** — two consecutive `discover()` calls return
   the same set of Sensor_Ids.
3. **Adapter-level error behaviour** — `read()` on a sensor id you just
   discovered succeeds; `read()` on a sensor id you never discovered
   raises rather than silently returning a bogus value. (Errors like
   `UnsupportedOperationError`, `MalformedSensorIdError`, and
   `UnknownSensorError` are the registry's responsibility, not the
   adapter's, so they aren't checked here.)
4. **The read-only declaration** — see the next section.

## 6. The read-only obligation

Requirement 15.8 states this plainly, and it's worth restating here
rather than softening it: read-only behaviour outside sensortap's own
device access wrappers is **a contractual obligation on the adapter
author, not a machine-verifiable property.**

sensortap ships no device-access-tracking wrapper today, so there is
nothing for the Conformance_Check to instrument to observe *which*
operations your adapter actually performs against real hardware. The
interface itself defines no actuation path — no method transmits a
setpoint, control value, or firmware payload — but the registry cannot
verify that your adapter's internals never reach outside that surface
through some other means (a vendor SDK call, a raw device handle, etc.).

The only thing checked mechanically is that you made the declaration at
all: `meta.read_only_declared is True`. That's the same gate the registry
enforces before it will even load your adapter. Declaring it is a
promise you're making as the adapter author, not a guarantee sensortap
enforces at runtime. Building a fake enforcement mechanism here would
give false confidence the spec explicitly warns against — so don't
expect one, and don't imply one in your own adapter's documentation
either.

## 7. Submission checklist

Before opening a PR (or publishing your third-party package), confirm:

- [ ] `discover()` output is schema-valid for every returned `SensorInfo`
- [ ] Sensor_Ids are stable across repeated `discover()` calls
- [ ] The Conformance_Check passes (`python -m sensortap.adapters.conformance module:Class`)
- [ ] One file, one sensor family — you haven't folded unrelated sensors
      into a single adapter
- [ ] `meta.read_only_declared = True` is set, and you've genuinely
      upheld that obligation in your adapter's implementation

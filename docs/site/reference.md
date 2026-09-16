# sensortap reference

Complete reference for the sensortap CLI, Python API, schema, and adapter
interface. Every value here is taken from the shipped source, not from an
example.

- **Version:** schema `1.0`, adapter interface `1.0`
- **Platform:** Windows 10 or later. Linux adapters are designed but not built.
- **License:** MIT
- **Repository:** <https://github.com/BaselAshraf81/sensortap>

**Contents**

1. [Install](#1-install)
2. [Concepts](#2-concepts)
3. [Sensor ids](#3-sensor-ids)
4. [CLI](#4-cli)
5. [Environment variables](#5-environment-variables)
6. [Python API](#6-python-api)
7. [Schema types](#7-schema-types)
8. [Vocabularies](#8-vocabularies)
9. [Consent](#9-consent)
10. [Streaming](#10-streaming)
11. [Backend status](#11-backend-status)
12. [Errors and exit codes](#12-errors-and-exit-codes)
13. [Shipped adapters](#13-shipped-adapters)
14. [Writing an adapter](#14-writing-an-adapter)
15. [Guarantees and limits](#15-guarantees-and-limits)

---

## 1. Install

```sh
pip install sensortap
```

Covers camera, microphone, motion, orientation, light, battery, radio, and
touchpad sensors. No extra downloads.

Hardware-monitor sensors (per-core CPU load, temperatures, voltages, power,
clock speeds) need the optional extra, which pulls in a bundled self-contained
.NET helper of a few dozen megabytes:

```sh
pip install "sensortap[hwmon]"
```

Development install:

```sh
pip install -e ".[dev]"
pytest
```

---

## 2. Concepts

| Term | Meaning |
| --- | --- |
| **Registry** | The single stateful orchestrator. Loads adapters, runs discovery, de-duplicates, routes reads. |
| **Adapter** | One backend plug-in owning one sensor family. Knows nothing about any other adapter. |
| **Backend** | The platform API or process an adapter talks to (WinRT, a .NET helper, sysfs). |
| **Helper process** | A separate child process an adapter may spawn. Used by `hwmon_bridge`. |
| **Sensor id** | The stable, three-segment string identifying one sensor. |
| **Kind** | A member of a closed 24-value vocabulary describing what a sensor measures. |
| **Consent grant** | An in-memory, time-bounded authorization to read one privacy-sensitive sensor. |
| **Block** | One buffered chunk of samples from a `buffer`-dtype sensor, delivered through a stream. |

Nothing in the core branches on the operating system. All platform variance
lives inside adapters, so the public surface is identical everywhere.

---

## 3. Sensor ids

Exactly three dot-separated segments:

```
<kind>.<source-qualifier>.<instance-qualifier>
```

Example: `temp.hwmon.2aed4545974396bc`

| Rule | Value |
| --- | --- |
| Segment count | Exactly 3 |
| Allowed characters | `a-z`, `0-9`, `-` |
| Segment length | 1 to 64 characters |
| Total length | At most 200 characters |
| Case | Lowercase only |

- **kind** is a member of the closed [kind vocabulary](#kind-vocabulary).
- **source-qualifier** identifies the reporting backend, for example `hwmon`,
  `winrt`, `win-radio`, `reference`. It is not required to equal the adapter's
  own `adapter_id`.
- **instance-qualifier** is a stable per-device value. Adapters derive it by
  hashing a persistent hardware identifier (BLAKE2b, 8-byte digest, 16
  lowercase hex characters, unsalted so ids reproduce across runs and machines
  for the same hardware), or use a run-invariant fallback index when no
  persistent identifier exists. It is never a bus-probe-order index.

If two distinct sensors resolve to the same id, every one of them is kept and
a disambiguation suffix (`-0`, `-1`, ...) is appended to the instance
qualifier. Ids are validated for grammar before any lookup is attempted, so a
malformed id fails without touching an adapter.

---

## 4. CLI

```
sensortap [global-options] <command> [command-arguments] [global-options]
```

Global options are accepted both before and after the subcommand:
`sensortap --json list` and `sensortap list --json` are equivalent.

Output discipline: everything destined for stdout is buffered and flushed only
after the whole command succeeds, so a command that fails partway never leaks
a partial table into a pipe. On failure, only the cause is written, to stderr.

### Commands

#### `list`

Enumerate every discovered sensor, followed by per-adapter status lines and a
summary line.

```sh
sensortap list
sensortap list --json
sensortap list --include-elevated --include-motherboard
```

Takes no positional arguments.

#### `read`

Read one sensor once.

```sh
sensortap read <sensor-id>
sensortap read temp.hwmon.2aed4545974396bc
sensortap read microphone.winrt.0 --consent microphone.winrt.0
```

| Argument | Required | Description |
| --- | --- | --- |
| `sensor_id` | yes | The sensor id to read. |

Rejected with exit code 4 for a `buffer`-dtype sensor; use `stream` instead.
The command is bounded at 10 seconds regardless of adapter behaviour, after
which it exits with code 6.

#### `stream`

Stream one sensor continuously, one line per block, flushed as it arrives.

```sh
sensortap stream <sensor-id>
sensortap stream microphone.reference.0 --consent microphone.reference.0 --duration 10
```

| Argument | Required | Description |
| --- | --- | --- |
| `sensor_id` | yes | The sensor id to stream. |

Runs until the duration elapses, the stream ends, or the process is
interrupted. Requires the owning adapter to implement streaming; otherwise
exits with code 4.

#### `inspect`

Print the full `SensorInfo` record for one sensor, every field included.

```sh
sensortap inspect <sensor-id>
sensortap inspect accel.reference.0 --json
```

| Argument | Required | Description |
| --- | --- | --- |
| `sensor_id` | yes | The sensor id to inspect. |

Exits with code 3 if the id is not in the current enumeration.

#### `doctor`

Run the shipped conformance check against every adapter that loaded on this
machine, and report the result.

```sh
sensortap doctor
sensortap doctor --include-elevated
sensortap doctor --json
```

Takes no positional arguments.

The test suite can only prove the adapter contract holds for hardware the
maintainer owns. `doctor` moves that same check to the user's machine, where the
hardware actually is. It is the intended first step for any bug report: several
shipped defects were structurally present everywhere but only observable on a
machine carrying the relevant sensor.

Each adapter is run through the five checks in
`sensortap.adapters.conformance.run_conformance_check` — schema compliance of
discovered records, `Sensor_Id` stability across consecutive `discover()` calls,
reachability of every discovered sensor through `read()` or `open_stream()`,
adapter-level error behaviour, and the declared read-only obligation.

Plain-text output prints an `environment` block, then an `adapters` block with
one `[ok  ]`/`[FAIL]` line per adapter and the failing check details indented
beneath each failure, then a summary line. On
failure it appends a GitHub issue URL with title and body prefilled from the
run. The environment block carries version, Python version, platform string,
machine architecture and elevation state, and deliberately carries no hostname,
no username and no sensor ids, because it is built to be pasted in public.

`--json` emits:

| Field | Type | Description |
| --- | --- | --- |
| `environment` | object | `sensortap`, `schema`, `adapter_interface`, `python`, `platform`, `machine`, `elevated`. |
| `adapters` | array | One entry per loaded adapter: `adapter_id`, `passed`, and `checks` (each `name`, `passed`, `detail`). |
| `summary` | object | `adapters_checked`, `adapters_passed`, `adapters_failed`. |
| `report_url` | string | Present only when at least one adapter failed; the prefilled issue URL. |

Exits **0** when every adapter passes and **7** when any adapter fails. Code 7
is deliberately distinct from the generic **1**: "sensortap itself broke" and
"sensortap works and found a real contract violation on your hardware" are
different outcomes, and CI needs to distinguish them.

### Global options

| Flag | Type | Default | Applies to | Description |
| --- | --- | --- | --- | --- |
| `--json` | boolean | off | all | Machine-readable JSON output instead of the text table. |
| `--consent SENSOR_ID` | string, repeatable | none | `read`, `stream` | Grant consent for one privacy-sensitive sensor id. Repeat the flag per sensor. Wildcards and patterns are rejected. |
| `--include-elevated` | boolean | off | all | Load adapters that declare `requires_elevation_optin`, and include sensors that require elevation. |
| `--include-motherboard` | boolean | off | all | Opt in to motherboard, Super-IO and embedded-controller sensors. See [below](#the-motherboard-opt-in). |
| `--discovery-timeout MS` | integer | `5000` | all | Per-round discovery timeout in milliseconds. Must be 100 to 60000 inclusive; outside that range is a usage error and the configured timeout is left unchanged. |
| `--duration SECONDS` | float | none | `stream` | Stop streaming after this many seconds. Must be 1 to 86400 inclusive. Validated before streaming starts. |

Invoking `sensortap` with no command, or an unrecognised one, prints usage
naming all four commands and exits with code 2.

### The motherboard opt-in

`--include-motherboard` enables motherboard, Super-IO and embedded-controller
monitoring in the bundled hardware-monitor helper. It typically adds board
temperatures, fan tachometers, and extra voltage rails.

It is off by default for three reasons:

1. That access path can reach the Ring 0 kernel driver. sensortap never
   installs, registers, or starts a kernel driver, so where no such driver is
   already present these sensors simply do not appear.
2. Embedded-controller register reads can conflict with a vendor tool or
   another monitoring application (HWiNFO, OEM fan control) already holding the
   same registers.
3. An unrecognised Super-IO chip can return plausible-looking nonsense rather
   than an obvious failure.

Sensors that appear only under this flag carry `hwmon-mb` as their source
qualifier instead of `hwmon`, so an id records which capability set produced
it.

On a machine with no Ring 0 driver present and no recognised Super-IO chip,
the flag is safe and finds nothing extra. Desktop boards with a standard
Nuvoton or ITE chip usually have more to report than laptops.

---

## 5. Environment variables

| Variable | Values | Default | Effect |
| --- | --- | --- | --- |
| `SENSORTAP_HWMON_MOTHERBOARD` | `1`, `true`, `yes`, `on` (case-insensitive, surrounding whitespace ignored) enable it; anything else, including unset, leaves it off | unset | Same as `--include-motherboard`. The CLI flag sets this variable. |

This travels by environment variable rather than as a constructor argument
because the registry instantiates every adapter with no arguments, and the
adapter interface has no per-adapter configuration channel: acquisition
configuration is deliberately limited to sampling rate and block size, which
is a different thing from a load-time capability toggle. Set it before
constructing a `Registry`, since adapters read it at instantiation.

---

## 6. Python API

### Module-level functions

A single lazily-created `Registry` backs these. Importing `sensortap` has no
side effects: no adapters load, no `setup()` runs, and no helper process
spawns until one of these is first called. Teardown is registered with
`atexit` and runs exactly once per adapter.

```python
import sensortap
```

#### `list_sensors(*, kind=None, source=None, id=None) -> list[SensorInfo]`

Run discovery across every loaded adapter concurrently and return the
de-duplicated, validated, ordered records.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | `str \| None` | `None` | Exact-match filter on `kind`. |
| `source` | `str \| None` | `None` | Matches when the value appears anywhere in a record's `source` tuple. |
| `id` | `str \| None` | `None` | Exact-match filter on the sensor id. |

Filters are conjunctive and exact; nothing matching returns an empty list.
Never opens a device and never triggers an operating-system permission
prompt, including for camera and microphone sensors.

Ordering is deterministic: by adapter load order, then ascending id within an
adapter.

#### `read(sensor_id) -> Reading`

Read one sensor once.

| Parameter | Type | Description |
| --- | --- | --- |
| `sensor_id` | `str` | The sensor id to read. |

Order of checks, each of which happens before any adapter is invoked:
grammar validation, existence against the most recent enumeration, consent,
then dtype. Adapter errors are not swallowed; they propagate to the caller
unchanged.

**Enumerate before you read.** "The most recent enumeration" means there has to
*be* one. `read()` never discovers implicitly — that would hide a concurrent
sweep of every adapter, under a multi-second timeout, inside what looks like a
single-sensor call. So on a fresh process, call `list_sensors()` (or
`Registry.list_sensors()`) first; a bare `read()` raises `UnknownSensorError`
for an id that genuinely exists. The CLI does this enumeration for you, which
is why `sensortap read <id>` works as a one-liner and the Python equivalent
needs two.

Raises `MalformedSensorIdError`, `UnknownSensorError`, `ConsentError`, or
`BlockPathRequiredError`. A rejected read never consumes a `seq` value.

#### `stream(sensor_id, *, block_size=None, rate_hz=None, buffer_blocks=64) -> Stream`

Open a streaming path for one sensor.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `sensor_id` | `str` | required | The sensor id to stream. |
| `block_size` | `int \| None` | `None` | Samples per block. Outside the backend's supported range raises `BlockSizeOutOfRangeError` and allocates nothing. |
| `rate_hz` | `float \| None` | `None` | Requested sampling rate. The applied rate may differ. |
| `buffer_blocks` | `int` | `64` | Ring buffer depth, 2 to 1024 inclusive. Outside that range, or a non-integer, raises `ValueError`. |

These are the only two acquisition parameters anywhere in the interface. There
is no `**kwargs`, so no additional configuration key can be smuggled through.

Raises `UnsupportedOperationError` if the owning adapter does not implement
streaming; that adapter's other sensors remain readable through `read()`.

#### `consent(sensor_ids) -> ConsentGrant`

Create a consent grant covering exactly the named sensor ids. See
[Consent](#9-consent).

#### `backend_status() -> list[BackendStatus]`

Return the current per-adapter status records. Performs no I/O and no adapter
interaction, so it answers immediately even before any enumeration has run.

### `Registry`

Construct one directly for isolation from the module-level singleton. Separate
instances never share consent state.

```python
from sensortap import Registry

registry = Registry(
    discovery_timeout_ms=5000,
    include_elevated=False,
    audit_hook=None,
)
```

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `discovery_timeout_ms` | `int` | `5000` | Per-round discovery timeout. Must be 100 to 60000 inclusive; validated before any adapter is touched. |
| `include_elevated` | `bool` | `False` | Load adapters declaring `requires_elevation_optin`. |
| `audit_hook` | `Callable[[AuditEvent], None] \| None` | `None` | Receives every consent lifecycle event. |

Methods: `list_sensors`, `read`, `stream`, `consent`, `backend_status`, and
`shutdown`.

`shutdown()` calls `teardown()` on every loaded adapter exactly once, catching
and recording rather than re-raising any exception so one failing teardown
cannot stop the rest. It is idempotent and also registered with `atexit`.

An adapter whose `discover()` times out contributes no records for that round.
It is not an error and does not raise to the caller.

---

## 7. Schema types

### `SensorInfo`

Frozen and slotted. Records are shared across threads during concurrent
enumeration, so immutability removes any question of a consumer mutating a
record the registry still holds.

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | `str` | Schema version this record follows, currently `"1.0"`. |
| `id` | `str` | The sensor id. |
| `kind` | `str` | A member of the closed kind vocabulary. |
| `dtype` | `Dtype` | Value shape discriminator. |
| `unit` | `str \| None` | Unit token, at most 32 characters. `None` when unitless. |
| `channels` | `tuple[str, ...]` | Channel names, for example `("x", "y", "z")`. |
| `shape` | `tuple[int, ...]` | Value shape. Last dimension varies fastest. |
| `range` | `tuple[float, float] \| None` | Minimum and maximum, when known. |
| `resolution` | `float \| None` | Smallest distinguishable increment, when known. |
| `rate_hz` | `RateSpec` | Sampling-rate specification. |
| `delivery` | `Delivery` | How the backend hands values to the adapter. |
| `derived` | `bool` | `True` when the value is computed from more than one physical sensor rather than read directly. |
| `requires_consent` | `bool` | `True` for privacy-sensitive sensors. |
| `requires_elevation` | `bool` | `True` when reachable only with elevated privileges. |
| `source` | `tuple[str, ...]` | Every reporting backend, in load order. |
| `vendor` | `str \| None` | Vendor string, when reported. |
| `part_number` | `str \| None` | Part number, when reported. |
| `availability` | `Availability` | Current availability state. |
| `extra` | `Mapping[str, object]` | Adapter-specific extras. Empty by default. |

### `Reading`

Frozen and slotted.

| Field | Type | Description |
| --- | --- | --- |
| `id` | `str` | The sensor id this reading came from. |
| `t_mono` | `float` | Monotonic timestamp. Never decreases for a given sensor. |
| `t_wall` | `float` | Wall-clock timestamp, Unix epoch seconds. |
| `values` | `tuple[float, ...]` | Flat values matching the declared `shape`. Empty only when `status` is `unavailable`. |
| `seq` | `int` | Per-sensor sequence number. Distinct across concurrent readers. |
| `status` | `Status` | Reading status. |

Concurrent readers of one sensor always receive distinct `seq` values, and
`t_mono` is clamped forward if an adapter ever reports a non-increasing clock.

### `RateSpec`

| Field | Type | Description |
| --- | --- | --- |
| `default` | `float \| None` | Default rate in Hz. |
| `min` | `float \| None` | Slowest configurable rate in Hz. |
| `max` | `float \| None` | Fastest configurable rate in Hz. |
| `supported` | `tuple[float, ...]` | Discrete configurable rates. Empty means a continuous range between `min` and `max`. |

A requested rate outside `min`..`max` raises `RateOutOfRangeError`. When
`supported` is non-empty and the request is not a member, the nearest
supported rate by absolute distance is used, with exact ties resolving to the
lower rate.

### Enums

All four are `StrEnum`, so they compare and serialize as plain strings, and
the value sets are identical across platforms by construction.

#### `Dtype`

| Value | Meaning |
| --- | --- |
| `scalar` | One value. |
| `vector3` | Three values on three axes. |
| `matrix` | A two-dimensional grid, for example a touchpad capacitive image. |
| `buffer` | A block of samples. Readable only through a stream, never `read()`. |

#### `Delivery`

| Value | Meaning |
| --- | --- |
| `push` | The backend delivers values as they arrive. |
| `poll` | The adapter fetches a value on demand. |

#### `Availability`

| Value | Meaning |
| --- | --- |
| `present` | Discovered and currently readable. |
| `absent` | The sensor class is not exposed by the platform or backend. |
| `unavailable` | Discovered, but currently fails to read. |
| `in_use_by_other_app` | Another process holds exclusive access. |
| `permission_denied` | The operating system denied access. |

#### `Status`

| Value | Meaning |
| --- | --- |
| `ok` | A current, valid value. |
| `stale` | Older than two sampling intervals. |
| `degraded` | Valid, but at least one earlier block was discarded under backpressure. |
| `unavailable` | No value. `values` is empty. |

---

## 8. Vocabularies

### Kind vocabulary

Closed, 24 members. Adding a kind is a MINOR schema bump; removing or
re-meaning one is MAJOR. An adapter needing a kind outside this set must land
a one-line vocabulary change alongside its adapter file. The friction is
deliberate: an open vocabulary would make cross-platform `kind` filtering
meaningless.

```
accel              gyro               magn               incline
orientation        hinge-angle        light              proximity
temp               fan                voltage            current
power              clock              load               battery
camera             microphone         touchpad           touchscreen
keystroke-timing   radio-signal       humidity           pressure
```

### Unit vocabulary

`unit` is validated as a well-formed token of at most 32 characters, not as a
semantic UCUM expression. Adapters shipped with sensortap emit only:

```
degC   V   A   W   Hz   m/s2   deg/s   uT   lx   deg   %   mW.h   rpm
```

Fan speed is pinned to `rpm` once in the schema so no adapter picks its own
spelling.

---

## 9. Consent

Camera, microphone, and touchpad capacitive-image sensors are
privacy-sensitive: their `SensorInfo.requires_consent` is `True`.

Enumeration never opens a device or triggers a permission prompt. Reading or
streaming one of these requires a grant naming that exact sensor id first, or
the read is refused with `ConsentError` and the device is left closed.

```python
import sensortap

with sensortap.consent(["microphone.winrt.0"]) as grant:
    for block in sensortap.stream("microphone.winrt.0"):
        ...
# grant ended, every device opened under it closed
```

| Property | Behaviour |
| --- | --- |
| Storage | Process memory only. Never written to disk, an environment variable, or any other store. |
| Initial state | Every gate starts with zero grants. |
| Scope | Per `Registry` instance. Separate instances never share grant state. |
| Wildcards | Rejected. A `*`, `?`, `[`, or `]` anywhere, a non-string, or an empty set raises `InvalidConsentRequestError` and creates no grant. |
| Ending | Context exit, explicit `revoke()`, or the `atexit` backstop. Every device opened under the grant is closed before returning, even if one close fails. |
| Idempotence | `revoke()` is safe to call repeatedly. |

`ConsentGrant` exposes `sensor_ids`, `revoke()`, `register_open_device()`, and
the context-manager protocol.

### Audit events

Every grant creation, use, denial, and end emits an `AuditEvent` to the
`audit_hook` passed to `Registry`.

| Field | Type | Description |
| --- | --- | --- |
| `sensor_id` | `str` | The sensor involved. |
| `outcome` | `str` | One of `granted`, `used`, `denied`, `ended`. |
| `t_wall` | `float` | Wall-clock timestamp. |

The type carries no values field, so a reading cannot leak into the audit
trail. An exception raised by the hook is swallowed rather than propagated.

---

## 10. Streaming

`Stream` is both an iterator and a context manager. A fixed-capacity ring
buffer sits between a background producer thread and the consumer.

```python
import sensortap

with sensortap.stream("microphone.winrt.0", buffer_blocks=128) as s:
    for block in s:
        print(block.seq, block.status, len(block.values))
        if s.discarded_blocks:
            break
```

| Member | Type | Description |
| --- | --- | --- |
| `sensor_id` | `str` | The streamed sensor id. |
| `buffer_blocks` | `int` | Configured ring buffer depth. |
| `discarded_blocks` | `int` | Count of blocks dropped under backpressure. |
| `applied_rate_hz` | `float \| None` | Rate the backend actually applied. |
| `achieved_rate_hz()` | `float \| None` | Measured rate over the most recent 100 delivered blocks. `None` below 2 delivered blocks. |
| `close()` | `None` | Release backend resources. Idempotent. |

Backpressure: when the buffer is full, the oldest undelivered block is
dropped, `discarded_blocks` increments, and the next block actually delivered
carries `status = degraded`. `seq` is assigned at production time, before the
buffer, so a discard leaves a gap in `seq` rather than renumbering.

While the buffer is empty and the stream is open, iteration blocks until a
block arrives. Once the producer stops and the buffer drains, iteration raises
`StopIteration`. Iterating an explicitly closed stream raises
`ClosedStreamError`.

Resources are released by `close()`, by garbage collection through
`weakref.finalize` if the stream is dropped without closing, or by an `atexit`
backstop.

Some backends permit only one concurrent stream per sensor; a second request
raises `StreamBusyError` and leaves the existing stream open and unaffected.

---

## 11. Backend status

`backend_status()` returns one `BackendStatus` per adapter, maintained
continuously rather than computed on demand.

| Field | Type | Description |
| --- | --- | --- |
| `adapter_id` | `str` | The adapter's stable identifier. |
| `state` | `"loaded" \| "not_loaded" \| "degraded" \| "unknown"` | Current state. |
| `discovered_count` | `int` | Sensors reported by the most recent enumeration. |
| `reason` | `NotLoadedReason \| None` | Populated only when `state` is `not_loaded`. |
| `remediation` | `str \| None` | Human-readable hint, 1 to 500 characters. |
| `unsatisfied_deps` | `tuple[DependencySpec, ...]` | Up to 20 entries, each with `name` and `version_constraint`. |
| `last_timeout_ms` | `int \| None` | The discovery timeout exceeded on the most recent enumeration. |
| `helper_state` | `"running" \| "not_running" \| "failed_to_start" \| None` | For helper-dependent adapters. |
| `introspection_failed` | `bool` | `True` when the state could not be determined. |
| `last_enumeration_ms` | `float \| None` | Duration of the most recent enumeration. |

`NotLoadedReason` is a closed set: `unsupported_platform`,
`missing_dependency`, `load_error`, `elevation_required`, `not_opted_in`.

Loaded-with-zero-sensors is `state="loaded"` with `discovered_count=0`, which
never collapses into a `not_loaded` record: `reason` is meaningful only when
`state` is `not_loaded`.

---

## 12. Errors and exit codes

All exceptions derive from `SensortapError`, so one `except` clause catches
everything. Each carries structured fields, so no caller needs to parse a
message string.

```python
from sensortap.registry.errors import SensortapError, UnknownSensorError
```

### Exit codes

Frozen; part of the CLI's contract.

| Code | Meaning | Exceptions |
| --- | --- | --- |
| `0` | Success | — |
| `1` | Unexpected internal error | anything unanticipated |
| `2` | Unknown or malformed command or option | argparse failure, `InvalidTimeoutError`, `MalformedSensorIdError` |
| `3` | Unknown sensor id | `UnknownSensorError` |
| `4` | Sensor unavailable or device open failure | `SensorUnavailableError`, `DeviceOpenError`, `StreamBusyError`, `BlockPathRequiredError`, `UnsupportedOperationError` |
| `5` | Missing consent | `ConsentError`, `InvalidConsentRequestError` |
| `6` | Read timeout | `ReadTimeoutError` |
| `7` | `doctor` found adapters that fail the conformance contract | — (a finding, not an exception) |

### Exception reference

| Exception | Raised when | Key fields |
| --- | --- | --- |
| `UnknownSensorError` | The id is absent from the current enumeration. | `sensor_id`, `last_known_availability` |
| `MalformedSensorIdError` | The id violates the grammar or length limits. No lookup is performed. | `sensor_id`, `offending_segment` |
| `BlockPathRequiredError` | `read()` was called on a `buffer` sensor. | `sensor_id` |
| `BlockSizeOutOfRangeError` | A requested block size is outside the supported range. Nothing is allocated. | `sensor_id`, `requested_block_size`, `min_block_size`, `max_block_size` |
| `ClosedStreamError` | A block was requested from a closed stream. | `sensor_id` |
| `StreamBusyError` | A second stream was requested where the backend permits one. | `sensor_id` |
| `RateOutOfRangeError` | A requested rate falls outside `min`..`max`. | `sensor_id`, `requested_rate_hz`, `min_rate_hz`, `max_rate_hz`, `supported_rates_hz` |
| `RateFixedError` | Rate configuration was requested on a fixed-rate sensor. | `sensor_id` |
| `RateConflictError` | A rate change was requested on an open stream, or a second stream requested a different rate. The applied rate is unchanged. | `sensor_id`, `applied_rate_hz`, `requested_rate_hz` |
| `InvalidTimeoutError` | A discovery timeout is non-numeric or outside 100..60000 ms. The configured timeout is unchanged. | `supplied_value`, `min_ms`, `max_ms` |
| `UnsupportedOperationError` | An optional adapter operation was requested that the owning adapter does not implement. | `sensor_id`, `operation` |
| `UnsupportedConfigKeyError` | A configuration key other than rate or block size was supplied. | `sensor_id`, `key` |
| `ConsentError` | A privacy-sensitive sensor was read without a grant. The device is left closed. | `sensor_id` |
| `InvalidConsentRequestError` | A grant used a wildcard, a pattern, or an empty set. No grant is created. | `reason` |
| `SensorUnavailableError` | A discovered sensor cannot currently be opened or read. | `sensor_id`, `reason` |
| `DeviceOpenError` | The underlying device handle failed to open. | `sensor_id`, `reason` |
| `ReadTimeoutError` | A poll read exceeded its time budget. | `sensor_id`, `timeout_ms` |

`ValidationError` and `SchemaVersionError` are never raised to a caller. The
registry constructs and records them while dropping the offending record and
keeping every other valid record from that adapter.

---

## 13. Shipped adapters

Nine Windows adapters, all run against real hardware.

| Adapter id | Kinds | Notes |
| --- | --- | --- |
| `winrt_motion` | `accel`, `gyro`, `magn` | WinRT motion classes. Rate configurable through report interval. |
| `winrt_orientation` | `incline`, `orientation`, `hinge-angle` | All `derived`: fused from multiple physical sensors. |
| `winrt_light` | `light` | Ambient light in lux. |
| `winrt_camera` | `camera` | Consent-gated. |
| `winrt_audio` | `microphone` | Consent-gated. `buffer` dtype, stream only. |
| `win_battery` | `battery`, `power`, `temp`, `voltage` | Charge percentage, capacity, charge rate. |
| `win_radio` | `radio-signal` | Wi-Fi signal, Bluetooth presence. |
| `win_touchpad` | `touchpad` | Capacitive image is consent-gated. |
| `hwmon_bridge` | `temp`, `fan`, `voltage`, `current`, `power`, `clock`, `load` | Bundled .NET helper wrapping LibreHardwareMonitor. Board/Super-IO/EC sensors behind `--include-motherboard`. |

A tenth adapter, `reference`, ships as a synthetic contributor example. It
requires no hardware and exposes one sensor per dtype, each demonstrating a
different path an adapter has to get right. It is not hardware and its
readings are not measurements.

| Sensor | Demonstrates |
| --- | --- |
| `temp.reference.0` | The normal push path, plus the stale-value rule: `read()` reports `stale` rather than passing off an old value as `ok`. The one to use when you want an example that returns a number. |
| `accel.reference.0` | The never-received-a-value path. It is `present` and it *deliberately never produces a sample*, so `read()` returns immediately with `status = unavailable` and empty `values` instead of blocking forever. This is the fixture working correctly, not a broken sensor. |
| `touchpad.reference.0` | The plain synchronous poll path. |
| `microphone.reference.0` | The block/stream path; `buffer` dtype, so `stream()` only. |

`accel.reference.0` is worth knowing about before you meet it: a sensor that is
`present` but reads `unavailable` looks like a bug and is not one. That
combination is legal and load-bearing across the whole project — a device can
exist while a current value does not.

### Helper process security

`hwmon_bridge` spawns a child process and speaks newline-delimited JSON over a
named pipe. Python is the pipe server; the helper is the client.

- The pipe carries a per-user security descriptor and rejects remote clients.
- A single-use launch token is delivered over the child's stdin, never on the
  command line.
- The helper's first message must be exactly `HELLO <token>`, compared in
  constant time.
- The connecting process id is verified against the spawned child's.
- Any authentication failure closes the pipe and terminates the child
  immediately, with no retry.
- Total readiness budget is 10 seconds. Failure marks the adapter unavailable
  rather than raising, so every other adapter still enumerates.

sensortap never installs, registers, or starts a kernel driver.

---

## 14. Writing an adapter

One sensor family is one file, registered through a standard Python entry
point. No edit to sensortap's own source is needed.

```toml
[project.entry-points."sensortap.adapters"]
my_widget = "my_package.adapter:MyWidgetAdapter"
```

The interface is a `typing.Protocol`, so structural typing applies and your
class needs no import-time dependency on a sensortap base class.

### `AdapterMeta`

Declared as a class variable. The registry reads it without instantiating your
class, so the platform and version gates cost no side effects.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `adapter_id` | `str` | required | Stable, lowercase identifier. |
| `interface_version` | `str` | required | `"MAJOR.MINOR"`. MAJOR must match the core's `1.0`; MINOR is advisory. |
| `supported_platforms` | `frozenset[str]` | required | Platform tags, for example `{"win32"}`. |
| `priority` | `int \| None` | `None` | Resolves id conflicts between adapters. `None` sorts lowest. |
| `requires_elevation_optin` | `bool` | `False` | When `True`, the adapter loads only under `--include-elevated`. |
| `read_only_declared` | `bool` | `False` | **Must be `True`** or the registry refuses to load the adapter. |

### Required methods

| Method | Contract |
| --- | --- |
| `discover() -> Sequence[SensorInfo]` | Enumerate everything this adapter can report. Must not open camera, microphone, or HID device handles; those open lazily on first read or stream. |
| `read(sensor_id) -> Reading` | Return the current value for one owned sensor. Never called for a `buffer` sensor. |

### Optional methods

A missing optional method is not a load-time error. The registry raises
`UnsupportedOperationError` only when a caller actually invokes that
operation, and the adapter's other sensors stay readable through `read()`.

| Method | Purpose |
| --- | --- |
| `open_stream(sensor_id, *, block_size, rate_hz, buffer_blocks) -> StreamSource` | Open a block/streaming path. |
| `supported_block_sizes(sensor_id) -> tuple[int, int]` | Minimum and maximum block size. |
| `configure_rate(sensor_id, rate_hz) -> float` | Request a rate; return the rate actually applied. Raise `RateFixedError` for fixed-rate sensors. |
| `setup() -> None` | One-time initialization after instantiation. |
| `teardown() -> None` | Release adapter-level resources. Called at most once. |
| `health() -> AdapterHealth` | Report detail beyond `BackendStatus`. Exposes `status` and `detail`. |

### `StreamSource`

| Method | Contract |
| --- | --- |
| `next_block() -> Reading` | Return the next block. Blocks until one is ready or the stream closes. |
| `close() -> None` | Release backend resources. Must complete within 1000 ms. |
| `achieved_rate() -> float \| None` | Measured delivery rate. `None` below 2 samples. |
| `applied_rate() -> float` | The rate actually applied, which may differ from the request. |

Buffering, discard-on-backpressure, and the iterator surface belong to the
registry's `Stream`, not to your adapter.

### Conformance check

A reusable check ships inside the package, so you can validate your adapter
against the same rules the nine Windows adapters were checked against, with no
access to sensortap's repository:

```sh
python -m sensortap.adapters.conformance my_package.adapter:MyWidgetAdapter
```

It runs five checks: schema compliance of discovered records, `Sensor_Id`
stability across consecutive `discover()` calls, reachability of every
discovered sensor through `read()` or `open_stream()`, adapter-level error
behaviour, and the declared read-only obligation. `sensortap doctor` runs the
same check against every adapter loaded on the current machine — see
[CLI → `doctor`](#doctor).

### Load sequence

1. Entry points are discovered and sorted by `(distribution_name, entry_point_name)` for a deterministic load order.
2. `AdapterMeta` is read off the class without instantiating it.
3. A platform or interface-MAJOR mismatch is a skip, not a failure.
4. An adapter requiring elevation opt-in is skipped unless the caller opted in.
5. Import, instantiation, and `setup()` are bounded at 5000 ms in total. An overrun or raise is recorded as a load failure, retained for the process lifetime, and iteration continues with the remaining adapters.
6. An adapter whose `read_only_declared` is not `True` is refused.

One bad adapter never aborts loading the rest.

### Post-collection pipeline

Records returned by `discover()` pass through six ordered steps:

1. **Validate** every record. An invalid record is dropped individually; that adapter's other records survive.
2. **De-duplicate** on byte-for-byte id equality only. No fuzzy matching.
3. **Resolve conflicts** between different adapters reporting the same id, by declared `priority`, ties going to the first loaded. The winner's `source` lists every reporting adapter.
4. **Disambiguate collisions** where one adapter reports the same id for genuinely distinct sensors, by appending a suffix. Every sensor is kept.
5. **Order** by adapter load order, then ascending id.
6. **Filter** on `kind`, `source`, and `id`.

---

## 15. Guarantees and limits

### Guaranteed

- Importing `sensortap` loads no adapters and spawns no processes.
- Enumeration never opens a device or triggers a permission prompt.
- Reading a camera, microphone, or touchpad capacitive image requires an explicit per-id consent grant. Grants live in memory only.
- No kernel driver is ever installed, registered, or started.
- The interface defines no actuation path: no method transmits a setpoint, control value, or firmware payload. Acquisition configuration is limited to sampling rate and block size.
- Sensor ids are stable across runs on unchanged hardware.
- `t_mono` never decreases for a sensor, and concurrent readers get distinct `seq` values.
- One adapter's failure, timeout, or exception never prevents other adapters from loading or enumerating.
- The public API is identical on every platform. Nothing in the core branches on the operating system.
- Every enum value set is identical across platforms.

### Not guaranteed, or not built

- **Linux.** The registry and adapter interface are already platform-neutral and nothing in the core imports anything Windows-specific, but no Linux backend exists yet. This is the clearest open contribution: sysfs hwmon, IIO, V4L2, ALSA, evdev, one file per family.
- **A sensor's existence.** A WinRT class the operating system does not expose reports `absent` rather than guessing whether the physical chip is there. Reporting a wrong number is worse than reporting none.
- **`read_only_declared` enforcement.** It is a declaration the registry requires, not a property it can verify inside an adapter's internals.
- **Unit semantics.** `unit` is validated as a token, not as a UCUM expression.
- **Motherboard and EC sensors.** Available only under an explicit opt-in, and absent entirely where no Ring 0 driver is already present or no Super-IO chip is recognised.
- **Adoption figures.** None exist to quote, and none are invented.
- **A touchpad's maximum contact count.** Touchpad presence is detected from the HID usage tables — usage page `0x0D` (Digitizer), usage `0x05` (Touch Pad) — which is vendor-neutral and separates a touchpad from a touchscreen (`0x04`) or a pen (`0x02`). A maximum-contacts figure, though, lives in the HID report descriptor, and parsing it requires opening the device, which `discover()` never does. Where WinRT's `PointerDevice` does not also surface the touchpad, `touchpad.win-ptp.0` is `present` while `touchpad.win-contacts.0` is `absent`: the touchpad exists and its contact count is not obtainable without acquisition.
- **Correct behaviour on hardware the maintainer does not own.** Adapters are written against the schema contract and the test suite proves it holds for the hardware available. It cannot prove anything about hardware it has never seen. `sensortap doctor` exists so that the check runs where the hardware is.

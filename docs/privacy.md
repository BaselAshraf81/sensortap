# Privacy

sensortap can expose cameras, microphones and other sensors that carry
personal information. This page states, plainly and precisely, what
protects a user from silent access, what leaves the machine (nothing),
and the honest limits of those protections.

## Which sensors require consent

A sensor is a **Privacy_Sensitive_Sensor** when its `SensorInfo.requires_consent`
field is `true`. As of the adapters currently implemented, that is exactly:

| Sensor_Id pattern | Adapter | Why |
| --- | --- | --- |
| `camera.winrt.*` | `sensortap/adapters/windows/winrt_camera.py` (`WindowsCameraAdapter`) | opens a real camera device on read |
| `microphone.winrt.*` | `sensortap/adapters/windows/winrt_audio.py` (`WindowsAudioAdapter`) | opens a real microphone device on stream |
| `touchpad.win-capimg.0` | `sensortap/adapters/windows/win_touchpad.py` (`WindowsTouchpadAdapter`) | the raw capacitive touch image, a biometric/gesture-adjacent signal |

Everything else the touchpad adapter reports (`touchpad.win-ptp.0`,
presence of an integrated touch pointer device, and
`touchpad.win-contacts.0`, its maximum contact count) is **not**
consent-gated: both are static capability flags, not live personal data,
so `requires_consent = False` for those two. Only the capacitive image
sensor is gated — verify this directly in `win_touchpad.py`'s
`discover()`, where only `capacitive_image_info` sets
`requires_consent=True`.

Non-personal telemetry — thermistors, fan tachometers, voltage/current/
power/clock/load sensors from `hwmon_bridge.py`, motion/orientation/light
sensors, battery and radio-signal sensors — all set `requires_consent =
False`. The requirements document (`requirements.md`, Req 7.1) names
camera, microphone, touchpad capacitive image and keystroke timing as the
categories that must be gated; no adapter currently shipped implements a
keystroke-timing sensor, so that category exists in the schema vocabulary
(`sensortap/schema/kinds.py`) but has no live instance yet.

## How the Consent_Gate protects you

**From the perspective of an application built on sensortap:**

- Enumerating sensors (`sensortap.list_sensors()` / `sensortap list`)
  never opens a device and never triggers an operating-system permission
  prompt, even for a camera or microphone. `SensorInfo` records for
  Privacy_Sensitive_Sensors are built from OS *metadata* only — see
  `winrt_camera.py`'s and `winrt_audio.py`'s `discover()` methods, which
  construct no `MediaCapture`/`AudioGraph` object.
- Reading or streaming a Privacy_Sensitive_Sensor requires the calling
  application to explicitly name that exact Sensor_Id in a consent grant
  first. Attempting a read or stream without one raises a consent error
  naming the Sensor_Id and opens no device
  (`sensortap/registry/consent.py`, `ConsentGate.is_granted()`, wired
  into the read/stream path per Req 7.3/7.4).
- Grants live in process memory only — a plain `dict` on the
  `ConsentGate` instance — and are never written to disk, to an
  environment variable, or to any other store. A fresh process always
  starts with zero grants.
- A grant ends automatically: on exit of the `with` block that created
  it, on an explicit `revoke()` call, or on process exit via an
  `atexit` hook that ends every still-open grant (`ConsentGate.
  _end_all_grants()`). Ending a grant closes every device that was
  opened under it before the grant is considered gone.
- Naming a sensor in a sensortap consent grant does not, by itself,
  grant OS-level access. The first actual device open under that grant
  still goes through the normal Windows permission check (camera/
  microphone privacy settings); sensortap surfaces a denial as
  `Availability.PERMISSION_DENIED` on the next `discover()` call rather
  than pretending the read succeeded.
- Every grant creation, use, denial, and end is recorded as an
  `AuditEvent(sensor_id, outcome, t_wall)`. That event type deliberately
  carries no values/readings field, so a sensor reading can never leak
  into the audit trail through this mechanism.

**From the perspective of an adapter author or application developer**,
consent is requested as a context manager:

```python
import sensortap

with sensortap.consent(["camera.winrt.9b1c07de4a2f6538"]) as grant:
    reading = sensortap.read("camera.winrt.9b1c07de4a2f6538")
# grant ends here; the camera is closed
```

`sensortap.consent()` rejects wildcards, patterns, and empty Sensor_Id
sets outright — a grant must name each Sensor_Id it covers literally
(`ConsentGate.grant()` in `sensortap/registry/consent.py`). The CLI
exposes the same protection: `sensortap read`/`sensortap stream` on a
Privacy_Sensitive_Sensor without a `--consent <that-id>` flag naming it
prints a consent error, opens no device, and exits non-zero.

## No data leaves this machine

sensortap transmits no data off the local machine. This is structurally
true, not a policy promise:

- There is no networking code anywhere in the Python core — no
  `socket`, `http`, `urllib`, `requests`, or equivalent import exists in
  `sensortap/registry/`, `sensortap/adapters/`, or `sensortap/cli/`.
- The one component that runs out-of-process, the Windows Helper_Process
  (`helper_src/`, built as `sensortap-helper.exe`), talks to the Python
  core over a local named pipe only. Its C# source
  (`helper_src/Program.cs`) and the Python-side pipe client
  (`sensortap/adapters/windows/helper/client.py`) contain no `Socket`,
  `HttpClient`, or any other networking API — verified by inspecting
  both files directly. The pipe is created by Python with a per-user
  security descriptor and `PIPE_REJECT_REMOTE_CLIENTS`, so even the
  named-pipe transport itself cannot be reached remotely.

## The Ring0 shim: hardware-monitoring sensors on Windows

Some hardware-monitoring data — specifically, motherboard/Super-IO chip
sensors (as opposed to CPU, GPU, memory and storage sensors) — is only
reachable through LibreHardwareMonitorLib's Ring0 driver path (the
`WinRing0`/`inpoutx64`-family kernel driver). sensortap's stance, stated
plainly:

- sensortap never installs, registers, or starts that driver itself.
  If it is reachable at all, it is only because some *other* already-
  installed software put it there.
- Access to sensors behind that shim is opt-in at the package level:
  the Windows Helper_Process and its pywin32-based client are shipped
  behind the `sensortap[hwmon]` install extra (`pyproject.toml`), not
  in the base `pip install sensortap` wheel.
- Security software routinely flags Ring0-style drivers such as
  `WinRing0`/`inpoutx64`. If one is present on your machine (installed
  by other software, not by sensortap), your antivirus or EDR product
  may flag it. That flag is about the driver's presence, not about
  anything sensortap does to it.

**Current state versus designed-for state, to be precise about actual
risk:** as shipped today, the Windows helper (`helper_src/
HardwareMonitor.cs`) deliberately leaves `IsMotherboardEnabled = false`,
so it never reaches LibreHardwareMonitorLib's Ring0 driver-install code
path and never reports a motherboard/Super-IO sensor. Concretely, **no
sensor sensortap currently surfaces requires the Ring0 shim** — every
sensor the helper reports today (CPU, GPU, memory, storage: temp, fan,
voltage, current, power, clock, load) needs no elevated driver access.
The gating logic that checks for this
(`sensortap/adapters/windows/hwmon_bridge.py`'s `_requires_elevation()`
and `_ring0_unavailable_reason()`) is implemented and wired correctly so
that if motherboard sensors are ever enabled in a future helper build,
a sensor reachable only through an absent shim degrades to
`Availability.UNAVAILABLE` naming the missing shim — never to a fabricated
reading and never to sensortap installing anything to satisfy it. Today
that code path is unreachable; it exists for forward compatibility, not
because there is a live risk right now.

## The three release layers, and their honest limits

Streams (`sensortap/registry/streaming.py`, `Stream`) and consent grants
(`sensortap/registry/consent.py`, `ConsentGrant`) each release their
underlying resources — open device handles, background producer threads,
and (for consent) closing every device opened under the grant — through
three layers, applied in this order of preference:

1. **Explicit release.** Calling `Stream.close()` (or exiting a `with
   sensortap.stream(...)` block), or ending a consent grant via context
   exit or `ConsentGrant.revoke()`. This is the reliable, immediate path
   and should be preferred by every caller.
2. **`weakref.finalize` as a garbage-collection backstop.** If a `Stream`
   becomes unreachable without an explicit `close()`, `weakref.finalize`
   releases its resources once the object is collected
   (`Stream.__init__`'s `self._finalizer = weakref.finalize(...)`).
3. **`atexit` as a process-exit backstop.** Both `Stream` and
   `ConsentGate` register an `atexit` hook that releases any resources
   or ends any grants still outstanding at normal interpreter shutdown
   (`streaming.py`'s `atexit.register(_release_resources, ...)` and
   `consent.py`'s `ConsentGate._register_atexit()`).

**Honest limit, stated plainly: `os._exit()`, `SIGKILL`, and any hard
crash of the process bypass all three of these layers.** None of
explicit `close()`, `weakref.finalize`, or `atexit` runs across a hard
kill — Python's interpreter-shutdown machinery, which `atexit` and
finalizers depend on, is simply never reached. This means camera and
microphone device handles, open streams, and in-memory consent grants
have no guaranteed release path across a hard kill of the process. This
is a real, documented limitation of any pure-Python resource-management
approach, not something sensortap works around — there is no way to
guarantee resource release across a hard kill of the OS process, on any
platform.

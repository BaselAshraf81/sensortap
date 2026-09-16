# sensortap Windows Helper (`helper_src/`)

This is the source for `sensortap-helper.exe`, the out-of-process userspace
.NET helper that the Hardware_Monitor_Adapter talks to on Windows, per
requirements 13.5, 13.6, 15.5 and the design document's "The Windows
Helper_Process" section. It is **not** shipped in the Python `sdist` or the
pure-Python wheel; it is built and bundled separately (task 9.5) into the
`win_amd64` wheel only.

> **This code has NOT been compiled or run.** The development environment
> used to write it has no .NET SDK installed (`dotnet --version` reports "No
> .NET SDKs were found"). Everything below was written from knowledge of
> .NET 8, `System.IO.Pipes`, `System.Text.Json` (all stable, well-documented,
> unlikely to have surprises) and LibreHardwareMonitorLib's public API as
> documented on its GitHub repository and NuGet page (less certain — see
> "Known uncertainties" below). **Build and smoke-test this on a real
> machine with the .NET 8 SDK before trusting it in CI or release packaging
> (task 9.5).**

## Building

Requires the .NET 8 SDK.

```powershell
# From helper_src/
dotnet restore
dotnet build -c Release
```

Release publish (self-contained, single-file, trimmed, per design.md's
"Language and build" note and the ~15-25MB size estimate):

```powershell
dotnet publish -c Release -r win-x64 `
  -p:PublishSingleFile=true `
  -p:SelfContained=true `
  -p:PublishTrimmed=true `
  -p:IncludeNativeLibrariesForSelfExtract=true `
  -o publish/
```

The resulting `publish/sensortap-helper.exe` is what task 9.5 places at
`sensortap/adapters/windows/helper/bin/sensortap-helper.exe`.

An automated version of this exact command runs in CI on `windows-latest`
in `.github/workflows/build.yml`, which also copies the result into
`adapters/windows/helper/bin/` and runs the Python test suite against it
(including the pywin32/winsdk-dependent code paths that this development
environment could not fully exercise). See that workflow file for the
authoritative automated build; the manual steps above remain useful for
local development and debugging.

If `PublishTrimmed` causes runtime errors (trimming can remove members that
LibreHardwareMonitorLib accesses via reflection), drop that flag first and
re-test; a larger untrimmed single-file binary is a safe fallback.

## Roles and invocation

Per the decision recorded against task 8.2 in `tasks.md`: **Python is the
named pipe server**, using a per-user security descriptor and
`PIPE_REJECT_REMOTE_CLIENTS`. **This helper is the pipe client.** Python
creates the pipe, then launches this executable with the pipe name as its
only command-line argument:

```
sensortap-helper.exe <pipe-name>
```

`<pipe-name>` is the short name Python chose (e.g. `sensortap-<random>`),
without the `\\.\pipe\` prefix — `NamedPipeClientStream` adds that prefix
itself when constructed with `"."` as the server name.

## Launch_Token handshake

1. Python generates a 256-bit token (`secrets.token_urlsafe(32)`).
2. Python spawns this process with the pipe name as `argv[0]`, writes the
   token as one line to the child's **stdin**, then closes stdin. The token
   is never passed on the command line (visible in the process list) and
   never as an environment variable.
3. This helper reads exactly one line from stdin — the token — before
   attempting to connect to the pipe.
4. This helper connects to the pipe as a client, then sends `HELLO {token}`
   as its very first message on the pipe (bare text line, not JSON).
5. Python compares the token with `hmac.compare_digest`. On mismatch, or on
   receiving anything other than a well-formed `HELLO {token}` as the first
   message, Python closes the pipe and terminates the child immediately.
6. From this helper's side, an authentication rejection is observed
   indirectly: the pipe closes (or a subsequent read fails) instead of a
   normal request ever arriving. The wire protocol as specified in
   `tasks.md`/`design.md` defines no explicit "AUTH_OK" reply — success is
   implicit in the pipe staying open and a well-formed `{"cmd": ...}` line
   eventually arriving. Any I/O failure before that point causes this
   helper to exit with a non-zero code (see exit codes below), satisfying
   Req 13.6's "exit on any authentication failure".

## Wire protocol

Newline-delimited JSON, one JSON object per line, in both directions, after
the one-line `HELLO {token}` handshake message.

### Requests (Python → helper)

```json
{"cmd": "list"}
{"cmd": "read", "ids": ["/amdcpu/0/temperature/0", "/lpc/nct6798d/fan/0"]}
{"cmd": "ping"}
{"cmd": "shutdown"}
```

- `list` — enumerate every Temperature/Fan/Voltage/Clock/Load sensor
  currently known to LibreHardwareMonitorLib, across all hardware and
  sub-hardware nodes. No fixed limit below 512 sensors per category is
  imposed (Req 13.5) — in this implementation there is no cap at all.
- `read` — return current values for exactly the given `ids` (the `id`
  field from a prior `list` response). Unknown ids are silently omitted
  from the response rather than causing an error.
- `ping` — liveness check; always responds `{"ok": true}` once the process
  is running and past the handshake.
- `shutdown` — helper acknowledges with `{"ok": true}` then exits with code
  0. This is the graceful-shutdown path described in design.md's
  "Lifecycle" section (Python waits up to 5000 ms for this before killing
  the process).

### Responses (helper → Python)

```json
{"ok": true, "sensors": [ { "...": "..." } ]}
{"ok": true}
{"ok": false, "error": "human-readable message"}
```

`sensors` is present for `list` and `read` responses; omitted (absent, not
null) for `ping` and `shutdown` acknowledgements.

### Sensor record shape

```json
{
  "id": "/amdcpu/0/temperature/0",
  "hardware_id": "/amdcpu/0",
  "hardware_name": "AMD Ryzen 9 5900X",
  "hardware_type": "Cpu",
  "sensor_type": "Temperature",
  "name": "CPU Package",
  "value": 42.5,
  "min": 30.1,
  "max": 78.9
}
```

- `id` is LibreHardwareMonitorLib's own internal sensor identifier string.
  It is stable for the lifetime of one helper process/run but is **not**
  the final cross-run-stable `Sensor_Id` that the Python core assigns —
  deriving that (by hashing a persistent hardware identifier, per
  `registry/ids.py`'s `instance_hash()`) is the job of the Python-side
  `hwmon_bridge.py` (task 9.4), not this helper.
- `hardware_id` / `hardware_name` / `hardware_type` describe the sensor's
  parent hardware node, so the Python bridge can build a persistent
  hardware identifier without needing a second round-trip.
- `sensor_type` is one of `Temperature`, `Fan`, `Voltage`, `Clock`, `Load`
  (LibreHardwareMonitorLib's `SensorType` enum member name, verbatim) —
  chosen so the bridge's mapping to `kind` (`temp`, `fan`, `voltage`,
  `clock`, `load`) is a one-line lookup table.
- `value` / `min` / `max` are `null` when the library has not sampled that
  statistic yet.

## Driver installation (Req 15.4, 15.5)

This helper must never install, register or start a kernel driver, and must
only use a Ring0 shim (`WinRing0`/`inpoutx64`-family driver) if one is
**already installed** by other software.

`HardwareMonitor.cs` takes a conservative approach: it enables CPU, GPU,
Memory and Storage monitoring in LibreHardwareMonitorLib's `Computer`
object, but leaves `IsMotherboardEnabled = false`. Motherboard/Super-IO
sensor access is, per public documentation and source of
LibreHardwareMonitorLib, the path most likely to trigger the library's
internal Ring0-driver-install logic (used for embedded-controller and
Super-IO chip register reads). By never enabling that hardware group, this
helper's code path never reaches the library's driver-install logic at all.

**This is a deliberate, conservative simplification, not a confirmed API
call.** See the detailed comment at the top of `HardwareMonitor.cs` for the
full reasoning. It has the effect that any sensor reachable *only* through
motherboard/Super-IO access will not appear in this helper's `list` output,
which is consistent with the required fallback behavior (report as
unavailable / omit rather than install anything) but is more conservative
than strictly necessary — the ideal fix, once someone can build against the
real library, is to find (or confirm the absence of) a documented flag that
lets the library read from an *already-running* Ring0 driver service
without ever attempting to install one, and wire that in explicitly for
motherboard sensors too.

## Known uncertainties (LibreHardwareMonitorLib API surface)

Flagged explicitly for verification once a real .NET 8 SDK and NuGet
restore are available:

1. **Package version pin (`0.9.3`)** in `SensortapHelper.csproj` — believed
   to be a real published version, not confirmed against a live NuGet feed
   in this environment. Verify it resolves, or bump to current latest.
2. **Driver-installation-disable mechanism** — see the section above and
   the long comment in `HardwareMonitor.cs`. The exact current API for
   "use an existing Ring0 driver service but never install one" was not
   confirmed against source in this environment.
3. **`Computer.Accept(IVisitor)` / `IVisitor` shape** (`VisitComputer`,
   `VisitHardware`, `VisitSensor`, `VisitParameter`) — believed correct
   based on the library's documented sample console application, but not
   compiled here.
4. **`IHardware.SubHardware`, `IHardware.Sensors`, `ISensor.Identifier`,
   `ISensor.SensorType`, `ISensor.Value`/`Min`/`Max`** — believed correct
   based on public documentation; property nullability (`float?` for
   `Value`/`Min`/`Max`) is believed but not verified against the exact
   0.9.x release's generated types.
5. Enabling flags used
   (`IsCpuEnabled`, `IsGpuEnabled`, `IsMotherboardEnabled`,
   `IsMemoryEnabled`, `IsStorageEnabled`, `IsNetworkEnabled`,
   `IsControllerEnabled`, `IsBatteryEnabled`, `IsPsuEnabled`) — names
   believed correct for recent LibreHardwareMonitorLib releases; confirm
   against the actual installed version, since these have changed across
   major versions of the library historically.

None of `System.IO.Pipes.NamedPipeClientStream`, `System.Text.Json`, or the
general .NET 8 console app / `Console.In`/`Console.Out` stdin handling used
in `Program.cs` carry the same uncertainty — those are stable BCL APIs.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Clean shutdown (`shutdown` command received, or pipe closed normally after a `shutdown` acknowledgement) |
| 1 | Authentication failure — no token on stdin, pipe connect failed, or the pipe closed/errored before a normal request loop could be established |
| 2 | Usage error — missing `<pipe-name>` argument |
| 3 | Unexpected error (uncaught exception outside the categories above) |

## What this task did not implement

Per the task boundary stated in `tasks.md`, this task covers only the C#
helper itself:

- `adapters/windows/helper/protocol.py` (task 9.2) — the Python-side wire
  protocol module.
- `adapters/windows/helper/client.py` (task 9.3) — the Python-side process
  lifecycle, pipe server, and token handshake implementation.
- `hwmon_bridge.py` (task 9.4) — the Hardware_Monitor_Adapter that maps this
  helper's sensor records onto `SensorInfo`/`Reading`.
- CI build wiring and wheel packaging (task 9.5, now done — see
  `.github/workflows/build.yml` and the `hwmon` extra / `package-data`
  entry in `pyproject.toml`).

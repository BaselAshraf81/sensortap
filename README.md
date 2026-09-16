# sensortap

One registry. Every sensor your computer has.

`sensortap` discovers every sensor a machine can reach, camera, microphone,
motion, battery, radios, and (on Windows, via a bundled helper) hardware
telemetry, and reads every one of them through the same small API. It's the
layer you build a sensor toy on top of, not the toy itself.

**[baselashraf.com/sensortap](https://baselashraf.com/sensortap/)** · Windows
today, Linux planned · Free and open source, [MIT](LICENSE)

## Why

Nobody unifies this. Windows' own `Windows.Devices.Sensors` covers motion and
light but is opt-in per OEM, so a real hinge-angle sensor commonly goes unused
because no app asks for it. LibreHardwareMonitor covers CPU/GPU/board
telemetry and nothing else. Nothing puts a camera, a microphone, a touchpad,
and a voltage rail behind one interface with the same shape.

That's the gap this fills, for people who like tinkering with sensors: linking
real hardware signals to UI, animation, or a small effect, the way
[Mac Duo](https://github.com/sumimakito/Mac-Duo) and my own
[WinDuo](https://github.com/BaselAshraf81/winduo) link one sensor to one
effect, by hand, per platform. sensortap is the layer under the next ten ideas
like that.

## Install

```sh
pip install sensortap
sensortap list
```

That's it for camera, microphone, motion, battery, and radio sensors.
Hardware-monitor sensors (per-core CPU load, temperatures, voltages, power,
clock speeds) need the optional extra, which pulls in a bundled .NET helper:

```sh
pip install "sensortap[hwmon]"
```

Verified live on a Dell G3 3779 running the actual CLI, not written from
memory:

```
$ sensortap list
...
total sensors: 91  kinds: 16  backends contributing: 10  non-contributing: 0
```

### Admin rights change what the hardware monitor can see

Running `sensortap list` unelevated vs. from an elevated (Administrator)
terminal can report a genuinely different sensor count for `hwmon_bridge`,
with no error and no listed reason. CPU MSR temperature/clock reads and
storage SMART reads both silently return fewer sensors without admin
rights; LibreHardwareMonitor doesn't fail loudly, it just reports less.
Confirmed on real hardware: 50 vs. 70 `hwmon_bridge` sensors on the same
machine, same run parameters, differing only by elevation.

`sensortap list` now surfaces this: when `hwmon_bridge` loaded and the
current process is not elevated, the plain-text output prints a one-line
note after the summary, and `--json`'s `summary.elevated` field reports
the same fact structurally. sensortap never requests elevation itself and
never triggers a UAC prompt; re-running from an elevated terminal is the
user's call, not something this tool does on your behalf.

### Going deeper: motherboard and EC sensors

Board temperatures, fan tachometers and extra voltage rails live behind
motherboard/Super-IO/embedded-controller access, which is **off by default**:

```sh
sensortap list --include-motherboard
```

It is opt-in because that path can conflict with a vendor tool or another
monitoring app (HWiNFO, OEM fan control) already holding the same EC
registers, and an unrecognised Super-IO chip can return plausible-looking
nonsense rather than an obvious failure. sensortap still never installs or
starts a kernel driver; on a machine with no Ring0 driver present and no
recognised Super-IO chip, this flag safely finds nothing extra (that is the
case on the G3 3779 above). On a desktop board with a standard Nuvoton or
ITE chip there is usually much more to find.

Sensors that only appear because of this opt-in carry `hwmon-mb` in their
id instead of `hwmon`, so a pasted id records which capability set produced
it.

## Use it

**From the command line:**

```sh
sensortap list
sensortap read accel.reference.0
sensortap inspect temp.hwmon.2aed4545974396bc
sensortap stream microphone.reference.0 --consent microphone.reference.0
sensortap doctor
```

**From Python:**

```python
import sensortap

sensors = sensortap.list_sensors()          # never opens a device, never prompts
reading = sensortap.read("accel.winrt.0")     # no consent needed, motion carries no personal data

with sensortap.consent(["microphone.winrt.0"]):   # camera/mic/touchpad-image need this
    for block in sensortap.stream("microphone.winrt.0"):
        ...
```

### Check your hardware: `sensortap doctor`

Every adapter is written against a schema contract, and the test suite proves
that contract holds for the hardware the author owns. It cannot prove anything
about the hardware he doesn't. Real defects have shipped that were structurally
present on every machine but only *observable* on a machine carrying the
relevant sensor: a microphone rate ceiling that silently dropped every mic, a
channel-count mismatch that silently dropped every camera, an ambient-light
adapter that ignored the requested sensor id.

So the contract check ships to you. `sensortap doctor` runs the same conformance
check the test suite runs, against every adapter that actually loaded on *your*
machine:

```sh
sensortap doctor                     # check what's visible now
sensortap doctor --include-elevated   # from an elevated terminal, includes hwmon
sensortap doctor --json               # machine-readable, for CI
```

It prints the environment facts that matter (OS, Python version, architecture,
elevation state), then one line per adapter:

```
environment
  sensortap: 0.1.0
  python: 3.12.10
  platform: Windows-10-10.0.19045-SP0
  machine: AMD64
  elevated: True

adapters
  [ok  ] winrt_motion
  [ok  ] winrt_audio
  [FAIL] winrt_light
           adapter-level error behaviour: read() returned a Reading for a
           sensor_id this adapter does not own

9/10 adapters passed  failed: 1
```

On failure it also prints a GitHub issue URL with the title and body already
filled in from the run, so reporting a hardware-specific bug is one click and
no typing. The environment block deliberately carries no hostname, no username
and no sensor ids — it's built to be pasted in public.

`doctor` exits **7** when adapters fail their contract, kept distinct from the
generic **1**: "the tool crashed" and "the tool works and your hardware found a
real bug" are different signals, and CI should be able to tell them apart.

## How it's built

One `Registry`, many independent `Adapter`s. Each adapter owns one sensor
family, reports through the same schema (`SensorInfo` + `Reading`), and knows
nothing about any other adapter. The registry loads every adapter it finds
through a standard Python entry point, runs discovery concurrently under a
shared timeout so a slow backend can't stall the rest, validates and
de-duplicates what comes back, and hands callers one flat, typed list.

Nine adapters ship today, all against real hardware:

| Adapter | Covers |
|---|---|
| `winrt_motion` | Accelerometer, gyroscope, magnetometer |
| `winrt_orientation` | Inclinometer, orientation sensor, hinge angle |
| `winrt_light` | Ambient light |
| `winrt_camera` | Camera (consent-gated) |
| `winrt_audio` | Microphone (consent-gated) |
| `win_battery` | Charge percentage, capacity, charge rate |
| `win_radio` | Wi-Fi signal, Bluetooth presence |
| `win_touchpad` | Touch-pointer presence, contact count, capacitive image (consent-gated) |
| `hwmon_bridge` | CPU/GPU/board temperature, fan, voltage, current, power, clock, load, via a bundled .NET helper wrapping LibreHardwareMonitor. Board/Super-IO/EC sensors behind `--include-motherboard` |

## Documentation

**[baselashraf.com/sensortap/docs](https://baselashraf.com/sensortap/docs/)** is
the documentation site: search, a section index, an on-page table of contents,
and a copy button on every command. It covers every CLI command, flag, and exit
code; the whole Python API; every schema field and enum value; the sensor-id
grammar; the kind and unit vocabularies; the consent and streaming models; the
complete error table; and the adapter interface for contributors.

The same content is also **[docs/REFERENCE.md](docs/REFERENCE.md)**, one
self-contained markdown file with no external includes, so you can paste the
whole thing into an LLM context window and ask questions about sensortap
without the model guessing at the API. The docs site has a "Copy page for AI"
button that does exactly this, or from a shell:

```sh
# copy the entire reference to the clipboard (Windows)
Get-Content docs/REFERENCE.md -Raw | Set-Clipboard

# macOS
pbcopy < docs/REFERENCE.md

# Linux (X11 / Wayland)
xclip -selection clipboard < docs/REFERENCE.md
wl-copy < docs/REFERENCE.md
```

It is also served as plain text at
[baselashraf.com/sensortap/reference.md](https://baselashraf.com/sensortap/reference.md),
so an agent with web access can fetch it directly.

Other docs: [privacy.md](docs/privacy.md) for the consent model in prose,
[schema.md](docs/schema.md) for the schema reference generated from the code,
and [contributing/adapter-guide.md](docs/contributing/adapter-guide.md) for the
adapter walkthrough.

## Adding a sensor

One sensor family is one file, registered through a Python entry point, no
edit to sensortap's own source required:

```toml
[project.entry-points."sensortap.adapters"]
my_widget = "my_package.adapter:MyWidgetAdapter"
```

A reusable Conformance_Check ships in the package itself:

```sh
python -m sensortap.adapters.conformance my_package.adapter:MyWidgetAdapter
```

Full walkthrough: [docs/contributing/adapter-guide.md](docs/contributing/adapter-guide.md).

## Privacy

Enumerating sensors never opens a device or triggers an OS permission prompt,
even for a camera. Reading or streaming a camera, microphone, or touchpad
capacitive-image sensor requires your code to explicitly grant consent for
that exact sensor id first; grants live in memory only and are never written
to disk. Full detail: [docs/privacy.md](docs/privacy.md).

## What's built, what isn't

- **Windows**: built out, tested against real hardware.
- **Linux**: designed into the architecture (the registry and adapter
  interface are already platform-neutral) but not yet implemented. This is
  the clearest open contribution — sysfs hwmon, IIO, V4L2, ALSA, evdev, one
  file per family, same as every Windows adapter.
- **The hwmon helper is optional weight, on purpose.** The base install stays
  small; the .NET helper is a separate, disclosed download behind
  `sensortap[hwmon]`.

- **A touchpad's maximum contact count isn't always reachable.** Presence is
  detected from the HID spec — usage page `0x0D` (Digitizer), usage `0x05`
  (Touch Pad) — which is vendor-neutral and also separates a touchpad from a
  touchscreen (`0x04`) or a pen (`0x02`). A contact count, though, lives in the
  HID report descriptor, and parsing that means *opening* the device, which
  discovery never does. So on hardware where WinRT's `PointerDevice` doesn't
  surface the touchpad, `touchpad.win-ptp.0` reports `present` while
  `touchpad.win-contacts.0` reports `absent`. That pair is meaningful rather
  than broken: there is a touchpad, and its contact count isn't free.

## Contributing

```sh
pip install -e ".[dev]"
pytest
```

219 tests, none requiring physical sensor hardware in the default run. See
[docs/contributing/adapter-guide.md](docs/contributing/adapter-guide.md) for
writing a new adapter and [docs/schema.md](docs/schema.md) for the schema
reference, generated straight from the code.

## License

[MIT](LICENSE). Free for any use, commercial included.

## Credit

Built by **[Basel Ashraf](https://baselashraf.com)**. In the same lineage as
[WinDuo](https://github.com/BaselAshraf81/winduo) and
[Roadwright](https://roadwright.baselashraf.com).

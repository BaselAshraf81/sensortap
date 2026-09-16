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

**[docs/REFERENCE.md](docs/REFERENCE.md)** is the full reference: every CLI
command, flag, and exit code; the whole Python API; every schema field and
enum value; the sensor-id grammar; the kind and unit vocabularies; the consent
and streaming models; the complete error table; and the adapter interface for
contributors.

It is one self-contained markdown file with no external includes, so you can
paste the whole thing into an LLM context window and ask questions about
sensortap without the model guessing at the API:

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
**[baselashraf.com/sensortap/reference.md](https://baselashraf.com/sensortap/reference.md)**,
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

## Contributing

```sh
pip install -e ".[dev]"
pytest
```

197 tests, none requiring physical sensor hardware in the default run. See
[docs/contributing/adapter-guide.md](docs/contributing/adapter-guide.md) for
writing a new adapter and [docs/schema.md](docs/schema.md) for the schema
reference, generated straight from the code.

## License

[MIT](LICENSE). Free for any use, commercial included.

## Credit

Built by **[Basel Ashraf](https://baselashraf.com)**. In the same lineage as
[WinDuo](https://github.com/BaselAshraf81/winduo) and
[Roadwright](https://roadwright.baselashraf.com).

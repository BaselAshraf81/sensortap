"""The closed `kind` vocabulary (Req 2.12).

Published alongside the schema version and re-exported in the docs build so
the vocabulary page cannot drift from the code. Adding a `kind` is a MINOR
schema bump (it never invalidates an existing record); removing or
re-meaning a `kind` is a MAJOR bump. An adapter that needs a `kind` not in
this set must land a one-line vocabulary PR alongside its adapter file —
deliberate friction, because an open vocabulary would destroy the
cross-platform `kind` filtering of Req 1.6.

Unit vocabulary (Open Decision 5, Req 2.1, 14.3, 14.5): `unit` is validated
as a well-formed token of at most 32 characters, not as a semantic UCUM
expression. The vocabulary emitted by adapters shipped with sensortap is
pinned to: `degC`, `V`, `A`, `W`, `Hz`, `m/s2`, `deg/s`, `uT`, `lx`, `deg`,
`%`, `mW.h`.

Fan speed unit (Open Decision 6): pinned once as `FAN_SPEED_UNIT` below so
every platform adapter imports the same literal rather than choosing its
own spelling (`rpm` vs `/min`) per adapter.
"""

from __future__ import annotations

#: Fan speed unit string, pinned once and imported by every adapter that
#: reports a `fan` kind sensor (Open Decision 6).
FAN_SPEED_UNIT = "rpm"

#: The closed `kind` vocabulary. 24 initial members: the design document
#: states "25 initial members" but the vocabulary it enumerates lists only
#: 24 distinct kinds (accel, gyro, magn, incline, orientation, hinge-angle,
#: light, proximity, temp, fan, voltage, current, power, clock, load,
#: battery, camera, microphone, touchpad, touchscreen, keystroke-timing,
#: radio-signal, humidity, pressure). No other requirement or design section
#: names a 25th sensor kind, so this is kept at 24 rather than inventing an
#: arbitrary addition just to match the stated count.
KIND_VOCABULARY: frozenset[str] = frozenset(
    {
        "accel",
        "gyro",
        "magn",
        "incline",
        "orientation",
        "hinge-angle",
        "light",
        "proximity",
        "temp",
        "fan",
        "voltage",
        "current",
        "power",
        "clock",
        "load",
        "battery",
        "camera",
        "microphone",
        "touchpad",
        "touchscreen",
        "keystroke-timing",
        "radio-signal",
        "humidity",
        "pressure",
    }
)

"""Import-level probe for the `winsdk` WinRT sensor bindings.

This is deliberately NOT marked ``@pytest.mark.hardware``: it does not touch
any physical sensor, it only checks that the generated ``winsdk`` projection
exposes the WinRT sensor classes sensortap's Windows adapter depends on.
That means it runs in the default CI pass on a Windows runner, with no
sensor hardware attached.

Requirements: 13.1, 13.2

Finding (recorded here for traceability): as of ``winsdk==1.0.0b10`` on
Python 3.12 (Windows), all seven classes below import successfully,
including ``HingeAngleSensor``. If a future winsdk release drops or renames
``HingeAngleSensor``, the parametrized test will fail with an ``ImportError``
naming the missing class, and the project's headline claim ("hinge-angle
support") plus the hinge-angle adapter's implementation route will need to
be revisited.
"""

from __future__ import annotations

import importlib

import pytest

MODULE_PATH = "winsdk.windows.devices.sensors"

EXPECTED_CLASSES = (
    "Accelerometer",
    "Gyrometer",
    "Magnetometer",
    "Inclinometer",
    "OrientationSensor",
    "LightSensor",
    "HingeAngleSensor",
)


def test_winsdk_sensors_module_imports() -> None:
    """The generated WinRT sensors module itself must be importable."""
    module = importlib.import_module(MODULE_PATH)
    assert module is not None


@pytest.mark.parametrize("class_name", EXPECTED_CLASSES)
def test_winsdk_exposes_sensor_class(class_name: str) -> None:
    """Each WinRT sensor class sensortap's Windows adapter needs is present.

    A failure here means ``from winsdk.windows.devices.sensors import
    <class_name>`` raises ``ImportError`` -- i.e. the installed ``winsdk``
    projection does not expose that class. For ``HingeAngleSensor``
    specifically, that would mean the hinge-angle adapter needs a different
    binding route (e.g. hand-written WinRT activation) and the project's
    headline claim about hinge-angle support needs softening until that
    route is implemented.
    """
    module = importlib.import_module(MODULE_PATH)
    assert hasattr(module, class_name), (
        f"winsdk.windows.devices.sensors has no attribute '{class_name}'. "
        "The generated projection does not expose this WinRT sensor class."
    )


def test_winsdk_exposes_all_seven_sensor_classes_via_import() -> None:
    """Regression guard: the exact `from ... import ...` form must work.

    This mirrors the real usage pattern in the Windows sensor adapter, not
    just attribute presence on the module.
    """
    from winsdk.windows.devices.sensors import (  # noqa: F401
        Accelerometer,
        Gyrometer,
        HingeAngleSensor,
        Inclinometer,
        LightSensor,
        Magnetometer,
        OrientationSensor,
    )

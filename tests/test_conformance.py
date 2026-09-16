"""Tests for the reusable Conformance_Check (Req 10.8, 15.7, 16.8, 16.9)."""

from __future__ import annotations

from typing import ClassVar

import pytest

from sensortap.adapters.conformance import run_conformance_check
from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION, AdapterMeta
from sensortap.adapters.reference import ReferenceAdapter
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo
from sensortap.schema.version import SCHEMA_VERSION


def _valid_sensor_info(**overrides) -> SensorInfo:
    defaults = dict(
        schema_version=SCHEMA_VERSION,
        id="temp.broken.0",
        kind="temp",
        dtype=Dtype.SCALAR,
        unit="degC",
        channels=("temp",),
        shape=(1,),
        range=(-40.0, 125.0),
        resolution=0.1,
        rate_hz=RateSpec(default=10.0, min=1.0, max=100.0),
        delivery=Delivery.PUSH,
        derived=False,
        requires_consent=False,
        requires_elevation=False,
        source=("broken",),
        vendor=None,
        part_number=None,
        availability=Availability.PRESENT,
    )
    defaults.update(overrides)
    return SensorInfo(**defaults)


class _SchemaViolatingAdapter:
    """Deliberately non-conforming: `discover()` returns an invalid `kind`."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="schema_violator",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=frozenset({"win32", "linux", "darwin"}),
        read_only_declared=True,
    )

    def discover(self):
        return (_valid_sensor_info(kind="not-a-real-kind"),)

    def read(self, sensor_id: str) -> Reading:
        return Reading(
            id=sensor_id,
            t_mono=0.0,
            t_wall=0.0,
            values=(1.0,),
            seq=0,
            status=Status.OK,
        )


class _UnstableIdAdapter:
    """Deliberately non-conforming: Sensor_Id changes between discover() calls."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="unstable_id",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=frozenset({"win32", "linux", "darwin"}),
        read_only_declared=True,
    )

    def __init__(self) -> None:
        self._call_count = 0

    def discover(self):
        self._call_count += 1
        return (_valid_sensor_info(id=f"temp.broken.{self._call_count}"),)

    def read(self, sensor_id: str) -> Reading:
        return Reading(
            id=sensor_id,
            t_mono=0.0,
            t_wall=0.0,
            values=(1.0,),
            seq=0,
            status=Status.OK,
        )


class _NotReadOnlyDeclaredAdapter:
    """Deliberately non-conforming: never declares the read-only obligation."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="not_declared",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=frozenset({"win32", "linux", "darwin"}),
        read_only_declared=False,
    )

    def discover(self):
        return (_valid_sensor_info(),)

    def read(self, sensor_id: str) -> Reading:
        return Reading(
            id=sensor_id,
            t_mono=0.0,
            t_wall=0.0,
            values=(1.0,),
            seq=0,
            status=Status.OK,
        )


class _SilentUnknownIdAdapter:
    """Deliberately non-conforming: read() on an unknown id never raises."""

    meta: ClassVar[AdapterMeta] = AdapterMeta(
        adapter_id="silent_unknown",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=frozenset({"win32", "linux", "darwin"}),
        read_only_declared=True,
    )

    def discover(self):
        return (_valid_sensor_info(),)

    def read(self, sensor_id: str) -> Reading:
        # Bogus: returns a value regardless of sensor_id, never raises.
        return Reading(
            id=sensor_id,
            t_mono=0.0,
            t_wall=0.0,
            values=(1.0,),
            seq=0,
            status=Status.OK,
        )


@pytest.fixture()
def reference_adapter():
    adapter = ReferenceAdapter()
    adapter.setup()
    try:
        yield adapter
    finally:
        adapter.teardown()


def test_reference_adapter_passes_cleanly(reference_adapter):
    report = run_conformance_check(reference_adapter)
    assert report.passed, report.render()
    assert len(report.checks) == 5


class _UnreachableBufferAdapter:
    """Declares a `buffer` sensor but implements no `open_stream()`.

    This is the exact shape of a defect that shipped in `winrt_camera`:
    the registry routes `buffer` sensors only through `stream()`, so
    `read()` raises `BlockPathRequiredError` and `stream()` raises
    `UnsupportedOperationError`. The sensor is unreadable by any caller on
    every machine, and every other conformance check passes it, because
    the checks that exercise reading deliberately skip `buffer` records.
    """

    meta = AdapterMeta(
        adapter_id="unreachable_buffer",
        interface_version=ADAPTER_INTERFACE_VERSION,
        supported_platforms=frozenset({"win32", "linux", "darwin"}),
        read_only_declared=True,
    )

    def discover(self):
        return (
            SensorInfo(
                schema_version=SCHEMA_VERSION,
                id="microphone.unreachable.0",
                kind="microphone",
                dtype=Dtype.BUFFER,
                unit=None,
                channels=("mono",),
                shape=(1024, 1),
                range=None,
                resolution=None,
                rate_hz=RateSpec(default=None, min=None, max=None),
                delivery=Delivery.PUSH,
                derived=False,
                requires_consent=True,
                requires_elevation=False,
                source=("unreachable",),
                vendor=None,
                part_number=None,
                availability=Availability.PRESENT,
            ),
        )

    def read(self, sensor_id: str):
        raise KeyError(sensor_id)


def test_buffer_sensor_without_open_stream_fails_reachability_check():
    report = run_conformance_check(_UnreachableBufferAdapter())
    assert not report.passed
    check = next(c for c in report.checks if "reachable" in c.name)
    assert not check.passed
    assert "open_stream" in check.detail


def test_reference_adapter_passes_reachability_check(reference_adapter):
    """The reference adapter exposes a buffer sensor *and* implements
    open_stream(), so it must pass -- guarding the new check against
    flagging correct adapters."""
    report = run_conformance_check(reference_adapter)
    check = next(c for c in report.checks if "reachable" in c.name)
    assert check.passed, check.detail


def test_schema_violating_adapter_fails_schema_check():
    report = run_conformance_check(_SchemaViolatingAdapter())
    assert not report.passed
    schema_check = next(c for c in report.checks if "schema compliance" in c.name)
    assert not schema_check.passed


def test_unstable_sensor_id_adapter_fails_stability_check():
    report = run_conformance_check(_UnstableIdAdapter())
    assert not report.passed
    stability_check = next(c for c in report.checks if "stability" in c.name)
    assert not stability_check.passed


def test_undeclared_read_only_adapter_fails_declaration_check():
    report = run_conformance_check(_NotReadOnlyDeclaredAdapter())
    assert not report.passed
    declaration_check = next(c for c in report.checks if "read-only obligation" in c.name)
    assert not declaration_check.passed


def test_silent_unknown_id_adapter_fails_error_behaviour_check():
    report = run_conformance_check(_SilentUnknownIdAdapter())
    assert not report.passed
    error_check = next(c for c in report.checks if "error behaviour" in c.name)
    assert not error_check.passed


def test_report_render_produces_readable_pass_fail_summary(reference_adapter):
    report = run_conformance_check(reference_adapter)
    rendered = report.render()
    assert "reference" in rendered
    assert "Overall: PASSED" in rendered

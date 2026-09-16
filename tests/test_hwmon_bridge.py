"""Tests for the Windows hardware-monitoring adapter (Req 13.5, 13.12,
15.4, 15.5).

There is no compiled `.NET` helper to test against live (same limitation
noted in task 9.3's own tests) -- every test here uses a fake
`HelperClient`-like object exposing the same `state` property and
`send_list()`/`send_read()` methods `WindowsHardwareMonitorAdapter`
actually calls, so what's verified is the adapter's own mapping/gating
logic, not the real named-pipe transport or the real .NET helper.
"""

from __future__ import annotations

from sensortap.adapters.windows.helper.protocol import HelperResponse, HelperSensorRecord
from sensortap.adapters.windows.hwmon_bridge import WindowsHardwareMonitorAdapter
from sensortap.registry.ids import instance_hash
from sensortap.schema.enums import Availability, Dtype, Status


class _FakeHelperClient:
    """Minimal stand-in for `HelperClient` exposing only what
    `WindowsHardwareMonitorAdapter` uses."""

    def __init__(self, *, state: str = "running") -> None:
        self.state = state
        self.setup_calls = 0
        self.shutdown_calls = 0
        self._list_response: HelperResponse | None = None
        self._read_response: HelperResponse | None = None

    def setup(self) -> None:
        self.setup_calls += 1

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def send_list(self) -> HelperResponse:
        assert self._list_response is not None
        return self._list_response

    def send_read(self, ids: list[str]) -> HelperResponse:
        assert self._read_response is not None
        return self._read_response

    def set_list_response(self, response: HelperResponse) -> None:
        self._list_response = response

    def set_read_response(self, response: HelperResponse) -> None:
        self._read_response = response


def _record(
    id_: str = "/amdcpu/0/temperature/0",
    *,
    sensor_type: str = "Temperature",
    hardware_type: str = "Cpu",
    value: float | None = 42.0,
    min_: float | None = 10.0,
    max_: float | None = 90.0,
) -> HelperSensorRecord:
    return HelperSensorRecord(
        id=id_,
        hardware_id="/amdcpu/0",
        hardware_name="AMD Ryzen",
        hardware_type=hardware_type,
        sensor_type=sensor_type,
        name="CPU Core",
        value=value,
        min=min_,
        max=max_,
    )


def test_discover_returns_empty_tuple_when_helper_not_running() -> None:
    fake = _FakeHelperClient(state="not_running")
    adapter = WindowsHardwareMonitorAdapter(helper_client=fake)

    records = adapter.discover()

    assert records == ()


def test_discover_maps_helper_records_to_sensor_info() -> None:
    fake = _FakeHelperClient(state="running")
    temp = _record(sensor_type="Temperature", hardware_type="Cpu")
    fan = _record(
        id_="/lpc/nct6798d/fan/0",
        sensor_type="Fan",
        hardware_type="SuperIO",
        value=1200.0,
        min_=None,
        max_=None,
    )
    fake.set_list_response(HelperResponse(ok=True, sensors=[temp, fan], error=None))

    adapter = WindowsHardwareMonitorAdapter(helper_client=fake)
    records = adapter.discover()

    assert len(records) == 2
    temp_info, fan_info = records

    assert temp_info.kind == "temp"
    assert temp_info.unit == "degC"
    assert temp_info.dtype is Dtype.SCALAR
    assert temp_info.shape == (1,)
    assert temp_info.channels == ("value",)
    assert temp_info.range == (10.0, 90.0)
    assert temp_info.requires_consent is False
    assert temp_info.availability is Availability.PRESENT

    assert fan_info.kind == "fan"
    assert fan_info.unit == "rpm"
    assert fan_info.range is None


def test_discover_sensor_id_hashes_helper_identifier() -> None:
    fake = _FakeHelperClient(state="running")
    temp = _record(id_="/amdcpu/0/temperature/0")
    fake.set_list_response(HelperResponse(ok=True, sensors=[temp], error=None))

    adapter = WindowsHardwareMonitorAdapter(helper_client=fake)
    (info,) = adapter.discover()

    expected_hash = instance_hash("/amdcpu/0/temperature/0")
    assert info.id == f"temp.hwmon.{expected_hash}"


def test_discover_skips_unrecognised_sensor_types() -> None:
    fake = _FakeHelperClient(state="running")
    unknown = _record(sensor_type="Factor", hardware_type="Cpu")
    fake.set_list_response(HelperResponse(ok=True, sensors=[unknown], error=None))

    adapter = WindowsHardwareMonitorAdapter(helper_client=fake)
    records = adapter.discover()

    assert records == ()


def test_discover_calls_setup_lazily_once() -> None:
    fake = _FakeHelperClient(state="running")
    fake.set_list_response(HelperResponse(ok=True, sensors=[], error=None))

    adapter = WindowsHardwareMonitorAdapter(helper_client=fake)
    adapter.discover()
    adapter.discover()

    assert fake.setup_calls == 1


def test_read_returns_ok_reading_from_helper_response() -> None:
    fake = _FakeHelperClient(state="running")
    temp = _record(value=55.5)
    fake.set_list_response(HelperResponse(ok=True, sensors=[temp], error=None))

    adapter = WindowsHardwareMonitorAdapter(helper_client=fake)
    (info,) = adapter.discover()

    fake.set_read_response(
        HelperResponse(ok=True, sensors=[_record(value=55.5)], error=None)
    )
    reading = adapter.read(info.id)

    assert reading.status is Status.OK
    assert reading.values == (55.5,)
    assert reading.id == info.id


def test_read_returns_unavailable_when_helper_not_running() -> None:
    fake = _FakeHelperClient(state="running")
    temp = _record()
    fake.set_list_response(HelperResponse(ok=True, sensors=[temp], error=None))

    adapter = WindowsHardwareMonitorAdapter(helper_client=fake)
    (info,) = adapter.discover()

    fake.state = "not_running"
    reading = adapter.read(info.id)

    assert reading.status is Status.UNAVAILABLE
    assert reading.values == ()


def test_read_returns_unavailable_on_helper_failure_response() -> None:
    fake = _FakeHelperClient(state="running")
    temp = _record()
    fake.set_list_response(HelperResponse(ok=True, sensors=[temp], error=None))

    adapter = WindowsHardwareMonitorAdapter(helper_client=fake)
    (info,) = adapter.discover()

    fake.set_read_response(HelperResponse(ok=False, sensors=None, error="boom"))
    reading = adapter.read(info.id)

    assert reading.status is Status.UNAVAILABLE
    assert reading.values == ()


def test_teardown_calls_helper_shutdown() -> None:
    fake = _FakeHelperClient(state="running")
    adapter = WindowsHardwareMonitorAdapter(helper_client=fake)

    adapter.teardown()

    assert fake.shutdown_calls == 1

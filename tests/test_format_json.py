"""Focused tests for the JSON formatter module (Req 2.14, 12.6)."""

from __future__ import annotations

import json

from sensortap.cli.format_json import (
    format_reading_json,
    format_sensor_info_json,
    format_sensor_list_json,
    reading_to_dict,
    sensor_info_to_dict,
)
from sensortap.schema.enums import Availability, Delivery, Dtype, Status
from sensortap.schema.rate import RateSpec
from sensortap.schema.reading import Reading
from sensortap.schema.sensor_info import SensorInfo

_REQUIRED_FIELDS = (
    "schema_version",
    "id",
    "kind",
    "dtype",
    "unit",
    "channels",
    "shape",
    "range",
    "resolution",
    "rate_hz",
    "delivery",
    "derived",
    "requires_consent",
    "requires_elevation",
    "source",
    "vendor",
    "part_number",
    "availability",
)


def _make_sensor_info(**overrides) -> SensorInfo:
    defaults = dict(
        schema_version="1.0",
        id="accel.builtin.0",
        kind="accel",
        dtype=Dtype.VECTOR3,
        unit="m/s^2",
        channels=("x", "y", "z"),
        shape=(3,),
        range=(-78.4, 78.4),
        resolution=0.01,
        rate_hz=RateSpec(default=100.0, min=1.0, max=200.0, supported=(50.0, 100.0, 200.0)),
        delivery=Delivery.PUSH,
        derived=False,
        requires_consent=False,
        requires_elevation=False,
        source=("winrt",),
        vendor="Acme",
        part_number="ACC-1",
        availability=Availability.PRESENT,
        extra={"x_hwmon_chip_path": "/sys/class/hwmon/hwmon0"},
    )
    defaults.update(overrides)
    return SensorInfo(**defaults)


def test_all_required_fields_present():
    d = sensor_info_to_dict(_make_sensor_info())
    for field_name in _REQUIRED_FIELDS:
        assert field_name in d


def test_extra_keys_flattened_at_top_level_not_nested():
    d = sensor_info_to_dict(_make_sensor_info())
    assert "extra" not in d
    assert d["x_hwmon_chip_path"] == "/sys/class/hwmon/hwmon0"


def test_enum_fields_serialize_as_plain_strings():
    d = sensor_info_to_dict(_make_sensor_info())
    assert d["dtype"] == "vector3"
    assert d["delivery"] == "push"
    assert d["availability"] == "present"
    assert isinstance(d["dtype"], str)
    assert "Dtype" not in d["dtype"]


def test_rate_hz_serializes_as_nested_object():
    d = sensor_info_to_dict(_make_sensor_info())
    assert d["rate_hz"] == {
        "default": 100.0,
        "min": 1.0,
        "max": 200.0,
        "supported": [50.0, 100.0, 200.0],
    }


def test_range_serializes_as_two_element_array():
    d = sensor_info_to_dict(_make_sensor_info())
    assert d["range"] == [-78.4, 78.4]


def test_range_none_serializes_as_null():
    d = sensor_info_to_dict(_make_sensor_info(range=None))
    assert d["range"] is None


def test_sensor_info_json_round_trips_and_is_valid_json():
    info = _make_sensor_info()
    text = format_sensor_info_json(info)
    parsed = json.loads(text)
    assert parsed["id"] == "accel.builtin.0"
    assert "\x1b" not in text


def test_sensor_list_json_is_array_of_full_records_with_no_ansi():
    sensors = [_make_sensor_info(id="accel.builtin.0"), _make_sensor_info(id="accel.builtin.1")]
    text = format_sensor_list_json(sensors)
    parsed = json.loads(text)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    for record in parsed:
        for field_name in _REQUIRED_FIELDS:
            assert field_name in record
    assert "\x1b" not in text


def test_reading_to_dict_and_format_reading_json():
    reading = Reading(
        id="accel.builtin.0",
        t_mono=123.456,
        t_wall=1700000000.0,
        values=(1.0, 2.0, 3.0),
        seq=7,
        status=Status.OK,
    )
    d = reading_to_dict(reading)
    assert d == {
        "id": "accel.builtin.0",
        "t_mono": 123.456,
        "t_wall": 1700000000.0,
        "values": [1.0, 2.0, 3.0],
        "seq": 7,
        "status": "ok",
    }
    text = format_reading_json(reading)
    assert json.loads(text) == d
    assert "\x1b" not in text

"""Tests for the Python-side wire protocol module for the Windows helper
process (Req 13.6). Mirrors `helper_src/WireProtocol.cs`'s shapes exactly.
"""

from __future__ import annotations

import json

import pytest

from sensortap.adapters.windows.helper.protocol import (
    HelperProtocolError,
    LineFramer,
    decode_response,
    encode_list_request,
    encode_ping_request,
    encode_read_request,
    encode_shutdown_request,
)


def test_encode_list_request() -> None:
    line = encode_list_request()
    assert line.endswith(b"\n")
    assert json.loads(line.decode("utf-8")) == {"cmd": "list"}


def test_encode_read_request() -> None:
    line = encode_read_request(["/amdcpu/0/temperature/0", "/lpc/nct6798d/fan/0"])
    assert json.loads(line.decode("utf-8")) == {
        "cmd": "read",
        "ids": ["/amdcpu/0/temperature/0", "/lpc/nct6798d/fan/0"],
    }


def test_encode_ping_request() -> None:
    line = encode_ping_request()
    assert json.loads(line.decode("utf-8")) == {"cmd": "ping"}


def test_encode_shutdown_request() -> None:
    line = encode_shutdown_request()
    assert json.loads(line.decode("utf-8")) == {"cmd": "shutdown"}


def test_decode_success_response_with_sensors() -> None:
    payload = {
        "ok": True,
        "sensors": [
            {
                "id": "/amdcpu/0/temperature/0",
                "hardware_id": "/amdcpu/0",
                "hardware_name": "AMD Ryzen 9 5900X",
                "hardware_type": "Cpu",
                "sensor_type": "Temperature",
                "name": "CPU Package",
                "value": 42.5,
                "min": 30.1,
                "max": 78.9,
            }
        ],
    }
    line = (json.dumps(payload) + "\n").encode("utf-8")

    response = decode_response(line)

    assert response.ok is True
    assert response.error is None
    assert response.sensors is not None
    record = response.sensors[0]
    assert record.id == "/amdcpu/0/temperature/0"
    assert record.hardware_name == "AMD Ryzen 9 5900X"
    assert record.value == 42.5
    assert record.min == 30.1
    assert record.max == 78.9


def test_decode_success_response_without_sensors() -> None:
    line = b'{"ok": true}\n'

    response = decode_response(line)

    assert response.ok is True
    assert response.sensors is None
    assert response.error is None


def test_decode_error_response() -> None:
    line = b'{"ok": false, "error": "something went wrong"}\n'

    response = decode_response(line)

    assert response.ok is False
    assert response.sensors is None
    assert response.error == "something went wrong"


def test_decode_malformed_json_raises_framing_error() -> None:
    with pytest.raises(HelperProtocolError):
        decode_response(b"not json at all\n")


def test_decode_missing_ok_field_raises_framing_error() -> None:
    with pytest.raises(HelperProtocolError):
        decode_response(b'{"sensors": []}\n')


def test_line_framer_handles_split_line_across_two_feeds() -> None:
    framer = LineFramer()

    first = framer.feed(b'{"ok": true')
    assert first == []

    second = framer.feed(b'}\n')
    assert second == [b'{"ok": true}']


def test_line_framer_handles_multiple_lines_in_one_feed() -> None:
    framer = LineFramer()

    lines = framer.feed(b'{"ok": true}\n{"ok": false, "error": "x"}\n')

    assert lines == [b'{"ok": true}', b'{"ok": false, "error": "x"}']

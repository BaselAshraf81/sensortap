"""Python-side mirror of the Windows helper's wire protocol
(`helper_src/WireProtocol.cs`, `helper_src/README.md`'s "Wire protocol"
section), per Req 13.6.

This module owns only the *framing* -- request encoding, response
decoding, and newline-delimited line assembly. It does not own the named
pipe I/O itself (task 9.3's `client.py`) or the mapping onto
`SensorInfo`/`Reading` (task 9.4's `hwmon_bridge.py`).

Encoding convention: every request is encoded to UTF-8 bytes with a
trailing ``b"\\n"``, since the pipe is read/written as bytes on the
Windows side (`System.IO.Pipes` / pywin32 both work naturally with bytes).
Callers write the returned bytes directly to the pipe.

Wire shapes (verbatim from `WireProtocol.cs` / README.md):

Requests (Python -> helper)::

    {"cmd": "list"}
    {"cmd": "read", "ids": ["/amdcpu/0/temperature/0", ...]}
    {"cmd": "ping"}
    {"cmd": "shutdown"}

Responses (helper -> Python)::

    {"ok": true, "sensors": [ {...} ]}
    {"ok": true}
    {"ok": false, "error": "human-readable message"}

Sensor record shape (``SensorRecord`` in `WireProtocol.cs`, field names
verbatim -- ``[JsonPropertyName(...)]`` snake_case on the wire)::

    {
      "id": "...", "hardware_id": "...", "hardware_name": "...",
      "hardware_type": "...", "sensor_type": "...", "name": "...",
      "value": 42.5, "min": 30.1, "max": 78.9
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass


class HelperProtocolError(Exception):
    """Raised when a line received from the helper process cannot be
    parsed as a well-formed wire-protocol response.

    This covers both framing failures (the line is not valid JSON at all)
    and shape failures (the JSON parses but is missing/mistypes the
    fields the protocol requires, e.g. no boolean "ok"). It is an
    adapter-origin failure: `hwmon_bridge.py` (task 9.4) is expected to
    catch this and degrade the affected read/discovery to an unavailable
    outcome rather than letting it propagate as a crash, in the same
    spirit as `SensorUnavailableError`/`DeviceOpenError` elsewhere in the
    codebase -- kept local to this module rather than added to
    `registry/errors.py` because it describes a failure mode specific to
    this one out-of-process transport, not a condition any Backend can
    hit.
    """

    def __init__(self, message: str, *, raw_line: bytes | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.raw_line = raw_line

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.raw_line is not None:
            return f"{self.message} (raw line: {self.raw_line!r})"
        return self.message


@dataclass(frozen=True)
class HelperSensorRecord:
    """One sensor record as reported by the helper, mirroring
    `SensorRecord` in `WireProtocol.cs` field-for-field."""

    id: str
    hardware_id: str
    hardware_name: str
    hardware_type: str
    sensor_type: str
    name: str
    value: float | None
    min: float | None
    max: float | None


@dataclass(frozen=True)
class HelperResponse:
    """A decoded helper response, mirroring `HelperResponse` in
    `WireProtocol.cs`.

    `sensors` is `None` for `ping`/`shutdown` acknowledgements (the field
    is absent on the wire) and for failure responses; it is a (possibly
    empty) list for `list`/`read` success responses.
    """

    ok: bool
    sensors: list[HelperSensorRecord] | None
    error: str | None


def encode_list_request() -> bytes:
    """Encode a ``{"cmd": "list"}`` request line."""

    return _encode_request({"cmd": "list"})


def encode_read_request(ids: list[str]) -> bytes:
    """Encode a ``{"cmd": "read", "ids": [...]}`` request line."""

    return _encode_request({"cmd": "read", "ids": list(ids)})


def encode_ping_request() -> bytes:
    """Encode a ``{"cmd": "ping"}`` request line."""

    return _encode_request({"cmd": "ping"})


def encode_shutdown_request() -> bytes:
    """Encode a ``{"cmd": "shutdown"}`` request line."""

    return _encode_request({"cmd": "shutdown"})


def _encode_request(payload: dict) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def decode_response(line: bytes) -> HelperResponse:
    """Decode one newline-delimited response line into a `HelperResponse`.

    `line` is a single line already split on ``b"\\n"`` by the caller
    (trailing newline, if present, is stripped here for convenience) --
    this function does not itself read from the pipe or split buffered
    data; see `LineFramer` for that.

    Raises `HelperProtocolError` if `line` is not valid JSON, is not a
    JSON object, or is missing/mistypes the fields the protocol requires.
    """

    stripped = line.rstrip(b"\r\n")
    try:
        text = stripped.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HelperProtocolError(
            f"response line is not valid UTF-8: {exc}", raw_line=line
        ) from exc

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HelperProtocolError(
            f"response line is not valid JSON: {exc}", raw_line=line
        ) from exc

    if not isinstance(parsed, dict):
        raise HelperProtocolError(
            "response line is not a JSON object", raw_line=line
        )

    if "ok" not in parsed or not isinstance(parsed["ok"], bool):
        raise HelperProtocolError(
            "response is missing a boolean 'ok' field", raw_line=line
        )

    ok = parsed["ok"]
    error = parsed.get("error")
    if error is not None and not isinstance(error, str):
        raise HelperProtocolError(
            "response 'error' field is not a string", raw_line=line
        )

    sensors_raw = parsed.get("sensors")
    sensors: list[HelperSensorRecord] | None
    if sensors_raw is None:
        sensors = None
    elif isinstance(sensors_raw, list):
        try:
            sensors = [_decode_sensor_record(item, line) for item in sensors_raw]
        except HelperProtocolError:
            raise
    else:
        raise HelperProtocolError(
            "response 'sensors' field is not a list", raw_line=line
        )

    return HelperResponse(ok=ok, sensors=sensors, error=error)


def _decode_sensor_record(item: object, raw_line: bytes) -> HelperSensorRecord:
    if not isinstance(item, dict):
        raise HelperProtocolError(
            "sensor record is not a JSON object", raw_line=raw_line
        )

    try:
        return HelperSensorRecord(
            id=_require_str(item, "id", raw_line),
            hardware_id=_require_str(item, "hardware_id", raw_line),
            hardware_name=_require_str(item, "hardware_name", raw_line),
            hardware_type=_require_str(item, "hardware_type", raw_line),
            sensor_type=_require_str(item, "sensor_type", raw_line),
            name=_require_str(item, "name", raw_line),
            value=_optional_float(item, "value", raw_line),
            min=_optional_float(item, "min", raw_line),
            max=_optional_float(item, "max", raw_line),
        )
    except HelperProtocolError:
        raise


def _require_str(item: dict, key: str, raw_line: bytes) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise HelperProtocolError(
            f"sensor record field {key!r} is missing or not a string",
            raw_line=raw_line,
        )
    return value


def _optional_float(item: dict, key: str, raw_line: bytes) -> float | None:
    if key not in item or item[key] is None:
        return None
    value = item[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise HelperProtocolError(
            f"sensor record field {key!r} is not numeric", raw_line=raw_line
        )
    return float(value)


class LineFramer:
    """Assembles newline-delimited lines from a byte stream delivered in
    arbitrary chunks by the pipe I/O layer.

    Reusable across the request and response direction; task 9.3's pipe
    reader is expected to feed raw bytes read from the pipe into `feed()`
    and act on each complete line returned, rather than re-implementing
    buffering itself.
    """

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, data: bytes) -> list[bytes]:
        """Append `data` to the internal buffer and return every complete
        newline-terminated line found so far (without the trailing
        newline). Any trailing, not-yet-terminated data is kept buffered
        for the next call."""

        self._buffer += data
        lines: list[bytes] = []
        while True:
            index = self._buffer.find(b"\n")
            if index == -1:
                break
            lines.append(self._buffer[:index])
            self._buffer = self._buffer[index + 1 :]
        return lines

    def reset(self) -> None:
        """Discard any buffered, incomplete-line data."""

        self._buffer = b""

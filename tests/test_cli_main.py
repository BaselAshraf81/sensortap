"""Focused tests for the CLI's argument parsing, dispatch and exit codes
(Req 12.1, 12.9).

Uses the `ReferenceAdapter` (registered under the `sensortap.adapters`
entry point group) so no real hardware is required.
"""

from __future__ import annotations

import io
import sys

import pytest

from sensortap.cli.main import (
    EXIT_CONSENT,
    EXIT_SUCCESS,
    EXIT_UNKNOWN_SENSOR,
    EXIT_USAGE,
    _build_parser,
    main,
)


def _run_cli(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_no_command_prints_usage_naming_all_four_and_exits_usage_code(capsys):
    code, out, err = _run_cli([], capsys)
    assert code == EXIT_USAGE
    assert out == ""
    for command in ("list", "read", "stream", "inspect"):
        assert command in err


def test_unrecognized_command_prints_usage_and_nonzero_exit(capsys):
    code, out, err = _run_cli(["bogus"], capsys)
    assert code != EXIT_SUCCESS
    assert out == ""
    for command in ("list", "read", "stream", "inspect"):
        assert command in err


def test_list_succeeds_and_exits_zero(capsys):
    code, out, err = _run_cli(["list"], capsys)
    assert code == EXIT_SUCCESS
    assert err == ""
    # The reference adapter always exposes at least one sensor.
    assert "reference" in out


def test_read_unknown_sensor_exits_unknown_sensor_code_with_nothing_on_stdout(capsys):
    code, out, err = _run_cli(["read", "does.not.exist"], capsys)
    assert code == EXIT_UNKNOWN_SENSOR
    assert out == ""
    assert err != ""


def test_read_known_sensor_succeeds(capsys):
    code, out, err = _run_cli(["read", "touchpad.reference.0"], capsys)
    assert code == EXIT_SUCCESS
    assert err == ""
    assert "touchpad.reference.0" in out


def test_inspect_known_sensor_succeeds(capsys):
    code, out, err = _run_cli(["inspect", "touchpad.reference.0"], capsys)
    assert code == EXIT_SUCCESS
    assert err == ""
    assert "touchpad.reference.0" in out


def test_consent_flag_parsed_as_repeatable_list():
    parser = _build_parser()
    args = parser.parse_args(
        ["read", "camera.mf.0", "--consent", "camera.mf.0", "--consent", "mic.mf.0"]
    )
    assert args.consent == ["camera.mf.0", "mic.mf.0"]


def test_global_options_work_before_and_after_subcommand():
    parser = _build_parser()
    before = parser.parse_args(["--json", "list"])
    after = parser.parse_args(["list", "--json"])
    assert before.json is True
    assert after.json is True


def test_include_motherboard_defaults_off_and_parses_either_side():
    """The motherboard/Super-IO/EC opt-in must never be on unless asked
    for: that path can conflict with a vendor tool or another monitoring
    app holding the same EC registers."""
    parser = _build_parser()
    assert parser.parse_args(["list"]).include_motherboard is False
    assert parser.parse_args(["--include-motherboard", "list"]).include_motherboard is True
    assert parser.parse_args(["list", "--include-motherboard"]).include_motherboard is True


def test_list_plain_text_includes_backend_status_and_summary_line(capsys):
    code, out, err = _run_cli(["list"], capsys)
    assert code == EXIT_SUCCESS
    assert "reference: loaded" in out
    assert "total sensors:" in out
    assert "kinds:" in out
    assert "backends contributing:" in out


def test_list_json_includes_backend_status_and_summary(capsys):
    import json

    code, out, err = _run_cli(["list", "--json"], capsys)
    assert code == EXIT_SUCCESS
    payload = json.loads(out)
    assert "sensors" in payload
    assert "backend_status" in payload
    assert "summary" in payload
    assert payload["summary"]["total_sensors"] == len(payload["sensors"])
    assert any(b["adapter_id"] == "reference" for b in payload["backend_status"])


def test_inspect_prints_complete_record_not_just_table_columns(capsys):
    code, out, err = _run_cli(["inspect", "touchpad.reference.0"], capsys)
    assert code == EXIT_SUCCESS
    for field_name in (
        "schema_version",
        "dtype",
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
    ):
        assert f"{field_name}:" in out


def test_stream_duration_out_of_range_is_usage_error_before_streaming(capsys):
    code, out, err = _run_cli(
        ["stream", "microphone.reference.0", "--consent", "microphone.reference.0", "--duration", "999999"],
        capsys,
    )
    assert code == EXIT_USAGE
    assert out == ""
    assert "duration" in err


def test_stream_duration_zero_is_usage_error(capsys):
    code, out, err = _run_cli(
        ["stream", "microphone.reference.0", "--consent", "microphone.reference.0", "--duration", "0"],
        capsys,
    )
    assert code == EXIT_USAGE
    assert out == ""


def test_read_privacy_sensitive_sensor_without_consent_fails_and_opens_no_device(capsys):
    code, out, err = _run_cli(["read", "microphone.reference.0"], capsys)
    assert code == EXIT_CONSENT
    assert out == ""
    assert err != ""


def test_stream_privacy_sensitive_sensor_without_consent_fails(capsys):
    code, out, err = _run_cli(["stream", "microphone.reference.0"], capsys)
    assert code == EXIT_CONSENT
    assert out == ""


def test_read_privacy_sensitive_sensor_with_consent_succeeds(capsys):
    code, out, err = _run_cli(
        ["stream", "microphone.reference.0", "--consent", "microphone.reference.0", "--duration", "1"],
        capsys,
    )
    assert code == EXIT_SUCCESS
    assert out != ""

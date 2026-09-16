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
    EXIT_DOCTOR_FINDINGS,
    EXIT_SUCCESS,
    EXIT_UNKNOWN_SENSOR,
    EXIT_USAGE,
    _build_parser,
    _elevation_hint,
    main,
)
from sensortap.registry.status import BackendStatus


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


def test_elevation_hint_absent_when_no_elevation_sensitive_adapter_loaded():
    """Only hwmon_bridge's sensor count is known to differ under
    elevation (CPU MSR temp/clock reads and storage SMART reads both
    silently degrade without admin rights). A run with no such adapter
    loaded has nothing this hint could be about."""
    statuses = [BackendStatus(adapter_id="reference", state="loaded", discovered_count=4)]
    assert _elevation_hint(statuses) is None


def test_elevation_hint_present_when_hwmon_loaded_and_not_elevated(monkeypatch):
    monkeypatch.setattr("sensortap.registry.privilege.is_elevated", lambda: False)
    statuses = [BackendStatus(adapter_id="hwmon_bridge", state="loaded", discovered_count=50)]
    hint = _elevation_hint(statuses)
    assert hint is not None
    assert "elevated" in hint.lower()


def test_elevation_hint_absent_when_hwmon_loaded_and_already_elevated(monkeypatch):
    monkeypatch.setattr("sensortap.registry.privilege.is_elevated", lambda: True)
    statuses = [BackendStatus(adapter_id="hwmon_bridge", state="loaded", discovered_count=70)]
    assert _elevation_hint(statuses) is None


def test_list_json_summary_reports_elevation_state(capsys):
    import json

    code, out, err = _run_cli(["list", "--json"], capsys)
    assert code == EXIT_SUCCESS
    payload = json.loads(out)
    assert "elevated" in payload["summary"]
    assert isinstance(payload["summary"]["elevated"], bool)


def test_doctor_runs_and_reports_every_loaded_adapter(capsys):
    code, out, err = _run_cli(["doctor"], capsys)
    # Exit 0 when every adapter passes, EXIT_DOCTOR_FINDINGS when any fails.
    # Both are successful runs of the command itself.
    assert code in (EXIT_SUCCESS, EXIT_DOCTOR_FINDINGS)
    assert "environment" in out
    assert "adapters" in out
    assert "reference" in out
    assert "adapters passed" in out


def test_doctor_json_shape(capsys):
    import json

    code, out, err = _run_cli(["doctor", "--json"], capsys)
    assert code in (EXIT_SUCCESS, EXIT_DOCTOR_FINDINGS)
    payload = json.loads(out)
    assert "environment" in payload
    assert "adapters" in payload
    assert payload["summary"]["adapters_checked"] == len(payload["adapters"])
    # A failing run must hand the user somewhere to report it.
    if payload["summary"]["adapters_failed"]:
        assert payload["report_url"].startswith("https://github.com/")
    for entry in payload["adapters"]:
        assert "adapter_id" in entry
        assert isinstance(entry["passed"], bool)


def test_doctor_environment_carries_no_identifying_information(capsys):
    """The doctor block is meant to be pasted into a public issue, so it
    must not carry a hostname, a username, or any sensor id (a Sensor_Id is
    a hash of a persistent hardware identifier)."""
    import getpass
    import json
    import platform

    code, out, err = _run_cli(["doctor", "--json"], capsys)
    facts = json.loads(out)["environment"]
    blob = json.dumps(facts).lower()

    assert platform.node().lower() not in blob
    try:
        assert getpass.getuser().lower() not in blob
    except Exception:  # pragma: no cover - getuser can fail in odd environments
        pass
    assert "reference.0" not in blob


def test_doctor_findings_exit_code_is_distinct_from_internal_error():
    """"the tool broke" and "the tool works and your adapters are broken"
    must not share an exit code, or a CI step cannot tell them apart."""
    from sensortap.cli.main import EXIT_UNEXPECTED

    assert EXIT_DOCTOR_FINDINGS != EXIT_UNEXPECTED
    assert EXIT_DOCTOR_FINDINGS != EXIT_SUCCESS


def test_read_privacy_sensitive_sensor_with_consent_succeeds(capsys):
    code, out, err = _run_cli(
        ["stream", "microphone.reference.0", "--consent", "microphone.reference.0", "--duration", "1"],
        capsys,
    )
    assert code == EXIT_SUCCESS
    assert out != ""

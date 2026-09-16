"""CLI argument parsing, command dispatch and exit codes (Req 12.1, 12.9).

Five subcommands -- ``list``, ``read``, ``stream``, ``inspect``, ``doctor``
-- built on stdlib ``argparse``. No command or an unrecognised one prints
usage naming them (Req 12.1).

``doctor`` exists because of a pattern this project kept hitting: every
defect found so far was structurally present on every machine but only
*observable* on hardware carrying the relevant sensor. An unreachable
camera, a light adapter that ignored the sensor id it was handed, and a
rate ceiling that silently dropped every microphone all passed review and
CI on the author's laptop. ``doctor`` ships the Conformance_Check to the
user and runs it against the adapters actually loaded on their machine, so
hardware the author will never own gets tested anyway, and hands them a
prefilled issue link when something fails.

Exit codes (design.md, "Exit codes" -- part of the CLI's contract):

| Code | Failure class                                   | Exceptions                                            |
| ---- | ------------------------------------------------ | ------------------------------------------------------|
| 0    | Success                                          | --                                                     |
| 2    | Unknown or malformed command or option           | argparse failure, InvalidTimeoutError,                |
|      |                                                   | MalformedSensorIdError                                |
| 3    | Unknown Sensor_Id                                | UnknownSensorError                                    |
| 4    | Sensor unavailable or device open failure        | SensorUnavailableError, DeviceOpenError, StreamBusyError |
| 5    | Missing consent                                  | ConsentError, InvalidConsentRequestError              |
| 6    | Read timeout                                     | ReadTimeoutError                                      |
| 7    | `doctor` ran fine and found a failing adapter    | -- (a findings code, not an error)                     |
| 1    | Anything else                                    | unexpected internal error                             |

Code 7 is deliberately distinct from 1: "the tool broke" and "the tool
works and is telling you your adapters are broken" are different outcomes,
and a CI step needs to tell them apart.

Output discipline (Req 12.9): everything destined for stdout is buffered in
memory and flushed only once the whole command has succeeded, so a command
that fails partway through (e.g. `list` enumerates fine but a later step
fails) never leaks a partial table to a pipe. On failure, only the cause is
printed, to stderr; nothing goes to stdout.

This module implements the dispatch/exit-code skeleton only. Command
bodies here are minimal/plain -- the table formatter (task 10.2) and the
JSON formatter (task 10.3) replace/extend their output.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from typing import Sequence

from sensortap.registry.errors import (
    ConsentError,
    DeviceOpenError,
    InvalidConsentRequestError,
    InvalidTimeoutError,
    MalformedSensorIdError,
    ReadTimeoutError,
    SensorUnavailableError,
    StreamBusyError,
    UnknownSensorError,
)
from sensortap.registry.routing import Registry
from sensortap.cli.format_table import (
    Style,
    format_reading,
    format_sensor_info_detail,
    format_sensor_table,
)
from sensortap.cli.format_json import (
    format_list_result_json,
    format_reading_json,
    format_sensor_info_json,
)

_COMMANDS = ("list", "read", "stream", "inspect", "doctor")

#: Where `doctor` points a user when it finds a failing adapter.
_ISSUES_URL = "https://github.com/BaselAshraf81/sensortap/issues/new"

EXIT_SUCCESS = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_UNKNOWN_SENSOR = 3
EXIT_UNAVAILABLE = 4
EXIT_CONSENT = 5
EXIT_TIMEOUT = 6
#: `doctor` ran successfully but found at least one adapter failing its
#: conformance check. Its own code rather than an overload of
#: EXIT_UNEXPECTED (1): "the tool broke" and "the tool works and is
#: telling you your adapters are broken" are different outcomes, and a CI
#: step needs to distinguish them.
EXIT_DOCTOR_FINDINGS = 7


class _UsageError(Exception):
    """Raised internally to signal a usage problem this module wants to
    report itself (rather than letting argparse call ``sys.exit``), so
    "no command" and "unrecognised command" both funnel through one
    exit path that prints usage naming all four commands.
    """


def _add_global_options(parser: argparse.ArgumentParser) -> None:
    """Add the global options to ``parser``.

    ``default=argparse.SUPPRESS`` is important here: these options are
    added both to the top-level parser and to every subparser (so
    ``sensortap --json list`` and ``sensortap list --json`` both work),
    and argparse merges the subparser's namespace over the top-level
    one. Without SUPPRESS, the subparser's own (unset) default would
    silently overwrite a value already set at the top level.
    """
    parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable JSON output"
    )
    parser.add_argument(
        "--consent",
        action="append",
        default=argparse.SUPPRESS,
        metavar="SENSOR_ID",
        help="grant consent for a privacy-sensitive sensor id (repeatable)",
    )
    parser.add_argument(
        "--include-elevated",
        action="store_true",
        default=argparse.SUPPRESS,
        help="include sensors requiring elevation",
    )
    parser.add_argument(
        "--include-motherboard",
        action="store_true",
        default=argparse.SUPPRESS,
        help=(
            "opt in to motherboard, Super-IO and embedded-controller sensors "
            "(board temps, fan tachometers, extra voltage rails). Off by "
            "default: this path can conflict with a vendor tool or another "
            "monitoring app holding the same EC registers"
        ),
    )
    parser.add_argument(
        "--discovery-timeout",
        type=int,
        default=argparse.SUPPRESS,
        metavar="MS",
        help="discovery timeout in milliseconds",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=argparse.SUPPRESS,
        metavar="SECONDS",
        help="stream duration in seconds",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sensortap",
        description="Cross-platform sensor discovery and raw-readings CLI.",
    )
    # Set the real defaults once, on the top-level namespace, before any
    # subparser (whose options are SUPPRESSed) gets a chance to merge in.
    parser.set_defaults(
        json=False,
        consent=None,
        include_elevated=False,
        include_motherboard=False,
        discovery_timeout=None,
        duration=None,
    )
    _add_global_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=False)

    list_parser = subparsers.add_parser("list", help="list discovered sensors")
    _add_global_options(list_parser)

    read_parser = subparsers.add_parser("read", help="read one sensor")
    read_parser.add_argument("sensor_id")
    _add_global_options(read_parser)

    stream_parser = subparsers.add_parser("stream", help="stream one sensor")
    stream_parser.add_argument("sensor_id")
    _add_global_options(stream_parser)

    inspect_parser = subparsers.add_parser(
        "inspect", help="print the full SensorInfo record for one sensor"
    )
    inspect_parser.add_argument("sensor_id")
    _add_global_options(inspect_parser)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help=(
            "run the conformance check against every adapter loaded on this "
            "machine and report anything broken"
        ),
    )
    _add_global_options(doctor_parser)

    return parser


def _usage_text(parser: argparse.ArgumentParser) -> str:
    """Usage text naming all four commands, used for both the "no
    command" and the "unrecognised command" cases.
    """
    return (
        parser.format_usage()
        + f"\ncommands: {', '.join(_COMMANDS)}\n"
    )


def _build_list_summary(sensors, backend_status) -> dict:
    """Total sensor count, distinct `kind` count, contributing
    (``state == "loaded"``) vs non-contributing backend counts (Req 12.7),
    and whether the current process is elevated -- surfaced structurally
    here (not only as the plain-text hint) so a scripted `--json` caller
    can also tell why a rerun with admin rights might report a different
    count, without scraping stderr or a human-facing note string.
    """
    distinct_kinds = {s.kind for s in sensors}
    contributing = sum(1 for b in backend_status if b.state == "loaded")
    non_contributing = len(backend_status) - contributing

    from sensortap.registry.privilege import is_elevated

    return {
        "total_sensors": len(sensors),
        "distinct_kinds": len(distinct_kinds),
        "contributing_backends": contributing,
        "non_contributing_backends": non_contributing,
        "elevated": is_elevated(),
    }


def _format_backend_status_line(status) -> str:
    reason = getattr(status, "reason", None)
    parts = [f"{status.adapter_id}: {status.state}", f"discovered={status.discovered_count}"]
    if reason is not None:
        parts.append(f"reason={reason.value}")
    return "  ".join(parts)


#: Adapters whose live sensor count is known to differ under Windows
#: process elevation (Req 8.6/8.7's elevation detection exists precisely
#: for this: `is_elevated()` was implemented but never called from the
#: CLI, so a user running unelevated had no way to learn that CPU MSR
#: temperature/clock reads and storage SMART reads both silently degrade
#: to fewer sensors -- not an error, not a listed reason, just a smaller
#: number with no explanation. Confirmed live: 50 hwmon sensors
#: unelevated vs. 70 elevated on the same machine, same run). Kept as a
#: named set rather than a blanket "not elevated" banner, since most
#: adapters (WinRT motion, battery, radio, touchpad) are unaffected by
#: elevation and a banner shown regardless of relevance would train users
#: to ignore it.
_ELEVATION_SENSITIVE_ADAPTER_IDS = frozenset({"hwmon_bridge"})


def _elevation_hint(backend_status) -> str | None:
    """One line noting that running elevated may surface more sensors,
    shown only when it is actually true for this run: an
    elevation-sensitive adapter loaded, and the current process is not
    elevated. Never triggers a UAC prompt itself (Req 8.6) -- this is
    information, not an action.
    """

    if not any(b.adapter_id in _ELEVATION_SENSITIVE_ADAPTER_IDS for b in backend_status):
        return None

    from sensortap.registry.privilege import is_elevated

    if is_elevated():
        return None

    return (
        "note: not running elevated. CPU temperature/clock reads and storage "
        "health reads can silently return fewer sensors without admin rights, "
        "with no error and no listed reason. Re-run from an elevated terminal "
        "to check whether more sensors appear."
    )


def _run_list(registry: Registry, args: argparse.Namespace, out: io.StringIO) -> None:
    sensors = registry.list_sensors()
    backend_status = registry.backend_status()
    summary = _build_list_summary(sensors, backend_status)
    if args.json:
        out.write(format_list_result_json(sensors, backend_status, summary) + "\n")
        return
    out.write(format_sensor_table(sensors, style=Style()))
    out.write("\n")
    for status in backend_status:
        out.write(_format_backend_status_line(status) + "\n")
    out.write(
        "\n"
        f"total sensors: {summary['total_sensors']}  "
        f"kinds: {summary['distinct_kinds']}  "
        f"backends contributing: {summary['contributing_backends']}  "
        f"non-contributing: {summary['non_contributing_backends']}\n"
    )
    hint = _elevation_hint(backend_status)
    if hint is not None:
        out.write(f"\n{hint}\n")


def _apply_consent(registry: Registry, args: argparse.Namespace) -> None:
    if args.consent:
        registry.consent(args.consent)


#: Outer bound on `read` (Req 12.3): the registry's own per-adapter
#: read bounds (e.g. the Windows adapters' ~2000 ms bound, task 6.1) make
#: this redundant for adapters shipped in this repo, but nothing in the
#: `Adapter` Protocol requires a third-party adapter's `read()` to bound
#: itself at all. This wrapper is the CLI's own belt-and-braces
#: enforcement of "exits within 10 s of invocation" regardless of what
#: adapter is backing the sensor, converting an overrun into
#: `ReadTimeoutError` so it maps to the existing exit code 6.
_READ_BOUND_S = 10.0


def _bounded_read(registry: Registry, sensor_id: str):
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(registry.read, sensor_id)
        try:
            return future.result(timeout=_READ_BOUND_S)
        except concurrent.futures.TimeoutError:
            raise ReadTimeoutError(sensor_id=sensor_id, timeout_ms=int(_READ_BOUND_S * 1000)) from None


def _run_read(registry: Registry, args: argparse.Namespace, out: io.StringIO) -> None:
    _apply_consent(registry, args)
    sensors = registry.list_sensors(id=args.sensor_id)
    reading = _bounded_read(registry, args.sensor_id)
    if args.json:
        out.write(format_reading_json(reading) + "\n")
        return
    info = sensors[0] if sensors else None
    if info is None:
        out.write(f"{reading.id}\t{reading.status}\t{list(reading.values)}\n")
    else:
        out.write(format_reading(reading, info, style=Style()))


_MIN_STREAM_DURATION_S = 1
_MAX_STREAM_DURATION_S = 86400


def _validate_stream_duration(duration: float | None) -> None:
    """Bound `--duration` to 1..86400 seconds (Req 12.4) as a usage
    error (exit code 2), checked before any streaming starts.
    """
    if duration is None:
        return
    if not (_MIN_STREAM_DURATION_S <= duration <= _MAX_STREAM_DURATION_S):
        raise _UsageError(
            f"--duration must be between {_MIN_STREAM_DURATION_S} and "
            f"{_MAX_STREAM_DURATION_S} seconds, got {duration}"
        )


def _run_stream(registry: Registry, args: argparse.Namespace, out: io.StringIO) -> None:
    _validate_stream_duration(args.duration)
    _apply_consent(registry, args)
    sensors = registry.list_sensors(id=args.sensor_id)
    info = sensors[0] if sensors else None
    stream = registry.stream(args.sensor_id)
    try:
        import time

        deadline = None
        if args.duration is not None:
            deadline = time.monotonic() + args.duration
        for reading in stream:
            if args.json:
                sys.stdout.write(format_reading_json(reading) + "\n")
            elif info is not None:
                sys.stdout.write(format_reading(reading, info, style=Style()))
            else:
                sys.stdout.write(f"{reading.id}\t{reading.status}\t{list(reading.values)}\n")
            sys.stdout.flush()
            if deadline is not None and time.monotonic() >= deadline:
                break
    finally:
        stream.close()


def _run_inspect(registry: Registry, args: argparse.Namespace, out: io.StringIO) -> None:
    sensors = registry.list_sensors(id=args.sensor_id)
    if not sensors:
        raise UnknownSensorError(sensor_id=args.sensor_id, last_known_availability=None)
    info = sensors[0]
    if args.json:
        out.write(format_sensor_info_json(info) + "\n")
        return
    out.write(format_sensor_info_detail(info))


def _environment_facts() -> dict:
    """Machine facts worth having in a bug report, and nothing more.

    Deliberately excludes anything identifying: no hostname, no username,
    no device serial numbers, no sensor ids (a Sensor_Id is a hash of a
    persistent hardware identifier, and this output is meant to be pasted
    into a public issue).
    """

    import platform

    from sensortap.registry.privilege import is_elevated
    from sensortap.schema.version import SCHEMA_VERSION
    from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION

    try:
        from importlib.metadata import version

        sensortap_version = version("sensortap")
    except Exception:  # noqa: BLE001 - running from a source tree without metadata
        sensortap_version = "unknown"

    return {
        "sensortap": sensortap_version,
        "schema": SCHEMA_VERSION,
        "adapter_interface": ADAPTER_INTERFACE_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "elevated": is_elevated(),
    }


def _issue_url(failures: list[dict], facts: dict) -> str:
    """Build a prefilled GitHub issue URL for the failures `doctor` found.

    The whole point of this command is that most sensortap bugs are
    invisible on the maintainer's own hardware -- every defect found so
    far was structurally present on every machine but only *observable* on
    hardware with the relevant sensor. Shipping the check to users turns
    each install into a test on hardware the author will never own, and
    this URL removes the friction between "my machine reports a problem"
    and "the maintainer knows about it".
    """

    import urllib.parse

    adapters = ", ".join(sorted({f["adapter_id"] for f in failures}))
    title = f"doctor: conformance failure in {adapters}"

    lines = ["Reported by `sensortap doctor`.", "", "## Environment", ""]
    for key, value in facts.items():
        lines.append(f"- {key}: `{value}`")
    lines += ["", "## Failing checks", ""]
    for failure in failures:
        lines.append(f"### `{failure['adapter_id']}`")
        for check in failure["failed_checks"]:
            lines.append(f"- **{check['name']}**: {check['detail']}")
        lines.append("")
    lines += [
        "## Hardware",
        "",
        "<!-- Please add: laptop/desktop model, and which sensors you expected"
        " to see. That context is what the maintainer cannot get from the"
        " output above. -->",
    ]

    body = "\n".join(lines)
    query = urllib.parse.urlencode({"title": title, "body": body})
    return f"{_ISSUES_URL}?{query}"


def _run_doctor(
    registry: Registry, args: argparse.Namespace, out: io.StringIO
) -> int | None:
    """Run the shipped Conformance_Check against every adapter loaded on
    this machine.

    This is the honest answer to "how do we know an adapter works on
    hardware we do not have": we do not, so the check ships to the user
    and runs against their real devices. Every defect found in this
    project so far -- an unreachable camera, a light adapter that ignored
    the sensor id it was handed, a rate ceiling that silently dropped
    every microphone -- was present on all machines but only *observable*
    on hardware carrying the relevant sensor.

    Reaches into `registry._loaded_adapters` deliberately: `doctor` needs
    the live adapter instances the registry actually loaded (with their
    real `setup()` already run and helper processes already started), and
    the documented public surface is intentionally five methods plus
    `shutdown()`. This is intra-package access from the CLI that ships
    alongside the registry, not an addition to the public API.
    """

    from sensortap.adapters.conformance import run_conformance_check

    facts = _environment_facts()
    results: list[dict] = []

    for loaded in registry._loaded_adapters:
        adapter = loaded.instance
        adapter_id = adapter.meta.adapter_id
        try:
            report = run_conformance_check(adapter)
            checks = [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in report.checks
            ]
            results.append(
                {"adapter_id": adapter_id, "passed": report.passed, "checks": checks}
            )
        except Exception as exc:  # noqa: BLE001 - one bad adapter never stops the sweep
            results.append(
                {
                    "adapter_id": adapter_id,
                    "passed": False,
                    "checks": [
                        {
                            "name": "conformance check ran",
                            "passed": False,
                            "detail": f"the check itself raised: {exc!r}",
                        }
                    ],
                }
            )

    failures = [
        {
            "adapter_id": r["adapter_id"],
            "failed_checks": [c for c in r["checks"] if not c["passed"]],
        }
        for r in results
        if not r["passed"]
    ]

    if args.json:
        payload = {
            "environment": facts,
            "adapters": results,
            "summary": {
                "adapters_checked": len(results),
                "adapters_passed": sum(1 for r in results if r["passed"]),
                "adapters_failed": len(failures),
            },
        }
        if failures:
            payload["report_url"] = _issue_url(failures, facts)
        out.write(json.dumps(payload) + "\n")
        return EXIT_DOCTOR_FINDINGS if failures else None

    out.write("environment\n")
    for key, value in facts.items():
        out.write(f"  {key}: {value}\n")
    out.write("\nadapters\n")
    for result in results:
        mark = "ok  " if result["passed"] else "FAIL"
        out.write(f"  [{mark}] {result['adapter_id']}\n")
        for check in result["checks"]:
            if not check["passed"]:
                out.write(f"           {check['name']}: {check['detail']}\n")

    passed = sum(1 for r in results if r["passed"])
    out.write(
        f"\n{passed}/{len(results)} adapters passed"
        f"  failed: {len(failures)}\n"
    )

    if failures:
        out.write(
            "\nThese are bugs worth reporting. Most sensortap defects are "
            "invisible on the author's own hardware, so a report from a "
            "machine with different sensors is the only way they get found.\n"
            "\nOpen a prefilled issue:\n"
            f"{_issue_url(failures, facts)}\n"
        )
        return EXIT_DOCTOR_FINDINGS

    hint = _elevation_hint(registry.backend_status())
    if hint is not None:
        out.write(f"\n{hint}\n")
    return None


_DISPATCH = {
    "list": _run_list,
    "read": _run_read,
    "stream": _run_stream,
    "inspect": _run_inspect,
    "doctor": _run_doctor,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)

    # argparse's own error path (unknown option, missing positional,
    # etc.) already exits with code 2 by convention; we let it, but we
    # must still print stdout nothing -- argparse writes usage/errors to
    # stderr on its own, and never touches stdout, so this is safe.
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return code if code != 0 else EXIT_SUCCESS

    if not args.command or args.command not in _COMMANDS:
        sys.stderr.write(_usage_text(parser))
        return EXIT_USAGE

    out = io.StringIO()
    try:
        # Must be set before Registry() constructs adapters: the
        # hardware-monitor adapter reads this in its __init__ to decide
        # whether to launch its helper with motherboard/EC monitoring on
        # (see adapters/windows/hwmon_bridge.MOTHERBOARD_OPTIN_ENV_VAR for
        # why the opt-in travels by environment variable rather than through
        # a registry config channel that does not exist).
        if args.include_motherboard:
            # Imported lazily: `sensortap.adapters.windows` is platform-gated
            # and does not import on Linux, but this flag must still parse and
            # run there (where it is simply a no-op, since no adapter reads
            # it) rather than crashing the whole CLI.
            try:
                from sensortap.adapters.windows.hwmon_bridge import (
                    MOTHERBOARD_OPTIN_ENV_VAR,
                )

                os.environ[MOTHERBOARD_OPTIN_ENV_VAR] = "1"
            except ImportError:
                pass

        registry_kwargs: dict = {"include_elevated": args.include_elevated}
        if args.discovery_timeout is not None:
            registry_kwargs["discovery_timeout_ms"] = args.discovery_timeout
        registry = Registry(**registry_kwargs)
        handler = _DISPATCH[args.command]
        # A handler returns None for the ordinary success path, or an
        # explicit exit code when the command's own outcome carries one.
        # Only `doctor` uses this today: it is a lint-style command whose
        # findings must be able to fail a CI step, while still having
        # genuinely succeeded at running.
        handler_exit_code = handler(registry, args, out)
    except (InvalidTimeoutError, MalformedSensorIdError, _UsageError) as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_USAGE
    except UnknownSensorError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_UNKNOWN_SENSOR
    except (SensorUnavailableError, DeviceOpenError, StreamBusyError) as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_UNAVAILABLE
    except (ConsentError, InvalidConsentRequestError) as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_CONSENT
    except ReadTimeoutError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_TIMEOUT
    except Exception as exc:  # noqa: BLE001 - catch-all, still exit non-zero
        sys.stderr.write(f"{exc}\n")
        return EXIT_UNEXPECTED

    # Success: flush the buffered stdout now, and only now.
    sys.stdout.write(out.getvalue())
    sys.stdout.flush()
    return EXIT_SUCCESS if handler_exit_code is None else handler_exit_code


if __name__ == "__main__":
    sys.exit(main())

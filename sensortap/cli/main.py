"""CLI argument parsing, command dispatch and exit codes (Req 12.1, 12.9).

Four subcommands -- ``list``, ``read``, ``stream``, ``inspect`` -- built on
stdlib ``argparse``. No command or an unrecognised one prints usage naming
all four (Req 12.1).

Exit codes (design.md, "Exit codes" -- frozen, part of the CLI's contract):

| Code | Failure class                                   | Exceptions                                            |
| ---- | ------------------------------------------------ | ------------------------------------------------------|
| 0    | Success                                          | --                                                     |
| 2    | Unknown or malformed command or option           | argparse failure, InvalidTimeoutError,                |
|      |                                                   | MalformedSensorIdError                                |
| 3    | Unknown Sensor_Id                                | UnknownSensorError                                    |
| 4    | Sensor unavailable or device open failure        | SensorUnavailableError, DeviceOpenError, StreamBusyError |
| 5    | Missing consent                                  | ConsentError, InvalidConsentRequestError              |
| 6    | Read timeout                                     | ReadTimeoutError                                      |
| 1    | Anything else                                    | unexpected internal error                             |

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

_COMMANDS = ("list", "read", "stream", "inspect")

EXIT_SUCCESS = 0
EXIT_UNEXPECTED = 1
EXIT_USAGE = 2
EXIT_UNKNOWN_SENSOR = 3
EXIT_UNAVAILABLE = 4
EXIT_CONSENT = 5
EXIT_TIMEOUT = 6


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


_DISPATCH = {
    "list": _run_list,
    "read": _run_read,
    "stream": _run_stream,
    "inspect": _run_inspect,
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
        handler(registry, args, out)
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
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())

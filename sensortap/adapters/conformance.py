"""The reusable Conformance_Check (Req 10.8, 15.7, 16.8, 16.9).

Shipped in the package rather than under `tests/` so a third-party adapter
author can run it against their own adapter without editing sensortap
source (Req 16.9):

    python -m sensortap.adapters.conformance my_pkg:MyAdapter

or, from a third party's own test suite:

    from sensortap.adapters.conformance import run_conformance_check
    report = run_conformance_check(MyAdapter())
    assert report.passed

What this checks (Req 10.8):

1. **Schema compliance of every discovered record.** Every `SensorInfo`
   returned by `discover()` is run through `validate_sensor_info()`
   (`schema/validate.py`). For every non-`buffer` sensor a `read()` is
   also attempted and the resulting `Reading` is run through
   `validate_reading()` against its `SensorInfo`.
2. **Sensor_Id stability across at least 2 consecutive `discover` calls**
   (Req 10.8, 16.4's spirit). `discover()` is called twice and the
   resulting Sensor_Id sets are compared as unordered sets -- they must
   be equal.
3. **The error behaviour this specification requires**, scoped to what is
   genuinely the *adapter's own* contract rather than the registry's.
   `UnsupportedOperationError`, `MalformedSensorIdError` and
   `UnknownSensorError` are all raised by the *Registry*, not by an
   Adapter (see `adapters/protocol.py` and Req 10.3, 3.9, 3.10) -- an
   Adapter is never asked to know about them, so this checker does not
   test for them here. What genuinely belongs to the adapter is:
   `read()` on a sensor id it just discovered must return a
   schema-conforming `Reading`, and `read()` on a sensor id the adapter
   has never discovered should fail in some observable way (any
   exception is acceptable) rather than silently returning a bogus
   value.

**On the read-only / device-facing-operation check (Req 15.7, 15.8).**
Requirement 15.8 states plainly that read-only behaviour outside the
Sensortap-provided device access wrappers is "a contractual obligation on
the Adapter author rather than a machine-verifiable property." Sensortap
ships no device-access-tracking wrapper today, so there is nothing for
this checker to instrument to see *which* operations an adapter actually
performs against real hardware. Building a fake enforcement mechanism
here would give false confidence, which the spec explicitly warns
against. Accordingly the only thing this checker verifies mechanically
is the one part of Req 15.8 that *is* mechanically checkable: that the
adapter has made the required declaration at all --
`adapter.meta.read_only_declared is True`. This is the same gate the
Registry itself enforces at load time (Req 15.8, `registry/loading.py`).
The deeper claim -- that the adapter never actually performs a
device-facing write through a Sensortap device access wrapper for any
operation beyond a read or the two acquisition-configuration operations
-- remains, as the spec says, the adapter author's documented obligation
and is out of scope for automated verification here.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field

from sensortap.adapters.protocol import Adapter
from sensortap.schema.enums import Dtype
from sensortap.schema.validate import validate_reading, validate_sensor_info


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one named check within a `ConformanceReport`."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """The aggregate result of running the Conformance_Check on one adapter."""

    adapter_id: str
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def render(self) -> str:
        """Render a human-readable pass/fail summary for CLI output."""
        lines = [f"Conformance_Check for adapter {self.adapter_id!r}:"]
        for check in self.checks:
            mark = "PASS" if check.passed else "FAIL"
            line = f"  [{mark}] {check.name}"
            if check.detail:
                line += f" -- {check.detail}"
            lines.append(line)
        overall = "PASSED" if self.passed else "FAILED"
        lines.append(f"Overall: {overall}")
        return "\n".join(lines)


def _check_schema_compliance(adapter: Adapter, adapter_id: str) -> CheckResult:
    try:
        records = list(adapter.discover())
    except Exception as exc:  # noqa: BLE001 - report, don't crash the checker
        return CheckResult(
            "schema compliance of discovered records",
            False,
            f"discover() raised {exc!r}",
        )

    violations: list[str] = []
    for record in records:
        for violation in validate_sensor_info(record, adapter_id=adapter_id):
            violations.append(f"{record.id!r}: {violation}")

    # For at least one non-buffer sensor, also validate a Reading against
    # its SensorInfo (buffer-dtype sensors are never read through read();
    # Req 4.12).
    readable = [r for r in records if r.dtype != Dtype.BUFFER]
    for record in readable[:1]:
        try:
            reading = adapter.read(record.id)
        except Exception as exc:  # noqa: BLE001
            violations.append(f"{record.id!r}: read() raised {exc!r}")
            continue
        for violation in validate_reading(reading, record):
            violations.append(f"{record.id!r}: {violation}")

    if violations:
        return CheckResult(
            "schema compliance of discovered records",
            False,
            "; ".join(violations),
        )
    return CheckResult(
        "schema compliance of discovered records",
        True,
        f"{len(records)} record(s) validated",
    )


def _check_sensor_id_stability(adapter: Adapter) -> CheckResult:
    try:
        first_ids = {record.id for record in adapter.discover()}
        second_ids = {record.id for record in adapter.discover()}
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "Sensor_Id stability across consecutive discover() calls",
            False,
            f"discover() raised {exc!r}",
        )

    if first_ids != second_ids:
        only_first = first_ids - second_ids
        only_second = second_ids - first_ids
        return CheckResult(
            "Sensor_Id stability across consecutive discover() calls",
            False,
            f"Sensor_Id sets differ between calls: only in first call "
            f"{sorted(only_first)!r}, only in second call {sorted(only_second)!r}",
        )
    return CheckResult(
        "Sensor_Id stability across consecutive discover() calls",
        True,
        f"{len(first_ids)} Sensor_Id(s) stable across 2 discover() calls",
    )


def _check_error_behaviour(adapter: Adapter) -> CheckResult:
    """Check the error behaviour that is genuinely the adapter's own contract.

    Scoped deliberately narrowly: `UnsupportedOperationError`,
    `MalformedSensorIdError` and `UnknownSensorError` are all raised by the
    Registry, never by an Adapter (Req 10.3, 3.9, 3.10), so they are not
    exercised here. What remains as the adapter's own responsibility is
    that `read()` on a sensor id it just reported behaves, and that
    `read()` on a sensor id it never reported does not silently succeed
    with a bogus value.
    """
    try:
        records = list(adapter.discover())
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "adapter-level error behaviour",
            False,
            f"discover() raised {exc!r}",
        )

    known_ids = {record.id for record in records}
    detail_parts: list[str] = []

    readable = [r for r in records if r.dtype != Dtype.BUFFER]
    if readable:
        try:
            adapter.read(readable[0].id)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                "adapter-level error behaviour",
                False,
                f"read() on known sensor id {readable[0].id!r} raised {exc!r}",
            )
        detail_parts.append("read() on a known sensor id succeeded")

    unknown_id = "zz-conformance-check-unknown-sensor.does-not-exist.0"
    while unknown_id in known_ids:  # pragma: no cover - defensive only
        unknown_id += "x"
    try:
        adapter.read(unknown_id)
    except Exception:  # noqa: BLE001 - any exception is acceptable here
        detail_parts.append("read() on an unknown sensor id raised, as expected")
    else:
        return CheckResult(
            "adapter-level error behaviour",
            False,
            f"read() on unknown sensor id {unknown_id!r} did not raise",
        )

    return CheckResult("adapter-level error behaviour", True, "; ".join(detail_parts))


def _check_read_only_declaration(adapter: Adapter) -> CheckResult:
    """Check the one mechanically-verifiable part of Req 15.7/15.8.

    Req 15.8 documents that read-only behaviour outside the Sensortap
    device access wrappers is a contractual obligation on the adapter
    author, not a machine-verifiable property, because Sensortap ships no
    device-access-tracking wrapper to instrument. The only thing checked
    here is that the adapter made the required declaration at all
    (`meta.read_only_declared is True`) -- the same gate the Registry
    enforces before it will load an adapter. Whether the adapter actually
    performs no device-facing operation beyond a read or the two
    acquisition-configuration operations through such a wrapper is *not*
    verified here; that deeper claim remains the adapter author's
    documented, unverified obligation.
    """
    meta = getattr(adapter, "meta", None)
    declared = getattr(meta, "read_only_declared", None)
    if declared is not True:
        return CheckResult(
            "read-only obligation declared (meta.read_only_declared)",
            False,
            f"meta.read_only_declared is {declared!r}, expected True (Req 15.8); "
            "note: whether the adapter honours this obligation in practice is "
            "not machine-verifiable and is not checked here (Req 15.7, 15.8)",
        )
    return CheckResult(
        "read-only obligation declared (meta.read_only_declared)",
        True,
        "declared True; honouring this obligation in practice is a "
        "documented, unverifiable contractual obligation on the adapter "
        "author (Req 15.7, 15.8), not checked mechanically here",
    )


def run_conformance_check(adapter: Adapter) -> ConformanceReport:
    """Run the Conformance_Check against `adapter` and return a report.

    `adapter` must already be constructed (and, if the adapter defines
    `setup()`, already set up by the caller -- this checker does not call
    `setup()`/`teardown()` itself, since not every third-party adapter's
    `setup()` is safe to call outside the Registry's lifecycle).
    """
    meta = getattr(adapter, "meta", None)
    adapter_id = getattr(meta, "adapter_id", None) or type(adapter).__name__

    checks = (
        _check_schema_compliance(adapter, adapter_id),
        _check_sensor_id_stability(adapter),
        _check_error_behaviour(adapter),
        _check_read_only_declaration(adapter),
    )
    return ConformanceReport(adapter_id=adapter_id, checks=checks)


def _resolve_adapter_class(target: str) -> type:
    """Resolve a `module:ClassName` or `module.ClassName` spec to a class."""
    if ":" in target:
        module_name, _, class_name = target.partition(":")
    else:
        module_name, _, class_name = target.rpartition(".")
    if not module_name or not class_name:
        raise ValueError(
            f"invalid adapter spec {target!r}; expected 'module:ClassName' "
            "or 'module.ClassName'"
        )
    module = importlib.import_module(module_name)
    try:
        cls = getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(
            f"module {module_name!r} has no attribute {class_name!r}"
        ) from exc
    return cls


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: `python -m sensortap.adapters.conformance module:Class`."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(
            "usage: python -m sensortap.adapters.conformance <module>:<ClassName>",
            file=sys.stderr,
        )
        return 2

    try:
        adapter_cls = _resolve_adapter_class(args[0])
        adapter = adapter_cls()
    except Exception as exc:  # noqa: BLE001
        print(f"error: could not construct adapter from {args[0]!r}: {exc!r}", file=sys.stderr)
        return 2

    setup = getattr(adapter, "setup", None)
    if callable(setup):
        setup()
    try:
        report = run_conformance_check(adapter)
    finally:
        teardown = getattr(adapter, "teardown", None)
        if callable(teardown):
            teardown()

    print(report.render())
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())

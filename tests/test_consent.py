"""Focused tests for the Consent_Gate and privilege detection.

Requirements: 7.4, 7.5, 7.6, 7.7, 7.10, 8.6, 8.7, 8.9
"""

from __future__ import annotations

import pytest

from sensortap.registry.consent import AuditEvent, ConsentGate
from sensortap.registry.errors import InvalidConsentRequestError
from sensortap.registry.privilege import is_elevated


def test_gate_starts_with_zero_grants():
    gate = ConsentGate()
    assert not gate.is_granted("cam.mf.0")


@pytest.mark.parametrize(
    "sensor_ids",
    [
        [],
        ["*"],
        ["cam.mf.*"],
        ["cam.mf.0", ""],
        [""],
    ],
)
def test_grant_rejects_wildcard_pattern_and_empty(sensor_ids):
    gate = ConsentGate()
    with pytest.raises(InvalidConsentRequestError):
        gate.grant(sensor_ids)
    # No grant was created for any sensor id that might have been valid.
    for sid in sensor_ids:
        if sid:
            assert not gate.is_granted(sid)


def test_grant_lifecycle_context_manager():
    events: list[AuditEvent] = []
    gate = ConsentGate(audit_hook=events.append)

    with gate.grant(["cam.mf.0", "mic.alsa.0"]) as grant:
        assert grant.sensor_ids == frozenset({"cam.mf.0", "mic.alsa.0"})
        assert gate.is_granted("cam.mf.0")
        assert gate.is_granted("mic.alsa.0")
        assert not gate.is_granted("cam.mf.1")

    # Ended on context exit.
    assert not gate.is_granted("cam.mf.0")
    assert not gate.is_granted("mic.alsa.0")

    outcomes = [e.outcome for e in events]
    assert outcomes.count("granted") == 2
    assert outcomes.count("ended") == 2


def test_grant_revoke_closes_registered_devices_and_ends_grant():
    gate = ConsentGate()
    closed = []

    grant = gate.grant(["cam.mf.0"])
    grant.register_open_device("cam.mf.0", lambda: closed.append("cam.mf.0"))
    assert gate.is_granted("cam.mf.0")

    grant.revoke()

    assert closed == ["cam.mf.0"]
    assert not gate.is_granted("cam.mf.0")

    # Idempotent: a second revoke, or a subsequent context exit, does
    # nothing further.
    grant.revoke()
    assert closed == ["cam.mf.0"]


def test_two_gate_instances_are_independent():
    gate_a = ConsentGate()
    gate_b = ConsentGate()

    with gate_a.grant(["cam.mf.0"]):
        assert gate_a.is_granted("cam.mf.0")
        assert not gate_b.is_granted("cam.mf.0")


def test_is_elevated_never_raises():
    # Whatever the actual result on this machine, the call must not
    # raise, and must return a bool.
    result = is_elevated()
    assert isinstance(result, bool)

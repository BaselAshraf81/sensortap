"""The Consent_Gate: an in-memory, per-Registry-instance authorization
store for Privacy_Sensitive_Sensors.

Design summary (see design.md, "The Consent Gate" and "AuditEvent"):

- Storage is a ``dict[str, set[GrantId]]`` held on the owning
  :class:`ConsentGate` instance, in process memory only. Nothing is
  persisted to disk, to an environment variable, or to any other store.
  Every ``ConsentGate`` starts with zero grants (Req 7.6).
- ``grant()`` rejects wildcard, pattern and empty Sensor_Id sets with
  :class:`InvalidConsentRequestError`, creating no grant (Req 7.7).
- The returned :class:`ConsentGrant` is a context manager. Ending a grant
  -- via context exit, explicit ``revoke()``, or the ``atexit`` path --
  closes every device opened under it before returning, then removes the
  grant from the store (Req 7.5).
- Every grant creation, use, denial and end emits an
  ``AuditEvent(sensor_id, outcome, t_wall)`` to an audit hook. The event
  type carries no values field, so a reading cannot leak into the audit
  trail by accident (Req 7.10).

This module implements the gate and the audit event only. Wiring
``Registry.read``/``Registry.stream`` to check ``is_granted()`` before
dispatch is task 2.9's responsibility.

Requirements: 7.4, 7.5, 7.6, 7.7, 7.10, 8.6, 8.7, 8.9
"""

from __future__ import annotations

import atexit
import itertools
import time
import weakref
from dataclasses import dataclass, field
from typing import Callable, Sequence

from sensortap.registry.errors import InvalidConsentRequestError

# An opaque identifier for one grant. A monotonically increasing integer
# is sufficient: it is unique within a process and needs no external
# meaning.
GrantId = int

_WILDCARD_CHARS = frozenset("*?[]")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One audit record for a Consent_Gate lifecycle event.

    Deliberately carries no values/readings field -- a Reading can never
    leak into the audit trail through this type.

    Requirements: 7.10
    """

    sensor_id: str
    outcome: str  # "granted" | "used" | "denied" | "ended"
    t_wall: float


def _default_audit_hook(event: AuditEvent) -> None:
    """No-op default audit hook."""


def _looks_like_wildcard_or_pattern(sensor_id: object) -> bool:
    """Return True if ``sensor_id`` is not a plain, non-empty string, or
    looks like a wildcard/pattern rather than a literal Sensor_Id.
    """
    if not isinstance(sensor_id, str):
        return True
    if sensor_id == "":
        return True
    return any(ch in _WILDCARD_CHARS for ch in sensor_id)


class ConsentGrant:
    """A context manager representing one active consent grant over a
    fixed set of Sensor_Id values.

    Usable as::

        with gate.grant(["camera.mf.9b1c..."]) as g:
            ...
        # grant ended; every device opened under it is closed

    or ended explicitly via ``revoke()``.
    """

    def __init__(
        self,
        gate: "ConsentGate",
        grant_id: GrantId,
        sensor_ids: frozenset[str],
    ) -> None:
        self.sensor_ids = sensor_ids
        self._gate = gate
        self._grant_id = grant_id
        self._ended = False
        # sensor_id -> list of close callables for devices opened under
        # this grant. Devices are not implemented yet; this is the hook
        # the future Adapter integration will call via
        # register_open_device().
        self._open_devices: dict[str, list[Callable[[], None]]] = {
            sid: [] for sid in sensor_ids
        }

    def register_open_device(self, sensor_id: str, close: Callable[[], None]) -> None:
        """Register a close callback for a device opened under this grant
        for ``sensor_id``. The callback is invoked when the grant ends.

        Raises ``KeyError`` if ``sensor_id`` is not part of this grant.
        """
        if sensor_id not in self._open_devices:
            raise KeyError(sensor_id)
        self._open_devices[sensor_id].append(close)

    def revoke(self) -> None:
        """End this grant explicitly.

        Idempotent: calling ``revoke()`` more than once, or exiting the
        context after an explicit ``revoke()``, has no further effect.

        Requirements: 7.5
        """
        self._end()

    def __enter__(self) -> "ConsentGrant":
        return self

    def __exit__(self, *exc: object) -> None:
        self._end()

    def _end(self) -> None:
        if self._ended:
            return
        self._ended = True

        # Close every device opened under this grant before returning,
        # regardless of the order sensors were registered in, and even
        # if a close callback raises -- one failing device must not
        # leave the others open.
        for sensor_id, closers in self._open_devices.items():
            for close in closers:
                try:
                    close()
                except Exception:
                    pass

        self._gate._end_grant(self)

        for sensor_id in self.sensor_ids:
            self._gate._emit(AuditEvent(sensor_id, "ended", time.time()))


class ConsentGate:
    """In-memory store of active consent grants.

    One instance is owned per ``Registry`` instance (per design.md,
    ``Registry.consent()``); there is no global/shared singleton, so
    separate ``Registry`` instances never share grant state.

    Every instance starts with zero grants (Req 7.6).
    """

    def __init__(self, *, audit_hook: Callable[[AuditEvent], None] | None = None) -> None:
        self._grants: dict[str, set[GrantId]] = {}
        self._active_grants: dict[GrantId, ConsentGrant] = {}
        self._id_counter = itertools.count(1)
        self._audit_hook = audit_hook or _default_audit_hook

        # Ensure any grant still open at process exit is ended, closing
        # its devices, even if the caller never used a `with` block.
        self._atexit_registered = False
        self._register_atexit()

    def _register_atexit(self) -> None:
        if self._atexit_registered:
            return
        self._atexit_registered = True
        # Use a weakref so the ConsentGate (and therefore the Registry
        # it belongs to) is not kept alive solely by the atexit registry.
        gate_ref = weakref.ref(self)

        def _cleanup() -> None:
            gate = gate_ref()
            if gate is not None:
                gate._end_all_grants()

        atexit.register(_cleanup)

    def _emit(self, event: AuditEvent) -> None:
        try:
            self._audit_hook(event)
        except Exception:
            pass

    def grant(self, sensor_ids: Sequence[str]) -> ConsentGrant:
        """Create a new grant covering exactly the named Sensor_Id
        values.

        Rejects wildcard, pattern and empty Sensor_Id sets with
        :class:`InvalidConsentRequestError`, creating no grant.

        Requirements: 7.4, 7.6, 7.7
        """
        if not sensor_ids:
            raise InvalidConsentRequestError(
                reason="grant request must name at least one Sensor_Id"
            )

        for sid in sensor_ids:
            if _looks_like_wildcard_or_pattern(sid):
                raise InvalidConsentRequestError(
                    reason=(
                        f"grant request contains a wildcard, pattern, or "
                        f"non-literal Sensor_Id: {sid!r}"
                    )
                )

        frozen_ids = frozenset(sensor_ids)
        grant_id = next(self._id_counter)
        consent_grant = ConsentGrant(self, grant_id, frozen_ids)

        for sid in frozen_ids:
            self._grants.setdefault(sid, set()).add(grant_id)
        self._active_grants[grant_id] = consent_grant

        for sid in frozen_ids:
            self._emit(AuditEvent(sid, "granted", time.time()))

        return consent_grant

    def is_granted(self, sensor_id: str) -> bool:
        """Return True if there is a current grant covering
        ``sensor_id``.

        Emits a "used" audit event when granted, and a "denied" audit
        event when not, per Req 7.10.
        """
        granted = bool(self._grants.get(sensor_id))
        if granted:
            self._emit(AuditEvent(sensor_id, "used", time.time()))
        else:
            self._emit(AuditEvent(sensor_id, "denied", time.time()))
        return granted

    def _end_grant(self, consent_grant: ConsentGrant) -> None:
        grant_id = consent_grant._grant_id
        for sid in consent_grant.sensor_ids:
            ids = self._grants.get(sid)
            if ids is not None:
                ids.discard(grant_id)
                if not ids:
                    del self._grants[sid]
        self._active_grants.pop(grant_id, None)

    def _end_all_grants(self) -> None:
        """End every still-open grant, closing their devices. Invoked
        from the ``atexit`` path.

        Requirements: 7.5
        """
        for consent_grant in list(self._active_grants.values()):
            consent_grant._end()

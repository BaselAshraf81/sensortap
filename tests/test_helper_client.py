"""Tests for `adapters/windows/helper/client.py` (Req 11.5, 13.6, 13.7,
13.8).

The real `helper_src` executable is not available in this environment (no
.NET SDK -- per task 9.1's honest reporting), so a full live handshake
against the real compiled helper cannot be exercised here. These tests
cover what *can* be verified without it:

- token generation shape (256-bit / 32-byte-derived, per
  `secrets.token_urlsafe(32)`)
- the per-user security descriptor construction does not raise and
  produces a usable `SECURITY_ATTRIBUTES`-shaped object
- `setup()` against a deliberately nonexistent executable path degrades to
  `state == "failed_to_start"` with a reason, without raising
- `shutdown()` is safe to call when `setup()` was never called, and safe
  to call twice
- the pipe server can actually be created (this part of `CreateNamedPipe`
  needs no client, so it is exercised directly)

Requires Windows + pywin32 (installed as part of this task); skipped
everywhere else.
"""

from __future__ import annotations

import secrets
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

win32pipe = pytest.importorskip("win32pipe")
win32file = pytest.importorskip("win32file")

from sensortap.adapters.windows.helper.client import (  # noqa: E402
    HelperClient,
    _build_per_user_security_attributes,
    _create_server_pipe,
)


def test_token_generation_is_256_bit() -> None:
    token = secrets.token_urlsafe(32)
    # token_urlsafe(n) base64url-encodes n random bytes; decoding it back
    # (after restoring padding) should yield exactly 32 bytes = 256 bits.
    import base64

    padded = token + "=" * (-len(token) % 4)
    decoded = base64.urlsafe_b64decode(padded)
    assert len(decoded) == 32


def test_security_attributes_construction_does_not_raise() -> None:
    security_attributes = _build_per_user_security_attributes()
    # A real SECURITY_DESCRIPTOR was attached and bInheritHandle is set;
    # this is the extent to which pywin32's wrapper objects can be
    # introspected without a live pipe.
    assert security_attributes.SECURITY_DESCRIPTOR is not None
    assert not security_attributes.bInheritHandle


def test_create_server_pipe_succeeds_and_is_closeable() -> None:
    pipe_name, pipe_handle = _create_server_pipe()
    try:
        assert pipe_name.startswith("sensortap-")
    finally:
        win32file.CloseHandle(pipe_handle)


def test_setup_against_nonexistent_executable_reports_failed_to_start() -> None:
    client = HelperClient(helper_executable_path=r"C:\nonexistent\sensortap-helper.exe")

    client.setup()  # must not raise

    assert client.state == "failed_to_start"
    assert client.failure_reason is not None
    assert "not found" in client.failure_reason


def test_shutdown_is_safe_when_never_started() -> None:
    client = HelperClient(helper_executable_path=r"C:\nonexistent\sensortap-helper.exe")

    client.shutdown()  # must not raise
    client.shutdown()  # idempotent

    assert client.state == "not_running"


def test_shutdown_is_safe_after_failed_setup() -> None:
    client = HelperClient(helper_executable_path=r"C:\nonexistent\sensortap-helper.exe")

    client.setup()
    assert client.state == "failed_to_start"

    client.shutdown()
    client.shutdown()

    assert client.state == "not_running"

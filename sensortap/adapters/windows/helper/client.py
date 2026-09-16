"""Named-pipe client + lifecycle for the Windows Helper_Process (Req 11.5,
13.6, 13.7, 13.8).

Per the 8.2 decision recorded in tasks.md, **Python is the named pipe
SERVER**: it creates the pipe with a per-user security descriptor and
`PIPE_REJECT_REMOTE_CLIENTS`, then spawns `helper_src`'s compiled
executable (`Program.cs`), which connects out as the pipe CLIENT and sends
`HELLO {token}` as its very first message (verified directly against
`helper_src/Program.cs`, task 9.1's output).

This module owns the pipe server construction, the child process spawn,
the Launch_Token handshake (generation, delivery over stdin, constant-time
comparison, PID verification, and rejection), and the request/response
lifecycle built on top of `protocol.py`'s wire encoding and `LineFramer`.
It does not implement `hwmon_bridge.py` (task 9.4) — this is the
client/transport layer only.

pywin32 was verified importable in this environment (`win32pipe`,
`win32security`, `win32api`, `win32file` all import cleanly; it was not
preinstalled and was installed via `pip install pywin32` as part of this
task, per task 8.2's explicit choice of pywin32 over raw ctypes). Process
identity verification uses `win32pipe.GetNamedPipeClientProcessId`
directly — verified present on the installed pywin32 build
(`hasattr(win32pipe, "GetNamedPipeClientProcessId")` is `True`) — rather
than falling back to `ctypes.windll.kernel32.GetNamedPipeClientProcessId`,
consistent with task 8.2's rationale for using the auditable, tested
pywin32 surface throughout rather than mixing in raw ctypes calls.

Because pywin32 is Windows-only and this module is only ever loaded on
`win32` (guarded by `adapters/windows/__init__.py`'s platform guard at the
package level, same pattern as every other module under
`adapters/windows/`), the pywin32 imports below are unconditional at
module scope.
"""

from __future__ import annotations

import hmac
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32security

from sensortap.adapters.windows.helper.protocol import (
    HelperProtocolError,
    HelperResponse,
    LineFramer,
    encode_list_request,
    encode_ping_request,
    encode_read_request,
    encode_shutdown_request,
    decode_response,
)

#: Default location the CI build (task 9.5) publishes the compiled helper
#: to, per the design's package layout. Overridable via the constructor for
#: testing (e.g. pointing at a deliberately nonexistent path, or at a stub
#: executable) without needing a real .NET build.
DEFAULT_HELPER_EXECUTABLE_PATH = (
    Path(__file__).resolve().parent / "bin" / "sensortap-helper.exe"
)

#: Total readiness budget for `setup()`: pipe creation + spawn + connect +
#: handshake, start to finish (Req 13.7/13.8's "unavailable ... on absence,
#: start failure, auth failure or readiness timeout").
READINESS_TIMEOUT_MS = 10_000

#: How long `shutdown()` waits for the child to exit on its own after the
#: `shutdown` command is sent, before escalating to `terminate()`/`kill()`.
SHUTDOWN_GRACE_SECONDS = 5.0

#: `BackendStatus.helper_state`'s exact three values (Req 11.5, mirrored
#: from `registry/status.py`'s `HelperState` / `design.md`'s
#: `Literal["running", "not_running", "failed_to_start"]`).
HelperState = Literal["running", "not_running", "failed_to_start"]

#: pywin32's exposed buffer size for the pipe (bytes each direction).
_PIPE_BUFFER_SIZE = 65536


class HelperUnavailableError(Exception):
    """Raised by request-sending methods when called while the helper is
    not in the `running` state (e.g. `setup()` never called, or it
    degraded to `not_running`/`failed_to_start`)."""


@dataclass(frozen=True, slots=True)
class _StartFailure:
    """Internal record of why `setup()` did not reach `running`."""

    reason: str


class HelperClient:
    """Owns one Helper_Process's named pipe server, child process, and
    Launch_Token handshake.

    Lazy: nothing is created until `setup()` is called. `setup()` itself
    never raises — any failure (missing executable, spawn failure, auth
    failure, readiness timeout) is caught internally and reflected in
    `state`, so a caller (future `hwmon_bridge.py`) can report the
    hardware-monitor adapter as unavailable while every other adapter
    enumerates successfully, matching the established pattern in
    `registry/loading.py` / `registry/discovery.py` of never letting one
    backend's failure block the rest.
    """

    def __init__(
        self,
        *,
        helper_executable_path: Path | str = DEFAULT_HELPER_EXECUTABLE_PATH,
        readiness_timeout_ms: int = READINESS_TIMEOUT_MS,
        enable_motherboard: bool = False,
    ) -> None:
        self._helper_executable_path = Path(helper_executable_path)
        self._readiness_timeout_ms = readiness_timeout_ms
        # Off by default. When True, `--motherboard` is added to the helper's
        # argv, opting that process in to motherboard/Super-IO/EC monitoring.
        # See `helper_src/HardwareMonitor.cs`'s constructor doc for what that
        # unlocks and what it risks; this client only forwards the user's
        # explicit choice, it never decides on their behalf.
        self._enable_motherboard = enable_motherboard

        self._lock = threading.Lock()
        self._state: HelperState = "not_running"
        self._reason: str | None = None

        self._process: subprocess.Popen[bytes] | None = None
        self._pipe_handle: object | None = None
        self._framer = LineFramer()
        self._setup_started = False

    # ------------------------------------------------------------------
    # public state
    # ------------------------------------------------------------------

    @property
    def state(self) -> HelperState:
        """One of `"running"` / `"not_running"` / `"failed_to_start"`,
        matching `registry/status.py`'s `HelperState` exactly (Req 11.5)."""

        with self._lock:
            return self._state

    @property
    def failure_reason(self) -> str | None:
        """Human-readable reason for the current `not_running` /
        `failed_to_start` state, or `None` while `running`."""

        with self._lock:
            return self._reason

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Lazily start the pipe server, spawn the helper, and perform the
        Launch_Token handshake, bounded at `_readiness_timeout_ms` total.

        Never raises. On any failure the client's `state` becomes
        `"failed_to_start"` with `failure_reason` set, and this method
        returns normally.

        Idempotent: calling `setup()` again while already `running` is a
        no-op; calling it again after a failed attempt retries from
        scratch.
        """

        with self._lock:
            if self._state == "running":
                return
            self._setup_started = True

        deadline = time.monotonic() + self._readiness_timeout_ms / 1000
        try:
            self._setup_bounded(deadline)
        except Exception as exc:  # noqa: BLE001 - any failure degrades, never raises
            self._fail("failed_to_start", f"unexpected error during setup: {exc}")
            self._cleanup_partial()

    def _setup_bounded(self, deadline: float) -> None:
        if not self._helper_executable_path.is_file():
            self._fail(
                "failed_to_start",
                f"helper executable not found at {self._helper_executable_path}",
            )
            return

        token = secrets.token_urlsafe(32)

        try:
            pipe_name, pipe_handle = _create_server_pipe()
        except Exception as exc:  # noqa: BLE001
            self._fail("failed_to_start", f"failed to create named pipe: {exc}")
            return

        self._pipe_handle = pipe_handle

        argv = [str(self._helper_executable_path), pipe_name]
        if self._enable_motherboard:
            argv.append("--motherboard")

        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            self._fail("failed_to_start", f"failed to spawn helper process: {exc}")
            self._close_pipe()
            return

        self._process = process

        try:
            # Token goes on stdin only, never the command line (Req 13.6).
            assert process.stdin is not None
            process.stdin.write((token + "\n").encode("utf-8"))
            process.stdin.close()
        except Exception as exc:  # noqa: BLE001
            self._fail("failed_to_start", f"failed to deliver Launch_Token: {exc}")
            self._terminate_child()
            self._close_pipe()
            return

        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        try:
            self._await_connection(remaining_ms)
        except Exception as exc:  # noqa: BLE001
            self._fail("failed_to_start", f"pipe connect failed: {exc}")
            self._terminate_child()
            self._close_pipe()
            return

        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        ok, reason = self._perform_handshake(token, remaining_ms)
        if not ok:
            self._fail("failed_to_start", reason or "handshake failed")
            self._terminate_child()
            self._close_pipe()
            return

        with self._lock:
            self._state = "running"
            self._reason = None

    def _await_connection(self, timeout_ms: int) -> None:
        """Block (bounded) until the spawned helper connects to the pipe
        server, using `ConnectNamedPipe` in overlapped mode so the wait can
        be bounded rather than hanging forever if the child never
        connects."""

        overlapped = win32file.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        try:
            rc = win32pipe.ConnectNamedPipe(self._pipe_handle, overlapped)
            # ERROR_IO_PENDING / ERROR_PIPE_CONNECTED both indicate the
            # overlapped op is legitimately in flight or already done.
            if rc not in (0, win32event.WAIT_TIMEOUT):
                pass
            result = win32event.WaitForSingleObject(overlapped.hEvent, max(timeout_ms, 0))
            if result == win32event.WAIT_TIMEOUT:
                raise TimeoutError(f"no client connected within {timeout_ms} ms")
        finally:
            win32api.CloseHandle(overlapped.hEvent)

    def _perform_handshake(self, expected_token: str, timeout_ms: int) -> tuple[bool, str | None]:
        """Read the first message off the pipe, verify it is exactly
        ``HELLO {token}`` via constant-time comparison, and verify the
        connecting process is the child this instance spawned
        (`GetNamedPipeClientProcessId`).

        Returns `(True, None)` on success, `(False, reason)` on any
        failure. Never raises for an ordinary auth failure; only genuine
        I/O errors propagate to the caller, which already treats any
        raise as a failure.
        """

        assert self._process is not None
        deadline = time.monotonic() + timeout_ms / 1000

        # Req: verify the connecting process identity before trusting
        # anything it sends.
        try:
            client_pid = win32pipe.GetNamedPipeClientProcessId(self._pipe_handle)
        except Exception as exc:  # noqa: BLE001
            return False, f"could not determine connecting process id: {exc}"

        if client_pid != self._process.pid:
            return False, (
                f"connecting process pid {client_pid} does not match spawned "
                f"child pid {self._process.pid}"
            )

        first_line = self._read_first_line(deadline)
        if first_line is None:
            return False, "no message received from helper before timeout"

        expected = f"HELLO {expected_token}".encode("utf-8")
        if not hmac.compare_digest(first_line, expected):
            return False, "handshake token mismatch or malformed first message"

        return True, None

    def _read_first_line(self, deadline: float) -> bytes | None:
        while True:
            lines = self._framer.feed(b"")
            if lines:
                return lines[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                chunk = _read_pipe_chunk(self._pipe_handle, timeout_ms=int(remaining * 1000))
            except Exception:  # noqa: BLE001
                return None
            if not chunk:
                return None
            lines = self._framer.feed(chunk)
            if lines:
                return lines[0]

    def _fail(self, state: HelperState, reason: str) -> None:
        with self._lock:
            self._state = state
            self._reason = reason

    def _cleanup_partial(self) -> None:
        self._terminate_child()
        self._close_pipe()

    def _terminate_child(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass

    def _close_pipe(self) -> None:
        handle = self._pipe_handle
        self._pipe_handle = None
        if handle is not None:
            try:
                win32file.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # requests
    # ------------------------------------------------------------------

    def send_list(self) -> HelperResponse:
        return self._send_request(encode_list_request())

    def send_read(self, ids: list[str]) -> HelperResponse:
        return self._send_request(encode_read_request(ids))

    def send_ping(self) -> HelperResponse:
        return self._send_request(encode_ping_request())

    def _send_request(self, payload: bytes) -> HelperResponse:
        if self.state != "running":
            raise HelperUnavailableError(
                f"helper is not running (state={self.state!r}, reason={self.failure_reason!r})"
            )

        win32file.WriteFile(self._pipe_handle, payload)

        while True:
            lines = self._framer.feed(b"")
            if lines:
                break
            chunk = _read_pipe_chunk(self._pipe_handle, timeout_ms=5000)
            if not chunk:
                raise HelperProtocolError("pipe closed before a response was received")
            lines = self._framer.feed(chunk)
            if lines:
                break

        return decode_response(lines[0])

    # ------------------------------------------------------------------
    # shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Send `shutdown`, wait up to 5000 ms for a clean exit, then
        kill. Idempotent: safe to call when `setup()` was never called or
        already failed, and safe to call twice."""

        with self._lock:
            process = self._process
            pipe_handle = self._pipe_handle
            was_running = self._state == "running"
            self._state = "not_running"
            self._reason = None

        if was_running and pipe_handle is not None:
            try:
                win32file.WriteFile(pipe_handle, encode_shutdown_request())
            except Exception:  # noqa: BLE001
                pass

        if process is not None:
            try:
                process.wait(timeout=SHUTDOWN_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass

        if pipe_handle is not None:
            try:
                win32file.CloseHandle(pipe_handle)
            except Exception:  # noqa: BLE001
                pass

        self._process = None
        self._pipe_handle = None


# ----------------------------------------------------------------------
# pipe construction helpers
# ----------------------------------------------------------------------


def _create_server_pipe() -> tuple[str, object]:
    """Create a named pipe server instance with a per-user security
    descriptor and `PIPE_REJECT_REMOTE_CLIENTS` (per task 8.2's decision).

    Returns `(pipe_name, pipe_handle)` where `pipe_name` is the short name
    (no `\\\\.\\pipe\\` prefix) passed to the spawned helper's argv[0], per
    `Program.cs`'s documented invocation contract.
    """

    pipe_name = f"sensortap-{secrets.token_urlsafe(16)}"
    full_pipe_name = rf"\\.\pipe\{pipe_name}"

    security_attributes = _build_per_user_security_attributes()

    open_mode = (
        win32con.PIPE_ACCESS_DUPLEX
        | win32con.FILE_FLAG_OVERLAPPED
    )
    pipe_mode = (
        win32pipe.PIPE_TYPE_MESSAGE
        | win32pipe.PIPE_READMODE_BYTE
        | win32pipe.PIPE_WAIT
        | win32pipe.PIPE_REJECT_REMOTE_CLIENTS
    )

    handle = win32pipe.CreateNamedPipe(
        full_pipe_name,
        open_mode,
        pipe_mode,
        1,  # max instances
        _PIPE_BUFFER_SIZE,
        _PIPE_BUFFER_SIZE,
        0,  # default timeout
        security_attributes,
    )

    return pipe_name, handle


def _build_per_user_security_attributes() -> win32security.SECURITY_ATTRIBUTES:
    """Build a `SECURITY_ATTRIBUTES` whose DACL grants full access to the
    current process token's user SID only, per task 8.2's decision that
    "an ACL built wrong is a security bug".

    Uses the current process token's `TokenUser` SID (via
    `win32security.OpenProcessToken` + `GetTokenInformation`) rather than
    `LookupAccountName` on the resolved username, since the token SID is
    the authoritative identity for the process that will own the pipe and
    avoids a name->SID lookup that could resolve differently under domain
    accounts.
    """

    process_token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    user_sid, _attributes = win32security.GetTokenInformation(
        process_token, win32security.TokenUser
    )

    security_descriptor = win32security.SECURITY_DESCRIPTOR()
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        user_sid,
    )
    security_descriptor.SetSecurityDescriptorDacl(1, dacl, 0)

    security_attributes = win32security.SECURITY_ATTRIBUTES()
    security_attributes.SECURITY_DESCRIPTOR = security_descriptor
    security_attributes.bInheritHandle = False

    return security_attributes


def _read_pipe_chunk(pipe_handle: object, *, timeout_ms: int) -> bytes:
    """Read whatever is currently available from the pipe, bounded by
    `timeout_ms`. Returns an empty `bytes` on timeout or pipe closure."""

    overlapped = win32file.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
    try:
        _rc, buffer = win32file.ReadFile(pipe_handle, _PIPE_BUFFER_SIZE, overlapped)
        result = win32event.WaitForSingleObject(overlapped.hEvent, max(timeout_ms, 0))
        if result == win32event.WAIT_TIMEOUT:
            return b""
        bytes_read = win32file.GetOverlappedResult(pipe_handle, overlapped, False)
        return bytes(buffer[:bytes_read])
    except Exception:  # noqa: BLE001
        return b""
    finally:
        win32api.CloseHandle(overlapped.hEvent)

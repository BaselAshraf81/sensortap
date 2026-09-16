"""Elevation / privilege detection.

Pure reads of the current process's privilege state. Never triggers an OS
elevation prompt (Req 8.6). If detection genuinely fails for any reason,
the process is treated as **not elevated** (Req 8.9) -- failing closed is
the only safe default.

Requirements: 7.4 (indirectly, via consent), 8.6, 8.7, 8.9
"""

from __future__ import annotations

import ctypes
import os
import sys


def _is_elevated_windows() -> bool:
    """Best-effort elevation check on Windows.

    Tries ``shell32.IsUserAnAdmin()`` first (a pure read of the current
    token, does not raise UAC). Falls back to querying the process
    token's ``TokenElevation`` attribute via ``advapi32`` when the first
    call is unavailable or raises.
    """
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        pass

    try:
        return _token_elevation_windows()
    except Exception:
        return False


def _token_elevation_windows() -> bool:
    """Fallback elevation check via ``OpenProcessToken`` +
    ``GetTokenInformation(TokenElevation)``.

    Wrapped entirely in try/except by the caller; any failure here
    propagates up to be treated as "not elevated".
    """
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32

    TOKEN_QUERY = 0x0008
    TokenElevation = 20

    h_process = kernel32.GetCurrentProcess()
    h_token = ctypes.c_void_p()

    if not advapi32.OpenProcessToken(h_process, TOKEN_QUERY, ctypes.byref(h_token)):
        return False

    try:
        elevation = ctypes.c_uint32()
        returned_len = ctypes.c_uint32()
        ok = advapi32.GetTokenInformation(
            h_token,
            TokenElevation,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(returned_len),
        )
        if not ok:
            return False
        return bool(elevation.value)
    finally:
        kernel32.CloseHandle(h_token)


def _is_elevated_posix() -> bool:
    """Elevation check on Linux/macOS: effective user id 0."""
    return os.geteuid() == 0


def is_elevated() -> bool:
    """Return whether the current process holds administrative
    privileges, without ever raising an OS elevation prompt.

    On any platform, if privilege state cannot be determined, this
    returns ``False`` (treat as not elevated) rather than raising.

    Requirements: 8.6, 8.7, 8.9
    """
    try:
        if sys.platform == "win32":
            return _is_elevated_windows()
        return _is_elevated_posix()
    except Exception:
        return False

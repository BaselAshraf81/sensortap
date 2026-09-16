"""The Windows adapter family: platform guard and shared plumbing.

Every adapter module under `sensortap.adapters.windows` targets Win32 only
(Req 13.1-13.12). `WINDOWS_PLATFORMS` is the one place that platform tag is
spelled, so every concrete adapter's `AdapterMeta.supported_platforms` stays
in step by construction rather than by copy-pasted string literals.

The real shared plumbing -- the `GetDefault()`-returning-null handling, the
2000 ms read bound, and persistent-identifier extraction -- lives in
`sensortap.adapters.windows._common` so that this `__init__.py` stays a
thin, side-effect-free guard that is safe to import on any platform (the
registry's loading gate only reads `AdapterMeta` off classes; it must never
be blocked from even importing a Windows adapter module on non-Windows CI
runners just to discover it should be skipped).
"""

from __future__ import annotations

#: The platform tag every Windows adapter declares in its
#: `AdapterMeta.supported_platforms` (Req 13.1). A shared constant instead
#: of a per-adapter literal keeps every Windows adapter file in step should
#: sensortap ever need to widen this (e.g. to admit a future `"win64"`
#: distinction, which does not exist today -- `sys.platform` reports
#: `"win32"` on both 32- and 64-bit Windows).
WINDOWS_PLATFORMS: frozenset[str] = frozenset({"win32"})

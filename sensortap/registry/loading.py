"""Adapter loading: entry-point discovery, gating and bounded instantiation.

Implements the load sequence described in design.md under "Adapter
loading" (Req 8.3, 10.4, 10.6, 10.9, 10.11, 15.8):

1. Discover entry points in the `sensortap.adapters` group, sorted by
   `(distribution_name, entry_point_name)` for a deterministic load order
   (Req 10.4).
2. Read `AdapterMeta` off the class *without instantiating* it.
3. Skip (not a failure) on a platform or interface-MAJOR mismatch (Req
   10.9) -- this is a `not_loaded` outcome with reason "unsupported
   platform", not a load failure.
4. Skip adapters that require elevation opt-in unless the caller opted in
   (Req 8.3), with reason "not opted in".
5. Bound import + instantiate + `setup()` at 5000 ms total; on overrun or
   raise, record a load-failure entry retained for the process lifetime
   and continue with the remaining adapters (Req 10.6).
6. Refuse to load an adapter whose `meta.read_only_declared` is not
   `True` (Req 15.8).

This module does not implement discovery/dedup/status (later registry
tasks); it only produces the sets of loaded adapters, skipped adapters and
load failures that those later pieces will consume.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from typing import Literal

from sensortap.adapters.protocol import ADAPTER_INTERFACE_VERSION, Adapter, AdapterMeta
from sensortap.schema.version import MalformedSchemaVersionError, parse_major

#: Total time budget, in milliseconds, for import + instantiate + setup()
#: of one adapter (Req 10.6).
LOAD_TIMEOUT_MS = 5000

#: Entry point group third-party packages register adapters under (Req 10.4).
ENTRY_POINT_GROUP = "sensortap.adapters"

SkipReason = Literal["unsupported_platform", "not_opted_in"]


@dataclass(frozen=True, slots=True)
class SkippedAdapter:
    """An adapter that was deliberately not loaded, but which is not a
    load failure (Req 10.9, 8.3).
    """

    entry_point_name: str
    distribution_name: str | None
    reason: SkipReason
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterLoadFailure:
    """A load failure recorded for the process lifetime.

    Distinct from :class:`SkippedAdapter`: this represents a genuine
    problem -- a raise, a timeout, or a missing read-only declaration --
    rather than a deliberate, expected skip.
    """

    entry_point_name: str
    distribution_name: str | None
    reason: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class LoadedAdapter:
    """A successfully loaded, instantiated and set-up adapter."""

    entry_point_name: str
    distribution_name: str | None
    instance: Adapter


@dataclass(slots=True)
class LoadResult:
    """The outcome of one call to :func:`load_adapters`."""

    loaded: list[LoadedAdapter] = field(default_factory=list)
    skipped: list[SkippedAdapter] = field(default_factory=list)
    failures: list[AdapterLoadFailure] = field(default_factory=list)


def _sort_key(ep: EntryPoint) -> tuple[str, str]:
    dist = ep.dist
    dist_name = dist.name if dist is not None else ""
    return (dist_name, ep.name)


def _distribution_name(ep: EntryPoint) -> str | None:
    return ep.dist.name if ep.dist is not None else None


def _normalized_platform() -> str:
    """Return the platform tag adapters declare `supported_platforms` in.

    `sys.platform` already yields the tags used in the design's examples
    (`"win32"`, `"linux"`), so no further normalization is required today.
    """
    return sys.platform


def _load_class_without_instantiating(ep: EntryPoint) -> type:
    """Import the module and resolve the entry point's target class,
    without constructing an instance of it.

    `EntryPoint.load()` imports the module and attribute-resolves down to
    the target object, but never calls it -- that is exactly the
    "read `AdapterMeta` off the class without instantiating" step.
    """
    return ep.load()


def _instantiate_and_setup(adapter_cls: type) -> Adapter:
    """Instantiate the adapter class and call its optional `setup()`.

    Runs entirely inside the bounded worker in :func:`_load_one`; this
    function itself applies no timeout.
    """
    instance = adapter_cls()
    setup = getattr(instance, "setup", None)
    if callable(setup):
        setup()
    return instance


def _load_one(
    ep: EntryPoint, *, include_elevated: bool
) -> tuple[LoadedAdapter | None, SkippedAdapter | None, AdapterLoadFailure | None]:
    """Run the full load sequence for one entry point.

    Returns exactly one of (loaded, skipped, failure) populated, the
    other two `None`.
    """

    name = ep.name
    dist_name = _distribution_name(ep)

    # Step 1: read AdapterMeta off the class without instantiating.
    try:
        adapter_cls = _load_class_without_instantiating(ep)
        meta: AdapterMeta = adapter_cls.meta
    except Exception as exc:  # noqa: BLE001 - any import/attr error is a load failure
        return (
            None,
            None,
            AdapterLoadFailure(
                entry_point_name=name,
                distribution_name=dist_name,
                reason="load_error",
                detail=f"failed to import/read AdapterMeta: {exc!r}",
            ),
        )

    # Step 2: platform gate.
    current_platform = _normalized_platform()
    if current_platform not in meta.supported_platforms:
        return (
            None,
            SkippedAdapter(
                entry_point_name=name,
                distribution_name=dist_name,
                reason="unsupported_platform",
                detail=(
                    f"platform {current_platform!r} not in "
                    f"{sorted(meta.supported_platforms)!r}"
                ),
            ),
            None,
        )

    # Step 2 (continued): interface MAJOR version gate.
    try:
        adapter_major = parse_major(meta.interface_version)
        core_major = parse_major(ADAPTER_INTERFACE_VERSION)
    except MalformedSchemaVersionError as exc:
        return (
            None,
            SkippedAdapter(
                entry_point_name=name,
                distribution_name=dist_name,
                reason="unsupported_platform",
                detail=f"malformed interface_version: {exc}",
            ),
            None,
        )
    if adapter_major != core_major:
        return (
            None,
            SkippedAdapter(
                entry_point_name=name,
                distribution_name=dist_name,
                reason="unsupported_platform",
                detail=(
                    f"interface_version {meta.interface_version!r} MAJOR "
                    f"{adapter_major} != core MAJOR {core_major}"
                ),
            ),
            None,
        )

    # Step 3: elevation opt-in gate.
    if meta.requires_elevation_optin and not include_elevated:
        return (
            None,
            SkippedAdapter(
                entry_point_name=name,
                distribution_name=dist_name,
                reason="not_opted_in",
                detail="adapter requires elevation opt-in",
            ),
            None,
        )

    # Step 4: bounded instantiate + setup().
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_instantiate_and_setup, adapter_cls)
        try:
            instance = future.result(timeout=LOAD_TIMEOUT_MS / 1000)
        except FutureTimeoutError:
            return (
                None,
                None,
                AdapterLoadFailure(
                    entry_point_name=name,
                    distribution_name=dist_name,
                    reason="load_timeout",
                    detail=f"exceeded {LOAD_TIMEOUT_MS} ms budget",
                ),
            )
        except Exception as exc:  # noqa: BLE001 - instantiate/setup raised
            return (
                None,
                None,
                AdapterLoadFailure(
                    entry_point_name=name,
                    distribution_name=dist_name,
                    reason="load_error",
                    detail=f"instantiate/setup raised: {exc!r}",
                ),
            )

    # Step 5: read-only declaration check.
    if meta.read_only_declared is not True:
        return (
            None,
            None,
            AdapterLoadFailure(
                entry_point_name=name,
                distribution_name=dist_name,
                reason="read_only_not_declared",
                detail=(
                    "adapter refused: meta.read_only_declared must be True "
                    "before the registry loads it (Req 15.8)"
                ),
            ),
        )

    return (
        LoadedAdapter(entry_point_name=name, distribution_name=dist_name, instance=instance),
        None,
        None,
    )


def load_adapters(*, include_elevated: bool = False) -> LoadResult:
    """Discover and load every adapter registered under
    `sensortap.adapters`.

    Entry points are processed in deterministic order --
    `(distribution_name, entry_point_name)` -- so `source` ordering,
    priority tie-breaking and result ordering elsewhere in the registry
    are reproducible (Req 1.2, 1.7, 10.10). One bad adapter never aborts
    loading of the rest: every outcome is recorded and iteration
    continues.
    """

    eps = sorted(entry_points(group=ENTRY_POINT_GROUP), key=_sort_key)

    result = LoadResult()
    for ep in eps:
        loaded, skipped, failure = _load_one(ep, include_elevated=include_elevated)
        if loaded is not None:
            result.loaded.append(loaded)
        elif skipped is not None:
            result.skipped.append(skipped)
        elif failure is not None:
            result.failures.append(failure)

    return result

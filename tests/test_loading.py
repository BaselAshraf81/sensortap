"""Minimal tests for registry/loading.py gating logic (Req 8.3, 10.6, 10.9, 15.8).

Uses dummy adapter classes registered via a fake EntryPoint-like object,
since real entry points require packaging metadata. We monkeypatch
`entry_points` to avoid depending on installed distributions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import ClassVar

import pytest

from sensortap.adapters.protocol import AdapterMeta
from sensortap.registry import loading


def _meta(**overrides) -> AdapterMeta:
    base = dict(
        adapter_id="dummy",
        interface_version="1.0",
        supported_platforms=frozenset({"win32", "linux"}),
        read_only_declared=True,
    )
    base.update(overrides)
    return AdapterMeta(**base)


class GoodAdapter:
    meta: ClassVar[AdapterMeta] = _meta()

    def discover(self):
        return []

    def read(self, sensor_id):
        raise NotImplementedError


class UnsupportedPlatformAdapter:
    meta: ClassVar[AdapterMeta] = _meta(supported_platforms=frozenset({"plan9"}))

    def discover(self):
        return []

    def read(self, sensor_id):
        raise NotImplementedError


class WrongMajorAdapter:
    meta: ClassVar[AdapterMeta] = _meta(interface_version="2.0")

    def discover(self):
        return []

    def read(self, sensor_id):
        raise NotImplementedError


class ElevationOptInAdapter:
    meta: ClassVar[AdapterMeta] = _meta(requires_elevation_optin=True)

    def discover(self):
        return []

    def read(self, sensor_id):
        raise NotImplementedError


class RaisingSetupAdapter:
    meta: ClassVar[AdapterMeta] = _meta()

    def __init__(self):
        pass

    def setup(self):
        raise RuntimeError("boom")

    def discover(self):
        return []

    def read(self, sensor_id):
        raise NotImplementedError


class SlowSetupAdapter:
    meta: ClassVar[AdapterMeta] = _meta()

    def setup(self):
        time.sleep(6)

    def discover(self):
        return []

    def read(self, sensor_id):
        raise NotImplementedError


class NotReadOnlyAdapter:
    meta: ClassVar[AdapterMeta] = _meta(read_only_declared=False)

    def discover(self):
        return []

    def read(self, sensor_id):
        raise NotImplementedError


@dataclass
class _FakeDist:
    name: str


class _FakeEntryPoint:
    def __init__(self, name: str, cls: type, dist_name: str = "fake-dist"):
        self.name = name
        self._cls = cls
        self.dist = _FakeDist(dist_name)

    def load(self):
        return self._cls


def _patch_entry_points(monkeypatch, eps):
    monkeypatch.setattr(loading, "entry_points", lambda group=None: eps)


def test_good_adapter_loads(monkeypatch):
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("good", GoodAdapter)])
    result = loading.load_adapters()
    assert len(result.loaded) == 1
    assert isinstance(result.loaded[0].instance, GoodAdapter)
    assert not result.skipped
    assert not result.failures


def test_unsupported_platform_is_skipped_not_failed(monkeypatch):
    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("bad_platform", UnsupportedPlatformAdapter)]
    )
    result = loading.load_adapters()
    assert not result.loaded
    assert not result.failures
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "unsupported_platform"


def test_interface_major_mismatch_is_skipped_not_failed(monkeypatch):
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("wrong_major", WrongMajorAdapter)])
    result = loading.load_adapters()
    assert not result.loaded
    assert not result.failures
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "unsupported_platform"


def test_elevation_optin_skipped_when_not_opted_in(monkeypatch):
    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("elevated", ElevationOptInAdapter)]
    )
    result = loading.load_adapters(include_elevated=False)
    assert not result.loaded
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "not_opted_in"

    result2 = loading.load_adapters(include_elevated=True)
    assert len(result2.loaded) == 1
    assert not result2.skipped


def test_raising_setup_records_load_failure(monkeypatch):
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("raises", RaisingSetupAdapter)])
    result = loading.load_adapters()
    assert not result.loaded
    assert len(result.failures) == 1
    assert result.failures[0].reason == "load_error"


def test_slow_setup_times_out_and_records_failure(monkeypatch):
    monkeypatch.setattr(loading, "LOAD_TIMEOUT_MS", 200)
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("slow", SlowSetupAdapter)])
    result = loading.load_adapters()
    assert not result.loaded
    assert len(result.failures) == 1
    assert result.failures[0].reason == "load_timeout"


def test_not_read_only_declared_is_refused(monkeypatch):
    _patch_entry_points(monkeypatch, [_FakeEntryPoint("not_ro", NotReadOnlyAdapter)])
    result = loading.load_adapters()
    assert not result.loaded
    assert len(result.failures) == 1
    assert result.failures[0].reason == "read_only_not_declared"


def test_deterministic_load_order(monkeypatch):
    eps = [
        _FakeEntryPoint("zeta", GoodAdapter, dist_name="dist-b"),
        _FakeEntryPoint("alpha", GoodAdapter, dist_name="dist-a"),
        _FakeEntryPoint("beta", GoodAdapter, dist_name="dist-a"),
    ]
    _patch_entry_points(monkeypatch, eps)
    result = loading.load_adapters()
    order = [(la.distribution_name, la.entry_point_name) for la in result.loaded]
    assert order == [("dist-a", "alpha"), ("dist-a", "beta"), ("dist-b", "zeta")]

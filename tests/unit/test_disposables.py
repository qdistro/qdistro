"""Tests for the pure disposable-silo helpers + the session-manager reaper
sweep (07-disposables-plan P1)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SM_DIR = REPO_ROOT / "session_manager"
sys.path.insert(0, str(SM_DIR))


def _load(label: str, path: Path):
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("qdistro_disposables", SM_DIR / "qdistro_disposables.py")


# ---- naming ----------------------------------------------------------

def test_disposable_name_shape():
    n = disp.disposable_name("pdf", "20260612-151828")
    assert n == "disp-pdf-20260612-151828"
    assert disp.is_disposable_container(n)
    assert disp.parse_disposable_name(n) == ("pdf", "20260612-151828")


def test_disposable_name_collision_suffix():
    n = disp.disposable_name("pdf", "20260612-151828", suffix="ab12")
    assert n == "disp-pdf-20260612-151828-ab12"
    assert disp.is_disposable_container(n)


@pytest.mark.parametrize("bad", ["Bad", "has_underscore", "white space",
                                 "../x", "", "a/b", "x" * 64])
def test_validate_workload_rejects(bad):
    with pytest.raises(disp.DisposableError):
        disp.validate_workload(bad)


def test_disposable_name_rejects_bad_timestamp():
    with pytest.raises(disp.DisposableError):
        disp.disposable_name("pdf", "not-a-ts")


# ---- secctx app_id ---------------------------------------------------

def test_secctx_appid():
    token = "f8e14f7cb8d479f9f1f2de4fd5c98f2a"
    assert disp.disposable_secctx_appid(token) == f"qdistro.disp.{token}"
    assert disp.is_disposable_appid(f"qdistro.disp.{token}")
    assert not disp.is_disposable_appid("qdistro.tier2")
    assert not disp.is_disposable_appid("qdistro.disp.NOTHEX")


def test_secctx_appid_rejects_bad_token():
    with pytest.raises(disp.DisposableError):
        disp.disposable_secctx_appid("xyz")  # not hex / too short


def test_dispose_action():
    assert disp.dispose_action("pdf") == "qdistro.dispose.spawn:pdf"
    with pytest.raises(disp.DisposableError):
        disp.dispose_action("BAD")


# ---- sweep targets ---------------------------------------------------

def test_sweep_targets_only_disposables():
    names = [
        "disp-pdf-20260612-151828",         # disposable
        "disp-office-20260612-151900-ab",   # disposable w/ suffix
        "qdistro-silo-mybrowser",           # persistent tier-2: NOT touched
        "disp-",                            # malformed: NOT a disposable
        "dispatcher",                       # not a disposable (no shape)
        "disposable-thing",                 # not disp- prefix shape
    ]
    targets = disp.disp_sweep_targets(names)
    assert targets == [
        "disp-pdf-20260612-151828",
        "disp-office-20260612-151900-ab",
    ]


def test_is_disposable_container_strict():
    assert disp.is_disposable_container("disp-pdf-20260612-151828")
    assert not disp.is_disposable_container("disp-pdf")           # no ts
    assert not disp.is_disposable_container("disp-pdf-20260612")  # short ts
    assert not disp.is_disposable_container("qdistro-silo-pdf")
    assert not disp.is_disposable_container("")
    # A trailing newline must NOT slip past (fullmatch, not $-anchored match).
    assert not disp.is_disposable_container("disp-pdf-20260612-151828\n")
    assert not disp.is_disposable_container("disp-pdf-20260612-151828\nevil")


def test_validate_workload_rejects_trailing_newline():
    with pytest.raises(disp.DisposableError):
        disp.validate_workload("pdf\n")
    with pytest.raises(disp.DisposableError):
        disp.validate_workload("pdf\nrm")


# ---- session-manager reaper (with a fake ops) ------------------------

class FakeOps:
    def __init__(self, containers):
        self._containers = list(containers)
        self.removed: list[str] = []

    def disp_container_list(self):
        return list(self._containers)

    def disp_container_remove(self, name):
        if not disp.is_disposable_container(name):
            raise ValueError(name)
        self.removed.append(name)
        self._containers.remove(name)
        return True


class _Reaper:
    """Minimal stand-in exercising the real reaper logic shape: list ->
    disp_sweep_targets -> remove. Mirrors
    SiloManager.reap_disposable_containers without importing the 2k-line
    daemon."""
    def __init__(self, ops):
        self._ops = ops

    def reap(self):
        reaped = []
        for name in disp.disp_sweep_targets(self._ops.disp_container_list()):
            if self._ops.disp_container_remove(name):
                reaped.append(name)
        return reaped


def test_reaper_removes_only_disposables():
    ops = FakeOps([
        "disp-pdf-20260612-151828",
        "qdistro-silo-browser",       # persistent — must survive
        "disp-office-20260612-152000",
    ])
    reaped = _Reaper(ops).reap()
    assert set(reaped) == {
        "disp-pdf-20260612-151828", "disp-office-20260612-152000"}
    assert ops.removed == reaped
    assert "qdistro-silo-browser" in ops._containers  # untouched

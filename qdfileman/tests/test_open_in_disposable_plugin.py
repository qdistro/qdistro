"""Tests for the open-in-disposable context-menu plugin.

This plugin is the file-manager CONSUMER of the shipped qdistro disposable
surface (``qdistro_app.open_in_disposable``). The security boundary lives in
the SDK + trusted launch binary; the plugin's job is to route the right file to
the right class fail-closed. So the tests pin:

  * class-resolution correctness — a fixed allowlist of class-name literals,
    never derived from the (possibly hostile) filename;
  * the fail-closed enablement probe — exit 0 enables, EVERYTHING else
    (non-zero, missing resolver, timeout, OSError) disables;
  * menu visibility — item appears only when SDK importable AND a class maps
    AND the probe says enabled; hidden otherwise;
  * the click path calls ``open_in_disposable`` with the EXACT path + resolved
    class, re-resolves on click (no caching), and surfaces any failure as a
    warning dialog instead of raising.

No real podman, broker, or D-Bus is touched. The SDK and the resolver probe are
monkeypatched the way qfileman's other plugin suites patch their shell-outs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from qfileman.plugins.builtin import open_in_disposable as oid


# The real shipped resolver + registry, used for an end-to-end probe assertion
# that doesn't mock the gate (so a registry/resolver drift would be caught).
# qdfileman lives in-tree in the qdistro monorepo, so the resolver is at
# <repo root>/session_manager/. Walk up from this file and take the NEAREST
# ancestor that has it: that is the checkout (or worktree) this test belongs
# to. (The pre-monorepo lookup was "<ancestor>/qdistro/session_manager/...",
# which in the monorepo only matched when the checkout happened to be NAMED
# qdistro, and from a worktree could reach a different checkout.) Skips
# cleanly if the file is exported without the rest of the repository.
def _find_real_resolver():
    here = Path(__file__).resolve()
    candidates = [
        p / "session_manager" / "qdistro_disposable_classes.py"
        for p in here.parents
    ]
    env_override = os.environ.get("QDISTRO_DISPOSABLE_CLASSES_RESOLVER", "")
    env = (Path(env_override),) if env_override else ()
    for cand in (*env, *candidates):
        if cand.exists():
            return cand
    return None


_REAL_RESOLVER = _find_real_resolver()
_REAL_REGISTRY = (
    _REAL_RESOLVER.parent / "disposable-classes.toml"
    if _REAL_RESOLVER is not None else None)


# ---------------------------------------------------------------------------
# resolve_class_for_path — fixed allowlist, never filename-derived
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "a.txt", "notes.md", "data.csv", "config.json", "script.py",
    "run.sh", "settings.yaml", "deploy.yml", "app.conf", "x.log",
    "main.c", "lib.rs", "Page.html.txt",  # final extension wins
])
def test_resolve_text_files_to_text_class(tmp_path, name):
    f = tmp_path / name
    f.write_text("x")
    assert oid.resolve_class_for_path(str(f)) == oid.TEXT_CLASS


@pytest.mark.parametrize("name", [
    "a.pdf", "doc.docx", "sheet.xlsx", "pic.png", "img.jpg",
    "blob.bin", "archive.zip", "tarball.tar.gz", "binary",
])
def test_resolve_non_text_files_to_none(tmp_path, name):
    """Binaries, images, and the DISABLED hostile classes (pdf/office/archive)
    are intentionally unmapped — no item, no tier-2 routing for them."""
    f = tmp_path / name
    f.write_bytes(b"\x00\x01\x02")
    assert oid.resolve_class_for_path(str(f)) is None


def test_resolve_directory_is_none(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    assert oid.resolve_class_for_path(str(d)) is None


def test_resolve_nonexistent_or_empty_is_none(tmp_path):
    assert oid.resolve_class_for_path("") is None
    assert oid.resolve_class_for_path(str(tmp_path / "nope.txt")) is None


def test_resolve_class_is_a_fixed_literal_not_filename_derived(tmp_path):
    """A hostile filename can only ever yield a known-safe literal or None — its
    bytes never become the class name (no broker-action injection)."""
    hostile = tmp_path / "evil; rm -rf; --network.pdf"
    hostile.write_bytes(b"\x00")
    assert oid.resolve_class_for_path(str(hostile)) is None  # .pdf is unmapped

    hostile_txt = tmp_path / "qdistro.dispose.open:pwned.txt"
    hostile_txt.write_text("x")
    # The .txt resolves to the FIXED literal, never the filename's contents.
    assert oid.resolve_class_for_path(str(hostile_txt)) == oid.TEXT_CLASS


# ---------------------------------------------------------------------------
# class_enabled — fail-closed probe (cached)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The enablement probe is cached for the process; reset it per test so
    mocked exit codes don't leak across tests."""
    oid._reset_enablement_cache()
    yield
    oid._reset_enablement_cache()


def _fake_run(returncode):
    def _run(argv, *a, **k):
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")
    return _run


def test_class_enabled_true_on_exit0(monkeypatch, tmp_path):
    resolver = tmp_path / "resolver.py"
    resolver.write_text("# stub")
    monkeypatch.setenv("QDISTRO_DISPOSABLE_CLASSES_RESOLVER", str(resolver))
    monkeypatch.setattr(oid.subprocess, "run", _fake_run(0))
    assert oid.class_enabled("text/plain") is True


@pytest.mark.parametrize("rc", [1, 3, 4, 5, 2])
def test_class_enabled_false_on_nonzero(monkeypatch, tmp_path, rc):
    """Unknown (3) / disabled (4) / malformed (5) / any non-zero → disabled."""
    resolver = tmp_path / "resolver.py"
    resolver.write_text("# stub")
    monkeypatch.setenv("QDISTRO_DISPOSABLE_CLASSES_RESOLVER", str(resolver))
    monkeypatch.setattr(oid.subprocess, "run", _fake_run(rc))
    assert oid.class_enabled("pdf") is False


def test_class_enabled_false_when_resolver_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("QDISTRO_DISPOSABLE_CLASSES_RESOLVER",
                       str(tmp_path / "does-not-exist.py"))

    def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("must not spawn when resolver is absent")

    monkeypatch.setattr(oid.subprocess, "run", _boom)
    assert oid.class_enabled("text/plain") is False


def test_class_enabled_false_on_timeout(monkeypatch, tmp_path):
    resolver = tmp_path / "resolver.py"
    resolver.write_text("# stub")
    monkeypatch.setenv("QDISTRO_DISPOSABLE_CLASSES_RESOLVER", str(resolver))

    def _timeout(argv, *a, **k):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=oid._PROBE_TIMEOUT_S)

    monkeypatch.setattr(oid.subprocess, "run", _timeout)
    assert oid.class_enabled("text/plain") is False


def test_class_enabled_false_on_oserror(monkeypatch, tmp_path):
    resolver = tmp_path / "resolver.py"
    resolver.write_text("# stub")
    monkeypatch.setenv("QDISTRO_DISPOSABLE_CLASSES_RESOLVER", str(resolver))

    def _oserror(argv, *a, **k):
        raise OSError("no python3")

    monkeypatch.setattr(oid.subprocess, "run", _oserror)
    assert oid.class_enabled("text/plain") is False


def test_class_enabled_probe_is_cached_not_reshelled(monkeypatch, tmp_path):
    """The probe must NOT re-shell on every call — it runs once per (resolver,
    class) and caches, so a right-click never freezes the Qt UI thread."""
    resolver = tmp_path / "resolver.py"
    resolver.write_text("# stub")
    monkeypatch.setenv("QDISTRO_DISPOSABLE_CLASSES_RESOLVER", str(resolver))
    calls = {"n": 0}

    def _counting_run(argv, *a, **k):
        calls["n"] += 1
        # argv must be a LIST (no shell) with the exact resolver invocation, so
        # a class string can never be interpreted by a shell.
        assert argv == ["python3", str(resolver), "--resolve", "text/plain"]
        assert "shell" not in k or k["shell"] is False
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(oid.subprocess, "run", _counting_run)
    assert oid.class_enabled("text/plain") is True
    assert oid.class_enabled("text/plain") is True
    assert oid.class_enabled("text/plain") is True
    assert calls["n"] == 1  # cached after the first probe


@pytest.mark.skipif(_REAL_RESOLVER is None,
                    reason="in-tree qdistro resolver not present")
def test_class_enabled_against_real_resolver(monkeypatch):
    """End-to-end against the REAL shipped resolver + registry (no mock): the
    enabled text/plain class probes True, the DISABLED pdf class probes False,
    and an unknown class probes False. Catches resolver/registry drift."""
    monkeypatch.setenv("QDISTRO_DISPOSABLE_CLASSES_RESOLVER", str(_REAL_RESOLVER))
    monkeypatch.setenv("QDISTRO_DISPOSABLE_CLASSES", str(_REAL_REGISTRY))
    assert oid.class_enabled("text/plain") is True
    assert oid.class_enabled("pdf") is False
    assert oid.class_enabled("not-a-real-class") is False


# ---------------------------------------------------------------------------
# get_menu_items — visibility is fail-closed at every gate
# ---------------------------------------------------------------------------

def _plugin():
    return oid.OpenInDisposablePlugin()


def test_menu_hidden_without_sdk(monkeypatch, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr(oid, "_sdk", lambda: None)
    assert _plugin().get_menu_items(str(f)) == []


def test_menu_hidden_for_empty_path(monkeypatch):
    monkeypatch.setattr(oid, "_sdk", lambda: SimpleNamespace())
    assert _plugin().get_menu_items("") == []


def test_menu_hidden_for_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(oid, "_sdk", lambda: SimpleNamespace())
    monkeypatch.setattr(oid, "class_enabled", lambda _c: True)
    assert _plugin().get_menu_items(str(tmp_path)) == []


def test_menu_hidden_for_unmapped_type(monkeypatch, tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"\x00")
    monkeypatch.setattr(oid, "_sdk", lambda: SimpleNamespace())
    monkeypatch.setattr(oid, "class_enabled", lambda _c: True)
    assert _plugin().get_menu_items(str(f)) == []


def test_menu_hidden_when_class_disabled(monkeypatch, tmp_path):
    """Type maps to a class, but the probe says disabled → no item."""
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr(oid, "_sdk", lambda: SimpleNamespace())
    monkeypatch.setattr(oid, "class_enabled", lambda _c: False)
    assert _plugin().get_menu_items(str(f)) == []


def test_menu_shows_item_when_mapped_and_enabled(monkeypatch, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr(oid, "_sdk", lambda: SimpleNamespace())
    monkeypatch.setattr(oid, "class_enabled", lambda _c: True)
    items = _plugin().get_menu_items(str(f))
    assert len(items) == 1
    label, cb = items[0]
    assert label == "Open in Disposable"
    assert callable(cb)


# ---------------------------------------------------------------------------
# _open — calls the SDK with the exact path + resolved class, re-resolved,
# and surfaces failures as a dialog (never raises)
# ---------------------------------------------------------------------------

def test_open_calls_sdk_with_exact_path_and_resolved_class(monkeypatch, tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hi")
    captured = {}

    def fake_open(path, *, class_name, **k):
        captured["path"] = path
        captured["class_name"] = class_name
        return {"CONTAINER": "disp-x"}

    monkeypatch.setattr(oid, "_sdk",
                        lambda: SimpleNamespace(open_in_disposable=fake_open))
    _plugin()._open(str(f))
    # The path is passed absolute (the SDK requires it; tmp_path is already
    # absolute so this is os.path.abspath of the same file).
    assert captured["path"] == os.path.abspath(str(f)) == str(f)
    assert captured["class_name"] == oid.TEXT_CLASS  # exact resolved literal


def test_open_reresolves_on_click_no_cached_class(monkeypatch, tmp_path):
    """_open does not trust a menu-time class — it re-resolves from the path. A
    path that resolves to None at click → no SDK call, a clean warning."""
    binf = tmp_path / "a.pdf"
    binf.write_bytes(b"\x00")
    called = {"n": 0}

    def fake_open(*a, **k):  # pragma: no cover - must not be called
        called["n"] += 1

    monkeypatch.setattr(oid, "_sdk",
                        lambda: SimpleNamespace(open_in_disposable=fake_open))
    warned = {}
    monkeypatch.setattr(oid.OpenInDisposablePlugin, "_warn",
                        staticmethod(lambda m: warned.setdefault("msg", m)))
    _plugin()._open(str(binf))
    assert called["n"] == 0
    assert "no enabled disposable class" in warned["msg"]


def test_open_warns_when_sdk_absent_at_click(monkeypatch, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr(oid, "_sdk", lambda: None)
    warned = {}
    monkeypatch.setattr(oid.OpenInDisposablePlugin, "_warn",
                        staticmethod(lambda m: warned.setdefault("msg", m)))
    _plugin()._open(str(f))
    assert "not available" in warned["msg"]


def test_open_surfaces_sdk_error_as_warning_without_raising(monkeypatch, tmp_path):
    """An OpenInDisposableError (broker refusal, disabled class, binary fail) is
    shown to the user, not raised."""
    f = tmp_path / "a.txt"
    f.write_text("x")

    class _SdkError(RuntimeError):
        pass

    def fake_open(path, *, class_name, **k):
        raise _SdkError("broker has no allow rule ... decision=unknown")

    monkeypatch.setattr(oid, "_sdk",
                        lambda: SimpleNamespace(open_in_disposable=fake_open))
    warned = {}
    monkeypatch.setattr(oid.OpenInDisposablePlugin, "_warn",
                        staticmethod(lambda m: warned.setdefault("msg", m)))
    # Must not raise.
    _plugin()._open(str(f))
    assert "Could not open in disposable" in warned["msg"]
    assert "decision=unknown" in warned["msg"]


# ---------------------------------------------------------------------------
# Discovery contract — the plugin is a well-formed MenuProvider
# ---------------------------------------------------------------------------

def test_plugin_is_menu_provider():
    from qfileman.plugin import MenuProvider
    p = _plugin()
    assert isinstance(p, MenuProvider)
    assert "menu_provider" in p.capabilities
    assert p.category == "Tools"
    assert p.name == "open_in_disposable"

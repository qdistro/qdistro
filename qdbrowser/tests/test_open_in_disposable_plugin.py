"""Unit tests for the qdbrowser open-in-disposable command plugin.

Pure routing logic (URL eligibility, the enablement probe) and the click
handler are exercised without Qt or a real disposable — the plugin is a thin
GUI consumer of the SHIPPED SDK, and the SDK + trusted binary are the security
boundary (proven in-VM by disp-open-probe.sh). These tests pin the GUI-side
contract: fail-closed eligibility, a fixed class literal, and the exact SDK
call shape.
"""

from __future__ import annotations

import subprocess
import types

import pytest
from qdbrowser.plugins import open_in_disposable as mod
from qdbrowser.plugins.open_in_disposable import (
    URL_PREVIEW_CLASS,
    OpenInDisposablePlugin,
    resolve_url_class,
)


# --------------------------------------------------------------------------
# resolve_url_class — fixed-literal, fail-closed eligibility.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://example.com",
    "https://example.com/path?q=1#frag",
    "https://sub.example.co.uk:8443/a/b",
    "  https://example.com  ",            # surrounding whitespace tolerated
])
def test_http_urls_map_to_the_preview_class(url):
    assert resolve_url_class(url) == URL_PREVIEW_CLASS


@pytest.mark.parametrize("url", [
    "",
    None,
    "file:///etc/passwd",
    "data:text/html,<script>alert(1)</script>",
    "javascript:alert(document.cookie)",
    "about:blank",
    "blob:https://example.com/uuid",
    "ftp://host/file",
    "http://",                            # scheme but no host
    "https:///path",                      # no host
    "not a url",
    "chrome://settings",
])
def test_ineligible_urls_resolve_to_none(url):
    assert resolve_url_class(url) is None


def test_non_str_is_rejected():
    assert resolve_url_class(b"http://example.com") is None  # type: ignore[arg-type]
    assert resolve_url_class(1234) is None                   # type: ignore[arg-type]


def test_class_literal_is_never_url_derived():
    # A hostile host/path can only ever yield the one known-safe literal.
    assert resolve_url_class("http://evil;rm -rf/.example/url-preview-pwned") \
        == URL_PREVIEW_CLASS


# --------------------------------------------------------------------------
# class_enabled — bounded, fail-closed, cached probe.
# --------------------------------------------------------------------------
def _fake_run(returncode=0, raises=None):
    def run(*_a, **_k):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args=[], returncode=returncode)
    return run


def test_class_enabled_true_on_resolver_exit0(monkeypatch):
    monkeypatch.setattr(mod.subprocess, "run", _fake_run(0))
    assert mod.class_enabled("c-enabled", _cache={}) is True


def test_class_enabled_false_on_nonzero(monkeypatch):
    monkeypatch.setattr(mod.subprocess, "run", _fake_run(4))  # disabled (min_tier)
    assert mod.class_enabled("c-disabled", _cache={}) is False


@pytest.mark.parametrize("exc", [
    FileNotFoundError("no resolver"),
    subprocess.TimeoutExpired(cmd="x", timeout=5),
    OSError("boom"),
])
def test_class_enabled_fail_closed_on_error(monkeypatch, exc):
    monkeypatch.setattr(mod.subprocess, "run", _fake_run(raises=exc))
    assert mod.class_enabled("c", _cache={}) is False


def test_class_enabled_is_cached(monkeypatch):
    calls = {"n": 0}

    def run(*_a, **_k):
        calls["n"] += 1
        return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", run)
    cache: dict = {}
    assert mod.class_enabled("c", _cache=cache) is True
    assert mod.class_enabled("c", _cache=cache) is True
    assert calls["n"] == 1  # probed once, then served from cache


# --------------------------------------------------------------------------
# get_commands — fail-closed menu visibility.
# --------------------------------------------------------------------------
class _FakeWebview:
    def __init__(self, url):
        self._url = url

    def url(self):
        return self._url


class _FakeWindow:
    def __init__(self, url):
        self._active_webview = _FakeWebview(url)
        self.notified: list[str] = []

    def notify(self, message):
        self.notified.append(message)


# Distinct sentinel stub object shared by callers that don't pass an explicit
# sdk (module-level singleton avoids a B008 function-call-in-default).
_DEFAULT_SDK = object()


def _plugin_with(monkeypatch, *, sdk=_DEFAULT_SDK, enabled=True):
    monkeypatch.setattr(mod, "_sdk", lambda: sdk)
    monkeypatch.setattr(mod, "class_enabled", lambda *_a, **_k: enabled)
    return OpenInDisposablePlugin()


def test_command_shown_for_eligible_url(monkeypatch):
    plug = _plugin_with(monkeypatch)
    cmds = plug.get_commands(_FakeWindow("https://example.com"))
    assert len(cmds) == 1
    label, cb = cmds[0]
    assert "disposable" in label.lower()
    assert callable(cb)


def test_command_hidden_for_ineligible_url(monkeypatch):
    plug = _plugin_with(monkeypatch)
    assert plug.get_commands(_FakeWindow("file:///etc/passwd")) == []


def test_command_hidden_when_no_sdk(monkeypatch):
    plug = _plugin_with(monkeypatch, sdk=None)
    assert plug.get_commands(_FakeWindow("https://example.com")) == []


def test_command_hidden_when_class_disabled(monkeypatch):
    plug = _plugin_with(monkeypatch, enabled=False)
    assert plug.get_commands(_FakeWindow("https://example.com")) == []


def test_command_hidden_when_no_webview(monkeypatch):
    plug = _plugin_with(monkeypatch)

    class _NoView:
        _active_webview = None
    assert plug.get_commands(_NoView()) == []


def test_webview_url_raising_does_not_break_palette(monkeypatch):
    plug = _plugin_with(monkeypatch)

    class _BadView:
        def url(self):
            raise RuntimeError("webview gone")

    class _W:
        _active_webview = _BadView()
    assert plug.get_commands(_W()) == []


# --------------------------------------------------------------------------
# _preview / _stage_url — the click handler calls the SDK correctly.
# --------------------------------------------------------------------------
def _fake_sdk():
    calls = []
    sdk = types.SimpleNamespace()

    def open_in_disposable(path, *, class_name, **kw):
        calls.append((path, class_name, kw))
        return {"LAUNCH_TOKEN": "deadbeef" * 4, "CONTAINER": "disp-x"}

    sdk.open_in_disposable = open_in_disposable
    sdk._calls = calls
    return sdk


def test_preview_calls_sdk_with_staged_file_and_class(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sdk = _fake_sdk()
    monkeypatch.setattr(mod, "_sdk", lambda: sdk)
    monkeypatch.setattr(mod, "class_enabled", lambda *_a, **_k: True)

    plug = OpenInDisposablePlugin()
    win = _FakeWindow("https://example.com/page")
    plug.activate(win)
    label, cb = plug.get_commands(win)[0]
    cb()

    assert len(sdk._calls) == 1
    path, class_name, _kw = sdk._calls[0]
    assert class_name == URL_PREVIEW_CLASS
    # The staged file is a real, readable file holding exactly the URL.
    with open(path, encoding="utf-8") as fh:
        assert fh.read().strip() == "https://example.com/page"
    # ...inside the per-user runtime dir, named so the RO bind lands at /mnt/input/url.
    assert str(tmp_path) in path
    assert path.endswith("/url")


def test_preview_surfaces_sdk_refusal_without_raising(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    class _OpenErr(Exception):
        pass

    sdk = types.SimpleNamespace()

    def open_in_disposable(path, *, class_name, **kw):
        raise _OpenErr("broker refused: qdistro.dispose.open denied")

    sdk.open_in_disposable = open_in_disposable
    monkeypatch.setattr(mod, "_sdk", lambda: sdk)

    plug = OpenInDisposablePlugin()
    win = _FakeWindow("https://example.com")
    plug.activate(win)
    plug._preview("https://example.com", URL_PREVIEW_CLASS)  # must not raise
    assert any("refused" in m.lower() for m in win.notified)


def test_preview_notifies_when_sdk_vanishes(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "_sdk", lambda: None)
    plug = OpenInDisposablePlugin()
    win = _FakeWindow("https://example.com")
    plug.activate(win)
    plug._preview("https://example.com", URL_PREVIEW_CLASS)
    assert any("unavailable" in m.lower() for m in win.notified)


def test_deactivate_cleans_staged_dirs(monkeypatch, tmp_path):
    import os
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    plug = OpenInDisposablePlugin()
    p = plug._stage_url("https://example.com")
    assert os.path.exists(p)
    staged_dir = os.path.dirname(p)
    plug.deactivate()
    assert not os.path.exists(staged_dir)

"""Reader mode plugin."""

import json
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest


def test_plugin_has_command(window):
    plug = window.plugins._instances["reader_mode"]
    cmds = plug.get_commands(window)
    assert any("reader" in label.lower() for label, _ in cmds)


def test_toggle_with_none_safe(window):
    plug = window.plugins._instances["reader_mode"]
    plug.toggle(None)  # must not raise


def test_toggle_invokes_runjs(window):
    plug = window.plugins._instances["reader_mode"]
    wv = window._active_webview
    wv.view.page().runJavaScript = MagicMock()
    plug.toggle(wv)
    wv.view.page().runJavaScript.assert_called_once()
    arg = wv.view.page().runJavaScript.call_args[0][0]
    # Sandboxed-iframe overlay markers.
    assert "__qdb_reader_overlay" in arg
    assert "iframe" in arg
    assert "sandbox" in arg
    assert "#f4ecd8" not in arg
    assert "var(--qdb-bg)" in arg


def test_restyle_js_updates_variables_without_toggling():
    from qdbrowser.plugins.reader_mode import _build_restyle_js
    from qdbrowser.theme import overlay_palette

    js = _build_restyle_js("dark")
    p = overlay_palette("dark")
    assert "__COLORS__" not in js
    assert "__qdb_reader_overlay" in js
    assert "setProperty" in js
    assert p["bg"] in js
    assert p["accent"] in js
    assert "mode:'off'" not in js
    assert "removed:true" not in js
    assert "srcdoc" not in js
    assert "pickRoot" not in js
    assert "location.reload" not in js
    assert "innerText" not in js


def test_restyle_does_not_reextract(window):
    from qdbrowser.plugins.reader_mode import _build_inject_js

    plug = window.plugins._instances["reader_mode"]
    wv = window._active_webview
    wv.view.page().runJavaScript = MagicMock()
    plug.restyle_overlays([wv])
    wv.view.page().runJavaScript.assert_called_once()
    js = wv.view.page().runJavaScript.call_args[0][0]
    assert "__qdb_reader_overlay" in js
    assert "pickRoot" not in js
    assert "srcdoc" not in js
    inject = _build_inject_js("dark")
    assert "pickRoot" in inject
    assert js != inject


def test_inject_and_restyle_reject_non_hex(monkeypatch):
    from qdbrowser import theme as theme_mod
    from qdbrowser.plugins.reader_mode import _build_inject_js, _build_restyle_js

    monkeypatch.setattr(theme_mod, "palette_dict", lambda mode="auto": {
        "bg": "red; } * { x:expression(alert(1))",
        "bg_mid": "javascript:alert(1)",
        "fg": "#gggggg",
        "border": "url(https://evil.example/x)",
        "accent": "expression(alert(1))",
    })
    p = theme_mod.overlay_palette("auto")
    assert p["bg"] == "#1e1e1e"
    assert p["fg"] == "#d4d4d4"
    assert p["accent"] == "#3d8fd4"
    inject = _build_inject_js()
    restyle = _build_restyle_js()
    for js in (inject, restyle):
        assert "javascript:" not in js
        assert "expression(" not in js
        assert "url(" not in js
        assert "#1e1e1e" in js
        assert "#3d8fd4" in js


_READER_RESTYLE_HARNESS = r"""
const article = "Keep this article";
const iframe = {
  id: "__qdb_reader_overlay",
  srcdoc: "<p>Keep this article</p>",
  style: {
    _props: {
      "--qdb-bg": "#f4ecd8",
      "--qdb-fg": "#222222",
      "--qdb-accent": "#444444",
    },
    background: "#f4ecd8",
    setProperty(name, value) { this._props[name] = value; },
  },
  contentDocument: {
    documentElement: {
      style: {
        _props: {
          "--qdb-bg": "#f4ecd8",
          "--qdb-fg": "#222222",
          "--qdb-accent": "#444444",
        },
        setProperty(name, value) { this._props[name] = value; },
      },
    },
    body: { textContent: article },
  },
};
const same = iframe;
globalThis.document = {
  getElementById(id) {
    return id === "__qdb_reader_overlay" ? iframe : null;
  },
};
const result = __RESTYLE__;
const root = iframe.contentDocument.documentElement.style._props;
process.stdout.write(JSON.stringify({
  result,
  sameIframe: iframe === same,
  article: iframe.contentDocument.body.textContent,
  srcdoc: iframe.srcdoc,
  rootBg: root["--qdb-bg"],
  rootFg: root["--qdb-fg"],
  rootAccent: root["--qdb-accent"],
  outerBg: iframe.style._props["--qdb-bg"],
  outerBackground: iframe.style.background,
}));
"""


def _run_reader_restyle_fixture(restyle_js: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the reader restyle fixture")
    harness = _READER_RESTYLE_HARNESS.replace("__RESTYLE__", restyle_js)
    proc = subprocess.run(
        [node, "--input-type=commonjs", "-e", harness],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout or "node restyle fixture failed")
    return json.loads(proc.stdout)


def test_restyle_script_updates_iframe_document_root():
    from qdbrowser.plugins.reader_mode import _build_restyle_js
    from qdbrowser.theme import overlay_palette

    colors = overlay_palette("dark")
    assert colors["bg"] != "#f4ecd8"
    out = _run_reader_restyle_fixture(_build_restyle_js("dark"))
    assert out["sameIframe"] is True
    assert out["article"] == "Keep this article"
    assert out["srcdoc"] == "<p>Keep this article</p>"
    assert out["result"] == {"ok": True, "restyled": True}
    assert out["rootBg"] == colors["bg"]
    assert out["rootFg"] == colors["fg"]
    assert out["rootAccent"] == colors["accent"]
    assert out["outerBg"] == colors["bg"]
    assert out["outerBackground"] == colors["bg"]


def test_restyle_document_root_assignments_are_required():
    from qdbrowser.plugins.reader_mode import _build_restyle_js
    from qdbrowser.theme import overlay_palette

    colors = overlay_palette("dark")
    stripped = _build_restyle_js("dark")
    for prop, key in (
        ("--qdb-bg", "bg"),
        ("--qdb-fg", "fg"),
        ("--qdb-accent", "accent"),
    ):
        stripped = stripped.replace(
            f"doc.documentElement.style.setProperty('{prop}', colors.{key});",
            "",
        )
    assert "doc.documentElement.style.setProperty" not in stripped
    out = _run_reader_restyle_fixture(stripped)
    assert out["sameIframe"] is True
    assert out["article"] == "Keep this article"
    assert out["rootBg"] == "#f4ecd8"
    assert out["rootFg"] == "#222222"
    assert out["rootAccent"] == "#444444"
    assert out["rootBg"] != colors["bg"]

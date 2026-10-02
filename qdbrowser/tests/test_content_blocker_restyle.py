"""Live restyle of injected EasyList cosmetic CSS without reload."""

from __future__ import annotations

import json
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest
from qdbrowser.plugins.content_blocker import (
    ContentBlockerPlugin,
    _build_inject_js,
    _build_restyle_js,
    _CosmeticRule,
)

_PARCHMENT = {
    "--qdb-bg": "#f4ecd8",
    "--qdb-fg": "#222222",
    "--qdb-bg-mid": "#e8dcc8",
    "--qdb-border": "#444444",
    "--qdb-accent": "#444444",
}

_COSMETIC_RESTYLE_HARNESS = r"""
const hide = ".keep-me { display: none !important; }";
const parchmentRoot =
  ":root{--qdb-bg:#f4ecd8;--qdb-fg:#222222;--qdb-bg-mid:#e8dcc8;"
  + "--qdb-border:#444444;--qdb-accent:#444444}";
const styleEl = {
  id: "__qdb_cosmetic",
  textContent: parchmentRoot + hide,
};
const same = styleEl;
const created = [];
const rootStyle = {
  _props: Object.assign({}, {
    "--qdb-bg": "#f4ecd8",
    "--qdb-fg": "#222222",
    "--qdb-bg-mid": "#e8dcc8",
    "--qdb-border": "#444444",
    "--qdb-accent": "#444444",
  }),
  setProperty(name, value) { this._props[name] = value; },
};
globalThis.document = {
  documentElement: {
    style: rootStyle,
    appendChild(el) { created.push(el); },
  },
  createElement(tag) {
    created.push(tag);
    return { id: "", textContent: "" };
  },
  getElementById(id) {
    return id === "__qdb_cosmetic" ? styleEl : null;
  },
};
const result = __RESTYLE__;
process.stdout.write(JSON.stringify({
  result,
  sameStyle: styleEl === same,
  created,
  css: styleEl.textContent,
  hideKept: styleEl.textContent.indexOf(hide) !== -1,
  rootBg: rootStyle._props["--qdb-bg"],
  rootFg: rootStyle._props["--qdb-fg"],
  rootAccent: rootStyle._props["--qdb-accent"],
}));
"""


def _run_cosmetic_restyle_fixture(restyle_js: str) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the cosmetic restyle fixture")
    harness = _COSMETIC_RESTYLE_HARNESS.replace("__RESTYLE__", restyle_js)
    proc = subprocess.run(
        [node, "--input-type=commonjs", "-e", harness],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            proc.stderr or proc.stdout or "node restyle fixture failed")
    return json.loads(proc.stdout)


def test_restyle_js_updates_variables_without_reinject():
    from qdbrowser.theme import overlay_palette

    js = _build_restyle_js("dark")
    p = overlay_palette("dark")
    assert "__COLORS__" not in js
    assert "__qdb_cosmetic" in js
    assert "setProperty" in js
    assert p["bg"] in js
    assert "createElement" not in js
    assert "appendChild" not in js
    assert "injected:true" not in js
    assert "location.reload" not in js
    assert "location.href" not in js
    assert "payload.css" not in js


def test_inject_js_creates_stylesheet_with_validated_colors():
    from qdbrowser.theme import overlay_palette

    css = ".ad { display: none !important; }"
    js = _build_inject_js(css, "dark")
    p = overlay_palette("dark")
    assert "__PAYLOAD__" not in js
    assert css in js
    assert p["bg"] in js
    assert "__qdb_cosmetic" in js
    assert "createElement" in js


def test_inject_and_restyle_reject_non_hex(monkeypatch):
    from qdbrowser import theme as theme_mod

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
    inject = _build_inject_js(".ad { display: none !important; }")
    restyle = _build_restyle_js()
    for js in (inject, restyle):
        assert "javascript:" not in js
        assert "expression(" not in js
        assert "url(" not in js
        assert "#1e1e1e" in js


def test_restyle_does_not_bump_stats_or_intercept(fresh_config, window):
    plug = window.plugins._instances["content_blocker"]
    plug._cosmetic_rules = [_CosmeticRule(None, ".ad")]
    plug.intercept = MagicMock()
    before = plug.stats["cosmetic_hidden"]
    wv = window._active_webview
    wv.view.page().runJavaScript = MagicMock()
    plug.restyle_overlays([wv])
    wv.view.page().runJavaScript.assert_called_once()
    js = wv.view.page().runJavaScript.call_args[0][0]
    assert "__qdb_cosmetic" in js
    assert "createElement" not in js
    assert "injected:true" not in js
    assert plug.stats["cosmetic_hidden"] == before
    plug.intercept.assert_not_called()


def test_on_load_finished_injects_and_bumps(fresh_config):
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug._cosmetic_rules = [_CosmeticRule(None, ".ad")]
    wv = MagicMock()
    wv.url.return_value = "https://news.site/page"
    wv.view.page().runJavaScript = MagicMock()
    before = plug.stats["cosmetic_hidden"]
    plug.on_load_finished(wv, True)
    wv.view.page().runJavaScript.assert_called_once()
    js = wv.view.page().runJavaScript.call_args[0][0]
    assert "__qdb_cosmetic" in js
    assert "createElement" in js
    assert ".ad" in js
    assert plug.stats["cosmetic_hidden"] == before + 1


def test_restyle_script_updates_document_root_and_keeps_hide_selectors():
    from qdbrowser.theme import overlay_palette

    colors = overlay_palette("dark")
    assert colors["bg"] != _PARCHMENT["--qdb-bg"]
    out = _run_cosmetic_restyle_fixture(_build_restyle_js("dark"))
    assert out["sameStyle"] is True
    assert out["created"] == []
    assert out["hideKept"] is True
    assert ".keep-me { display: none !important; }" in out["css"]
    assert out["result"] == {"ok": True, "restyled": True}
    assert out["rootBg"] == colors["bg"]
    assert out["rootFg"] == colors["fg"]
    assert out["rootAccent"] == colors["accent"]
    assert f"--qdb-bg:{colors['bg']}" in out["css"]
    assert _PARCHMENT["--qdb-bg"] not in out["css"]


def test_restyle_document_root_assignments_are_required():
    from qdbrowser.theme import overlay_palette

    colors = overlay_palette("dark")
    stripped = _build_restyle_js("dark")
    for prop, key in (
        ("--qdb-bg", "bg"),
        ("--qdb-fg", "fg"),
        ("--qdb-bg-mid", "bg_mid"),
        ("--qdb-border", "border"),
        ("--qdb-accent", "accent"),
    ):
        stripped = stripped.replace(
            f"root.style.setProperty('{prop}', colors.{key});",
            "",
        )
    assert "root.style.setProperty" not in stripped
    out = _run_cosmetic_restyle_fixture(stripped)
    assert out["sameStyle"] is True
    assert out["hideKept"] is True
    assert out["rootBg"] == _PARCHMENT["--qdb-bg"]
    assert out["rootFg"] == _PARCHMENT["--qdb-fg"]
    assert out["rootAccent"] == _PARCHMENT["--qdb-accent"]
    assert out["rootBg"] != colors["bg"]


def test_restyle_style_element_root_rewrite_is_required():
    from qdbrowser.theme import overlay_palette

    colors = overlay_palette("dark")
    stripped = _build_restyle_js("dark")
    stripped = stripped.replace(
        "el.textContent=prefix+css.replace(/^:root\\{[^}]*\\}/, '');",
        "",
    )
    assert "el.textContent=prefix" not in stripped
    out = _run_cosmetic_restyle_fixture(stripped)
    assert out["sameStyle"] is True
    assert out["hideKept"] is True
    assert f"--qdb-bg:{_PARCHMENT['--qdb-bg']}" in out["css"]
    assert f"--qdb-bg:{colors['bg']}" not in out["css"]

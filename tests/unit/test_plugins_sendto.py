"""Smoke-tests for the two Phase-4 send-to plugins.

These plugins live in `plugins/` and at runtime are
dropped into qterminator's (and qnotebook's) plugin discovery
paths. On the *host* we can't rely on qterminator or zim_qt being
installed, so the tests stub out the bits the plugins need to
import (just the two plugin base classes and the Qt imports the
plugins actually touch).

What we cover here is structural: does the file parse, does it
declare the right surface, do the menu-provider helpers return
something non-empty, does the receiver init happen on activate.
The real semantic assertions — "the terminal sees the text",
"the notebook page gets appended to" — are in the in-VM GUI
scenarios 15-17.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


pytest.importorskip("PyQt6.QtWidgets")
pytest.importorskip("dbus")


# --- shared setup ---------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[2]
_SDK = _ROOT / "sdk"
_PLUGINS = _ROOT / "plugins"

# qdistro_app must be importable for the plugins.
sys.path.insert(0, str(_SDK))


def _stub_qterminator_plugin():
    """Install a fake `qterminator.plugin` module that mirrors the
    on-disk API (Plugin + MenuProvider classes) so the plugin file
    can import without a real qterminator install.
    """
    if "qterminator.plugin" in sys.modules:
        return
    pkg = types.ModuleType("qterminator")
    mod = types.ModuleType("qterminator.plugin")

    class Plugin:
        name = "unnamed"
        description = ""
        version = "0.0"
        capabilities: list[str] = []

        def activate(self, app_controller):
            pass

        def deactivate(self):
            pass

    class MenuProvider(Plugin):
        capabilities = ["menu_provider"]
        category = "Plugins"

        def get_menu_items(self, terminal):
            return []

    mod.Plugin = Plugin
    mod.MenuProvider = MenuProvider
    pkg.plugin = mod
    sys.modules["qterminator"] = pkg
    sys.modules["qterminator.plugin"] = mod


def _load_plugin_module(path: Path, name: str):
    """Load a plugin file by absolute path into a uniquely named
    module — bypasses the fact that it's not on the normal import
    path."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _qapp():
    """Qt classes instantiated below need an app. Offscreen avoids any
    dependency on a running display."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# --- qterminator plugin ---------------------------------------------------

class TestQterminatorPlugin:
    def test_imports_and_exposes_plugin_class(self):
        _stub_qterminator_plugin()
        mod = _load_plugin_module(
            _PLUGINS / "qdistro_sendto_qterminator.py",
            "qdistro_sendto_qterminator_test",
        )
        assert hasattr(mod, "QdistroSendToPlugin")
        # Metadata the plugin system reads:
        inst = mod.QdistroSendToPlugin()
        assert inst.name == "qdistro_sendto"
        assert inst.category == "Plugins"
        assert "menu_provider" in inst.capabilities

    def test_get_menu_items_returns_one_entry(self):
        _stub_qterminator_plugin()
        mod = _load_plugin_module(
            _PLUGINS / "qdistro_sendto_qterminator.py",
            "qdistro_sendto_qterminator_test2",
        )
        inst = mod.QdistroSendToPlugin()
        # Not activated — still returns the single entry because the
        # menu item itself only needs qdistro_app at click-time.
        items = inst.get_menu_items(terminal=object())
        assert len(items) == 1
        label, cb = items[0]
        assert "Send" in label
        assert callable(cb)

    def test_service_name_follows_convention(self):
        # Service name format is part of the protocol surface — changing
        # it breaks broker.ListReceivers friendly-name extraction and
        # peer lookups in scenarios 15-17.
        _stub_qterminator_plugin()
        mod = _load_plugin_module(
            _PLUGINS / "qdistro_sendto_qterminator.py",
            "qdistro_sendto_qterminator_test3",
        )
        assert mod.SERVICE_FMT.startswith("com.qdistro.Qterminator.uid")
        assert "{uid}" in mod.SERVICE_FMT


# --- qnotebook plugin -----------------------------------------------------

class TestQnotebookPlugin:
    def test_imports_and_exposes_Plugin_class(self):
        mod = _load_plugin_module(
            _PLUGINS / "qdistro_sendto_qnotebook.py",
            "qdistro_sendto_qnotebook_test",
        )
        # zim_qt.plugins.discover requires a class literally named
        # `Plugin` with name + description + setup.
        assert hasattr(mod, "Plugin")
        inst = mod.Plugin()
        assert inst.name
        assert inst.description
        assert callable(inst.setup)

    def test_service_name_follows_convention(self):
        mod = _load_plugin_module(
            _PLUGINS / "qdistro_sendto_qnotebook.py",
            "qdistro_sendto_qnotebook_test2",
        )
        assert mod.SERVICE_FMT.startswith("com.qdistro.Qnotebook.uid")
        assert "{uid}" in mod.SERVICE_FMT

    def test_setup_without_qdistro_ok_is_noop(self, monkeypatch):
        # Simulate a qdistro-less install (plugin file copied to a
        # zim_qt that doesn't have qdistro_app on the pythonpath). The
        # plugin must not blow up; it should log and return.
        mod = _load_plugin_module(
            _PLUGINS / "qdistro_sendto_qnotebook.py",
            "qdistro_sendto_qnotebook_test3",
        )
        monkeypatch.setattr(mod, "_QDISTRO_OK", False)
        monkeypatch.setattr(mod, "_IMPORT_ERR", "fake import failure")
        inst = mod.Plugin()
        # A stand-in window that would blow up if touched further.
        class _W: pass
        inst.setup(_W())  # must return silently
        assert inst._receiver is None

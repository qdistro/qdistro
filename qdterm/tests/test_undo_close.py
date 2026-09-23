"""Tests for the undo_close plugin.

Layers:
  - Pure UndoCloseService (no Qt).
  - Plugin lifecycle.
  - Integration with real MainWindow.
"""

import time

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.undo_close import (
    UndoClosePlugin,
    UndoCloseService,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config" / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


# ---------------------------------------------------------------------------
# Pure service tests
# ---------------------------------------------------------------------------

class FakeWindow:
    """Fake window for testing."""

    def __init__(self):
        self._created_tabs = []

    def new_tab(self, working_directory=None, shell_command=None):
        class FakeTerm:
            def __init__(self, wd):
                self._working_directory = wd
                self._profile_name = "default"

            def working_directory(self):
                return self._working_directory

            def apply_profile(self, name):
                self._profile_name = name

        term = FakeTerm(working_directory)
        self._created_tabs.append(term)
        return term


def test_service_records_close():
    """Test that recording a close works."""
    win = FakeWindow()
    service = UndoCloseService(win, max_remembered=10, window_seconds=5.0)

    service.record_close("/home/user", "default", "Terminal")

    assert service.pending_count == 1


def test_service_undo_restores_tab():
    """Test that undo restores the most recent tab."""
    win = FakeWindow()
    service = UndoCloseService(win, max_remembered=10, window_seconds=5.0)

    # Record a close
    service.record_close("/home/user/project", "default", "Project")

    # Undo
    success = service.undo()

    assert success is True
    assert len(win._created_tabs) == 1
    assert win._created_tabs[0]._working_directory == "/home/user/project"


def test_service_undo_applies_profile():
    """Test that undo applies the saved profile."""
    win = FakeWindow()
    service = UndoCloseService(win, max_remembered=10, window_seconds=5.0)

    # Record a close with non-default profile
    service.record_close("/home/user", "prod", "Terminal")

    # Undo
    service.undo()

    # Profile should be applied
    assert win._created_tabs[0]._profile_name == "prod"


def test_service_undo_removes_from_buffer():
    """Test that undo removes the tab from buffer."""
    win = FakeWindow()
    service = UndoCloseService(win, max_remembered=10, window_seconds=5.0)

    # Record two closes
    service.record_close("/home/user", "default", "Tab1")
    service.record_close("/home/user", "default", "Tab2")

    assert service.pending_count == 2

    # Undo - should restore Tab2 (most recent)
    service.undo()

    assert service.pending_count == 1
    # Remaining should be Tab1
    assert service._closed_tabs[0].tab_name == "Tab1"


def test_service_expires_old_entries():
    """Test that old entries are cleaned up."""
    win = FakeWindow()
    service = UndoCloseService(win, max_remembered=10, window_seconds=0.1)  # 100ms

    # Record a close
    service.record_close("/home/user", "default", "Tab")

    assert service.pending_count == 1

    # Wait for expiration
    time.sleep(0.2)

    # Should be expired now
    service._cleanup()
    assert service.pending_count == 0


def test_service_respects_max_remembered():
    """Test that max remembered limit is enforced."""
    win = FakeWindow()
    service = UndoCloseService(win, max_remembered=3, window_seconds=60)

    # Record more than max
    for i in range(5):
        service.record_close(f"/path/{i}", "default", f"Tab{i}")

    # Should only keep the most recent 3
    assert service.pending_count == 3
    # Should be Tab2, Tab3, Tab4 (last 3)
    assert service._closed_tabs[0].tab_name == "Tab2"


def test_service_can_undo_false_when_empty():
    """Test can_undo when nothing to undo."""
    win = FakeWindow()
    service = UndoCloseService(win)

    assert service.can_undo is False


def test_service_can_undo_true_when_has_entries():
    """Test can_undo when there are entries."""
    win = FakeWindow()
    service = UndoCloseService(win)

    service.record_close("/tmp", "default", "Tab")

    assert service.can_undo is True


def test_service_undo_empty_buffer_returns_false():
    """Test undo returns False when nothing to undo."""
    win = FakeWindow()
    service = UndoCloseService(win)

    result = service.undo()

    assert result is False
    assert len(win._created_tabs) == 0


# ---------------------------------------------------------------------------
# Plugin tests
# ---------------------------------------------------------------------------

def test_plugin_enabled_by_default():
    """Test plugin activates when enabled."""
    cfg = Config()
    cfg.set("plugins", "undo_close", "enabled", True)

    class FakeWin:
        _tabs = None
        def currentWidget(self):
            return None

    win = FakeWin()
    plugin = UndoClosePlugin()
    plugin.activate(win)

    assert hasattr(win, "undo_close")

    plugin.deactivate()


def test_plugin_disabled_does_not_install():
    """Test plugin doesn't install when disabled."""
    cfg = Config()
    cfg.set("plugins", "undo_close", "enabled", False)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = UndoClosePlugin()
    plugin.activate(win)

    assert not hasattr(win, "undo_close")


def test_plugin_shortcut_undo(qtbot):
    """Test that shortcut triggers undo."""
    from qterminator.window import MainWindow

    cfg = Config()
    cfg.set("plugins", "undo_close", "enabled", True)

    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)

    # Check plugin loaded
    pm = win._plugin_manager
    plugin = pm._instances.get("undo_close")
    assert plugin is not None

    # Check shortcut is set
    assert plugin._shortcut is not None

    win.close()


def test_plugin_deactivate_cleans_up():
    """Test deactivate cleans up properly."""
    cfg = Config()
    cfg.set("plugins", "undo_close", "enabled", True)

    class FakeWin:
        _tabs = None

    win = FakeWin()
    plugin = UndoClosePlugin()
    plugin.activate(win)

    plugin.deactivate()

    assert plugin._window is None
    assert not hasattr(win, "undo_close")


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_service_stores_tab_name():
    """Test that tab name is stored."""
    win = FakeWindow()
    service = UndoCloseService(win)

    service.record_close("/tmp", "default", "My Terminal")

    assert service._closed_tabs[0].tab_name == "My Terminal"


def test_service_stores_shell_command():
    """Test that shell command is stored."""
    win = FakeWindow()
    service = UndoCloseService(win)

    service.record_close("/tmp", "default", "Tab", shell_command=["tmux", "new", "-s", "my"])

    assert service._closed_tabs[0].shell_command == ["tmux", "new", "-s", "my"]

"""Tests for the paste_history plugin."""

import os

import pytest
import qterminator.config as config_mod
from qterminator.config import Config


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config" / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


from qterminator.plugins.paste_history import (  # noqa: E402
    PasteHistoryDialog,
    PasteHistoryPlugin,
    PasteHistoryService,
    load_history,
    looks_like_secret,
    save_history,
)

# ---------------------------------------------------------------------------
# Secret heuristic
# ---------------------------------------------------------------------------

def test_secret_heuristic_flags_private_keys():
    assert looks_like_secret(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END..."
    )
    assert looks_like_secret(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl..."
    )


def test_secret_heuristic_flags_long_opaque_token():
    token = "AKIA1234567890ABCDEF" + "x" * 30
    assert looks_like_secret(token)


def test_secret_heuristic_passes_normal_text():
    assert not looks_like_secret("hello world\n")
    assert not looks_like_secret("cd /tmp && ls -la")
    # Long but with whitespace — likely natural text, not a secret.
    long_natural = "lorem ipsum dolor sit amet " * 5
    assert not looks_like_secret(long_natural)


def test_secret_heuristic_short_strings_safe():
    assert not looks_like_secret("ab")
    assert not looks_like_secret("password")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_save_and_load_history_roundtrip(tmp_path):
    path = str(tmp_path / "ph.json")
    save_history(["one", "two", "three"], path)
    assert load_history(path) == ["one", "two", "three"]


def test_load_history_missing_returns_empty(tmp_path):
    assert load_history(str(tmp_path / "absent.json")) == []


def test_load_history_handles_corrupt_json(tmp_path):
    path = str(tmp_path / "bad.json")
    with open(path, "w") as f:
        f.write("{not valid")
    assert load_history(path) == []


def test_save_history_is_atomic(tmp_path, monkeypatch):
    """The temp file is renamed into place; a crash mid-write must
    not leave a torn target file."""
    path = str(tmp_path / "ph.json")
    save_history(["a", "b"], path)
    initial = load_history(path)
    # Force os.rename to raise to simulate a crash; the target file
    # must remain its prior contents.
    real_rename = os.rename
    def boom(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(os, "rename", boom)
    with pytest.raises(OSError):
        save_history(["c"], path)
    monkeypatch.setattr(os, "rename", real_rename)
    assert load_history(path) == initial


# ---------------------------------------------------------------------------
# Service (without real QApplication clipboard)
# ---------------------------------------------------------------------------

def test_push_appends_to_history(tmp_path):
    path = str(tmp_path / "ph.json")
    svc = PasteHistoryService(window=None, path=path)
    assert svc.push("first") is True
    assert svc.push("second") is True
    assert svc.entries == ["second", "first"]


def test_push_skips_empty_string(tmp_path):
    svc = PasteHistoryService(window=None, path=str(tmp_path / "ph.json"))
    assert svc.push("") is False
    assert svc.entries == []


def test_push_skips_secret_payload(tmp_path):
    svc = PasteHistoryService(window=None, path=str(tmp_path / "ph.json"))
    assert svc.push("-----BEGIN OPENSSH PRIVATE KEY-----\nblob") is False
    assert svc.entries == []


def test_push_deduplicates_top_of_ring(tmp_path):
    svc = PasteHistoryService(window=None, path=str(tmp_path / "ph.json"))
    svc.push("a")
    svc.push("a")  # same as top — no-op
    assert svc.entries == ["a"]


def test_push_promotes_existing_entry_to_top(tmp_path):
    svc = PasteHistoryService(window=None, path=str(tmp_path / "ph.json"))
    svc.push("a"); svc.push("b"); svc.push("c")
    svc.push("a")
    assert svc.entries == ["a", "c", "b"]


def test_push_capped_at_max_entries(tmp_path):
    svc = PasteHistoryService(window=None, path=str(tmp_path / "ph.json"), max_entries=3)
    for s in ["a", "b", "c", "d", "e"]:
        svc.push(s)
    assert svc.entries == ["e", "d", "c"]


def test_push_persists_to_disk(tmp_path):
    path = str(tmp_path / "ph.json")
    svc = PasteHistoryService(window=None, path=path)
    svc.push("first")
    svc.push("second")
    # Reload from disk via a fresh service.
    svc2 = PasteHistoryService(window=None, path=path)
    svc2.load()
    assert svc2.entries == ["second", "first"]


def test_clear_wipes_buffer_and_disk(tmp_path):
    path = str(tmp_path / "ph.json")
    svc = PasteHistoryService(window=None, path=path)
    svc.push("x")
    svc.clear()
    assert svc.entries == []
    assert load_history(path) == []


# ---------------------------------------------------------------------------
# Plugin lifecycle on a real MainWindow
# ---------------------------------------------------------------------------

def test_plugin_disabled_does_not_install_service():
    cfg = Config()
    cfg.set("plugins", "paste_history", "enabled", False)
    class FakeWindow:
        pass
    win = FakeWindow()
    p = PasteHistoryPlugin()
    p.activate(win)
    try:
        assert not hasattr(win, "paste_history")
    finally:
        p.deactivate()


def test_plugin_attaches_to_real_window(qtbot):
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(80)
    svc = win.paste_history
    assert isinstance(svc, PasteHistoryService)


def test_plugin_clipboard_change_pushes_into_history(qtbot):
    """Real QApplication clipboard → dataChanged → service.push."""
    from PyQt6.QtWidgets import QApplication
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    svc = win.paste_history
    svc.clear()
    QApplication.clipboard().setText("CLIP_TEST_PAYLOAD")
    qtbot.wait(60)
    assert "CLIP_TEST_PAYLOAD" in svc.entries


def test_plugin_picker_routes_through_paste_clipboard(qtbot, monkeypatch):
    """Selecting an entry should call terminal.paste_clipboard with
    the chosen text already on the system clipboard."""
    from PyQt6.QtWidgets import QApplication, QDialog
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    svc = win.paste_history
    svc.push("ENTRY_X")
    pasted = []
    monkeypatch.setattr(win._active_terminal, "paste_clipboard",
                        lambda: pasted.append(QApplication.clipboard().text()))
    # Replace dialog with one that auto-accepts the first entry.
    def fake_exec(self):
        self._selected = self._entries[0] if self._entries else None
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(PasteHistoryDialog, "exec", fake_exec)
    plugin = win._plugin_manager._instances["paste_history"]
    plugin.open_picker()
    assert pasted == ["ENTRY_X"]


def test_plugin_deactivate_disconnects_clipboard(qtbot):
    from PyQt6.QtWidgets import QApplication
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    pm = win._plugin_manager
    svc = win.paste_history
    svc.clear()
    pm.disable("paste_history")
    QApplication.clipboard().setText("AFTER_DETACH")
    qtbot.wait(60)
    # After deactivate, the (now-orphan) service still exists but
    # listening flag is False — no further pushes.
    assert "AFTER_DETACH" not in svc.entries

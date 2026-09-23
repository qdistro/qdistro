"""Tests for the Norton / Total Commander hotkey layout.

We exercise two layers:

1. **Static**: walk the menubar and assert that every TC-style binding
   we promise in ``AGENTS.md`` is actually registered on the right
   action. This catches the most common breakage (typo in a key
   sequence, forgotten action).
2. **Behavioural**: drive a few of the new actions through
   ``QTest.keySequence`` to confirm Qt actually fires them. This
   catches subtler issues like Tab being eaten by widget focus.

The window is created via the standard ``window`` fixture from
``test_window.py``'s conftest — same lifecycle, same cleanup.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication
from qfileman.window import FileManagerWindow


@pytest.fixture
def window(qapp):
    win = FileManagerWindow()
    yield win
    win.close()
    win.deleteLater()
    qapp.processEvents()
    qapp.processEvents()


def _walk_actions(window):
    """Return ``{action_text_without_amp: action}`` for every menubar entry."""
    out = {}
    for top in window.menuBar().actions():
        menu = top.menu()
        if menu is None:
            continue
        for action in menu.actions():
            text = action.text().replace("&", "")
            if text:
                out[text] = action
    return out


# ---------------------------------------------------------------------------
# Static binding checks. One test per TC-style key so failures point
# directly at the broken shortcut.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label, expected_keys", [
    # Function keys — the bedrock of the Norton/TC layout.
    ("New Folder",       ["F7", "Ctrl+Shift+N"]),
    ("New Text File…",   ["Shift+F4"]),
    ("View",             ["F3"]),
    ("Edit",             ["F4"]),
    ("Copy…",            ["F5"]),
    ("Move…",            ["F6"]),
    ("Rename",           ["F2", "Shift+F6"]),
    ("Delete",           ["Del", "F8"]),
    ("Pack… (archive)",  ["Alt+F5"]),
    ("Unpack… (extract)", ["Alt+F6"]),
    ("Quit",             ["Ctrl+Q", "F10"]),
    # Modern + TC aliases on the same action.
    ("Find…",            ["Ctrl+F", "Alt+F7"]),
    ("Refresh",          ["Ctrl+R", "Shift+F5"]),
    ("Folder Size…",     ["Ctrl+L"]),
    ("Swap Panes",       ["Ctrl+U"]),
    ("Parent Directory", ["Alt+Up", "Backspace"]),
    ("Switch Pane",      ["Tab"]),
    ("About",            ["F1"]),
])
def test_hotkey_registered(window, label, expected_keys):
    actions = _walk_actions(window)
    assert label in actions, \
        f"Menu item {label!r} missing; have {sorted(actions)}"
    got = [s.toString() for s in actions[label].shortcuts()]
    for key in expected_keys:
        assert key in got, \
            f"{label}: expected shortcut {key!r}, got {got!r}"


def test_menubar_has_expected_top_level_menus(window):
    """The bar must include the standard six menus."""
    labels = [
        a.text().replace("&", "")
        for a in window.menuBar().actions()
        if a.menu() is not None
    ]
    for expected in ("File", "Edit", "View", "Go", "Plugins", "Help"):
        assert expected in labels, f"missing {expected!r} from {labels}"


def test_no_duplicate_shortcuts(window):
    """A given key sequence should only fire one action — otherwise Qt
    silently disables them all. Catch this early."""
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for label, action in _walk_actions(window).items():
        for s in action.shortcuts():
            key = s.toString()
            if not key:
                continue
            if key in seen and seen[key] != label:
                duplicates.append(f"{key}: {seen[key]} vs {label}")
            else:
                seen[key] = label
    assert not duplicates, "duplicate shortcuts:\n  " + "\n  ".join(duplicates)


# ---------------------------------------------------------------------------
# Behavioural: trigger a few action callbacks and verify side-effects.
# We don't drive the keyboard event loop (that introduces flakiness on
# headless CI); we call ``action.trigger()`` which is what the runtime
# does after matching a shortcut.
# ---------------------------------------------------------------------------

def test_f3_view_triggers_quick_view_plugin(window, tmp_path, monkeypatch):
    """F3 should hand the selected file to the embedded_viewer plugin."""
    from qfileman.plugin import PluginManager
    pm = PluginManager()
    pm.discover()
    pm.enable("embedded_viewer")
    window._plugin_manager = pm

    target = tmp_path / "hello.txt"
    target.write_text("hi\n")
    window._update_path(str(tmp_path))
    QApplication.processEvents()

    # Force the file list to point at our target.
    pane = window._active_pane
    for i in range(pane.file_list.count()):
        item = pane.file_list.item(i)
        data = item.data(0x0100)  # Qt.UserRole
        if data and data.get("path") == str(target):
            pane.file_list.setCurrentItem(item)
            break
    else:
        pytest.fail("file list did not pick up our test file")

    called = {}
    monkeypatch.setattr(
        pm.load("embedded_viewer"), "_view",
        lambda path: called.setdefault("path", path),
    )
    actions = _walk_actions(window)
    actions["View"].trigger()
    assert called.get("path") == str(target)


def _select_in_pane(window, target_path):
    pane = window._active_pane
    for i in range(pane.file_list.count()):
        item = pane.file_list.item(i)
        data = item.data(0x0100)
        if data and data.get("path") == str(target_path):
            pane.file_list.setCurrentItem(item)
            return True
    return False


def test_f5_copy_action_runs_rsync(window, tmp_path, monkeypatch):
    """F5 builds an rsync argv that copies (does not move) and feeds it to
    the runner. We stub the runner so the modal dialog doesn't block."""
    src = tmp_path / "src.txt"
    src.write_text("payload")
    dest = tmp_path / "copy-of-src.txt"

    window._update_path(str(tmp_path))
    QApplication.processEvents()
    assert _select_in_pane(window, src)

    from PyQt6.QtWidgets import QInputDialog
    monkeypatch.setattr(
        QInputDialog, "getText",
        staticmethod(lambda *a, **kw: (str(dest), True)),
    )

    captured = {}

    def fake_run(title, argv, cwd=None, *, notify=True, parent=None):
        captured["title"] = title
        captured["argv"] = list(argv)
        # Actually do the copy so the post-action _refresh sees it.
        import subprocess
        return subprocess.run(argv).returncode


    # The function is imported inside _copy_or_move, so patching the
    # _runner attribute is enough — the in-function `from ... import`
    # resolves through the module table at call time.
    from qfileman.plugins.builtin import _runner
    monkeypatch.setattr(_runner, "run_command_dialog", fake_run)

    _walk_actions(window)["Copy…"].trigger()
    assert "argv" in captured, "Copy did not invoke runner"
    assert captured["argv"][0] == "rsync"
    assert "--remove-source-files" not in captured["argv"]
    assert dest.exists()
    assert dest.read_text() == "payload"
    # Source must still be there — copy, not move.
    assert src.exists()


@pytest.mark.cheat_aware(
    protects="Move (F6) only removes the source AFTER rsync copies it to the "
    "destination (the dest must exist and the source must be gone)",
    severity="critical",
    cheats=[
        "drop `assert dest.exists()` or `assert not src.exists()`",
        "stop asserting --remove-source-files is in the argv",
        "make fake_run skip the real subprocess so nothing is actually moved",
    ],
    consequence="a Move that deletes the user's source file without ever "
    "writing the destination — irrecoverable data loss",
)
def test_f6_move_action_runs_rsync_remove_source(window, tmp_path, monkeypatch):
    """F6 builds an rsync argv that adds --remove-source-files (the
    long-standing rsync 'move-with-resume' idiom)."""
    src = tmp_path / "to-move.txt"
    src.write_text("payload")
    dest = tmp_path / "moved.txt"

    window._update_path(str(tmp_path))
    QApplication.processEvents()
    assert _select_in_pane(window, src)

    from PyQt6.QtWidgets import QInputDialog
    monkeypatch.setattr(
        QInputDialog, "getText",
        staticmethod(lambda *a, **kw: (str(dest), True)),
    )

    captured = {}

    def fake_run(title, argv, cwd=None, *, notify=True, parent=None):
        captured["argv"] = list(argv)
        import subprocess
        return subprocess.run(argv).returncode

    from qfileman.plugins.builtin import _runner
    monkeypatch.setattr(_runner, "run_command_dialog", fake_run)

    _walk_actions(window)["Move…"].trigger()
    assert captured["argv"][0] == "rsync"
    assert "--remove-source-files" in captured["argv"]
    assert dest.exists()
    assert not src.exists()


def test_swap_panes_exchanges_cwds(window, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    window._update_path(str(a))
    QApplication.processEvents()
    # Split to get a second pane, then point it at ``b``.
    window._split_right_action_callback = None
    window._split_right()
    QApplication.processEvents()
    panes = window._split_root.find_panes()
    assert len(panes) == 2
    # After the split both panes start at the active pane's cwd; move
    # the new pane to ``b`` so swap has something interesting to do.
    panes[1]._update_path(str(b))
    panes[0]._update_path(str(a))
    QApplication.processEvents()

    # Force pane 0 to be active so swap exchanges with pane 1.
    window._set_active_pane(panes[0])
    QApplication.processEvents()
    assert panes[0].current_path == str(a)
    assert panes[1].current_path == str(b)

    _walk_actions(window)["Swap Panes"].trigger()
    QApplication.processEvents()

    assert panes[0].current_path == str(b)
    assert panes[1].current_path == str(a)


def test_switch_pane_moves_focus_to_next(window, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    window._update_path(str(a))
    QApplication.processEvents()
    window._split_right()
    QApplication.processEvents()
    panes = window._split_root.find_panes()
    assert len(panes) == 2

    window._set_active_pane(panes[0])
    QApplication.processEvents()
    _walk_actions(window)["Switch Pane"].trigger()
    QApplication.processEvents()
    assert window._active_pane is panes[1]


def test_backspace_goes_to_parent(window, tmp_path):
    sub = tmp_path / "child"
    sub.mkdir()
    window._update_path(str(sub))
    QApplication.processEvents()
    assert window.current_path == str(sub)
    _walk_actions(window)["Parent Directory"].trigger()
    QApplication.processEvents()
    assert window.current_path == str(tmp_path)


def test_about_dialog_exists(window, monkeypatch):
    """F1 should produce an About box; we don't want it modal in tests."""
    called = {}
    from PyQt6.QtWidgets import QMessageBox
    monkeypatch.setattr(
        QMessageBox, "about",
        staticmethod(lambda *a, **kw: called.setdefault("ok", True)),
    )
    _walk_actions(window)["About"].trigger()
    assert called.get("ok") is True

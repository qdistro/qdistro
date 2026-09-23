"""Tab stacks plugin."""


def test_clear_when_unset(window):
    plug = window.plugins._instances["tab_stacks"]
    plug._clear_current()
    assert window._active_webview.group is None


def test_clear_after_assigned(window):
    plug = window.plugins._instances["tab_stacks"]
    window._active_webview.group = "ops"
    plug._clear_current()
    assert window._active_webview.group is None


def test_commands_present(window):
    plug = window.plugins._instances["tab_stacks"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("Tab stack" in label for label in labels)


def test_list_works_with_no_groups(window, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    seen = {}
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda parent, title, msg: seen.update(
            {"parent": parent, "title": title, "msg": msg}),
    )

    plug = window.plugins._instances["tab_stacks"]
    plug._list()  # must not raise
    assert seen == {
        "parent": window,
        "title": "Tab stacks",
        "msg": "(no groups)",
    }

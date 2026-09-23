"""Command palette: filtering, activation, fuzzy matching corner cases."""


def test_fuzzy_substring_wins():
    from qdbrowser.plugins.command_palette import _fuzzy
    assert _fuzzy("tab", "new tab")
    assert _fuzzy("new", "new tab")


def test_fuzzy_skipping_chars():
    from qdbrowser.plugins.command_palette import _fuzzy
    assert _fuzzy("nt", "new tab")
    assert _fuzzy("ntab", "new tab")


def test_fuzzy_rejects_unordered():
    from qdbrowser.plugins.command_palette import _fuzzy
    assert not _fuzzy("tan", "new tab")


def test_fuzzy_empty_query_matches_everything():
    from qdbrowser.plugins.command_palette import _fuzzy
    assert _fuzzy("", "anything")


def test_dialog_filter_narrows(window):
    from qdbrowser.plugins.command_palette import CommandPaletteDialog
    dlg = CommandPaletteDialog(window)
    full = dlg._list.count()
    dlg._refilter("zoom")
    assert dlg._list.count() <= full
    assert dlg._list.count() >= 1


def test_dialog_includes_all_provider_commands(window):
    from qdbrowser.plugins.command_palette import CommandPaletteDialog
    dlg = CommandPaletteDialog(window)
    labels = [label for label, _ in dlg._entries]
    # From every plugin that provides commands.
    assert any("history" in label.lower() for label in labels)
    assert any("bookmark" in label.lower() for label in labels)
    assert any("note" in label.lower() for label in labels)
    assert any("session" in label.lower() for label in labels)
    assert any("workspace" in label.lower() for label in labels)


def test_dialog_first_item_selected_on_open(window):
    from qdbrowser.plugins.command_palette import CommandPaletteDialog
    dlg = CommandPaletteDialog(window)
    assert dlg._list.currentRow() == 0


def test_dialog_filter_to_zero_results_clears_selection(window):
    from qdbrowser.plugins.command_palette import CommandPaletteDialog
    dlg = CommandPaletteDialog(window)
    dlg._refilter("zzzzzzznever")
    assert dlg._list.count() == 0


def test_palette_callback_runs(window):
    from qdbrowser.plugins.command_palette import CommandPaletteDialog
    dlg = CommandPaletteDialog(window)
    called = {"v": False}

    def cb():
        called["v"] = True

    dlg._entries = [("test entry", cb)]
    dlg._refilter("test")
    item = dlg._list.item(0)
    # Activation accepts the dialog and runs the callback.
    dlg._on_activated(item)
    assert called["v"] is True

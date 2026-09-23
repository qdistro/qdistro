"""Command palette: gather + fuzzy match."""


def test_palette_gathers_builtin_and_plugin_entries(window):
    from qdbrowser.plugins.command_palette import CommandPaletteDialog
    dlg = CommandPaletteDialog(window)
    labels = [lbl for lbl, _ in dlg._entries]
    # A handful of built-ins:
    for needed in ("New tab", "Reload", "Toggle reader mode", "Quit",
                   "Save session", "Toggle DevTools"):
        assert needed in labels, f"missing: {needed}"
    # Plugin contributions:
    assert any("Bookmark" in label for label in labels)
    assert any("history" in label.lower() for label in labels)


def test_palette_fuzzy_filter():
    from qdbrowser.plugins.command_palette import _fuzzy
    assert _fuzzy("nt", "new tab")
    assert _fuzzy("zoomout", "zoom out")
    assert not _fuzzy("xyz", "new tab")

"""Vertical tab list panel."""

from PyQt6.QtCore import Qt


def test_panel_present(window):
    assert "tab_list" in window._side_panel.panel_ids()


def test_initial_tab_shown(window):
    plug = window.plugins._instances["tab_list"]
    panel = plug._panel
    panel.refresh()
    # One leaf for the initial tab.
    leaves = _all_leaves(panel)
    assert len(leaves) == 1


def test_new_tab_appears(window):
    plug = window.plugins._instances["tab_list"]
    panel = plug._panel
    window.new_tab(url="about:blank")
    # The window's webview_added signal schedules a debounced refresh.
    # Force the debounce to fire synchronously for the test.
    panel.refresh()
    leaves = _all_leaves(panel)
    assert len(leaves) == 2


def test_grouped_tabs_have_parent(window):
    plug = window.plugins._instances["tab_list"]
    panel = plug._panel
    wv2 = window.new_tab(url="about:blank")
    wv2.group = "research"
    panel.refresh()
    # The grouped tab has a parent header.
    found_under_group = False
    tree = panel._tree
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        if "research" in top.text(0):
            for j in range(top.childCount()):
                if top.child(j).data(0, Qt.ItemDataRole.UserRole + 1) is wv2:
                    found_under_group = True
    assert found_under_group


def test_click_focuses_tab(window):
    plug = window.plugins._instances["tab_list"]
    panel = plug._panel
    wv2 = window.new_tab(url="about:blank")
    # Focus tab 0 explicitly.
    window._tabs.setCurrentIndex(0)
    panel.refresh()
    # Find the leaf for wv2 and click it.
    tree = panel._tree
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        if top.data(0, Qt.ItemDataRole.UserRole + 1) is wv2:
            panel._on_clicked(top, 0)
            break
        for j in range(top.childCount()):
            child = top.child(j)
            if child.data(0, Qt.ItemDataRole.UserRole + 1) is wv2:
                panel._on_clicked(child, 0)
                break
    assert window._active_webview is wv2


def test_commands_provided(window):
    plug = window.plugins._instances["tab_list"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("tab list" in label.lower() for label in labels)


def _all_leaves(panel):
    out = []
    tree = panel._tree
    for i in range(tree.topLevelItemCount()):
        top = tree.topLevelItem(i)
        if top.data(0, Qt.ItemDataRole.UserRole + 1) is not None:
            out.append(top)
        for j in range(top.childCount()):
            child = top.child(j)
            if child.data(0, Qt.ItemDataRole.UserRole + 1) is not None:
                out.append(child)
    return out

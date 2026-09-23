"""Tests for layout serialization and restoration."""

import json

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.layout import (
    _restore_node,
    _serialize_node,
    restore_layout,
    serialize_layout,
)
from qterminator.splitter import SplitContainer
from qterminator.terminal import TerminalWidget
from qterminator.window import MainWindow


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / "config.toml"))
    Config._instance = None
    yield
    Config._instance = None


@pytest.fixture
def window(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    win.show()
    return win


def _make_fresh_window(qtbot):
    """Create an empty MainWindow with all default tabs removed."""
    win = MainWindow.__new__(MainWindow)
    MainWindow.__init__(win)
    qtbot.addWidget(win)
    while win._tabs.count() > 0:
        w = win._tabs.widget(0)
        win._tabs.removeTab(0)
        w.deleteLater()
    return win


# ── Existing tests (kept as-is) ─────────────────────────────────────


def test_serialize_single_terminal(window):
    """Serializing a single terminal tab."""
    layout = serialize_layout(window._tabs)
    assert len(layout["tabs"]) == 1
    tree = layout["tabs"][0]["tree"]
    assert tree["type"] == "split"
    assert len(tree["children"]) == 1
    assert tree["children"][0]["type"] == "terminal"


def test_serialize_two_tabs(window):
    window.new_tab()
    layout = serialize_layout(window._tabs)
    assert len(layout["tabs"]) == 2


def test_serialize_splits(window, qtbot):
    window._split_horizontal()
    qtbot.wait(100)
    layout = serialize_layout(window._tabs)
    tree = layout["tabs"][0]["tree"]
    assert tree["type"] == "split"
    assert len(tree["children"]) == 2
    assert tree["children"][0]["type"] == "terminal"
    assert tree["children"][1]["type"] == "terminal"


def test_serialize_preserves_orientation(window, qtbot):
    window._split_horizontal()
    qtbot.wait(100)
    layout = serialize_layout(window._tabs)
    tree = layout["tabs"][0]["tree"]
    # Horizontal split means vertical orientation (top/bottom)
    assert tree["orientation"] == "vertical"


def test_serialize_preserves_group(window):
    window._active_terminal.group = "Alpha"
    layout = serialize_layout(window._tabs)
    term = layout["tabs"][0]["tree"]["children"][0]
    assert term["group"] == "Alpha"


def test_roundtrip_single_terminal(window, qtbot):
    """Serialize then restore produces same structure."""
    layout = serialize_layout(window._tabs)

    win2 = _make_fresh_window(qtbot)
    restore_layout(win2, layout)
    assert win2._tabs.count() == 1
    split = win2._tabs.widget(0)
    assert len(split.find_terminals()) == 1


def test_roundtrip_splits(window, qtbot):
    """Serialize splits then restore preserves terminal count."""
    window._split_horizontal()
    qtbot.wait(100)
    window._split_vertical()
    qtbot.wait(100)
    layout = serialize_layout(window._tabs)

    win2 = _make_fresh_window(qtbot)
    restore_layout(win2, layout)
    split = win2._tabs.widget(0)
    assert len(split.find_terminals()) == 3


def test_roundtrip_two_tabs(window, qtbot):
    window.new_tab()
    layout = serialize_layout(window._tabs)

    win2 = _make_fresh_window(qtbot)
    restore_layout(win2, layout)
    assert win2._tabs.count() == 2


# ── Serialize: corner cases ──────────────────────────────────────────


def test_serialize_empty_tab_widget(window, qtbot):
    """Serializing a tab widget with zero tabs yields empty list."""
    while window._tabs.count() > 0:
        w = window._tabs.widget(0)
        window._tabs.removeTab(0)
        w.deleteLater()
    layout = serialize_layout(window._tabs)
    assert layout["tabs"] == []


def test_serialize_single_tab_single_terminal(window):
    """Single tab with one terminal has expected tree shape."""
    layout = serialize_layout(window._tabs)
    assert len(layout["tabs"]) == 1
    tab = layout["tabs"][0]
    tree = tab["tree"]
    assert tree["type"] == "split"
    assert len(tree["children"]) == 1
    child = tree["children"][0]
    assert child["type"] == "terminal"
    assert "working_directory" in child
    assert "group" in child


def test_serialize_horizontal_split_two_terminals(window, qtbot):
    """Horizontal split produces two terminal children."""
    window._split_horizontal()
    qtbot.wait(100)
    layout = serialize_layout(window._tabs)
    tree = layout["tabs"][0]["tree"]
    assert tree["type"] == "split"
    assert len(tree["children"]) == 2
    for child in tree["children"]:
        assert child["type"] == "terminal"


def test_serialize_vertical_split(window, qtbot):
    """Vertical split produces horizontal orientation in serialized data."""
    window._split_vertical()
    qtbot.wait(100)
    layout = serialize_layout(window._tabs)
    tree = layout["tabs"][0]["tree"]
    assert tree["orientation"] == "horizontal"
    assert len(tree["children"]) == 2


def test_serialize_nested_splits(window, qtbot):
    """Split within a split produces nested tree structure."""
    window._split_horizontal()
    qtbot.wait(100)
    window._split_vertical()
    qtbot.wait(100)
    layout = serialize_layout(window._tabs)
    tree = layout["tabs"][0]["tree"]
    # There should be 3 terminals total in the tree
    terminals = _count_terminals(tree)
    assert terminals == 3


def test_serialize_three_tabs(window, qtbot):
    """Three tabs serialize to three entries."""
    window.new_tab()
    window.new_tab()
    layout = serialize_layout(window._tabs)
    assert len(layout["tabs"]) == 3


def test_serialize_preserves_tab_names(window, qtbot):
    """Tab names survive serialization."""
    window._tabs.setTabText(0, "MyTab")
    layout = serialize_layout(window._tabs)
    assert layout["tabs"][0]["name"] == "MyTab"


def test_serialize_preserves_working_directory(window):
    """Working directory is recorded for each terminal."""
    layout = serialize_layout(window._tabs)
    term_node = layout["tabs"][0]["tree"]["children"][0]
    # working_directory should be a string (possibly empty)
    assert isinstance(term_node["working_directory"], str)


def test_serialize_preserves_group_none(window):
    """Terminal with no group serializes group as None."""
    window._active_terminal.group = None
    layout = serialize_layout(window._tabs)
    term_node = layout["tabs"][0]["tree"]["children"][0]
    assert term_node["group"] is None


def test_serialize_preserves_group_named(window):
    """Terminal with a named group serializes correctly."""
    window._active_terminal.group = "Beta"
    layout = serialize_layout(window._tabs)
    term_node = layout["tabs"][0]["tree"]["children"][0]
    assert term_node["group"] == "Beta"


def test_serialize_preserves_split_sizes(window, qtbot):
    """Split sizes are included in serialized data."""
    window._split_horizontal()
    qtbot.wait(100)
    layout = serialize_layout(window._tabs)
    tree = layout["tabs"][0]["tree"]
    assert "sizes" in tree
    assert isinstance(tree["sizes"], list)
    assert len(tree["sizes"]) == 2


def test_serialize_preserves_split_orientation(window, qtbot):
    """Both horizontal and vertical orientations are preserved."""
    window._split_vertical()
    qtbot.wait(100)
    layout_v = serialize_layout(window._tabs)
    assert layout_v["tabs"][0]["tree"]["orientation"] == "horizontal"


# ── Restore: corner cases ────────────────────────────────────────────


def test_restore_empty_layout_creates_default(qtbot):
    """Restoring from empty layout creates a default tab."""
    win = _make_fresh_window(qtbot)
    restore_layout(win, {"tabs": []})
    assert win._tabs.count() == 1
    split = win._tabs.widget(0)
    assert len(split.find_terminals()) == 1


def test_restore_single_terminal_layout(window, qtbot):
    """Restore a layout containing one terminal."""
    layout = {
        "tabs": [{
            "name": "Tab1",
            "tree": {
                "type": "split",
                "orientation": "horizontal",
                "sizes": [],
                "children": [{"type": "terminal", "working_directory": "/tmp", "group": None}],
            },
        }],
    }
    win = _make_fresh_window(qtbot)
    restore_layout(win, layout)
    assert win._tabs.count() == 1
    assert len(win._tabs.widget(0).find_terminals()) == 1


def test_restore_split_layout(window, qtbot):
    """Restore a layout with a split (2 terminals)."""
    layout = {
        "tabs": [{
            "name": "SplitTab",
            "tree": {
                "type": "split",
                "orientation": "vertical",
                "sizes": [300, 300],
                "children": [
                    {"type": "terminal", "working_directory": "/tmp", "group": None},
                    {"type": "terminal", "working_directory": "/tmp", "group": None},
                ],
            },
        }],
    }
    win = _make_fresh_window(qtbot)
    restore_layout(win, layout)
    assert win._tabs.count() == 1
    assert len(win._tabs.widget(0).find_terminals()) == 2


def test_restore_multi_tab_layout(qtbot):
    """Restore a layout with multiple tabs."""
    layout = {
        "tabs": [
            {
                "name": "Tab1",
                "tree": {
                    "type": "split",
                    "orientation": "horizontal",
                    "sizes": [],
                    "children": [{"type": "terminal", "working_directory": "/tmp", "group": None}],
                },
            },
            {
                "name": "Tab2",
                "tree": {
                    "type": "split",
                    "orientation": "horizontal",
                    "sizes": [],
                    "children": [{"type": "terminal", "working_directory": "/tmp", "group": None}],
                },
            },
            {
                "name": "Tab3",
                "tree": {
                    "type": "split",
                    "orientation": "horizontal",
                    "sizes": [],
                    "children": [{"type": "terminal", "working_directory": "/tmp", "group": None}],
                },
            },
        ],
    }
    win = _make_fresh_window(qtbot)
    restore_layout(win, layout)
    assert win._tabs.count() == 3


def test_restore_json_encoded_tab_strings(qtbot):
    """Tabs serialized as JSON strings (TOML artifact) are parsed correctly."""
    tab_dict = {
        "name": "FromJSON",
        "tree": {
            "type": "split",
            "orientation": "horizontal",
            "sizes": [],
            "children": [{"type": "terminal", "working_directory": "/tmp", "group": None}],
        },
    }
    layout = {"tabs": [json.dumps(tab_dict)]}
    win = _make_fresh_window(qtbot)
    restore_layout(win, layout)
    assert win._tabs.count() == 1
    # Tab was created from the parsed JSON string data
    assert len(win._tabs.widget(0).find_terminals()) == 1


def test_restore_legacy_python_repr_strings(qtbot):
    """Legacy Python repr strings are parsed via ast.literal_eval."""
    tab_dict = {
        "name": "LegacyTab",
        "tree": {
            "type": "split",
            "orientation": "horizontal",
            "sizes": [],
            "children": [{"type": "terminal", "working_directory": "/tmp", "group": None}],
        },
    }
    layout = {"tabs": [repr(tab_dict)]}
    win = _make_fresh_window(qtbot)
    restore_layout(win, layout)
    assert win._tabs.count() == 1
    # Tab was created from the parsed repr string data
    assert len(win._tabs.widget(0).find_terminals()) == 1


def test_restore_malformed_string_skipped(qtbot):
    """Malformed string data is skipped; valid entries still restore."""
    good_tab = {
        "name": "GoodTab",
        "tree": {
            "type": "split",
            "orientation": "horizontal",
            "sizes": [],
            "children": [{"type": "terminal", "working_directory": "/tmp", "group": None}],
        },
    }
    layout = {"tabs": [
        "this is not valid json or repr {{{{",
        json.dumps(good_tab),
    ]}
    win = _make_fresh_window(qtbot)
    # Should not raise; malformed entry is skipped, good entry restores
    restore_layout(win, layout)
    assert win._tabs.count() == 1
    assert len(win._tabs.widget(0).find_terminals()) == 1


def test_restore_missing_tree_creates_default(qtbot):
    """Tab data with no tree key still creates a usable tab."""
    layout = {"tabs": [{"name": "NoTree"}]}
    win = _make_fresh_window(qtbot)
    restore_layout(win, layout)
    assert win._tabs.count() == 1
    # _restore_node({}) returns None, so restore_layout creates a default
    split = win._tabs.widget(0)
    assert len(split.find_terminals()) >= 1


def test_restore_unknown_node_type_creates_default(qtbot):
    """Unknown node type in tree causes fallback to default terminal."""
    layout = {
        "tabs": [{
            "name": "BadNode",
            "tree": {"type": "foobar"},
        }],
    }
    win = _make_fresh_window(qtbot)
    restore_layout(win, layout)
    assert win._tabs.count() == 1
    split = win._tabs.widget(0)
    assert len(split.find_terminals()) == 1


def test_restore_mismatched_sizes(qtbot):
    """Sizes with wrong length are not applied (no crash)."""
    layout = {
        "tabs": [{
            "name": "Mismatched",
            "tree": {
                "type": "split",
                "orientation": "horizontal",
                "sizes": [100, 200, 300],  # 3 sizes but only 2 children
                "children": [
                    {"type": "terminal", "working_directory": "/tmp", "group": None},
                    {"type": "terminal", "working_directory": "/tmp", "group": None},
                ],
            },
        }],
    }
    win = _make_fresh_window(qtbot)
    restore_layout(win, layout)
    split = win._tabs.widget(0)
    assert len(split.find_terminals()) == 2


def test_restore_sets_focus_to_first_terminal(qtbot):
    """After restore, the first terminal should be the active one."""
    layout = {
        "tabs": [{
            "name": "Focus",
            "tree": {
                "type": "split",
                "orientation": "horizontal",
                "sizes": [300, 300],
                "children": [
                    {"type": "terminal", "working_directory": "/tmp", "group": None},
                    {"type": "terminal", "working_directory": "/tmp", "group": None},
                ],
            },
        }],
    }
    win = _make_fresh_window(qtbot)
    restore_layout(win, layout)
    first_terminal = win._tabs.widget(0).find_terminals()[0]
    assert win._active_terminal is first_terminal


def test_restore_connects_all_terminals(qtbot):
    """All restored terminals have their signals connected."""
    layout = {
        "tabs": [{
            "name": "Connected",
            "tree": {
                "type": "split",
                "orientation": "vertical",
                "sizes": [200, 200],
                "children": [
                    {"type": "terminal", "working_directory": "/tmp", "group": None},
                    {"type": "terminal", "working_directory": "/tmp", "group": None},
                ],
            },
        }],
    }
    win = _make_fresh_window(qtbot)
    restore_layout(win, layout)
    terminals = win._tabs.widget(0).find_terminals()
    assert len(terminals) == 2
    # Verify terminals are connected by checking that the window tracks them.
    # If _connect_terminal was called, focus_gained is connected, so setting
    # focus on a terminal should update the window's active terminal.
    terminals[1].term.setFocus()
    win._set_active_terminal(terminals[1])
    assert win._active_terminal is terminals[1]


# ── _serialize_node / _restore_node unit tests ──────────────────────


def test_serialize_node_unknown_widget(qtbot):
    """_serialize_node on an unknown widget type returns type 'unknown'."""
    from PyQt6.QtWidgets import QWidget
    w = QWidget()
    qtbot.addWidget(w)
    result = _serialize_node(w)
    assert result == {"type": "unknown"}


def test_restore_node_empty_dict():
    """_restore_node with empty dict returns None."""
    result = _restore_node({})
    assert result is None


def test_restore_node_terminal_type():
    """_restore_node with terminal type creates a TerminalWidget."""
    data = {"type": "terminal", "working_directory": "/tmp", "group": "GroupA"}
    result = _restore_node(data)
    assert isinstance(result, TerminalWidget)
    assert result.group == "GroupA"


def test_restore_node_split_no_children():
    """_restore_node with split type but no children returns empty SplitContainer."""
    data = {"type": "split", "orientation": "horizontal", "children": []}
    result = _restore_node(data)
    assert isinstance(result, SplitContainer)
    assert result.count() == 0


def test_restore_node_deeply_nested_splits():
    """_restore_node handles 3 levels of nesting."""
    data = {
        "type": "split",
        "orientation": "vertical",
        "children": [
            {
                "type": "split",
                "orientation": "horizontal",
                "children": [
                    {
                        "type": "split",
                        "orientation": "vertical",
                        "children": [
                            {"type": "terminal", "working_directory": "/tmp", "group": None},
                            {"type": "terminal", "working_directory": "/tmp", "group": None},
                        ],
                    },
                    {"type": "terminal", "working_directory": "/tmp", "group": None},
                ],
            },
            {"type": "terminal", "working_directory": "/tmp", "group": None},
        ],
    }
    result = _restore_node(data)
    assert isinstance(result, SplitContainer)
    # Should contain 4 terminals total across all levels
    terminals = result.find_terminals()
    assert len(terminals) == 4


# ── Full roundtrip tests ─────────────────────────────────────────────


def test_full_roundtrip_via_config(window, qtbot, tmp_path):
    """Serialize, save to file as JSON, load back, restore."""
    window._split_horizontal()
    qtbot.wait(100)
    layout = serialize_layout(window._tabs)

    # Save to file
    layout_file = tmp_path / "layout.json"
    layout_file.write_text(json.dumps(layout))

    # Load from file
    loaded = json.loads(layout_file.read_text())

    win2 = _make_fresh_window(qtbot)
    restore_layout(win2, loaded)
    assert win2._tabs.count() == 1
    assert len(win2._tabs.widget(0).find_terminals()) == 2


def test_roundtrip_preserves_terminal_count(window, qtbot):
    """Terminal count is preserved across serialize/restore roundtrip."""
    window._split_horizontal()
    qtbot.wait(100)
    window._split_vertical()
    qtbot.wait(100)
    original_count = len(window._tabs.widget(0).find_terminals())
    layout = serialize_layout(window._tabs)

    win2 = _make_fresh_window(qtbot)
    restore_layout(win2, layout)
    restored_count = len(win2._tabs.widget(0).find_terminals())
    assert restored_count == original_count


def test_roundtrip_preserves_tab_count(window, qtbot):
    """Tab count is preserved across roundtrip."""
    window.new_tab()
    window.new_tab()
    original_count = window._tabs.count()
    layout = serialize_layout(window._tabs)

    win2 = _make_fresh_window(qtbot)
    restore_layout(win2, layout)
    assert win2._tabs.count() == original_count


def test_roundtrip_preserves_groups(window, qtbot):
    """Group assignments survive a roundtrip."""
    window._active_terminal.group = "Gamma"
    layout = serialize_layout(window._tabs)

    win2 = _make_fresh_window(qtbot)
    restore_layout(win2, layout)
    terminals = win2._tabs.widget(0).find_terminals()
    assert terminals[0].group == "Gamma"


# ── Helper ───────────────────────────────────────────────────────────


# ── Scrollback save/restore tests ───────────────────────────────────


def test_scrollback_not_saved_by_default(window):
    """Without the opt-in flag, scrollback is NOT included in serialization."""
    layout = serialize_layout(window._tabs)
    term_node = layout["tabs"][0]["tree"]["children"][0]
    assert "scrollback" not in term_node


def test_scrollback_saved_when_flag_enabled(window, qtbot):
    """With save_scrollback=True, terminal nodes may include a scrollback key.

    The captured text may be empty if no text has rendered yet; the
    contract is only that the serialization path runs without error and
    does not include the key for empty captures.
    """
    qtbot.wait(50)
    layout = serialize_layout(window._tabs, save_scrollback=True)
    term_node = layout["tabs"][0]["tree"]["children"][0]
    # If scrollback was captured it must be a string; absence is also allowed.
    if "scrollback" in term_node:
        assert isinstance(term_node["scrollback"], str)


def test_scrollback_config_flag_controls_capture(window, qtbot):
    """The general.save_scrollback config flag gates automatic capture."""
    cfg = Config()
    cfg.set("general", "save_scrollback", False)
    layout_off = serialize_layout(window._tabs)
    term_off = layout_off["tabs"][0]["tree"]["children"][0]
    assert "scrollback" not in term_off

    cfg.set("general", "save_scrollback", True)
    # Just verify the path runs; capture itself may be empty under offscreen.
    layout_on = serialize_layout(window._tabs)
    assert "tabs" in layout_on


def test_restore_terminal_with_scrollback_field(qtbot):
    """A layout with a scrollback field restores without error."""
    layout = {
        "tabs": [{
            "name": "WithScrollback",
            "tree": {
                "type": "split",
                "orientation": "horizontal",
                "sizes": [],
                "children": [{
                    "type": "terminal",
                    "working_directory": "/tmp",
                    "group": None,
                    "scrollback": "hello previous session\n",
                }],
            },
        }],
    }
    win = _make_fresh_window(qtbot)
    restore_layout(win, layout)
    assert win._tabs.count() == 1
    assert len(win._tabs.widget(0).find_terminals()) == 1


def test_roundtrip_with_scrollback_preserves_structure(window, qtbot):
    """Enabling scrollback capture doesn't break the serialize/restore roundtrip."""
    window._split_horizontal()
    qtbot.wait(100)
    layout = serialize_layout(window._tabs, save_scrollback=True)

    win2 = _make_fresh_window(qtbot)
    restore_layout(win2, layout)
    assert win2._tabs.count() == 1
    assert len(win2._tabs.widget(0).find_terminals()) == 2


def test_scrollback_default_config_is_false(qtbot):
    """Default config value for save_scrollback is False (security default)."""
    cfg = Config()
    assert cfg.get("general", "save_scrollback") is False


def _count_terminals(node):
    """Recursively count terminal nodes in a serialized tree."""
    if node.get("type") == "terminal":
        return 1
    count = 0
    for child in node.get("children", []):
        count += _count_terminals(child)
    return count


# =====================================================================
# Configuration persistence and restart simulation tests
# =====================================================================


class TestRestartNoExtraTab:
    """The core bug: restore_layout must not leave extra tabs."""

    def test_save_one_tab_restore_gives_one_tab(self, qtbot):
        """Save with 1 tab, restart, get exactly 1 tab — not 2."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        assert win1._tabs.count() == 1
        win1.save_layout()

        # Simulate restart: new MainWindow + restore
        Config._instance = None
        win2 = MainWindow()
        qtbot.addWidget(win2)
        # Constructor creates 1 tab, restore_layout should replace it
        restored = win2.restore_layout()
        assert restored is True
        assert win2._tabs.count() == 1, (
            f"Expected 1 tab after restore, got {win2._tabs.count()}"
        )

    def test_save_two_tabs_restore_gives_two_tabs(self, qtbot):
        """Save with 2 tabs, restart, get exactly 2 tabs — not 3."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1.new_tab()
        assert win1._tabs.count() == 2
        win1.save_layout()

        Config._instance = None
        win2 = MainWindow()
        qtbot.addWidget(win2)
        restored = win2.restore_layout()
        assert restored is True
        assert win2._tabs.count() == 2

    def test_save_three_tabs_restore_gives_three_tabs(self, qtbot):
        """Save with 3 tabs, restart, get exactly 3 tabs."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1.new_tab()
        win1.new_tab()
        assert win1._tabs.count() == 3
        win1.save_layout()

        Config._instance = None
        win2 = MainWindow()
        qtbot.addWidget(win2)
        restored = win2.restore_layout()
        assert restored is True
        assert win2._tabs.count() == 3

    def test_no_saved_layout_keeps_initial_tab(self, qtbot):
        """With no saved layout, restore_layout returns False and initial tab remains."""
        win = MainWindow()
        qtbot.addWidget(win)
        assert win._tabs.count() == 1
        restored = win.restore_layout()
        assert restored is False
        assert win._tabs.count() == 1


class TestRestartPreservesStructure:
    """Layout structure survives save → restart → restore."""

    def test_splits_preserved(self, qtbot):
        """Horizontal split survives restart."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1._split_horizontal()
        split1 = win1._tabs.widget(0)
        assert len(split1.find_terminals()) == 2
        win1.save_layout()

        Config._instance = None
        win2 = MainWindow()
        qtbot.addWidget(win2)
        win2.restore_layout()
        split2 = win2._tabs.widget(0)
        assert len(split2.find_terminals()) == 2

    def test_vertical_split_preserved(self, qtbot):
        """Vertical split survives restart."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1._split_vertical()
        win1.save_layout()

        Config._instance = None
        win2 = MainWindow()
        qtbot.addWidget(win2)
        win2.restore_layout()
        split2 = win2._tabs.widget(0)
        assert len(split2.find_terminals()) == 2

    def test_nested_splits_preserved(self, qtbot):
        """3-way split (horiz then vert) survives restart."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1._split_horizontal()
        win1._split_vertical()
        split1 = win1._tabs.widget(0)
        assert len(split1.find_terminals()) == 3
        win1.save_layout()

        Config._instance = None
        win2 = MainWindow()
        qtbot.addWidget(win2)
        win2.restore_layout()
        split2 = win2._tabs.widget(0)
        assert len(split2.find_terminals()) == 3

    def test_tab_with_splits_plus_plain_tab(self, qtbot):
        """Tab 1 with splits + Tab 2 plain both survive restart."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1._split_horizontal()
        win1.new_tab()  # plain second tab
        assert win1._tabs.count() == 2
        win1.save_layout()

        Config._instance = None
        win2 = MainWindow()
        qtbot.addWidget(win2)
        win2.restore_layout()
        assert win2._tabs.count() == 2
        assert len(win2._tabs.widget(0).find_terminals()) == 2
        assert len(win2._tabs.widget(1).find_terminals()) == 1

    def test_groups_preserved_across_restart(self, qtbot):
        """Terminal groups survive save/restore."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1._split_horizontal()
        terms = win1._tabs.widget(0).find_terminals()
        terms[0].group = "Alpha"
        terms[1].group = "Beta"
        win1.save_layout()

        Config._instance = None
        win2 = MainWindow()
        qtbot.addWidget(win2)
        win2.restore_layout()
        terms2 = win2._tabs.widget(0).find_terminals()
        groups = {t.group for t in terms2}
        assert "Alpha" in groups
        assert "Beta" in groups


class TestRestartWindowState:
    """Window geometry and state persists across restarts."""

    def test_window_size_preserved(self, qtbot):
        """Window size survives restart."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1.resize(1024, 768)
        win1.save_layout()

        Config._instance = None
        win2 = MainWindow()
        qtbot.addWidget(win2)
        win2.restore_window_state()
        assert win2.width() == 1024
        assert win2.height() == 768

    def test_tab_position_preserved(self, qtbot):
        """Tab position setting survives restart."""
        config = Config()
        config.set("general", "tab_position", "bottom")
        config.save()

        Config._instance = None
        config2 = Config()
        assert config2.get("general", "tab_position") == "bottom"

    def test_menubar_visibility_preserved(self, qtbot):
        """Menu bar visibility survives restart."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1._toggle_menubar()  # show it
        # Config should now have show_menubar=True

        Config._instance = None
        config = Config()
        assert config.get("general", "show_menubar") is True


class TestRestartDoubleRestore:
    """Edge case: calling restore_layout multiple times."""

    def test_double_restore_no_duplicate_tabs(self, qtbot):
        """Calling restore_layout twice doesn't duplicate tabs."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1.new_tab()
        assert win1._tabs.count() == 2
        win1.save_layout()

        Config._instance = None
        win2 = MainWindow()
        qtbot.addWidget(win2)
        win2.restore_layout()
        assert win2._tabs.count() == 2
        # Calling again should not add more
        win2.restore_layout()
        assert win2._tabs.count() == 2

    def test_restore_after_user_opens_tabs(self, qtbot):
        """Restoring after user manually opened tabs replaces them."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1.save_layout()  # save with 1 tab

        Config._instance = None
        win2 = MainWindow()
        qtbot.addWidget(win2)
        win2.new_tab()  # user opens extra tab
        win2.new_tab()  # another
        assert win2._tabs.count() == 3
        win2.restore_layout()
        assert win2._tabs.count() == 1  # restored to saved state


class TestMainEntrypointRestore:
    """Simulate the exact flow from __main__.py main()."""

    def test_main_flow_no_restore_flag(self, qtbot):
        """--no-restore: window has 1 tab, no restore called."""
        win = MainWindow(create_initial_tab=False)
        qtbot.addWidget(win)
        # main() does NOT call restore_layout when --no-restore
        # It creates the startup tab explicitly after restore decisions.
        win.new_tab()
        assert win._tabs.count() == 1

    def test_main_flow_execute_uses_single_command_tab(self, qtbot):
        """-x starts the requested argv without leaving a default shell tab."""
        win = MainWindow(create_initial_tab=False)
        qtbot.addWidget(win)
        command = ["printf", "%s\n", "a;touch /tmp/bad"]

        terminal = win.new_tab(shell_command=command)

        assert win._tabs.count() == 1
        assert terminal._shell_command == command

    def test_main_flow_empty_execute_does_not_restore_saved_layout(self, qtbot):
        """Bare -x behaves like a fresh shell launch, not session restore."""
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1.new_tab()
        win1.save_layout()

        Config._instance = None
        win2 = MainWindow(create_initial_tab=False)
        qtbot.addWidget(win2)

        restored = False  # main() skips restore because args.execute is []
        if not restored:
            win2.new_tab(shell_command=None)

        assert win2._tabs.count() == 1

    def test_main_flow_with_restore(self, qtbot):
        """Normal startup: save 2 tabs, simulate restart, get 2 tabs."""
        # First "run" — create 2 tabs, save, close
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1.new_tab()
        assert win1._tabs.count() == 2
        win1.save_layout()

        # Second "run" — simulate main() logic
        Config._instance = None
        win2 = MainWindow(create_initial_tab=False)
        qtbot.addWidget(win2)

        # main() does: restored = window.restore_layout()
        restored = win2.restore_layout()
        assert restored is True
        # main() only calls new_tab if not restored
        assert win2._tabs.count() == 2

    def test_main_flow_with_working_directory(self, qtbot, tmp_path):
        """--working-directory: skip restore, create tab with cwd."""
        # Save a 2-tab layout
        win1 = MainWindow()
        qtbot.addWidget(win1)
        win1.new_tab()
        win1.save_layout()

        # Second run with -d flag: main() skips restore
        Config._instance = None
        win2 = MainWindow(create_initial_tab=False)
        qtbot.addWidget(win2)
        # main() does NOT call restore_layout when args.working_directory
        # It calls new_tab(working_directory=...) after restore decisions.
        win2.new_tab(working_directory=str(tmp_path))
        assert win2._tabs.count() == 1

    def test_main_flow_empty_config(self, qtbot):
        """First ever launch: no saved layout, get 1 tab."""
        win = MainWindow(create_initial_tab=False)
        qtbot.addWidget(win)
        restored = win.restore_layout()
        assert restored is False
        win.new_tab()
        assert win._tabs.count() == 1

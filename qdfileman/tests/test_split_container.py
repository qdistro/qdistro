"""Unit tests for SplitContainer."""

from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from qfileman.pane import FilePane
from qfileman.split_container import SplitContainer


@pytest.fixture
def container(qapp):
    c = SplitContainer(Qt.Orientation.Horizontal)
    yield c
    c.close()
    c.deleteLater()
    qapp.processEvents()
    qapp.processEvents()


def _make_pane():
    return FilePane()


def test_add_pane_appends_widget(container):
    p = _make_pane()
    container.add_pane(p)
    assert container.count() == 1
    assert container.widget(0) is p


def test_find_panes_collects_in_left_to_right_order(container):
    p1, p2, p3 = _make_pane(), _make_pane(), _make_pane()
    container.add_pane(p1)
    container.add_pane(p2)
    container.add_pane(p3)
    assert container.find_panes() == [p1, p2, p3]


def test_split_same_orientation_inserts_next_to_target(container):
    p1 = _make_pane()
    container.add_pane(p1)
    # First split sets orientation; same orientation again inserts a sibling.
    new1 = container.split(p1, Qt.Orientation.Horizontal, _make_pane)
    assert new1 is not None
    new2 = container.split(p1, Qt.Orientation.Horizontal, _make_pane)
    assert container.orientation() == Qt.Orientation.Horizontal
    # All three should be direct children.
    panes = container.find_panes()
    assert len(panes) == 3
    assert p1 in panes and new1 in panes and new2 in panes


def test_split_cross_orientation_wraps_in_nested_container(container):
    p1 = _make_pane()
    p2 = _make_pane()
    container.add_pane(p1)
    container.add_pane(p2)  # two children, horizontal
    # Cross-orientation split of p1 → wraps in vertical sub-splitter.
    new = container.split(p1, Qt.Orientation.Vertical, _make_pane)
    assert new is not None
    # Root container still has 2 direct children (the nested splitter + p2).
    assert container.count() == 2
    # find_panes() walks the tree and returns 3 leaves total.
    panes = container.find_panes()
    assert len(panes) == 3
    assert p1 in panes and p2 in panes and new in panes
    # The nested splitter exists somewhere as a child.
    has_nested = any(
        isinstance(container.widget(i), SplitContainer)
        for i in range(container.count())
    )
    assert has_nested


def test_split_only_one_child_changes_orientation(container):
    p1 = _make_pane()
    container.add_pane(p1)
    assert container.count() == 1
    new = container.split(p1, Qt.Orientation.Vertical, _make_pane)
    assert new is not None
    assert container.orientation() == Qt.Orientation.Vertical
    assert container.count() == 2


def test_remove_pane_drops_from_tree(container, qapp):
    p1, p2 = _make_pane(), _make_pane()
    container.add_pane(p1)
    container.add_pane(p2)
    empty = container.remove_pane(p1)
    qapp.processEvents()
    assert empty is False
    assert container.find_panes() == [p2]


def test_remove_pane_collapses_nested_single_child(container, qapp):
    """After removing one leaf in a nested splitter, the nested splitter
    should be collapsed and its remaining leaf promoted to this level."""
    p1, p2 = _make_pane(), _make_pane()
    container.add_pane(p1)
    container.add_pane(p2)
    # Cross-orientation split wraps p1 in a nested splitter.
    new = container.split(p1, Qt.Orientation.Vertical, _make_pane)
    # Now remove the new pane → the nested splitter has only p1 left,
    # which should be promoted up.
    container.remove_pane(new)
    qapp.processEvents()
    panes = container.find_panes()
    assert set(panes) == {p1, p2}
    # After collapse, p1 and p2 should be direct children again.
    direct_children = [container.widget(i) for i in range(container.count())]
    assert p1 in direct_children
    assert p2 in direct_children


def test_remove_pane_returns_true_when_emptied(container, qapp):
    p1 = _make_pane()
    container.add_pane(p1)
    empty = container.remove_pane(p1)
    qapp.processEvents()
    assert empty is True


def test_remove_unknown_pane_returns_false(container, qapp):
    """Removing a pane that isn't in this tree must not raise."""
    p1 = _make_pane()
    container.add_pane(p1)
    stray = _make_pane()
    try:
        empty = container.remove_pane(stray)
        qapp.processEvents()
        assert empty is False
        # The known pane should still be there.
        assert container.find_panes() == [p1]
    finally:
        stray.deleteLater()


def test_panes_get_sensible_sizes_when_added_before_show(qapp, container):
    """Regression test for the 1-px-pane bug discovered in code review:
    panes added to a not-yet-shown SplitContainer must end up with
    proportional sizes once the container is shown, not collapsed to
    a single pixel each."""
    p1, p2, p3 = _make_pane(), _make_pane(), _make_pane()
    container.add_pane(p1)
    container.add_pane(p2)
    container.add_pane(p3)
    container.resize(900, 600)
    container.show()
    qapp.processEvents()
    sizes = container.sizes()
    assert all(s > 10 for s in sizes), (
        f"Every pane should have a sensible size after show; got {sizes}"
    )
    assert sum(sizes) > 100, f"Total extent should be substantial; got {sum(sizes)}"
    # The three panes should be roughly equal — within 5 pixels of each
    # other. QSplitter floors rounding so allow some slack.
    assert max(sizes) - min(sizes) <= 5, (
        f"Panes should be approximately equal-sized; got {sizes}"
    )
    container.hide()


def test_panes_get_sensible_sizes_when_added_after_show(qapp, container):
    """Panes added *after* the container is laid out should also be sized
    correctly — exercising the setSizes branch in _equalize."""
    container.resize(900, 600)
    container.show()
    qapp.processEvents()
    p1 = _make_pane()
    container.add_pane(p1)
    qapp.processEvents()
    p2 = _make_pane()
    container.add_pane(p2)
    qapp.processEvents()
    sizes = container.sizes()
    assert all(s > 10 for s in sizes), f"Got {sizes}"
    container.hide()


def test_split_unknown_target_returns_none(container):
    p1 = _make_pane()
    container.add_pane(p1)
    stray = _make_pane()
    try:
        result = container.split(stray, Qt.Orientation.Horizontal, _make_pane)
        assert result is None
    finally:
        stray.deleteLater()

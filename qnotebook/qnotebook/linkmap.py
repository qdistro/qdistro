"""Link map dock: 1-hop graph of pages around the current page.

Uses QGraphicsScene with circle items for pages and line items for links.
Forward links + backlinks (backlinks rendered with a dashed pen).
"""

from __future__ import annotations

import math
from collections.abc import Callable

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QPalette, QPen
from PyQt6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

try:
    import networkx as nx  # type: ignore
    HAS_NETWORKX = True
except Exception:
    nx = None  # type: ignore
    HAS_NETWORKX = False


def force_layout(nodes: list[str], edges: list[tuple[str, str]],
                 radius: float = 200.0) -> dict[str, tuple[float, float]]:
    """Spring layout positions for `nodes` with `edges`.

    Uses networkx.spring_layout when available; falls back to a simple
    circular layout otherwise."""
    if not nodes:
        return {}
    if HAS_NETWORKX:
        try:
            g = nx.Graph()
            for n in nodes:
                g.add_node(n)
            for s, d in edges:
                if s in g and d in g:
                    g.add_edge(s, d)
            pos = nx.spring_layout(g, seed=1, scale=radius)
            return {k: (float(v[0]), float(v[1])) for k, v in pos.items()}
        except Exception:
            pass
    # Circular fallback
    out: dict[str, tuple[float, float]] = {}
    n = len(nodes)
    for i, name in enumerate(nodes):
        ang = 2 * math.pi * i / max(1, n)
        out[name] = (radius * math.cos(ang), radius * math.sin(ang))
    return out


NODE_RADIUS = 24


def graph_colors(palette: QPalette) -> dict[str, QColor]:
    """Node/edge/label colours from the widget palette, so the map follows
    the desktop's light/dark mode instead of fixed dark-on-light hex values
    (unreadable labels and edges on a dark palette)."""
    text = palette.color(QPalette.ColorRole.WindowText)
    edge = QColor(text)
    edge.setAlpha(170)
    return {
        "center": palette.color(QPalette.ColorRole.Highlight),
        "peer": palette.color(QPalette.ColorRole.Mid),
        "outline": text,
        "edge": edge,
        "label": text,
    }


class _PageNode(QGraphicsEllipseItem):
    def __init__(self, page: str, x: float, y: float, is_center: bool,
                 on_click: Callable[[str], None],
                 colors: dict[str, QColor]) -> None:
        super().__init__(x - NODE_RADIUS, y - NODE_RADIUS, NODE_RADIUS * 2, NODE_RADIUS * 2)
        self.page = page
        self.is_center = is_center
        self._on_click = on_click
        self.apply_colors(colors)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def apply_colors(self, colors: dict[str, QColor]) -> None:
        self.setBrush(QBrush(colors["center" if self.is_center else "peer"]))
        self.setPen(QPen(colors["outline"], 1))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click(self.page)
            event.accept()
            return
        super().mousePressEvent(event)


class LinkMapDock(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        top.addWidget(QLabel("Hops:", self))
        self.hops_spin = QSpinBox(self)
        self.hops_spin.setRange(1, 3)
        self.hops_spin.setValue(1)
        self.hops_spin.valueChanged.connect(self._on_hops_changed)
        top.addWidget(self.hops_spin)
        top.addStretch(1)
        v.addLayout(top)
        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene, self)
        self.view.setRenderHints(self.view.renderHints())
        v.addWidget(self.view, 1)
        self._on_navigate: Callable[[str], None] = lambda _p: None
        self._on_hops_request: Callable[[int], None] = lambda _h: None
        self._nodes: dict[str, _PageNode] = {}
        self._edges: list[QGraphicsLineItem] = []
        self._labels: list[QGraphicsSimpleTextItem] = []

    def _colors(self) -> dict[str, QColor]:
        return graph_colors(self.palette())

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self._recolor()

    def _recolor(self) -> None:
        colors = self._colors()
        for node in self._nodes.values():
            node.apply_colors(colors)
        for line in self._edges:
            pen = line.pen()
            pen.setColor(colors["edge"])
            line.setPen(pen)
        for label in self._labels:
            label.setBrush(QBrush(colors["label"]))

    def _clear(self) -> None:
        self.scene.clear()
        self._nodes = {}
        self._edges = []
        self._labels = []

    def hops(self) -> int:
        return self.hops_spin.value()

    def set_on_hops_changed(self, cb: Callable[[int], None]) -> None:
        self._on_hops_request = cb

    def _on_hops_changed(self, value: int) -> None:
        self._on_hops_request(value)

    def set_on_navigate(self, cb: Callable[[str], None]) -> None:
        self._on_navigate = cb

    def build(self, current_page: str | None, forward: list[str], backward: list[str]) -> None:
        self._clear()
        if current_page is None:
            return
        center = QPointF(0.0, 0.0)
        self._add_node(current_page, center, is_center=True)
        # Place forward links on right half; back on left half of circle.
        all_peers: list[tuple[str, str]] = []
        for p in forward:
            if p != current_page:
                all_peers.append(("fwd", p))
        for p in backward:
            if p != current_page and all(p != q for _, q in all_peers):
                all_peers.append(("back", p))
        count = len(all_peers)
        radius = 130
        for i, (kind, page) in enumerate(all_peers):
            if count == 1:
                angle = 0.0
            else:
                angle = 2 * math.pi * i / count
            x = radius * math.cos(angle)
            y = radius * math.sin(angle)
            self._add_node(page, QPointF(x, y), is_center=False)
            pen = QPen(self._colors()["edge"], 1.5)
            if kind == "back":
                pen.setStyle(Qt.PenStyle.DashLine)
            line = QGraphicsLineItem(center.x(), center.y(), x, y)
            line.setPen(pen)
            line.setZValue(-1)
            self.scene.addItem(line)
            self._edges.append(line)
        rect = self.scene.itemsBoundingRect().adjusted(-30, -30, 30, 30)
        self.scene.setSceneRect(rect)
        self.view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def build_multihop(
        self,
        current_page: str | None,
        edges: list[tuple[str, str]],
        nodes: list[str] | None = None,
    ) -> None:
        """Build a multi-hop graph using force layout.

        `edges` are (src, dst) pairs. `nodes` defaults to the union of all
        edge endpoints plus current_page."""
        self._clear()
        if current_page is None:
            return
        all_nodes: list[str]
        if nodes is None:
            seen: list[str] = []
            for s, d in edges:
                for n in (s, d):
                    if n not in seen:
                        seen.append(n)
            if current_page not in seen:
                seen.insert(0, current_page)
            all_nodes = seen
        else:
            all_nodes = list(nodes)
        positions = force_layout(all_nodes, edges, radius=180.0)
        for name, (x, y) in positions.items():
            self._add_node(name, QPointF(x, y), is_center=(name == current_page))
        for s, d in edges:
            if s not in positions or d not in positions:
                continue
            sx, sy = positions[s]
            dx, dy = positions[d]
            line = QGraphicsLineItem(sx, sy, dx, dy)
            line.setPen(QPen(self._colors()["edge"], 1.2))
            line.setZValue(-1)
            self.scene.addItem(line)
            self._edges.append(line)
        rect = self.scene.itemsBoundingRect().adjusted(-30, -30, 30, 30)
        self.scene.setSceneRect(rect)
        self.view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def _add_node(self, page: str, pos: QPointF, is_center: bool) -> None:
        colors = self._colors()
        node = _PageNode(page, pos.x(), pos.y(), is_center, self._on_navigate,
                         colors)
        self.scene.addItem(node)
        self._nodes[page] = node
        label = QGraphicsSimpleTextItem(page.rsplit(":", 1)[-1])
        f = QFont()
        f.setPointSize(9)
        label.setFont(f)
        label.setBrush(QBrush(colors["label"]))
        self._labels.append(label)
        br = label.boundingRect()
        label.setPos(pos.x() - br.width() / 2, pos.y() + NODE_RADIUS + 2)
        self.scene.addItem(label)

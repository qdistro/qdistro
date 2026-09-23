"""Session snapshot: open pages, split layout, cursor positions, docks, find bar.

Serialized as JSON in `<notebook>/.qnotebook/session.json`. Toggleable via
QSettings key `session_restore_enabled` (default True)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .notebook import DOTDIR


def session_path(notebook_root: Path) -> Path:
    return notebook_root / DOTDIR / "session.json"


def save(notebook_root: Path, data: dict[str, Any]) -> None:
    p = session_path(notebook_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(notebook_root: Path) -> dict[str, Any]:
    p = session_path(notebook_root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def capture(window) -> dict[str, Any]:
    """Build a session dict from the current MainWindow state."""
    data: dict[str, Any] = {}
    if getattr(window, "_current_page", None):
        data["current_page"] = window._current_page
    data["primary_cursor"] = window.editor.textCursor().position()
    data["split"] = None
    if window.is_split() and window._secondary_editor is not None:
        data["split"] = {
            "orientation": ("horizontal" if window._editor_split.orientation().value ==
                             _H_ORIENT_VALUE else "vertical"),
            "page": getattr(window._secondary_editor, "_current_path", None),
            "cursor": window._secondary_editor.textCursor().position(),
        }
    data["docks"] = {
        "backlinks": window.backlinks_dock.isVisible(),
        "tags": window.tags_dock.isVisible(),
        "linkmap": window.linkmap_dock.isVisible(),
        "toc": window.toc_dock.isVisible(),
        "calendar": window.calendar_dock.isVisible(),
        "search": window.search_dock.isVisible(),
    }
    fb = window.find_bar
    data["find_bar"] = {
        "visible": fb.isVisible(),
        "text": fb.input.text(),
        "case_sensitive": fb.case_checkbox.isChecked(),
    }
    return data


def restore(window, data: dict[str, Any]) -> None:
    """Apply a captured session dict to the MainWindow."""
    if not data:
        return
    pg = data.get("current_page")
    if pg and window.notebook is not None and window.notebook.exists(pg):
        window.load_page(pg)
        pos = int(data.get("primary_cursor") or 0)
        if pos > 0:
            _set_cursor_pos(window.editor, pos)
    split = data.get("split") or None
    if split:
        window.split_editor(split.get("orientation", "horizontal"))
        sp = split.get("page")
        if sp and window.notebook.exists(sp) and window._secondary_editor is not None:
            window._secondary_editor.load_markdown(
                window.notebook.get_page(sp),
                page_path=sp,
                base_path=window.notebook.file_for(sp).parent,
            )
            spos = int(split.get("cursor") or 0)
            if spos > 0:
                _set_cursor_pos(window._secondary_editor, spos)
    docks = data.get("docks") or {}
    _set_dock(window.backlinks_dock, docks.get("backlinks", True))
    _set_dock(window.tags_dock, docks.get("tags", False))
    _set_dock(window.linkmap_dock, docks.get("linkmap", False))
    _set_dock(window.toc_dock, docks.get("toc", False))
    _set_dock(window.calendar_dock, docks.get("calendar", False))
    _set_dock(window.search_dock, docks.get("search", False))
    fb = data.get("find_bar") or {}
    if fb.get("visible"):
        window.find_bar.input.setText(fb.get("text", ""))
        window.find_bar.case_checkbox.setChecked(bool(fb.get("case_sensitive")))
        window.find_bar.show()


def _set_cursor_pos(editor, pos: int) -> None:
    cur = editor.textCursor()
    doc_len = editor.document().characterCount()
    if 0 <= pos < doc_len:
        cur.setPosition(pos)
        editor.setTextCursor(cur)


def _set_dock(dock, visible: bool) -> None:
    if visible and not dock.isVisible():
        dock.show()
    elif not visible and dock.isVisible():
        dock.hide()


from PyQt6.QtCore import Qt as _Qt  # noqa: E402

_H_ORIENT_VALUE = _Qt.Orientation.Horizontal.value

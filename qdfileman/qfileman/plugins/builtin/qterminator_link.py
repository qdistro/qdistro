"""QTerminator link plugin for QFileMan.

Wires the file manager to a running QTerminator tab so that:

* Entering a directory in QFileMan types ``cd <dir>`` into the linked
  tab automatically (NavigationHook).
* A context menu entry lets you push the selected path into the linked
  tab as text (MenuProvider, "Send Path to QTerminator").
* Another entry lets you pick which tab to link to, or unlink.

The link is process-wide: a single linked tab at a time. The tab id
is whatever ``list_tabs`` returns from agent_control, which is stable
for the lifetime of the QTerminator tab (it's the Python ``id()`` of
the underlying terminal widget). If the tab is closed, the next
``cd`` will silently fail; the user can re-link from the menu.

If the agent_control socket isn't reachable, the menu entries are
hidden — no point clicking them when nothing's listening.
"""

from __future__ import annotations

import logging
import os

from qfileman.plugin import MenuProvider, NavigationHook

log = logging.getLogger(__name__)


class _LinkState:
    """Shared between the MenuProvider and the NavigationHook instances."""

    tab_id: int | None = None
    tab_title: str = ""

    @classmethod
    def clear(cls) -> None:
        cls.tab_id = None
        cls.tab_title = ""

    @classmethod
    def set(cls, tab_id: int, title: str) -> None:
        cls.tab_id = tab_id
        cls.tab_title = title


def _qt():
    """Lazy import so import-time failures don't break plugin discovery."""
    from qfileman.plugins.builtin import _qterminator
    return _qterminator


def _socket_available() -> bool:
    try:
        return _qt().is_available()
    except Exception:
        return False


class QTerminatorLinkMenu(MenuProvider):
    name = "qterminator_link"
    description = "Link QFileMan navigation to a QTerminator tab"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path:
            return []
        if not _socket_available():
            return []
        items = []
        if _LinkState.tab_id is None:
            items.append(("Link with QTerminator…", self._link))
        else:
            label = f"Unlink QTerminator ({_LinkState.tab_title})"
            items.append((label, self._unlink))
            items.append(("Send Path to QTerminator", self._send_path))
        return items

    def _link(self, _path: str) -> None:
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        try:
            tabs = _qt().list_tabs()
        except Exception as e:
            QMessageBox.warning(None, "QTerminator", f"list_tabs failed: {e}")
            return
        if not tabs:
            QMessageBox.information(None, "QTerminator", "No tabs to link to.")
            return
        # Build a "title — cwd" label so the user can tell tabs apart.
        labels = [
            f"{t.get('title') or '(untitled)'} — {t.get('working_directory') or ''}"
            for t in tabs
        ]
        choice, ok = QInputDialog.getItem(
            None, "Link with QTerminator", "Tab:", labels, 0, False,
        )
        if not ok:
            return
        idx = labels.index(choice)
        _LinkState.set(int(tabs[idx]["id"]), tabs[idx].get("title") or "tab")

    def _unlink(self, _path: str) -> None:
        _LinkState.clear()

    def _send_path(self, path: str) -> None:
        if _LinkState.tab_id is None:
            return
        try:
            _qt().send_text(_LinkState.tab_id, path)
        except Exception as e:
            log.warning("send_text failed: %s", e)


class QTerminatorLinkHook(NavigationHook):
    """Pushes ``cd <new-dir>`` into the linked tab on every directory change."""

    name = "qterminator_link_hook"
    description = "Send cd to the linked QTerminator tab on navigation"
    version = "1.0"

    def on_enter_directory(self, path):
        if _LinkState.tab_id is None:
            return True
        if not path or not os.path.isdir(path):
            return True
        try:
            _qt().cd(_LinkState.tab_id, path)
        except Exception as e:
            # Tab might have been closed since we linked. Log and clear
            # the link so the user gets the chance to re-pick next time.
            log.info("qterminator cd failed; unlinking: %s", e)
            _LinkState.clear()
        return True

"""Bookmarks plugin for QFileMan.

Adds bookmark management entries to the file context menu.
"""

import logging

from qfileman.config import Config
from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


class BookmarksPlugin(MenuProvider):
    name = "bookmarks"
    description = "Manage folder bookmarks"
    version = "1.0"
    category = "File"

    def get_menu_items(self, path):
        if not path:
            return []

        items = [
            ("Add to Bookmarks", lambda p: self._add_bookmark(p)),
        ]

        # Offer "Remove" only if this path is currently bookmarked.
        for bm in Config().get_bookmarks():
            if bm.get("path") == path:
                label = f"Remove Bookmark: {bm.get('name')}"
                items.append((label, lambda p: self._remove_bookmark(p)))
                break

        return items

    def _add_bookmark(self, path):
        Config().add_bookmark(path)
        log.info("bookmark added: %s", path)

    def _remove_bookmark(self, path):
        Config().remove_bookmark(path)
        log.info("bookmark removed: %s", path)

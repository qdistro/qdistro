"""KFind plugin for QFileMan.

Adds *Find Files (KFind)…* to the context menu. KFind is KDE's
classic GUI front-end for find; this plugin just launches it scoped to
the current directory so right-click → search → fill the dialog feels
the way Krusader / Dolphin users expect.

If ``kfind`` isn't on PATH the menu entry isn't shown at all, so the
user doesn't see a "click me to get a not-installed warning" option.
``kfind`` accepts the path to search in as a positional argument.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


# Some distros only ship the legacy name; check both.
_KFIND_CANDIDATES = ("kfind", "kde-find")


def find_kfind_binary() -> str | None:
    for name in _KFIND_CANDIDATES:
        bin_ = shutil.which(name)
        if bin_:
            return bin_
    return None


class KFindPlugin(MenuProvider):
    name = "kfind"
    description = "Launch KFind in the current directory"
    version = "1.0"
    category = "Tools"

    def get_menu_items(self, path):
        if not path:
            return []
        if find_kfind_binary() is None:
            # Quietly hide if KFind isn't installed; not every user is on KDE.
            return []
        return [("Find Files (KFind)...", self._launch)]

    def _launch(self, path: str) -> None:
        bin_ = find_kfind_binary()
        if bin_ is None:
            return
        directory = path if os.path.isdir(path) else os.path.dirname(path) or "."
        try:
            subprocess.Popen([bin_, directory])
        except OSError as e:
            log.warning("kfind launch failed: %s", e)
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(None, "KFind", f"Launch failed: {e}")

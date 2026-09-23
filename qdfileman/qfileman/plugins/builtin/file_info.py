"""File info plugin for QFileMan.

Shows detailed file information in the status bar or dialog.
"""

import logging
import os
from datetime import datetime

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


class FileInfoPlugin(MenuProvider):
    name = "file_info"
    description = "Show detailed file information"
    version = "1.0"
    category = "File"

    def get_menu_items(self, path):
        if not path:
            return []
        return [("File Info", lambda p: self._show_info(p))]

    def _show_info(self, path):
        from PyQt6.QtWidgets import QMessageBox

        info = [f"Path: {path}"]
        try:
            stat = os.stat(path)
        except OSError as e:
            log.warning("stat(%s) failed: %s", path, e)
            info.append(f"(stat failed: {e})")
        else:
            info.append(f"Size: {stat.st_size} bytes")
            info.append(f"Modified: {datetime.fromtimestamp(stat.st_mtime)}")
            info.append(f"Accessed: {datetime.fromtimestamp(stat.st_atime)}")
            info.append(f"Created: {datetime.fromtimestamp(stat.st_ctime)}")
            info.append(f"Permissions: {oct(stat.st_mode)[-3:]}")
            info.append(f"Type: {'Directory' if os.path.isdir(path) else 'File'}")

        msg = QMessageBox()
        msg.setWindowTitle("File Info")
        msg.setText("\n".join(info))
        msg.exec()

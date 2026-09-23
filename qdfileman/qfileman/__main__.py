"""Entry point for QFileMan."""

import argparse
import logging
import os
import sys

from PyQt6.QtWidgets import QApplication

from qfileman import __version__
from qfileman.config import Config
from qfileman.plugin import PluginManager
from qfileman.theme import apply_theme
from qfileman.window import FileManagerWindow

log = logging.getLogger(__name__)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="qfileman",
        description="QFileMan - Qt file manager",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=os.path.expanduser("~"),
        help="Starting directory path",
    )
    parser.add_argument(
        "--no-plugins",
        action="store_true",
        help="Disable plugin loading",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(argv)


def setup_window(args, window, plugin_manager):
    """Wire the window with plugins and starting path based on parsed args.

    Split out from main() so it can be tested without an event loop.
    """
    if not args.no_plugins:
        plugin_manager.discover()
        config = Config()
        enabled = config.get("plugins", "enabled", default=[])
        for name in enabled:
            plugin_manager.enable(name, window)
        window.set_plugin_manager(plugin_manager)
    else:
        log.info("plugin loading disabled by --no-plugins")

    if args.path:
        window._update_path(args.path)


def main():
    args = parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("QFileMan")
    app.setApplicationVersion(__version__)

    # Theme must be applied before any widgets are constructed so the
    # palette propagates to every QPalette inheritor at creation time.
    theme_mode = Config().get("general", "theme_mode", default="system")
    apply_theme(app, theme_mode)

    window = FileManagerWindow()
    plugin_manager = PluginManager()
    setup_window(args, window, plugin_manager)

    # qdistro App1 registration — caught so a missing SDK / bus never
    # blocks the file manager from starting.
    try:
        from qfileman import qdistro_integration as _qdi
        window._qdistro_receiver = _qdi.maybe_install(window)
    except Exception as _qd_e:  # noqa: BLE001
        print(f"[qfileman] qdistro App1 registration failed: {_qd_e}",
              file=sys.stderr, flush=True)

    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

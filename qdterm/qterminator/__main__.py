"""Entry point for QTerminator."""

import argparse
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from qterminator.config import Config
from qterminator.theme import apply_theme
from qterminator.window import MainWindow


def parse_args():
    parser = argparse.ArgumentParser(
        prog="qterminator",
        description="QTerminator - Qt terminal emulator",
    )
    # Compatible with Terminator
    parser.add_argument(
        "-d", "--working-directory",
        metavar="DIR",
        help="Set the working directory for the terminal",
    )
    parser.add_argument(
        "-T", "--title",
        dest="title",
        help="Specify a title for the window",
    )
    parser.add_argument(
        "-e", "--command",
        dest="command",
        help="Specify a single command to execute inside the terminal",
    )
    parser.add_argument(
        "-x", "--execute",
        dest="execute",
        nargs=argparse.REMAINDER,
        help="Use the rest of the command line as a command to execute",
    )
    parser.add_argument(
        "--geometry",
        help="Window geometry as WxH or WxH+X+Y (e.g., 1024x768 or 1024x768+100+50)",
    )
    parser.add_argument(
        "-m", "--maximise", "-M", "--maximize",
        action="store_true",
        dest="maximise",
        help="Maximize the window",
    )
    parser.add_argument(
        "-f", "--fullscreen",
        action="store_true",
        help="Make the window fill the screen",
    )
    parser.add_argument(
        "-b", "--borderless",
        action="store_true",
        help="Disable window borders (frameless window)",
    )
    parser.add_argument(
        "-H", "--hidden",
        action="store_true",
        help="Hide the window at startup",
    )
    parser.add_argument(
        "-p", "--profile",
        help="Use a different profile as the default",
    )
    parser.add_argument(
        "-l", "--layout",
        help="Launch with the given saved layout name",
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="List all configured profiles and exit",
    )
    parser.add_argument(
        "--list-layouts",
        action="store_true",
        help="List all saved layouts and exit",
    )
    parser.add_argument(
        "-r", "--role",
        dest="role",
        help="Set a custom WM_WINDOW_ROLE property on the window",
    )
    parser.add_argument(
        "-i", "--icon",
        dest="icon",
        help="Set a custom icon for the window (path to image file)",
    )
    parser.add_argument(
        "--new-tab",
        action="store_true",
        help="Open a new tab in the running QTerminator instance",
    )
    # QTerminator-specific
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="Don't restore the previous session layout",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )
    return parser.parse_args()


def _apply_geometry(window, geometry):
    """Parse Terminator-style geometry: WxH or WxH+X+Y."""
    try:
        # Strip leading '+' or '-'
        geom = geometry
        size_part = geom
        x = y = None
        # Check for position
        for sep in ['+', '-']:
            if sep in geom[1:]:
                idx = geom.index(sep, 1)
                size_part = geom[:idx]
                pos_part = geom[idx:]
                # Parse "+X+Y" or "+X-Y" etc.
                import re
                m = re.match(r'([+-]\d+)([+-]\d+)', pos_part)
                if m:
                    x = int(m.group(1))
                    y = int(m.group(2))
                break
        w, h = size_part.split("x")
        window.resize(int(w), int(h))
        if x is not None and y is not None:
            window.move(x, y)
    except (ValueError, IndexError):
        pass


def _set_process_name(name="qterminator"):
    """Set process name shown in ps/top/htop.

    Tries setproctitle (changes argv[0]) first, falls back to
    prctl PR_SET_NAME (changes only /proc/PID/comm, max 15 chars).
    """
    try:
        import setproctitle
        setproctitle.setproctitle(name)
        return
    except ImportError:
        pass
    # Fallback: Linux-specific prctl
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        # PR_SET_NAME = 15. Comm is limited to 15 chars + null.
        libc.prctl(15, name.encode("ascii")[:15], 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _execute_shell_command(args):
    """Return argv for -x/--execute, or None when normal shell starts."""
    return list(args.execute) if args.execute else None


def main():
    _set_process_name("qterminator")
    args = parse_args()

    # Handle list-only modes early (don't open GUI)
    if args.list_profiles:
        config = Config()
        for p in config.list_profiles():
            print(p)
        sys.exit(0)
    if args.list_layouts:
        config = Config()
        layouts = config.get("layouts", default={})
        if isinstance(layouts, dict):
            for name in layouts:
                print(name)
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setApplicationName("QTerminator")
    app.setApplicationVersion("0.1.0")

    config = Config()
    theme_mode = config.get("general", "theme_mode", default="system")
    resolved_theme = apply_theme(app, theme_mode)

    # Set custom icon if given
    if args.icon:
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(args.icon))

    # Borderless window (frameless)
    window_flags = None
    if args.borderless:
        window_flags = Qt.WindowType.FramelessWindowHint

    window = MainWindow(
        resolved_theme=resolved_theme,
        create_initial_tab=False,
    )
    if window_flags:
        window.setWindowFlags(window_flags)

    # Set window role for window managers
    if args.role:
        window.setObjectName(args.role)

    # Determine which profile to use as default
    if args.profile:
        # Validate the profile exists
        if args.profile in config.list_profiles():
            config.set("profiles", "_active_default", args.profile)

    # Try to restore layout
    restored = False
    if args.layout:
        # Restore named layout if it exists
        layouts = config.get("layouts", default={})
        if isinstance(layouts, dict) and args.layout in layouts:
            from qterminator.layout import restore_layout
            restore_layout(window, layouts[args.layout])
            restored = True
    elif (not args.no_restore
            and not args.working_directory
            and args.execute is None
            and not args.command):
        restored = window.restore_layout()

    execute_argv = _execute_shell_command(args)

    if not restored:
        window.new_tab(
            working_directory=args.working_directory,
            shell_command=execute_argv,
        )

    if args.title:
        window.setWindowTitle(args.title)

    if args.geometry:
        _apply_geometry(window, args.geometry)
    elif not args.no_restore:
        window.restore_window_state()
    else:
        window.resize(800, 500)

    # Execute command if given
    cmd_text = None
    if args.command:
        cmd_text = args.command

    if cmd_text and window._active_terminal:
        window._active_terminal.send_text(cmd_text + "\n")

    # Register on the qdistro App1 session bus so PodApps sees us and
    # peer apps' Send-To menus can find us. Held on the window to keep
    # the bus name claim alive for the process lifetime; release is
    # automatic when window (and so the receiver) is GC'd at exit.
    try:
        from qterminator import qdistro_integration as _qdi
        window._qdistro_receiver = _qdi.maybe_install(window)
    except Exception as _qd_e:  # noqa: BLE001
        # Never let a bus / SDK quirk block app startup.
        print(f"[qterminator] qdistro App1 registration failed: {_qd_e}",
              file=sys.stderr, flush=True)

    # Window state
    if args.fullscreen:
        window.showFullScreen()
    elif args.maximise:
        window.showMaximized()
    elif args.hidden:
        window.hide()
    else:
        window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()

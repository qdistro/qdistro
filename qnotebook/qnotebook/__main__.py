"""Entry point: python3 -m qnotebook"""

import sys


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv
    # Headless CLI commands short-circuit before any QApplication is created.
    if len(argv) > 1 and any(a.startswith("--") for a in argv[1:]):
        from qnotebook import cli
        rc = cli.run(argv[1:])
        if rc >= 0:
            return rc
        # rc == -1 → fall through to GUI
    from PyQt6.QtWidgets import QApplication

    from qnotebook.window import MainWindow
    app = QApplication(argv)
    app.setApplicationName("qnotebook")
    app.setOrganizationName("qnotebook")
    notebook = argv[1] if len(argv) > 1 else None
    page = argv[2] if len(argv) > 2 else None
    win = MainWindow(notebook_path=notebook)
    if page:
        try:
            win.load_page(page)
        except Exception:
            pass
    # qdistro App1 registration — caught so a missing SDK / bus never
    # blocks notebook startup. The receiver lives on the window to
    # keep the bus-name claim alive for the process lifetime.
    try:
        from qnotebook import qdistro_integration as _qdi
        win._qdistro_receiver = _qdi.maybe_install(win)
    except Exception as _qd_e:  # noqa: BLE001
        print(f"[qnotebook] qdistro App1 registration failed: {_qd_e}",
              file=sys.stderr, flush=True)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

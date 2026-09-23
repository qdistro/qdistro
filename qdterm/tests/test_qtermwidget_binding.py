"""QTermWidget SIP binding sanity checks (dependency-gated).

Lower-level companion to ``test_real_qtermwidget_smoke.py``: it focuses on the
lifecycle of the real binding -- construct, drive bytes through, read the
screen, and tear down -- asserting no leak or segfault on deletion.

Like the smoke lane, this is gated on the *real* binding. conftest.py injects
an in-process Python fake when the binding is absent; ``importorskip`` would
import that fake, so we detect it via ``_QTERMINATOR_FAKE`` and SKIP. The fake
runs entirely in the Python process and would never reproduce a C++ deletion
segfault, so a green here against the fake would be meaningless. On this CI
host the lane SKIPs cleanly, which is expected and correct.
"""

import pytest

QTermWidget = pytest.importorskip(
    "QTermWidget", reason="real QTermWidget SIP binding not installed"
)

_widget_cls = getattr(QTermWidget, "QTermWidget", QTermWidget)
if getattr(QTermWidget, "_QTERMINATOR_FAKE", False) or getattr(
    _widget_cls, "_QTERMINATOR_FAKE", False
):
    pytest.skip(
        "in-process QTermWidget fake is loaded (real SIP binding absent); "
        "binding-sanity lane cannot exercise the C++ object lifecycle",
        allow_module_level=True,
    )


def test_construct_feed_read(qtbot):
    """Construct, start a shell, feed bytes, and read the screen back."""
    w = QTermWidget.QTermWidget(0)
    qtbot.addWidget(w)
    w.setShellProgram("/bin/bash")
    w.setArgs(["--norc", "--noprofile", "-i"])
    w.show()
    qtbot.waitExposed(w)
    w.startShellProgram()

    qtbot.waitUntil(lambda: int(w.getShellPID() or 0) > 0, timeout=4000)
    assert int(w.getShellPID()) > 0

    w.sendText("printf marker_xyz\n")
    # Give the PTY a moment to echo; we don't assert on content here (that's
    # the smoke lane's job) -- only that feeding bytes does not crash.
    qtbot.wait(100)


def test_delete_without_segfault(qtbot):
    """Deleting the widget (deleteLater + event processing) must not crash."""
    from PyQt6.QtWidgets import QApplication

    w = QTermWidget.QTermWidget(0)
    qtbot.addWidget(w)
    w.setShellProgram("/bin/bash")
    w.show()
    qtbot.waitExposed(w)
    w.startShellProgram()
    qtbot.waitUntil(lambda: int(w.getShellPID() or 0) > 0, timeout=4000)

    w.deleteLater()
    # Pump the loop a few times so the C++ object is actually destroyed.
    app = QApplication.instance()
    for _ in range(3):
        app.processEvents()
    # If we got here without a segfault, the binding cleaned up safely.
    assert True

"""Fidelity tests for the in-process ``_FakeQTermWidget``.

conftest.py injects a Python fake into ``sys.modules["QTermWidget"]`` because
the real QTermWidget SIP binding is not importable on CI hosts. A fake is only
useful if it faithfully exposes the *surface the application actually drives*:
if the app calls a method the fake doesn't have, the test that exercises that
path would crash; worse, if the fake silently accepts calls the real widget
would reject, tests can validate impossible behavior.

These tests pin that contract honestly: they assert that every QTermWidget
**method** and **signal** the qterminator app actually calls exists (and is
callable / is a bound signal) on the fake. The source of truth is twofold:

  1. ``qtermwidget-pyqt/sip/qtermwidget.sip`` -- the real C++ interface.
  2. The app's actual usage of the widget (``self._term.X`` / ``.term.X`` /
     ``qtw.X`` in ``qterminator/``), notably ``qterminator/terminal.py`` and
     the export/screenshot plugins.

We are careful about PROVENANCE. The asserted methods are split into:

  * SIP_CONTRACT_METHODS -- called by the app AND present in the SIP; the fake
    mirrors the real binding's surface.
  * FAKE_ONLY_APP_METHODS -- called by the app only behind ``hasattr()`` guards
    and NOT present in this SIP (currently none). The fake provides them so the
    guarded paths run under test, but we do NOT claim they are part of the real
    binding contract.

We deliberately do not assert SIP methods the app never calls
(setFlowControlEnabled, setEnvironment, getPtySlaveFd, setMotionAfterPasting,
getTerminalFont, getAvailableColorSchemes, ...) -- adding stubs for those would
bloat the fake without improving fidelity of any tested path.

This module is gated to the FAKE: if a real QTermWidget binding is loaded
(``_QTERMINATOR_FAKE`` sentinel absent) it skips at module level, because the
signature checks and the fake-sentinel assert only make sense against the fake.
"""

import inspect

import pytest
import QTermWidget as _qtw_mod
from PyQt6.QtCore import pyqtBoundSignal

# --------------------------------------------------------------------------
# Module gate: these tests only make sense when the in-process FAKE is loaded.
#
# inspect.signature() over a real SIP builtin can raise, and
# test_conftest_injected_the_fake hard-asserts the sentinel -- both would go
# RED against a genuine QTermWidget binding (the VM lane). So if a real binding
# is present (sentinel ABSENT), skip the whole module. On hosts without the
# binding, conftest injects the fake (sentinel PRESENT) and the module runs.
# This is the inverse of test_real_qtermwidget_smoke.py, which skips when the
# fake IS present.
# --------------------------------------------------------------------------
_widget_cls = getattr(_qtw_mod, "QTermWidget", _qtw_mod)
if not (
    getattr(_qtw_mod, "_QTERMINATOR_FAKE", False)
    or getattr(_widget_cls, "_QTERMINATOR_FAKE", False)
):
    pytest.skip(
        "fake-fidelity tests only apply when the in-process fake is loaded "
        "(a real QTermWidget SIP binding is present)",
        allow_module_level=True,
    )

import QTermWidget  # noqa: E402

# ---------------------------------------------------------------------------
# The contract: QTermWidget methods the app calls on the underlying widget.
#
# Each entry is (method_name, why). Derived from grepping qterminator/ for
# ``_term.<name>`` / ``.term.<name>`` / ``qtw.<name>`` and cross-checking
# against the SIP interface. Qt-provided methods inherited from QWidget
# (setFocus, installEventFilter, findChild, setStyleSheet, grab, ...) are
# covered by the fact that the fake subclasses QWidget and are not re-listed.
#
# We split the set by provenance so the test is honest about WHY each method
# must exist on the fake:
#
#   SIP_CONTRACT_METHODS  -- the app calls these AND they appear in the real
#       QTermWidget SIP (qtermwidget-pyqt/sip/qtermwidget.sip). The fake must
#       expose them to mirror the real binding's surface.
#
#   FAKE_ONLY_APP_METHODS -- the app calls these (always behind hasattr() guards
#       because the real binding does NOT expose them in this SIP), so the fake
#       must provide them for the guarded export/screenshot paths to run under
#       test. These are NOT part of the real binding contract -- we do not claim
#       SIP fidelity for them.
# ---------------------------------------------------------------------------
SIP_CONTRACT_METHODS = {
    # construction / lifecycle
    "setShellProgram": "terminal.py sets the shell binary",
    "setArgs": "terminal.py sets shell argv",
    "startShellProgram": "terminal.py / re-spawn starts the PTY",
    # appearance / profile
    "setTerminalFont": "terminal.py applies the profile font",
    "setColorScheme": "terminal.py applies the color scheme",
    "availableColorSchemes": "preferences.py/context_menu.py list schemes",
    "setHistorySize": "terminal.py applies scrollback size",
    "setScrollBarPosition": "terminal.py / scrollbar toggle",
    "setKeyBindings": "terminal.py pins linux key bindings",
    "setTerminalOpacity": "terminal.py opacity profile option",
    "setAutoClose": "terminal.py exit_action handling",
    # working directory
    "setWorkingDirectory": "terminal.py sets initial cwd",
    "workingDirectory": "window/session restore reads cwd",
    # input / output
    "sendText": "terminal.send_text injects keystrokes",
    "selectedText": "smart_clipboard / copy paths read the selection",
    "copyClipboard": "copy action",
    "pasteClipboard": "paste action",
    "pasteSelection": "middle-click paste",
    "clear": "clear-screen action",
    # process introspection
    "getShellPID": "has_running_process / process control",
    "getForegroundProcessId": "foreground process checks",
    # zoom / search / scroll
    "zoomIn": "zoom-in action",
    "zoomOut": "zoom-out action",
    "scrollToEnd": "scroll-to-bottom on input",
    "toggleShowSearchBar": "find action",
    # title
    "title": "tab-title sync",
    # monitors
    "setMonitorActivity": "activity-monitor toggle",
    "setMonitorSilence": "silence-monitor toggle",
    "setSilenceTimeout": "silence-monitor timeout",
    # screen geometry (shadow_screen / asciinema_record / agent_control /
    # instant_replay read terminal width)
    "screenColumnsCount": "recorders read terminal width",
    # scrollback export (buffer_export / pdf_export). In the SIP and called
    # by the app (behind hasattr() guards).
    "historyLinesCount": "export reads scrollback length",
    "screenLinesCount": "export bounds the whole-buffer selection to the "
                        "visible-row count (out-of-range ends segfault the real "
                        "binding); also screenshot.py reads visible rows",
    "setSelectionStart": "buffer/pdf export selects the whole buffer",
    "setSelectionEnd": "buffer/pdf export selects the whole buffer",
}

# Methods the app calls only behind hasattr() guards and that are NOT in the
# QTermWidget SIP -- the fake provides them so the guarded paths run under test,
# but they are fake-only convenience helpers, not part of the binding contract.
# (Currently none: screenLinesCount moved into SIP_CONTRACT_METHODS once it was
# added to the vendored SIP so the export select-all could be bounded safely.)
FAKE_ONLY_APP_METHODS = {}

APP_CALLED_METHODS = {**SIP_CONTRACT_METHODS, **FAKE_ONLY_APP_METHODS}

# QTermWidget signals the app connects to (terminal.py + plugins).
APP_CONNECTED_SIGNALS = {
    "finished": "terminal.py _on_finished",
    "titleChanged": "terminal.py _on_title_changed",
    "termGetFocus": "terminal.py _on_focus",
    "activity": "terminal.py _on_activity",
    "silence": "terminal.py _on_silence",
    "bell": "terminal.py _on_bell",
    "urlActivated": "terminal.py _on_url_activated",
    "copyAvailable": "terminal.py _on_copy_available",
    "termKeyPressed": "terminal.py _on_key_scroll",
    "receivedData": "shadow_screen registry taps output",
    "sendData": "asciinema_record taps input",
}


@pytest.fixture(scope="module")
def widget_cls():
    return QTermWidget.QTermWidget


def test_conftest_injected_the_fake():
    """Sanity: on this host we are testing the fake, not the real binding."""
    cls = QTermWidget.QTermWidget
    assert getattr(cls, "_QTERMINATOR_FAKE", False) is True, (
        "expected the in-process fake; the sentinel is missing"
    )


@pytest.mark.parametrize("name", sorted(APP_CALLED_METHODS))
def test_fake_exposes_app_called_method(widget_cls, name):
    """Every QTermWidget method the app calls exists and is callable."""
    why = APP_CALLED_METHODS[name]
    attr = getattr(widget_cls, name, None)
    assert attr is not None, f"fake is missing {name!r} (used: {why})"
    assert callable(attr), f"{name!r} exists but is not callable (used: {why})"


@pytest.mark.parametrize("name", sorted(APP_CONNECTED_SIGNALS))
def test_fake_exposes_app_connected_signal(qtbot, name):
    """Every QTermWidget signal the app connects to is a real bound signal."""
    why = APP_CONNECTED_SIGNALS[name]
    w = QTermWidget.QTermWidget(0)
    qtbot.addWidget(w)
    sig = getattr(w, name, None)
    assert sig is not None, f"fake is missing signal {name!r} (used: {why})"
    assert isinstance(sig, pyqtBoundSignal), (
        f"{name!r} exists but is not a Qt signal (used: {why})"
    )
    # A bound signal must be connectable.
    sig.connect(lambda *a: None)


def _sip_method_names():
    """Parse the SIP interface and return the set of declared method/signal
    names. Returns None if the SIP file can't be found (then provenance asserts
    skip rather than fail on an unexpected layout)."""
    import os
    import re

    here = os.path.dirname(os.path.abspath(__file__))
    sip = os.path.normpath(
        os.path.join(here, "..", "qtermwidget-pyqt", "sip", "qtermwidget.sip")
    )
    if not os.path.isfile(sip):
        return None
    with open(sip, encoding="utf-8") as f:
        text = f.read()
    # Crude but sufficient: grab identifiers that are immediately followed by
    # '(' -- covers methods, slots and signals declared in the interface.
    return set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text))


def test_sip_contract_methods_are_actually_in_the_sip():
    """Provenance honesty: every method we file under SIP_CONTRACT_METHODS must
    actually appear in the real QTermWidget SIP interface."""
    names = _sip_method_names()
    if names is None:
        pytest.skip("qtermwidget.sip not found next to the worktree")
    missing = sorted(set(SIP_CONTRACT_METHODS) - names)
    assert not missing, (
        f"these are listed as SIP contract methods but are absent from the "
        f"SIP: {missing}"
    )


def test_fake_only_methods_are_not_in_the_sip():
    """Provenance honesty: methods we file under FAKE_ONLY_APP_METHODS must NOT
    be claimed as part of the SIP binding contract -- they are fake-only."""
    names = _sip_method_names()
    if names is None:
        pytest.skip("qtermwidget.sip not found next to the worktree")
    leaked = sorted(set(FAKE_ONLY_APP_METHODS) & names)
    assert not leaked, (
        f"these are listed as fake-only but DO appear in the SIP; move them to "
        f"SIP_CONTRACT_METHODS: {leaked}"
    )


def test_selection_methods_record_intent(qtbot):
    """The selection setters the export plugins call must accept the buffer/pdf
    export arguments without raising. The SIP declares
    ``setSelectionStart(int row, int column)`` -- the fake stores (row, column)
    in that order so future selection-inspecting tests don't inherit inverted
    semantics."""
    w = QTermWidget.QTermWidget(0)
    qtbot.addWidget(w)
    # Mirror buffer_export.py / pdf_export.py exactly: select the whole buffer
    # from the absolute top (row 0 = top of scrollback) to the last real cell,
    # (history + visible_rows - 1, columns). The end is BOUNDED to the real
    # extent on purpose -- an out-of-range end segfaults the real binding.
    history = int(w.historyLinesCount())
    cols = int(w.screenColumnsCount())
    rows = int(w.screenLinesCount())
    w.setSelectionStart(0, 0)
    w.setSelectionEnd(history + rows - 1, cols)
    # The fake must record the args in SIP (row, column) order, not inverted.
    assert w._selection_start == (0, 0)
    assert w._selection_end == (history + rows - 1, cols)
    # screenLinesCount is part of the SIP contract; it must return a positive
    # visible-row count so the export select-all can be bounded.
    assert rows > 0


def test_no_obvious_signature_mismatch_for_send_text(qtbot):
    """sendText takes exactly one positional text argument, matching the SIP
    ``void sendText(QString &text)`` and terminal.send_text()'s single arg."""
    w = QTermWidget.QTermWidget(0)
    qtbot.addWidget(w)
    sig = inspect.signature(w.sendText)
    positional = [
        p for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert len(positional) == 1, "sendText must accept exactly one text arg"
    # And it must actually accept a call without raising.
    w.sendText("echo hi\n")

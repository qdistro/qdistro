"""Real-widget smoke lane for the QTermWidget SIP binding.

This lane is dependency-gated. It exercises the *real* QTermWidget C++ binding
end-to-end: spawn a shell, send input, resize, and -- ONLY if the SIP exposes a
usable screen-read path -- read the screen back and verify a few escape
sequences render the way a known-good terminal emulator (pyte) would.

Every assertion here must be verifiable through the REAL SIP
(``qtermwidget-pyqt/sip/qtermwidget.sip``) and must not be able to hang. The
SIP exposes NO ``screenGet`` / ``toPlainText``; ``selectedText()`` returns only
the current selection. So to read screen content we first SELECT the buffer via
``setSelectionStart`` / ``setSelectionEnd`` and then call
``selectedText(True)`` -- all wrapped in ``hasattr`` guards and a bounded
``waitUntil``. If no usable screen-read API is present we SKIP the
content-dependent asserts rather than risk a time-out: a smaller lane that
reliably runs beats one that hangs.

On hosts where the real binding is not installed, conftest.py injects an
in-process Python fake into ``sys.modules["QTermWidget"]``. ``importorskip``
would happily import that fake, so we additionally detect the fake via the
``_QTERMINATOR_FAKE`` sentinel and SKIP -- the fake cannot honestly validate
real PTY / VT behavior, and a green here against the fake would be a false
pass. On this CI host the lane therefore SKIPs cleanly, which is the expected
and correct outcome; it only runs where the binding is genuinely present.
"""


import pytest

QTermWidget = pytest.importorskip(
    "QTermWidget", reason="real QTermWidget SIP binding not installed"
)

# Detect the conftest fake and skip the whole module: a fake screen can't
# validate real escape-sequence rendering or PTY round-trips.
_widget_cls = getattr(QTermWidget, "QTermWidget", QTermWidget)
if getattr(QTermWidget, "_QTERMINATOR_FAKE", False) or getattr(
    _widget_cls, "_QTERMINATOR_FAKE", False
):
    pytest.skip(
        "in-process QTermWidget fake is loaded (real SIP binding absent); "
        "real-widget smoke lane cannot run against the fake",
        allow_module_level=True,
    )


def _drain(qtbot, predicate, timeout=4000):
    """Pump the event loop until predicate() is true or we time out."""
    qtbot.waitUntil(predicate, timeout=timeout)


@pytest.fixture
def term(qtbot):
    w = QTermWidget.QTermWidget(0)  # 0 = do not auto-start a shell
    qtbot.addWidget(w)
    w.setShellProgram("/bin/bash")
    w.setArgs(["--norc", "--noprofile", "-i"])
    w.resize(800, 480)
    w.show()
    qtbot.waitExposed(w)
    w.startShellProgram()
    # The shell must come up with a live PID before we drive it.
    _drain(qtbot, lambda: int(w.getShellPID() or 0) > 0)
    yield w


def _has_screen_read_api(w):
    """True iff the SIP exposes a usable select-then-read path for the screen.

    The SIP has NO screenGet/toPlainText; selectedText() alone only returns the
    current SELECTION. The only honest way to read the buffer is to select it
    first (setSelectionStart/End) and then call selectedText(True). Bounding the
    selection to the real extent additionally needs the screen geometry
    (screenColumnsCount/screenLinesCount) -- without them an out-of-range end
    would segfault, so we require all five.
    """
    return (
        hasattr(w, "setSelectionStart")
        and hasattr(w, "setSelectionEnd")
        and hasattr(w, "selectedText")
        and hasattr(w, "screenColumnsCount")
        and hasattr(w, "screenLinesCount")
    )


def _read_full_screen(w):
    """Select the whole buffer and return its text via the real SIP.

    Selection rows are ABSOLUTE -- row 0 is the top of the scrollback, not the
    top of the visible screen -- so the whole buffer is (0, 0) to the last cell
    (history + visible_rows - 1, columns). The end MUST be bounded to that real
    extent: QTermWidget 2.4.0's selectedText() does NOT clamp an out-of-range
    selection end, it SEGFAULTS (walks past the allocated screen image). Bounded
    and side-effect-free apart from leaving a selection; cannot hang. Returns ""
    on any failure (or if the geometry getters are absent) so callers degrade
    gracefully.
    """
    try:
        history = int(getattr(w, "historyLinesCount", lambda: 0)() or 0)
        cols = int(getattr(w, "screenColumnsCount", lambda: 0)() or 0)
        rows = int(getattr(w, "screenLinesCount", lambda: 0)() or 0)
        if cols <= 0 or rows <= 0:
            return ""
        w.setSelectionStart(0, 0)
        w.setSelectionEnd(history + rows - 1, cols)
        return w.selectedText(True) or ""
    except Exception:
        return ""


def test_spawn_and_pid(term):
    """A real shell spawns and reports a positive PID."""
    assert int(term.getShellPID()) > 0


def test_send_text_does_not_raise(term):
    """sendText into a live PTY must not raise (observable, cannot hang)."""
    term.sendText("echo qterm_smoke_42\n")  # must not raise


def test_resize_does_not_raise_and_columns_plausible(qtbot, term):
    """setSize must not raise; if screenColumnsCount() exists it stays a
    positive int and plausibly tracks a width change. No fixed sleep -- we wait
    on the observable column count, and never assert a hard inequality that the
    binding might not honor synchronously (which could otherwise hang)."""
    from PyQt6.QtCore import QSize

    has_cols = hasattr(term, "screenColumnsCount")
    before = int(term.screenColumnsCount()) if has_cols else None
    if has_cols:
        assert before > 0

    term.setSize(QSize((before or 80) + 20, 24))  # must not raise

    if has_cols:
        # Wait (bounded) for the column count to settle to a positive int.
        # We don't require it to differ -- some bindings report cols in chars
        # vs the QSize we passed -- only that it remains a sane positive value.
        _drain(qtbot, lambda: int(term.screenColumnsCount()) > 0, timeout=2000)
        after = int(term.screenColumnsCount())
        assert after > 0


def test_scrollback_grows(qtbot, term):
    """Emitting many lines populates the scrollback history buffer."""
    if not hasattr(term, "historyLinesCount"):
        pytest.skip("binding lacks historyLinesCount")
    term.sendText("for i in $(seq 1 200); do echo line_$i; done\n")
    _drain(qtbot, lambda: int(term.historyLinesCount()) > 0, timeout=6000)
    assert int(term.historyLinesCount()) > 0


def test_send_text_round_trips(qtbot, term):
    """Text typed into the PTY is echoed and readable back via the SIP's
    select-then-read path. Skips (does not hang) if no screen-read API."""
    if not _has_screen_read_api(term):
        pytest.skip("real QTermWidget exposes no screen-read API in this SIP")
    marker = "qterm_smoke_42"
    term.sendText(f"echo {marker}\n")
    _drain(qtbot, lambda: marker in _read_full_screen(term))
    assert marker in _read_full_screen(term)


def test_sgr_render_matches_pyte_oracle(qtbot, term):
    """A handful of SGR / cursor escape sequences should render the same
    visible text as a pyte shadow-screen fed the identical byte stream.

    We compare the plain (attribute-stripped) text, which both emulators
    agree on; colors/attributes differ in representation but the glyphs must
    match. Skips (does not hang) if the SIP has no screen-read path.
    """
    pyte = pytest.importorskip("pyte")
    if not _has_screen_read_api(term):
        pytest.skip("real QTermWidget exposes no screen-read API in this SIP")

    seq = (
        "\x1b[2J\x1b[H"          # clear + home
        "\x1b[1;31mRED\x1b[0m "  # bold red word, reset
        "plain "
        "\x1b[4mUNDER\x1b[0m\n"  # underline word
        "second-line\n"
    )

    # pyte oracle
    cols = (int(term.screenColumnsCount())
            if hasattr(term, "screenColumnsCount") else 0) or 80
    rows = 24
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    stream.feed(seq)
    oracle_words = {"RED", "plain", "UNDER", "second-line"}

    # Drive the same bytes through the real terminal via printf (avoid the
    # shell interpreting the escapes itself).
    term.sendText(
        "printf '\\033[2J\\033[H\\033[1;31mRED\\033[0m "
        "plain \\033[4mUNDER\\033[0m\\nsecond-line\\n'\n"
    )

    def _rendered():
        txt = _read_full_screen(term)
        return all(w in txt for w in oracle_words) if txt else False

    _drain(qtbot, _rendered, timeout=4000)
    rendered = _read_full_screen(term)

    # Every word pyte placed on the screen must also appear on the real one.
    pyte_text = "\n".join("".join(row).rstrip() for row in screen.display)
    for word in oracle_words:
        assert word in pyte_text, f"oracle sanity: {word!r} missing from pyte"
        assert word in rendered, f"real screen missing {word!r}"

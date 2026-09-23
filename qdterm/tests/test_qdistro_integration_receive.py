"""App1 Receive delivery into the active terminal (iso2 `16` E1).

A peer drop is a paste, not a command: no newline is synthesised, only
text kinds are typed, and oversized payloads are refused.
"""
from qterminator import qdistro_integration as qi


class _Term:
    def __init__(self):
        self.sent = []

    def send_text(self, text, force=False):
        self.sent.append(text)
        return True


class _Bar:
    def __init__(self):
        self.messages = []

    def showMessage(self, msg, _ms=0):
        self.messages.append(msg)


class _Window:
    def __init__(self, term=None):
        self._active_terminal = term
        self._bar = _Bar()

    def statusBar(self):
        return self._bar


def test_receive_does_not_append_newline():
    term = _Term()
    qi._deliver_to_active_terminal(_Window(term), "text/plain", "rm -rf ~")
    assert term.sent == ["rm -rf ~"]


def test_receive_keeps_payload_bytes_verbatim():
    term = _Term()
    qi._deliver_to_active_terminal(_Window(term), "text/plain", "echo hi\n")
    assert term.sent == ["echo hi\n"]


def test_receive_refuses_octet_stream():
    term = _Term()
    w = _Window(term)
    qi._deliver_to_active_terminal(w, "application/octet-stream", "x")
    assert term.sent == []
    assert any("unsupported kind" in m for m in w._bar.messages)


def test_receive_refuses_oversized_payload():
    term = _Term()
    w = _Window(term)
    qi._deliver_to_active_terminal(w, "text/plain", "a" * (qi.MAX_PAYLOAD_BYTES + 1))
    assert term.sent == []
    assert any("refused" in m for m in w._bar.messages)


def test_receive_without_terminal_reports_drop():
    w = _Window(None)
    qi._deliver_to_active_terminal(w, "text/plain", "hello")
    assert any("no active terminal" in m for m in w._bar.messages)


def test_advertised_kinds_are_text_only():
    assert "application/octet-stream" not in qi.APP_SUPPORTED_KINDS
    assert all(k.startswith("text/") for k in qi.APP_SUPPORTED_KINDS)

"""Single-instance forwarding tests (QLocalServer/QLocalSocket)."""

from __future__ import annotations

from pathlib import Path

from qnotebook import single_instance


def test_server_name_stable(tmp_path: Path) -> None:
    n1 = single_instance.server_name(tmp_path)
    n2 = single_instance.server_name(tmp_path)
    assert n1 == n2
    assert n1.startswith("qnotebook-")


def test_server_name_differs_per_path(tmp_path: Path) -> None:
    a = single_instance.server_name(tmp_path)
    b = single_instance.server_name(tmp_path / "sub")
    assert a != b


def test_try_forward_returns_false_when_no_server(tmp_path: Path) -> None:
    ok = single_instance.try_forward(tmp_path, {"cmd": "reload"}, timeout_ms=100)
    assert ok is False


def test_command_server_round_trip(qapp, tmp_path: Path, qtbot) -> None:

    class FakeWindow:
        _current_page = None
        def load_page(self, page):
            self.last_loaded = page

    fw = FakeWindow()
    srv = single_instance.CommandServer(fw)
    assert srv.start(tmp_path)

    ok = single_instance.try_forward(tmp_path,
                                      {"cmd": "open", "page": "Foo"})
    assert ok
    # Allow event loop to deliver the payload
    def delivered():
        return getattr(fw, "last_loaded", None) == "Foo"
    qtbot.waitUntil(delivered, timeout=2000)
    srv.stop()

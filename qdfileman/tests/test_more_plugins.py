"""Tests for the diff / kfind / fuzzy_search / open_terminal / qterminator_link
plugins, plus the qdshell + qterminator helpers and the runner's
progress parser.

Like ``test_builtin_plugins.py``, this sticks to pure-logic surfaces so
the suite stays green on machines without ``meld``, ``kfind``, ``fzf``,
or a running QTerminator/notification daemon.
"""

from __future__ import annotations

import json
import os
import socket
import threading

import pytest
from qfileman.plugin import PluginManager
from qfileman.plugins.builtin import _qdshell as qd_mod
from qfileman.plugins.builtin import _qterminator as qt_mod
from qfileman.plugins.builtin import _runner as runner_mod
from qfileman.plugins.builtin import diff as diff_mod
from qfileman.plugins.builtin import fuzzy_search as fz_mod
from qfileman.plugins.builtin import open_terminal as ot_mod

# ---------------------------------------------------------------------------
# Discovery: the new plugin files load through the manager.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "diff", "kfind", "fuzzy_search", "open_terminal", "qterminator_link",
])
def test_new_plugins_discovered(name):
    pm = PluginManager()
    pm.discover()
    assert name in pm.available_plugins()


def test_qterminator_link_registers_both_capabilities():
    """Module exposes a MenuProvider *and* a NavigationHook; both must
    surface in the manager's capability lookups."""
    pm = PluginManager()
    pm.discover()
    pm.enable("qterminator_link")
    cap_names = {p.name for p in pm.get_menu_providers()}
    hook_names = {h.name for h in pm.get_navigation_hooks()}
    assert "qterminator_link" in cap_names
    assert "qterminator_link_hook" in hook_names


# ---------------------------------------------------------------------------
# _runner: progress parser
# ---------------------------------------------------------------------------

def test_parse_progress_picks_last_percent():
    text = (
        "  1,234,567   42%   10.2MB/s    0:00:12\n"
        "  9,876,543   88%   12.0MB/s    0:00:02\n"
    )
    assert runner_mod.parse_progress(text) == 88


def test_parse_progress_clamps_silly_values():
    # rsync won't emit this but we still want to behave.
    assert runner_mod.parse_progress("999%") == 100


def test_parse_progress_none_when_absent():
    assert runner_mod.parse_progress("no digits here") is None
    assert runner_mod.parse_progress("") is None


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def test_diff_build_argv_uses_chosen_tool(monkeypatch):
    monkeypatch.setattr(diff_mod, "choose_tool", lambda: ("meld", ["meld"]))
    assert diff_mod.build_argv("/a", "/b") == ["meld", "/a", "/b"]


def test_diff_build_argv_falls_back_to_plain_diff(monkeypatch):
    monkeypatch.setattr(diff_mod, "choose_tool", lambda: None)
    assert diff_mod.build_argv("/a", "/b") == ["diff", "-u", "--", "/a", "/b"]


def test_diff_build_argv_plain_diff_dash_path_not_an_option(monkeypatch):
    # A file literally named ``-e`` must land after ``--`` so plain diff
    # cannot mistake it for an option (argv-injection guard).
    monkeypatch.setattr(diff_mod, "choose_tool", lambda: None)
    argv = diff_mod.build_argv("-e", "/b")
    assert "--" in argv
    assert argv.index("--") < argv.index("-e")


def test_diff_menu_items_offer_against_source_only_when_set(tmp_path):
    f1 = tmp_path / "a"
    f2 = tmp_path / "b"
    f1.write_text("1")
    f2.write_text("2")
    plugin = diff_mod.DiffPlugin()
    diff_mod.DiffPlugin._diff_source = None
    try:
        labels = [lbl for lbl, _ in plugin.get_menu_items(str(f1))]
        assert "Set as Diff Source" in labels
        assert not any(lbl.startswith("Diff Against Source") for lbl in labels)

        diff_mod.DiffPlugin._diff_source = str(f1)
        labels2 = [lbl for lbl, _ in plugin.get_menu_items(str(f2))]
        assert any(lbl.startswith("Diff Against Source") for lbl in labels2)
    finally:
        diff_mod.DiffPlugin._diff_source = None


# ---------------------------------------------------------------------------
# fuzzy_search
# ---------------------------------------------------------------------------

def test_fuzzy_score_requires_subsequence():
    assert fz_mod.fuzzy_score("xyz", "alphabet") is None


def test_fuzzy_score_empty_query_matches_anything():
    assert fz_mod.fuzzy_score("", "anything") == 0


def test_fuzzy_score_consecutive_beats_scattered():
    # Without separators between matches, the consecutive run wins —
    # the scattered candidate forces gap characters in between.
    consecutive = fz_mod.fuzzy_score("abc", "abc")
    scattered = fz_mod.fuzzy_score("abc", "axbxc")
    assert consecutive > scattered


def test_fuzzy_score_word_boundary_bonus():
    boundary = fz_mod.fuzzy_score("f", "foo_bar")
    midword = fz_mod.fuzzy_score("f", "Affix")  # lower → 'a','f','f','i','x'
    # The boundary match is at position 0 of "foo_bar"; the midword match
    # is at position 1 of "affix". Boundary scoring should win.
    assert boundary > midword


def test_fuzzy_score_smart_case():
    # All-lowercase query is case-insensitive.
    assert fz_mod.fuzzy_score("readme", "README.md") is not None
    # Mixed-case query is case-sensitive — no match against all caps.
    assert fz_mod.fuzzy_score("ReadMe", "README.md") is None


def test_rank_candidates_orders_by_score():
    paths = [
        "/x/banana.txt",       # 'ban' at the very start of basename
        "/x/cuban_food.txt",   # 'ban' inside, after a word-boundary
        "/x/abandoned.txt",    # 'ban' inside, no boundary just before
        "/x/nomatch.txt",      # filtered out — no subsequence
    ]
    ranked = fz_mod.rank_candidates("ban", paths)
    paths_in_order = [p for _s, p in ranked]
    assert "/x/nomatch.txt" not in paths_in_order
    # The flat prefix on banana.txt must beat any of the embedded matches.
    assert paths_in_order[0] == "/x/banana.txt"


def test_walk_files_skips_dotdirs(tmp_path):
    (tmp_path / "visible.txt").write_text("x")
    hidden_dir = tmp_path / ".hidden"
    hidden_dir.mkdir()
    (hidden_dir / "inside.txt").write_text("x")
    found = fz_mod.walk_files(str(tmp_path))
    bases = {os.path.basename(p) for p in found}
    assert "visible.txt" in bases
    assert "inside.txt" not in bases


def test_walk_files_respects_max(tmp_path):
    for i in range(20):
        (tmp_path / f"f{i}.txt").write_text("x")
    found = fz_mod.walk_files(str(tmp_path), max_entries=5)
    assert len(found) == 5


# ---------------------------------------------------------------------------
# open_terminal
# ---------------------------------------------------------------------------

def test_build_argv_known_terminals():
    argv = ot_mod.build_argv("alacritty", "/home/x")
    assert argv == ["alacritty", "--working-directory", "/home/x"]
    argv = ot_mod.build_argv("konsole", "/home/x")
    assert argv == ["konsole", "--workdir", "/home/x"]
    argv = ot_mod.build_argv("foot", "/home/x")
    assert "/home/x" in argv[1]
    assert argv[0] == "foot"


def test_build_argv_xterm_uses_shell_wrapper():
    argv = ot_mod.build_argv("xterm", "/home/with space")
    assert argv[0] == "xterm"
    # The cd line must be properly quoted to survive the shell.
    joined = " ".join(argv)
    assert "with space" in joined
    # And not naïvely unquoted:
    assert "cd /home/with space" not in joined


def test_build_argv_unknown_terminal():
    assert ot_mod.build_argv("totally-fake", "/x") is None


def test_detect_terminal_env_takes_priority(monkeypatch):
    # Pretend "konsole" is the env choice and that something exists at
    # its path; everything else is absent.
    monkeypatch.setenv("TERMINAL", "konsole")

    def fake_which(name):
        return "/usr/bin/konsole" if name == "konsole" else None

    monkeypatch.setattr(ot_mod.shutil, "which", fake_which)
    assert ot_mod.detect_terminal() == "konsole"


def test_detect_terminal_none_when_nothing_installed(monkeypatch):
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(ot_mod.shutil, "which", lambda _n: None)
    assert ot_mod.detect_terminal() is None


# ---------------------------------------------------------------------------
# _qterminator: framing + smoke against an in-process fake server
# ---------------------------------------------------------------------------

class _FakeAgentServer:
    """One-shot JSON-RPC server that records requests and replies."""

    def __init__(self, path: str, responses: list[dict]):
        self.path = path
        self.responses = responses
        self.received: list[dict] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._sock.bind(path)
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._sock.accept()
        buf = b""
        try:
            for resp in self.responses:
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                line, _, buf = buf.partition(b"\n")
                req = json.loads(line.decode("utf-8"))
                self.received.append(req)
                resp_with_id = dict(resp)
                resp_with_id.setdefault("jsonrpc", "2.0")
                resp_with_id["id"] = req["id"]
                conn.sendall((json.dumps(resp_with_id) + "\n").encode("utf-8"))
        finally:
            conn.close()
            self._sock.close()
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def test_qterminator_list_tabs_round_trip(tmp_path):
    sock_path = str(tmp_path / "agent.sock")
    server = _FakeAgentServer(
        sock_path,
        [{"result": [{"id": 42, "title": "main", "working_directory": "/"}]}],
    )
    server.start()
    out = qt_mod.list_tabs(sock_path)
    assert out == [{"id": 42, "title": "main", "working_directory": "/"}]
    assert server.received[0]["method"] == "list_tabs"


def test_qterminator_cd_quotes_path(tmp_path):
    sock_path = str(tmp_path / "agent.sock")
    server = _FakeAgentServer(
        sock_path,
        [{"result": {"ok": True}},  # attach
         {"result": {"ok": True}},  # send_text
         {"result": {"ok": True}}], # detach
    )
    server.start()
    qt_mod.cd(7, "/tmp/with space", path=sock_path)
    methods = [r["method"] for r in server.received]
    assert methods[:2] == ["attach", "send_text"]
    payload = server.received[1]["params"]["text"]
    # Path has a space → must be quoted so the shell doesn't split it.
    assert "with space" in payload
    assert "cd '/tmp/with space'" in payload or "cd \"/tmp/with space\"" in payload
    assert payload.endswith("\n")


def test_qterminator_unavailable_when_socket_missing(tmp_path):
    bogus = str(tmp_path / "nope.sock")
    assert qt_mod.is_available(bogus) is False
    with pytest.raises(qt_mod.QTerminatorUnavailable):
        qt_mod.list_tabs(bogus)


def test_qterminator_propagates_error(tmp_path):
    sock_path = str(tmp_path / "agent.sock")
    server = _FakeAgentServer(
        sock_path,
        [{"error": {"code": -32601, "message": "unknown method: bogus"}}],
    )
    server.start()
    with pytest.raises(qt_mod.QTerminatorError):
        qt_mod.list_tabs(sock_path)


# ---------------------------------------------------------------------------
# _qdshell: format helpers
# ---------------------------------------------------------------------------

def test_qdshell_format_started():
    summary, body = qd_mod.format_started("Rsync /src /dst")
    assert summary == "Rsync /src /dst"
    assert "Started" in body


def test_qdshell_format_finished_success():
    summary, body, urgency = qd_mod.format_finished("Rsync /src /dst", 0)
    assert "Done" in body
    assert urgency == 1


def test_qdshell_format_finished_failure():
    summary, body, urgency = qd_mod.format_finished("Rsync /src /dst", 1)
    assert "Failed" in body
    assert urgency == 2  # critical


def test_qdshell_format_finished_unknown():
    _, body, urgency = qd_mod.format_finished("X", None)
    assert "Cancelled" in body or "failed to start" in body
    assert urgency == 1


def test_qdshell_close_with_zero_id_is_noop():
    # Should not raise even if no daemon is running.
    qd_mod.close(0)

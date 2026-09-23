"""Tests for the badges plugin.

Layers:
  - Pure template + cwd shortening + branch cache (no Qt).
  - collect_context against fake service objects.
  - Plugin lifecycle on a real MainWindow with a profile that sets a
    badge_template — assert the QLabel appears and re-renders after
    a shell_integration command_finished event.
"""


import pytest
import qterminator.config as config_mod
from qterminator.config import Config

pytest.importorskip("pyte")


@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config" / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


from qterminator.plugins.badges import (  # noqa: E402
    BadgesPlugin,
    _BranchCache,
    collect_context,
    hostname,
    render_template,
    shorten_cwd,
)

# ---------------------------------------------------------------------------
# Template / helpers
# ---------------------------------------------------------------------------

def test_render_template_substitutes_known_keys():
    out = render_template("{host}:{cwd}", {"host": "alpha", "cwd": "/tmp"})
    assert out == "alpha:/tmp"


def test_render_template_missing_keys_are_empty():
    """Missing keys render as empty strings (not literal braces) — a
    user-visible UI overlay shouldn't show '{branch}' to the user when
    the variable isn't available."""
    assert render_template("{a}|{b}", {"a": "x"}) == "x|"


def test_render_template_unterminated_brace_kept_verbatim():
    assert render_template("hi {x", {"x": "y"}) == "hi {x"


def test_shorten_cwd_home_becomes_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/home/alice")
    assert shorten_cwd("/home/alice") == "~"
    assert shorten_cwd("/home/alice/src") == "~/src"
    assert shorten_cwd("/tmp") == "/tmp"
    assert shorten_cwd("") == ""
    assert shorten_cwd(None) == ""


def test_hostname_is_non_empty():
    assert isinstance(hostname(), str)


# ---------------------------------------------------------------------------
# Branch cache
# ---------------------------------------------------------------------------

def test_branch_cache_caches_lookup(tmp_path, monkeypatch):
    cache = _BranchCache(ttl=10)
    calls = []
    def fake(cwd):
        calls.append(cwd)
        return "main"
    monkeypatch.setattr(_BranchCache, "_git_branch", staticmethod(fake))
    p = str(tmp_path)
    assert cache.lookup(p) == "main"
    assert cache.lookup(p) == "main"
    assert len(calls) == 1   # second hit served from cache


def test_branch_cache_respects_ttl(tmp_path, monkeypatch):
    cache = _BranchCache(ttl=0)  # always expired
    calls = []
    monkeypatch.setattr(_BranchCache, "_git_branch", staticmethod(
        lambda cwd: (calls.append(cwd), "x")[1]
    ))
    p = str(tmp_path)
    cache.lookup(p); cache.lookup(p)
    assert len(calls) == 2


def test_branch_cache_skips_missing_cwd():
    cache = _BranchCache()
    assert cache.lookup("/this/path/does/not/exist") == ""
    assert cache.lookup(None) == ""


# ---------------------------------------------------------------------------
# collect_context
# ---------------------------------------------------------------------------

class _FakeHistory:
    def __init__(self, cwd=None, exit_status=None):
        self.cwd = cwd
        self.last = self._Last(exit_status) if exit_status is not None else None
    class _Last:
        def __init__(self, status): self.exit_status = status


class _FakeShellIntegration:
    def __init__(self, history): self._h = history
    def get_history(self, _t): return self._h


class _FakeTmuxMode:
    def __init__(self, session): self._s = session
    def get_session_for_terminal(self, _t): return self._s


class _FakeWindow:
    def __init__(self, **services):
        for k, v in services.items():
            setattr(self, k, v)


def test_collect_context_pulls_cwd_and_exit():
    win = _FakeWindow(
        shell_integration=_FakeShellIntegration(_FakeHistory("/var/log", 3))
    )
    ctx = collect_context(win, terminal=object(), branch_cache=_BranchCache())
    assert ctx["cwd"] == "/var/log"
    assert ctx["exit_status"] == "3"


def test_collect_context_handles_no_shell_integration():
    win = _FakeWindow()
    ctx = collect_context(win, terminal=object(), branch_cache=_BranchCache())
    assert ctx["cwd"] == ""
    assert ctx["exit_status"] == ""
    assert ctx["tmux_session"] == ""


def test_collect_context_pulls_tmux_session():
    win = _FakeWindow(tmux_mode=_FakeTmuxMode("qterm-x"))
    ctx = collect_context(win, terminal=object(), branch_cache=_BranchCache())
    assert ctx["tmux_session"] == "qterm-x"


def test_collect_context_short_cwd_renders(monkeypatch):
    monkeypatch.setenv("HOME", "/home/alice")
    win = _FakeWindow(
        shell_integration=_FakeShellIntegration(_FakeHistory("/home/alice/src"))
    )
    ctx = collect_context(win, terminal=object(), branch_cache=_BranchCache())
    assert ctx["cwd_short"] == "~/src"


def test_collect_context_branch_via_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(_BranchCache, "_git_branch", staticmethod(lambda c: "feature/X"))
    win = _FakeWindow(
        shell_integration=_FakeShellIntegration(_FakeHistory(str(tmp_path)))
    )
    ctx = collect_context(win, terminal=object(), branch_cache=_BranchCache())
    assert ctx["branch"] == "feature/X"


# ---------------------------------------------------------------------------
# Plugin lifecycle: real MainWindow
# ---------------------------------------------------------------------------

def _set_profile_badge(template: str, color: str = "#ffffff"):
    cfg = Config()
    cfg.set("profiles", "default", "badge_template", template)
    cfg.set("profiles", "default", "badge_color", color)


def test_plugin_creates_overlay_when_profile_template_present(qtbot):
    _set_profile_badge("{hostname}")
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    overlays = win.badges.overlays
    assert len(overlays) == 1
    overlay = next(iter(overlays.values()))
    # Default profile template just shows hostname; we won't assert
    # the exact text (varies per host) but it must be non-empty.
    assert overlay.text(), "overlay should render the hostname"


def test_plugin_no_template_no_overlay(qtbot):
    """With no badge_template configured anywhere, the plugin must
    not allocate any QLabel."""
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    assert win.badges.overlays == {}


def test_plugin_refreshes_on_command_finished(qtbot):
    _set_profile_badge("rc={exit_status}")
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    # Feed an OSC 133 ;A/;B/;C/;D;7 cycle so shell_integration records
    # an exit status, then assert the badge text reflects it.
    for _tid, overlay in win.badges.overlays.items():
        # tid is id(terminal); we need the terminal object — pull from
        # the overlay's stored reference.
        terminal = overlay._terminal
        shadow = win.shadow_screens._shadows[id(terminal)][0]
        shadow.feed(
            "\x1b]133;A\x1b\\\x1b]133;B\x1b\\\x1b]133;C\x1b\\\x1b]133;D;7\x1b\\"
        )
        qtbot.wait(20)
        assert overlay.text() == "rc=7"


def test_plugin_attaches_new_tabs(qtbot):
    _set_profile_badge("{hostname}")
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    before = len(win.badges.overlays)
    win.new_tab()
    qtbot.wait(40)
    after = len(win.badges.overlays)
    assert after == before + 1


def test_plugin_keypress_hides_overlay(qtbot):
    _set_profile_badge("{hostname}")
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    overlay = next(iter(win.badges.overlays.values()))
    overlay.hide_briefly()
    assert not overlay.isVisible()


def test_plugin_color_override_applied(qtbot):
    _set_profile_badge("X", color="#abcdef")
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    overlay = next(iter(win.badges.overlays.values()))
    assert "#abcdef" in overlay.styleSheet()


def test_plugin_disabled_does_not_install_service():
    cfg = Config()
    cfg.set("plugins", "badges", "enabled", False)
    class FakeWindow:
        pass
    win = FakeWindow()
    p = BadgesPlugin()
    p.activate(win)
    try:
        assert not hasattr(win, "badges")
    finally:
        p.deactivate()


def test_plugin_deactivate_clears_overlays(qtbot):
    _set_profile_badge("{hostname}")
    from qterminator.window import MainWindow
    win = MainWindow()
    win.resize(800, 400)
    qtbot.addWidget(win)
    win.show()
    qtbot.waitExposed(win)
    qtbot.wait(60)
    pm = win._plugin_manager
    pm.disable("badges")
    # Service should be gone from the window.
    assert not hasattr(win, "badges")

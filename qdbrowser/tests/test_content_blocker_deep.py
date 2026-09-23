"""Content blocker: interception logic + corner cases.

Uses the post-EasyList API: ``_blocked_hosts`` / ``_allow_hosts`` instead
of the old ``_blocked`` / ``_allow``. Per-site state is consulted off
the *document host* (``firstPartyUrl().host()``), so the MagicMock has
to provide that too.
"""

from unittest.mock import MagicMock


def _fake_info(req_host, doc_host=None):
    info = MagicMock()
    info.requestUrl.return_value.host.return_value = req_host
    if doc_host is None:
        doc_host = req_host
    info.firstPartyUrl.return_value.host.return_value = doc_host
    return info


def test_disabled_lets_everything_through(fresh_config):
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug._enabled = False
    plug._blocked_hosts_view = frozenset({"bad.com"})
    info = _fake_info("bad.com")
    plug.intercept(info)
    info.block.assert_not_called()


def test_blocks_exact_match(fresh_config):
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug._blocked_hosts_view = frozenset({"ads.bad.com"})
    info = _fake_info("ads.bad.com")
    plug.intercept(info)
    info.block.assert_called_once_with(True)
    assert plug.stats["blocked"] == 1


def test_blocks_suffix_match(fresh_config):
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug._blocked_hosts_view = frozenset({"bad.com"})
    info = _fake_info("sub.foo.bad.com")
    plug.intercept(info)
    info.block.assert_called_once_with(True)


def test_allowlist_wins(fresh_config):
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug._blocked_hosts_view = frozenset({"bad.com"})
    plug._allow_hosts_view = frozenset({"good.bad.com"})
    info = _fake_info("good.bad.com")
    plug.intercept(info)
    info.block.assert_not_called()
    assert plug.stats["allowed"] == 1


def test_no_host_skipped(fresh_config):
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug._blocked_hosts_view = frozenset({"bad.com"})
    info = _fake_info("")
    plug.intercept(info)
    info.block.assert_not_called()


def test_unrelated_host_passes(fresh_config):
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug._blocked_hosts_view = frozenset({"bad.com"})
    info = _fake_info("good.com")
    plug.intercept(info)
    info.block.assert_not_called()


def test_toggle_command(fresh_config):
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    before = plug._enabled
    plug._toggle()
    assert plug._enabled == (not before)


def test_get_commands_returns_baseline(fresh_config):
    """Get-commands at minimum returns the global toggle + stats + reload."""
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    # Pass a fake window with no active webview so the per-site rows
    # aren't appended.
    cmds = plug.get_commands(None)
    labels = [label for label, _ in cmds]
    assert any("Content blocker" in label for label in labels)
    assert any("stats" in label.lower() for label in labels)
    assert any("Reload" in label for label in labels)


def test_extra_blocked_loaded_from_config(fresh_config):
    from qdbrowser.config import Config
    Config().set("blocklist", "extra_blocked", ["myextra.com"])
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    assert "myextra.com" in plug._blocked_hosts


def test_allowlist_loaded_from_config(fresh_config):
    from qdbrowser.config import Config
    Config().set("blocklist", "allowlist", ["myallowed.com"])
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    assert "myallowed.com" in plug._allow_hosts


def test_disabled_via_config(fresh_config):
    from qdbrowser.config import Config
    Config().set("blocklist", "enabled", False)
    from qdbrowser.plugins.content_blocker import ContentBlockerPlugin
    plug = ContentBlockerPlugin()
    plug.activate(object())
    assert plug._enabled is False


def test_misbehaving_interceptor_isolated(window):
    """An interceptor that raises must not break the chain."""
    wv = window._active_webview

    class Boom:
        def intercept(self, info):
            raise RuntimeError("kaboom")

    called = {"n": 0}

    class Counter:
        def intercept(self, info):
            called["n"] += 1

    wv.add_interceptor(Boom())
    wv.add_interceptor(Counter())
    fake_info = MagicMock()
    fake_info.requestUrl.return_value.host.return_value = "test.com"
    wv._interceptor.interceptRequest(fake_info)
    assert called["n"] == 1

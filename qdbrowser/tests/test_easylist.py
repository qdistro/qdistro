"""EasyList parser + cosmetic rules + per-site toggles."""

from qdbrowser.plugins.content_blocker import (
    ContentBlockerPlugin,
    _CosmeticRule,
    _is_host_suffix,
    _NetworkRule,
    _same_site,
    parse_easylist,
)


def test_parses_host_rule():
    nets, cos = parse_easylist("||tracker.com^\n")
    assert len(nets) == 1
    assert nets[0].host_suffix == "tracker.com"
    assert cos == []


def test_parses_exception():
    nets, _ = parse_easylist("@@||good.example.com^\n")
    assert len(nets) == 1
    assert nets[0].is_exception is True
    assert nets[0].host_suffix == "good.example.com"


def test_parses_third_party_option():
    nets, _ = parse_easylist("||ads.com^$third-party\n")
    assert nets[0].third_party_only is True


def test_parses_cosmetic_global():
    _, cos = parse_easylist("##.global-ad\n")
    assert len(cos) == 1
    assert cos[0].host_suffix is None
    assert cos[0].selector == ".global-ad"


def test_parses_cosmetic_per_host():
    _, cos = parse_easylist("example.com##.banner\n")
    assert cos[0].host_suffix == "example.com"
    assert cos[0].selector == ".banner"


def test_skips_comments_and_headers():
    nets, cos = parse_easylist(
        "[Adblock Plus 2.0]\n"
        "! comment\n"
        "||real.com^\n"
    )
    assert len(nets) == 1
    assert nets[0].host_suffix == "real.com"


def test_parses_regex_rule():
    nets, _ = parse_easylist("/tracker[0-9]+\\.js/\n")
    assert nets[0].pattern is not None
    assert nets[0].pattern.search("https://x.com/tracker42.js")


def test_network_rule_matches_host():
    rule = _NetworkRule("||bad.com^")
    assert rule.matches("https://sub.bad.com/foo", "good.com")
    assert not rule.matches("https://safe.com/bar", "good.com")


def test_third_party_rule_skips_first_party():
    rule = _NetworkRule("||trk.com^$third-party")
    assert rule.matches("https://trk.com/x", "other.com")
    assert not rule.matches("https://trk.com/x", "trk.com")


def test_substring_rule_match():
    rule = _NetworkRule("/banner/")
    assert rule.matches("https://x.com/show/banner.js", None)


def test_is_host_suffix():
    assert _is_host_suffix("ads.example.com", "example.com")
    assert _is_host_suffix("example.com", "example.com")
    assert not _is_host_suffix("notexample.com", "example.com")


def test_same_site():
    assert _same_site("a.example.com", "b.example.com")
    assert not _same_site("a.example.com", "evil.tld")


def test_cosmetic_css_for_host(fresh_config):
    plug = ContentBlockerPlugin()
    plug._cosmetic_rules = [
        _CosmeticRule(None, ".global"),
        _CosmeticRule("news.site", ".banner"),
    ]
    plug._enabled = True
    css = plug.cosmetic_css_for("sub.news.site")
    assert ".global" in css
    assert ".banner" in css
    assert "display: none" in css


def test_cosmetic_css_returns_empty_when_disabled(fresh_config):
    plug = ContentBlockerPlugin()
    plug._cosmetic_rules = [_CosmeticRule(None, ".x")]
    plug._enabled = False
    assert plug.cosmetic_css_for("example.com") == ""


def test_per_site_state_default(fresh_config):
    plug = ContentBlockerPlugin()
    plug.activate(object())
    assert plug.site_state("anywhere.com") == "on"


def test_per_site_state_override(fresh_config):
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug.set_site_state("news.test", "off")
    assert plug.site_state("news.test") == "off"
    # Suffix match.
    assert plug.site_state("sub.news.test") == "off"


def test_per_site_state_persists_to_config(fresh_config):
    from qdbrowser.config import Config
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug.set_site_state("a.com", "cosmetic-only")
    saved = Config().get("blocklist", "site_toggles")
    assert saved.get("a.com") == "cosmetic-only"


def test_set_site_state_on_clears_override(fresh_config):
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug.set_site_state("x.com", "off")
    plug.set_site_state("x.com", "on")
    assert "x.com" not in plug._site_toggles


def test_intercept_respects_site_off(fresh_config):
    from unittest.mock import MagicMock
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug._blocked_hosts_view = frozenset({"ads.bad.com"})
    plug.set_site_state("news.test", "off")
    info = MagicMock()
    info.requestUrl.return_value.host.return_value = "ads.bad.com"
    info.firstPartyUrl.return_value.host.return_value = "news.test"
    plug.intercept(info)
    info.block.assert_not_called()


def test_intercept_blocks_when_site_on(fresh_config):
    from unittest.mock import MagicMock
    plug = ContentBlockerPlugin()
    plug.activate(object())
    plug._blocked_hosts_view = frozenset({"ads.bad.com"})
    info = MagicMock()
    info.requestUrl.return_value.host.return_value = "ads.bad.com"
    info.firstPartyUrl.return_value.host.return_value = "news.test"
    plug.intercept(info)
    info.block.assert_called_once_with(True)

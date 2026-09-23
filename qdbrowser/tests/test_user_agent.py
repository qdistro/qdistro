"""§6 User-agent spoofing prevention: a single source-of-truth UA is
resolved from config and applied identically to every QWebEngineProfile,
so silos can't be distinguished by per-profile UA drift.

These tests cover:
  - resolve_user_agent mapping (empty -> Qt default / None, presets, custom)
  - apply_user_agent actually setting the UA on a profile object
  - get_profile pinning the *same* UA across multiple named profiles
  - presets staying inside security_interceptor's strict-mode baseline
"""

from unittest.mock import MagicMock

from qdbrowser import webview as wv_mod
from qdbrowser.security_interceptor import _SAFE_UA_TOKENS
from qdbrowser.webview import (
    _UA_PRESETS,
    apply_user_agent,
    get_profile,
    pin_all_profiles,
    resolve_user_agent,
)

# -- resolve_user_agent ----------------------------------------------------


def test_resolve_empty_means_qt_default(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "")
    assert resolve_user_agent(cfg) is None


def test_resolve_whitespace_means_qt_default(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "   ")
    assert resolve_user_agent(cfg) is None


def test_resolve_non_string_means_qt_default(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", 12345)
    assert resolve_user_agent(cfg) is None


def test_resolve_firefox_preset(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "firefox")
    assert resolve_user_agent(cfg) == _UA_PRESETS["firefox"]


def test_resolve_preset_is_case_insensitive(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "Chrome")
    assert resolve_user_agent(cfg) == _UA_PRESETS["chrome"]


def test_resolve_edge_preset(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "edge")
    assert resolve_user_agent(cfg) == _UA_PRESETS["edge"]


def test_resolve_custom_string_verbatim(fresh_config):
    cfg = fresh_config.Config()
    custom = "MyCorpBrowser/1.0 (audited)"
    cfg.set("general", "user_agent", custom)
    assert resolve_user_agent(cfg) == custom


def test_resolve_strips_surrounding_whitespace_on_custom(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "  Custom/2.0  ")
    assert resolve_user_agent(cfg) == "Custom/2.0"


def test_resolve_uses_singleton_when_no_config(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "firefox")
    # No explicit config arg -> falls back to the Config singleton.
    assert resolve_user_agent() == _UA_PRESETS["firefox"]


# -- apply_user_agent ------------------------------------------------------


def test_apply_sets_ua_on_profile(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "firefox")
    prof = MagicMock()
    apply_user_agent(prof, cfg)
    prof.setHttpUserAgent.assert_called_once_with(_UA_PRESETS["firefox"])


def test_apply_resets_to_default_on_empty(fresh_config):
    # Reverting to "" must authoritatively reset the profile to Qt's
    # default (empty string), not leave a previously-pinned UA stale.
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "")
    prof = MagicMock()
    apply_user_agent(prof, cfg)
    prof.setHttpUserAgent.assert_called_once_with("")


def test_apply_swallows_setter_errors(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "chrome")
    prof = MagicMock()
    prof.setHttpUserAgent.side_effect = RuntimeError("no setter")
    # Must not raise — startup resilience.
    apply_user_agent(prof, cfg)


# -- get_profile consistency (real QWebEngineProfile) ----------------------


def test_get_profile_pins_configured_ua(qapp, fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "firefox")
    # Force fresh profile objects so apply_user_agent runs on creation.
    wv_mod._PROFILES.clear()
    prof = get_profile("ua-test-fox")
    try:
        assert prof.httpUserAgent() == _UA_PRESETS["firefox"]
    finally:
        wv_mod._PROFILES.clear()


def test_same_ua_across_profiles_no_drift(qapp, fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "chrome")
    wv_mod._PROFILES.clear()
    work = get_profile("ua-work")
    personal = get_profile("ua-personal")
    private = get_profile("private")
    try:
        ua = _UA_PRESETS["chrome"]
        assert work.httpUserAgent() == ua
        assert personal.httpUserAgent() == ua
        # Off-the-record profile gets the same pinned UA -> no silo drift.
        assert private.httpUserAgent() == ua
    finally:
        wv_mod._PROFILES.clear()


# -- pin_all_profiles (repin existing profiles after config change) --------


def test_pin_all_profiles_repins_existing(qapp, fresh_config):
    cfg = fresh_config.Config()
    # Start with Qt default, mint a profile (UA left untouched).
    cfg.set("general", "user_agent", "")
    wv_mod._PROFILES.clear()
    prof = get_profile("ua-repin")
    qt_default = prof.httpUserAgent()
    try:
        # Admin flips to a preset at runtime; existing profile is stale...
        cfg.set("general", "user_agent", "firefox")
        assert prof.httpUserAgent() == qt_default
        # ...until we re-pin every cached profile.
        pin_all_profiles(cfg)
        assert prof.httpUserAgent() == _UA_PRESETS["firefox"]
    finally:
        wv_mod._PROFILES.clear()


def test_pin_all_profiles_reverts_to_default(qapp, fresh_config):
    """Pinned -> "" must clear the custom UA back to Qt's "use default"
    sentinel (empty string), not leave it stuck on the old custom UA
    (both-directions enforcement). An empty UA is Qt's documented signal
    to send its built-in default, which is identical across profiles, so
    every profile is back to a consistent state with no drift.
    """
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "firefox")
    wv_mod._PROFILES.clear()
    prof = get_profile("ua-revert")
    try:
        assert prof.httpUserAgent() == _UA_PRESETS["firefox"]
        # Admin reverts to Qt default at runtime.
        cfg.set("general", "user_agent", "")
        pin_all_profiles(cfg)
        # "" == Qt's "use built-in default UA" sentinel (no stale custom UA).
        assert prof.httpUserAgent() == ""
    finally:
        wv_mod._PROFILES.clear()


def test_pin_all_profiles_noop_on_qt_default(qapp, fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "")
    wv_mod._PROFILES.clear()
    prof = get_profile("ua-default-noop")
    before = prof.httpUserAgent()
    try:
        pin_all_profiles(cfg)
        assert prof.httpUserAgent() == before
    finally:
        wv_mod._PROFILES.clear()


def test_pin_all_profiles_covers_qt_default_profile(qapp, fresh_config):
    """Qt's defaultProfile() is created outside get_profile; pin_all_profiles
    must cover it so it can't drift from qdbrowser profiles."""
    from PyQt6.QtWebEngineCore import QWebEngineProfile
    cfg = fresh_config.Config()
    cfg.set("general", "user_agent", "edge")
    wv_mod._PROFILES.clear()
    try:
        pin_all_profiles(cfg)
        assert QWebEngineProfile.defaultProfile().httpUserAgent() == \
            _UA_PRESETS["edge"]
    finally:
        wv_mod._PROFILES.clear()


# -- preset / strict-mode alignment ----------------------------------------


def test_every_preset_passes_strict_baseline():
    """A pinned preset must never be rejected by security_interceptor's
    strict-mode UA validator (otherwise we'd block our own requests)."""
    for name, ua in _UA_PRESETS.items():
        assert any(tok in ua for tok in _SAFE_UA_TOKENS), (
            f"preset {name!r} not in strict baseline: {ua!r}")


def test_sighup_repins_profiles(fresh_config, monkeypatch):
    """SIGHUP config reload must re-pin the UA on live profiles, so a
    reloaded ``[general] user_agent`` doesn't leave existing profiles
    drifting from newly-created ones."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    calls = []
    monkeypatch.setattr(wv_mod, "pin_all_profiles",
                        lambda *a, **k: calls.append(True))
    plug = AgentControlPlugin()
    plug._install_sighup_handler()
    plug._check_sighup_pending()
    assert calls, "SIGHUP reload did not re-pin profile user agents"


def test_custom_ua_outside_baseline_documents_strict_incompat():
    """Documented contract (resolve_user_agent docstring): a custom UA with
    no strict-baseline token is incompatible with user_agent_policy=strict —
    strict mode would block its own requests. This guards the documented
    limitation so it isn't silently 'fixed' into a false sense of safety."""
    custom = "MyCorpBrowser/1.0 (audited)"
    assert not any(tok in custom for tok in _SAFE_UA_TOKENS)

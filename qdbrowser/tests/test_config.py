"""Config singleton + TOML merge tests."""

import os


def test_defaults(fresh_config):
    cfg = fresh_config.Config()
    assert cfg.get("general", "homepage") == "about:blank"
    assert cfg.get("keybindings", "new_tab") == "Ctrl+T"
    assert cfg.get("keybindings", "command_palette") == "Ctrl+E"


def test_agent_control_guardrail_defaults(fresh_config):
    """iso2 13 E4: once the agent socket is on, guardrails are on too."""
    cfg = fresh_config.Config()
    assert cfg.get("agent_control", "policy_enforced") is True
    assert cfg.get("agent_control", "eval_rate_limit_per_minute") == 3
    assert cfg.get("agent_control", "require_handshake") is True
    # Broker mediation stays off: the bundled client is D-Bus
    # org.qdistro.BrowserBridge / QdBrowser1, not the JSON-RPC socket,
    # and CheckAgentAction would fail-closed without a broker.
    assert cfg.get("agent_control", "broker_enabled") is False
    assert cfg.get("agent_control", "allowed_client_exes") == []


def test_set_get_roundtrip(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "homepage", "https://example.com")
    assert cfg.get("general", "homepage") == "https://example.com"


def test_save_and_reload(fresh_config):
    cfg = fresh_config.Config()
    cfg.set("general", "homepage", "https://example.com")
    cfg.set("general", "window_width", 1600)
    cfg.save()
    assert os.path.exists(fresh_config.CONFIG_FILE)

    fresh_config.Config._instance = None
    cfg2 = fresh_config.Config()
    assert cfg2.get("general", "homepage") == "https://example.com"
    assert cfg2.get("general", "window_width") == 1600


def test_get_profile_falls_back_to_default(fresh_config):
    cfg = fresh_config.Config()
    p = cfg.get_profile("nonexistent")
    assert p["javascript_enabled"] is True
    assert p["default_zoom"] == 1.0


def test_default_keybindings_complete(fresh_config):
    cfg = fresh_config.Config()
    must_have = ["new_tab", "close_tab", "split_horizontal",
                 "split_vertical", "command_palette", "toggle_side_panel",
                 "address_bar", "find", "reload"]
    for key in must_have:
        assert cfg.get_keybinding(key), f"missing default keybind: {key}"

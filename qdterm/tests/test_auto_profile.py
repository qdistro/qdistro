"""Tests for the auto_profile plugin.

Layers:
  - Pure ProfileRule matching (no Qt).
  - Service lifecycle.
  - Plugin lifecycle tests.
"""

import pytest
import qterminator.config as config_mod
from qterminator.config import Config
from qterminator.plugins.auto_profile import (
    AutoProfilePlugin,
    AutoProfileService,
    ProfileRule,
    _read_proc_cmdline,
    parse_ssh_host,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(
        config_mod, "CONFIG_FILE", str(tmp_path / "config" / "config.toml"),
    )
    Config._instance = None
    yield
    Config._instance = None


# ---------------------------------------------------------------------------
# ProfileRule tests
# ---------------------------------------------------------------------------

def test_rule_hostname_match():
    rule = ProfileRule(hostname_regex=r"prod", profile="prod")
    rule.compile()

    assert rule.matches("prod-server", "", "") is True
    assert rule.matches("dev-server", "", "") is False
    assert rule.matches("my.prod.host", "", "") is True
    assert rule.matches("prod", "", "") is True


def test_rule_cwd_match():
    rule = ProfileRule(cwd_regex=r"^/etc/", profile="system")
    rule.compile()

    assert rule.matches("", "/etc/nixos", "") is True
    assert rule.matches("", "/etc/qdistro", "") is True
    assert rule.matches("", "/home/user", "") is False


def test_rule_command_match():
    rule = ProfileRule(command_regex=r"^sudo\s+", profile="sudo")
    rule.compile()

    assert rule.matches("", "", "sudo rm -rf /") is True
    assert rule.matches("", "", "ls") is False
    assert rule.matches("", "", "sudo -i") is True


def test_rule_multiple_conditions():
    rule = ProfileRule(
        hostname_regex=r"^prod-",
        cwd_regex=r"^/var/",
        profile="prod"
    )
    rule.compile()

    assert rule.matches("prod-server", "/var/log", "") is True
    assert rule.matches("dev-server", "/var/log", "") is False
    assert rule.matches("prod-server", "/home/user", "") is False


def test_rule_no_conditions_matches_anything():
    """A rule with only profile (no conditions) matches anything."""
    rule = ProfileRule(profile="default")
    rule.compile()

    assert rule.matches("anything", "anything", "anything") is True


def test_rule_compiles_without_regex():
    """A rule with no regex fields should compile without error."""
    rule = ProfileRule(profile="default")
    rule.compile()
    assert rule.matches("", "", "") is True


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------

class FakeTerminal:
    """Fake terminal for testing."""

    def __init__(self, profile="default"):
        self._profile_name = profile
        self._applied_profiles = []

    def apply_profile(self, name):
        self._profile_name = name
        self._applied_profiles.append(name)


class FakeShellIntegration:
    """Fake shell integration with correct global subscribe API."""

    def __init__(self):
        self._global_subs = []

    def subscribe_command_finished(self, callback):
        if callback not in self._global_subs:
            self._global_subs.append(callback)

    def unsubscribe_command_finished(self, callback):
        try:
            self._global_subs.remove(callback)
        except ValueError:
            pass

    def get_history(self, terminal):
        return None


class FakeWindow:
    """Fake window for testing."""

    def __init__(self):
        self.shell_integration = FakeShellIntegration()


class FakeRecord:
    """Fake command record."""

    def __init__(self, cwd="", text=""):
        self.cwd = cwd
        self.text = text


def test_service_attaches_and_caches_original_profile():
    win = FakeWindow()
    rules = [ProfileRule(cwd_regex=r"^/etc/", profile="system")]
    service = AutoProfileService(win, rules)

    term = FakeTerminal(profile="default")
    service.attach_terminal(term)

    tid = id(term)
    state = service._states[tid]

    assert state.original_profile == "default"
    assert state.current_profile == "default"


def test_service_applies_matched_profile():
    win = FakeWindow()
    rule = ProfileRule(cwd_regex=r"^/etc/", profile="system")
    rule.compile()
    rules = [rule]
    service = AutoProfileService(win, rules)

    term = FakeTerminal(profile="default")
    service.attach_terminal(term)

    # Simulate command finished with matching cwd
    record = FakeRecord(cwd="/etc/nixos")
    service._on_command_finished(term, record)

    assert term._profile_name == "system"

    tid = id(term)
    state = service._states[tid]
    assert state.applied_by_rule == "system"


def test_service_restores_original_on_no_match():
    win = FakeWindow()
    rule = ProfileRule(cwd_regex=r"^/etc/", profile="system")
    rule.compile()
    rules = [rule]
    service = AutoProfileService(win, rules)

    term = FakeTerminal(profile="default")
    service.attach_terminal(term)

    # First apply the rule
    record = FakeRecord(cwd="/etc/nixos")
    service._on_command_finished(term, record)
    assert term._profile_name == "system"

    # Now finish a command in a non-matching directory
    record = FakeRecord(cwd="/home/user")
    service._on_command_finished(term, record)

    # Should restore original
    assert term._profile_name == "default"

    tid = id(term)
    state = service._states[tid]
    assert state.applied_by_rule is None


def test_service_first_match_wins():
    win = FakeWindow()
    rules = [
        ProfileRule(cwd_regex=r"^/etc/", profile="system"),
        ProfileRule(cwd_regex=r"^/etc/nixos", profile="nixos"),
    ]
    for r in rules:
        r.compile()
    service = AutoProfileService(win, rules)

    term = FakeTerminal(profile="default")
    service.attach_terminal(term)

    record = FakeRecord(cwd="/etc/nixos")
    service._on_command_finished(term, record)

    # First rule (system) wins because it's checked first
    assert term._profile_name == "system"


def test_service_detach_restores_original():
    win = FakeWindow()
    rule = ProfileRule(cwd_regex=r"^/etc/", profile="system")
    rule.compile()
    rules = [rule]
    service = AutoProfileService(win, rules)

    term = FakeTerminal(profile="default")
    service.attach_terminal(term)

    # Apply a rule
    record = FakeRecord(cwd="/etc/nixos")
    service._on_command_finished(term, record)
    assert term._profile_name == "system"

    # Detach
    service.detach_terminal(term)

    # Should restore original
    assert term._profile_name == "default"


def test_service_ignores_terminal_not_attached():
    win = FakeWindow()
    rule = ProfileRule(cwd_regex=r"^/etc/", profile="system")
    rule.compile()
    rules = [rule]
    service = AutoProfileService(win, rules)

    term = FakeTerminal()
    # Don't attach

    # Should not crash
    record = FakeRecord(cwd="/etc/nixos")
    service._on_command_finished(term, record)


# ---------------------------------------------------------------------------
# SSH detection (parse_ssh_host / _read_proc_cmdline)
# ---------------------------------------------------------------------------

def test_parse_ssh_host_plain():
    assert parse_ssh_host(["ssh", "prod-1.example.com"]) == "prod-1.example.com"


def test_parse_ssh_host_user_host():
    assert parse_ssh_host(["ssh", "alice@prod-2"]) == "prod-2"


def test_parse_ssh_host_skips_value_flags():
    assert parse_ssh_host([
        "ssh", "-p", "2222", "-o", "StrictHostKeyChecking=no", "prod-3"
    ]) == "prod-3"


def test_parse_ssh_host_with_remote_command():
    assert parse_ssh_host([
        "/usr/bin/ssh", "prod-4", "tail", "-f", "/var/log/syslog",
    ]) == "prod-4"


def test_parse_ssh_host_not_ssh():
    assert parse_ssh_host(["ssh-add", "/home/me/.ssh/id_rsa"]) is None
    assert parse_ssh_host(["scp", "a.txt", "host:b.txt"]) is None
    assert parse_ssh_host([]) is None


def test_parse_ssh_host_only_options():
    # No host token at all.
    assert parse_ssh_host(["ssh", "-V"]) is None


def test_read_proc_cmdline_existing(tmp_path, monkeypatch):
    # Stand up a fake /proc/<pid>/cmdline by monkeypatching open().
    captured = {"pid": None}

    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/4242/cmdline":
            captured["pid"] = 4242
            from io import BytesIO
            return BytesIO(b"ssh\x00prod-9\x00")
        return real_open(path, *args, **kwargs)

    import builtins
    monkeypatch.setattr(builtins, "open", fake_open)
    assert _read_proc_cmdline(4242) == ["ssh", "prod-9"]
    assert captured["pid"] == 4242


def test_read_proc_cmdline_missing_returns_none():
    # PID we picked is overwhelmingly unlikely to exist.
    assert _read_proc_cmdline(0) is None


# ---------------------------------------------------------------------------
# Plugin tests
# ---------------------------------------------------------------------------

def test_plugin_loads_rules_from_config():
    cfg = Config()
    cfg.set("plugins", "auto_profile", "enabled", True)
    cfg.set("plugins", "auto_profile", "rules", [
        {"cwd_regex": "^/etc/", "profile": "system"},
        {"hostname_regex": "^prod-", "profile": "prod"},
    ])

    win = FakeWindow()
    plugin = AutoProfilePlugin()
    plugin.activate(win)

    assert len(plugin._rules) == 2
    assert plugin._rules[0].cwd_re is not None
    assert plugin._rules[1].hostname_re is not None

    plugin.deactivate()


def test_plugin_disabled_does_not_install_service():
    cfg = Config()
    cfg.set("plugins", "auto_profile", "enabled", False)

    class FakeWin:
        pass

    win = FakeWin()
    plugin = AutoProfilePlugin()
    plugin.activate(win)

    assert not hasattr(win, "auto_profile")


def test_plugin_no_rules_does_not_install_service():
    cfg = Config()
    cfg.set("plugins", "auto_profile", "enabled", True)
    cfg.set("plugins", "auto_profile", "rules", [])

    class FakeWin:
        pass

    win = FakeWin()
    plugin = AutoProfilePlugin()
    plugin.activate(win)

    assert not hasattr(win, "auto_profile")


def test_plugin_subscribes_to_shell_integration():
    """Test plugin subscribes to global shell_integration events."""
    cfg = Config()
    cfg.set("plugins", "auto_profile", "enabled", True)
    cfg.set("plugins", "auto_profile", "rules", [
        {"cwd_regex": "^/tmp", "profile": "default"},
    ])

    shell_int = FakeShellIntegration()

    class FakeWin:
        shell_integration = shell_int

    win = FakeWin()
    plugin = AutoProfilePlugin()
    plugin.activate(win)

    # Should have subscribed
    assert len(shell_int._global_subs) == 1

    plugin.deactivate()


def test_plugin_deactivate_stops_polling():
    cfg = Config()
    cfg.set("plugins", "auto_profile", "enabled", True)
    cfg.set("plugins", "auto_profile", "rules", [
        {"cwd_regex": "^/tmp", "profile": "default"},
    ])

    shell_int = FakeShellIntegration()

    class FakeWin:
        shell_integration = shell_int
        _tabs = None

    win = FakeWin()
    plugin = AutoProfilePlugin()
    plugin.activate(win)

    assert hasattr(win, "auto_profile")

    plugin.deactivate()

    assert not hasattr(win, "auto_profile")


def test_plugin_default_disabled():
    """Test plugin is disabled by default."""
    _cfg = Config()
    # Don't set enabled

    class FakeWin:
        pass

    win = FakeWin()
    plugin = AutoProfilePlugin()
    plugin.activate(win)

    assert not hasattr(win, "auto_profile")

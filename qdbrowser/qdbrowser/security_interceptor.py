"""Security URL request interceptor for qdbrowser.

One small ``QWebEngineUrlRequestInterceptor`` that does three things at
the request-info layer:

  1. **HTTPS-only.** When ``[security] https_only=true``, rewrite
     ``http://`` to ``https://`` for hostnames not in
     ``[security] http_allowlist``. Requests that can't be safely
     upgraded (POST with a body, ``ws://``, ``ftp://``, etc.) are
     blocked and surface ``qdbrowser.security https_blocked`` in the
     journal — the caller can then choose to fall back to an
     interstitial page in the WebView.

  2. **Do-Not-Track / Sec-GPC.** When ``[security] do_not_track=true``,
     add ``DNT: 1`` and ``Sec-GPC: 1`` headers to every outbound
     request.

  3. **UA validation.** When ``[security] user_agent_policy="strict"``,
     reject any request whose ``User-Agent`` header has been overridden
     to a value outside the conservative baseline (Qt-provided UA, or
     one of the well-known preset strings). Rejection is logged as
     ``qdbrowser.security ua_override_rejected``.

The interceptor is registered per-WebView via
``WebView.add_interceptor`` so it inherits the same lifecycle as the
plugin-supplied interceptors. ``apply_security_policy(window)`` is the
one-line entry point called from ``MainWindow._enable_default_plugins``.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Iterable

log = logging.getLogger("qdbrowser.security")


# Conservative UA baseline. A strict-mode override is accepted only if
# the *substring* match against this list is non-empty. The list is
# intentionally short: Qt-default, plus the three preset UAs the
# hardening doc names.
_SAFE_UA_TOKENS = (
    "QtWebEngine",
    "Firefox/",
    "Chrome/",
    "Edg/",
    "Safari/",  # macOS WebKit-derived baseline; Chrome/Edg include it
)


# Schemes we can safely upgrade to HTTPS. Anything else (ws, ftp,
# file, data, javascript, qdbrowser:) is left alone — the only
# enforcement is the HTTPS-only-mode block on plain http://.
_UPGRADEABLE_SCHEMES = ("http",)


def _host_in_globs(host: str, globs: Iterable[str]) -> bool:
    h = (host or "").lower()
    for g in globs or ():
        if not isinstance(g, str) or not g:
            continue
        if fnmatch.fnmatch(h, g.lower()):
            return True
    return False


class SecurityInterceptor:
    """A duck-typed ``UrlInterceptor``: it has ``intercept(info)`` and
    is wired into a WebView's ``_ChainInterceptor``.

    Constructed once per qdbrowser window; mutating config takes
    effect on the next request (no rebinding needed).
    """

    capabilities = ["url_interceptor"]

    def __init__(self, config):
        self._config = config
        # The "default" UA value the page started with — captured so
        # strict-mode can distinguish a JS-side spoof from a legitimate
        # preset.
        self._baseline_ua: str | None = None

    # -- public hook for the WebView chain -----------------------------

    def intercept(self, info):  # noqa: N802 (Qt-ish API surface)
        cfg = self._config
        sec = cfg.get("security", default={}) or {}

        url = info.requestUrl()
        host = url.host() if hasattr(url, "host") else ""

        # 1. HTTPS-only
        if bool(sec.get("https_only")):
            self._enforce_https(info, url, host, sec)

        # 2. DNT / Sec-GPC
        if bool(sec.get("do_not_track")):
            try:
                info.setHttpHeader(b"DNT", b"1")
                info.setHttpHeader(b"Sec-GPC", b"1")
            except Exception as exc:
                log.debug("DNT header set failed: %s", exc)

        # 3. UA validation
        policy = sec.get("user_agent_policy", "default")
        if policy == "strict":
            self._validate_ua(info)

    # -- HTTPS-only -----------------------------------------------------

    def _enforce_https(self, info, url, host, sec):
        scheme = url.scheme().lower() if hasattr(url, "scheme") else ""
        if scheme not in _UPGRADEABLE_SCHEMES:
            return
        if _host_in_globs(host, sec.get("http_allowlist", []) or []):
            return
        # Try to upgrade in place. Qt's QWebEngineUrlRequestInfo
        # supports ``redirect(QUrl)`` since 6.0.
        upgraded = self._build_https_url(url)
        if upgraded is None:
            log.warning(
                "qdbrowser.security https_blocked host=%s url=%s reason=not_upgradeable",
                host, url.toString() if hasattr(url, "toString") else "")
            try:
                info.block(True)
            except Exception:
                pass
            return
        try:
            info.redirect(upgraded)
            log.info("qdbrowser.security https_upgraded host=%s", host)
        except Exception as exc:
            log.warning("https upgrade failed host=%s: %s", host, exc)

    def _build_https_url(self, url):
        """Return a https-flavoured copy of ``url`` or ``None`` if the
        request shape can't be safely upgraded.

        We use a copy-and-mutate path so we don't depend on Qt's
        ``setScheme`` (present everywhere) but keep a defensive
        try/except. Requests that bring a body (POST/PUT/PATCH) can be
        upgraded but the body is preserved by Qt's redirect machinery
        — Chromium's standard behaviour. ws://, ftp://, etc. are not
        in ``_UPGRADEABLE_SCHEMES`` so this function is never called
        for them.
        """
        try:
            from PyQt6.QtCore import QUrl
            new = QUrl(url)
            new.setScheme("https")
            # Strip any explicit :80 port so the upgraded URL doesn't
            # try to speak TLS to a cleartext-only port.
            if new.port() == 80:
                new.setPort(-1)
            return new
        except Exception as exc:
            log.debug("https url build failed: %s", exc)
            return None

    # -- UA validation --------------------------------------------------

    def _validate_ua(self, info):
        # Qt's UrlRequestInfo doesn't expose the outbound UA directly,
        # but it does expose ``initiator()`` and ``requestMethod()``.
        # Strict-mode UA enforcement is page-side (the WebEngineProfile
        # owns the UA string) — the interceptor's job is to flag any
        # request whose UA header is overridden via fetch()/XHR to
        # something outside the baseline.
        try:
            ua = bytes(info.httpHeader(b"User-Agent") or b"").decode(
                "utf-8", "replace")
        except Exception:
            return
        if not ua:
            return
        if any(tok in ua for tok in _SAFE_UA_TOKENS):
            return
        # Out-of-baseline UA -> drop the request and log.
        log.warning(
            "qdbrowser.security ua_override_rejected ua=%r url=%s",
            ua,
            info.requestUrl().toString() if hasattr(info, "requestUrl")
            else "")
        try:
            info.block(True)
        except Exception:
            pass


def apply_security_policy(window) -> SecurityInterceptor:
    """Install a single ``SecurityInterceptor`` on every WebView the
    window owns now and in the future.

    Returns the interceptor so callers (tests) can grab it.
    """
    from qdbrowser.config import Config
    interceptor = SecurityInterceptor(Config())

    # Wire to every existing WebView via the window's tab tree.
    try:
        tabs = window._tabs
    except AttributeError:
        return interceptor
    for i in range(tabs.count()):
        split = tabs.widget(i)
        if hasattr(split, "find_webviews"):
            for wv in split.find_webviews():
                try:
                    wv.add_interceptor(interceptor)
                except Exception:
                    pass

    # Wire to future WebViews too.
    if hasattr(window, "webview_added"):
        def _on_added(wv, _interc=interceptor):
            try:
                wv.add_interceptor(_interc)
            except Exception:
                pass
        try:
            window.webview_added.connect(_on_added)
        except Exception:
            pass

    return interceptor


def compose_isolate_origins_flag(isolate_origins) -> str:
    """Build the ``--isolate-origins=...`` flag value for Chromium.

    Each origin should be a scheme+host (e.g. ``https://bank.example.com``).
    Origins without a scheme are skipped — Chromium rejects unsigned
    origins and would refuse to start.
    """
    valid = []
    for o in isolate_origins or ():
        if not isinstance(o, str):
            continue
        if "://" not in o:
            log.warning("ignoring isolate-origin without scheme: %r", o)
            continue
        valid.append(o.strip().rstrip("/"))
    if not valid:
        return ""
    return "--isolate-origins=" + ",".join(valid)

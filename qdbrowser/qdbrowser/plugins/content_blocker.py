"""Content blocker — hosts-list + EasyList syntax + cosmetic filters
+ per-site toggles.

Supported syntax (subset of EasyList / ABP):

  ||example.com^                  block any URL on example.com
  ||sub.example.com^$third-party  block when 3rd party
  /tracker[0-9]+\\.js             regex literal between slashes
  example.com##.ads               cosmetic: hide .ads on example.com
  ##.global-ads                   cosmetic: hide .global-ads everywhere
  0.0.0.0 host.tld                hosts-list (still supported)
  @@||good.example.com^           exception (whitelist) for that URL
  ! comment                       ignored
  # comment                       ignored

Loaded from:
  - ``~/.config/qdbrowser/blocklist.hosts``   — hosts-only legacy
  - ``~/.config/qdbrowser/blocklist.txt``     — EasyList-format primary

Per-site toggles live in ``[blocklist.site_toggles]`` of the TOML
config: ``{"example.com": "off", "news.site": "cosmetic-only"}``.
States: ``on`` (default), ``off``, ``cosmetic-only``, ``network-only``.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import threading
from urllib.parse import urlparse

from PyQt6.QtWebEngineCore import QWebEngineUrlRequestInfo

from qdbrowser.config import CONFIG_DIR, Config
from qdbrowser.plugin import CommandProvider, PageObserver, UrlInterceptor

log = logging.getLogger("qdbrowser.content_blocker")

HOSTS_PATH = os.path.join(CONFIG_DIR, "blocklist.hosts")
EASYLIST_PATH = os.path.join(CONFIG_DIR, "blocklist.txt")


# Defense against catastrophic-backtracking attacks via user-supplied
# blocklists. We reject regexes whose source is suspiciously long or
# that contain obvious nested-quantifier red flags. The cost we accept
# is dropping a tiny minority of legitimate-but-baroque rules.
_MAX_REGEX_LEN = 200
# A group ``(...X)Y`` where both ``X`` and ``Y`` are quantifiers (one
# inside the group, one after) is the canonical catastrophic-backtrack
# shape — e.g. ``(a+)+``, ``(.*)*``, ``(\w+)?``-anchored variants.
# Also flag doubled wildcards like ``.*.*.*`` that don't strictly nest
# but still blow up on adversarial input.
_DANGEROUS_REGEX_RE = re.compile(
    r"[+*?]\)[+*?]"
    r"|[.*+?]{4,}"
)


def _safe_compile(source: str) -> re.Pattern | None:
    """Compile a regex but refuse patterns that look like ReDoS bombs."""
    if len(source) > _MAX_REGEX_LEN:
        log.warning("blocklist rule too long (%d > %d), skipped",
                    len(source), _MAX_REGEX_LEN)
        return None
    if _DANGEROUS_REGEX_RE.search(source):
        log.warning("blocklist rule has nested quantifiers, skipped: %r",
                    source[:60])
        return None
    try:
        return re.compile(source)
    except re.error as exc:
        log.warning("blocklist rule failed to compile (%s): %r",
                    exc, source[:60])
        return None


# ---------------- EasyList-ish parser -------------------------------

class _NetworkRule:
    __slots__ = ("pattern", "host_suffix", "third_party_only",
                 "is_exception", "raw")

    def __init__(self, raw: str):
        self.raw = raw
        self.pattern: re.Pattern | None = None
        self.host_suffix: str | None = None
        self.third_party_only = False
        self.is_exception = False
        self._compile(raw)

    def _compile(self, raw: str):
        line = raw.strip()
        if line.startswith("@@"):
            self.is_exception = True
            line = line[2:]
        # Options after $
        if "$" in line:
            line, opts = line.rsplit("$", 1)
            for opt in opts.split(","):
                opt = opt.strip().lower()
                if opt in ("third-party", "3p"):
                    self.third_party_only = True
                # Other options (image, script, ...) — ignored in v0.

        # ||host^ form
        if line.startswith("||"):
            host_part = line[2:]
            if host_part.endswith("^"):
                host_part = host_part[:-1]
            # Strip path component.
            host_only = host_part.split("/", 1)[0].lower()
            self.host_suffix = host_only
            return

        # /regex/ form — user-supplied; guard against ReDoS.
        if line.startswith("/") and line.endswith("/") and len(line) > 2:
            self.pattern = _safe_compile(line[1:-1])
            return

        # Plain substring / wildcard.
        if line:
            esc = re.escape(line).replace(r"\*", ".*").replace(r"\^", r"[/:?=&]")
            self.pattern = _safe_compile(esc)

    def matches(self, url: str, document_host: str | None) -> bool:
        if self.host_suffix is not None:
            host = _url_host(url)
            if not host:
                return False
            if not _is_host_suffix(host, self.host_suffix):
                return False
        elif self.pattern is not None:
            if not self.pattern.search(url):
                return False
        else:
            return False

        if self.third_party_only and document_host:
            req_host = _url_host(url)
            if req_host and _same_site(req_host, document_host):
                return False
        return True


class _CosmeticRule:
    __slots__ = ("selector", "host_suffix")

    def __init__(self, host_suffix: str | None, selector: str):
        self.host_suffix = host_suffix.lower() if host_suffix else None
        self.selector = selector


def parse_easylist(text: str):
    """Return ``(network_rules, cosmetic_rules)`` from EasyList text."""
    network: list[_NetworkRule] = []
    cosmetic: list[_CosmeticRule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if (line.startswith("!") or line.startswith("#")) and "##" not in line:
            # ABP "!" comment, or plain "#" comment (rare). The
            # ``##`` check is what distinguishes a true comment from
            # a cosmetic selector that happens to start with ``#``.
            continue
        if line.startswith("[") and line.endswith("]"):
            continue  # [Adblock Plus 2.0] header
        if "##" in line and not line.startswith("/"):
            host_part, _, sel = line.partition("##")
            host_part = host_part.strip()
            if not sel:
                continue
            cosmetic.append(_CosmeticRule(host_part or None, sel))
            continue
        # Hosts-list disguised as easylist? handled by _parse_hosts caller.
        rule = _NetworkRule(line)
        if rule.host_suffix or rule.pattern:
            network.append(rule)
    return network, cosmetic


def _parse_hosts_file(path: str) -> set[str]:
    out: set[str] = set()
    if not os.path.exists(path):
        return out
    try:
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) == 1:
                    out.add(parts[0].lower())
                else:
                    out.add(parts[-1].lower())
    except OSError:
        pass
    return out


# Backwards compat: tests still import this private symbol.
_parse_hosts = _parse_hosts_file


def _url_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_host_suffix(host: str, suffix: str) -> bool:
    if host == suffix:
        return True
    return host.endswith("." + suffix)


def _same_site(a: str, b: str) -> bool:
    """Cheap eTLD+1 comparison: last two labels."""
    pa = a.rsplit(".", 2)[-2:]
    pb = b.rsplit(".", 2)[-2:]
    return pa == pb


def _suffix_match(host: str, set_: set[str]) -> bool:
    parts = host.split(".")
    for i in range(len(parts) - 1):
        sub = ".".join(parts[i:])
        if sub in set_:
            return True
    return False


# ---------------- plugin --------------------------------------------

class ContentBlockerPlugin(UrlInterceptor, CommandProvider, PageObserver):
    name = "content_blocker"
    description = "Hosts list + EasyList + cosmetic filters + per-site toggles."
    capabilities = ["url_interceptor", "command_provider", "page_observer"]

    def __init__(self):
        super().__init__()
        self._blocked_hosts: set[str] = set()
        self._allow_hosts: set[str] = set()
        self._network_rules: list = []
        self._cosmetic_rules: list = []
        self._enabled = True
        self._site_toggles: dict = {}
        # Immutable views consulted from the Qt URL-interceptor thread.
        # Mutation happens on the GUI thread; we swap the reference
        # atomically so a request never sees a half-updated set.
        self._blocked_hosts_view: frozenset = frozenset()
        self._allow_hosts_view: frozenset = frozenset()
        self._site_toggles_view: dict = {}
        # Per-counter ``itertools.count``: thread-safe atomic increment
        # in CPython (the GIL covers a single ``next()`` call). The
        # IO-thread interceptor increments; the GUI thread reads via
        # ``stats``. We snapshot by reading-without-advancing.
        self._stats_blocked = itertools.count()
        self._stats_allowed = itertools.count()
        self._stats_cosmetic_hidden = itertools.count()
        # Mirror of the last-returned value of each counter, updated
        # under a tiny lock so stats() returns a coherent snapshot.
        self._stats_lock = threading.Lock()
        self._stats_view = {"blocked": 0, "allowed": 0,
                            "cosmetic_hidden": 0}
        self._window = None

    # -- lifecycle -----------------------------------------------------

    def activate(self, window):
        self._window = window
        cfg = Config()
        self._enabled = bool(cfg.get("blocklist", "enabled", default=True))
        self._reload()
        self._site_toggles = (
            cfg.get("blocklist", "site_toggles", default={}) or {})
        self._site_toggles_view = dict(self._site_toggles)

    def _reload(self):
        self._blocked_hosts = _parse_hosts_file(HOSTS_PATH)
        cfg = Config()
        for h in cfg.get("blocklist", "extra_blocked", default=[]) or []:
            self._blocked_hosts.add(h.lower())
        for h in cfg.get("blocklist", "allowlist", default=[]) or []:
            self._allow_hosts.add(h.lower())

        # EasyList
        if os.path.exists(EASYLIST_PATH):
            try:
                with open(EASYLIST_PATH) as f:
                    text = f.read()
                self._network_rules, self._cosmetic_rules = parse_easylist(text)
            except OSError:
                pass
        # Freeze snapshots for the interceptor thread.
        self._blocked_hosts_view = frozenset(self._blocked_hosts)
        self._allow_hosts_view = frozenset(self._allow_hosts)

    # -- per-site state ------------------------------------------------

    def _bump(self, key: str) -> None:
        """Increment a stats counter atomically. Safe from the IO
        thread; the GUI thread reads via ``stats`` under a lock."""
        with self._stats_lock:
            self._stats_view[key] = self._stats_view.get(key, 0) + 1

    def site_state(self, host: str) -> str:
        """Resolve the toggle state for ``host``. Returns one of
        ``on``, ``off``, ``cosmetic-only``, ``network-only``."""
        if not host:
            return "on"
        host = host.lower()
        # Atomic snapshot — the GUI thread can mutate the underlying
        # dict between iterations.
        toggles = self._site_toggles_view
        if host in toggles:
            return toggles[host]
        for k, v in toggles.items():
            if _is_host_suffix(host, k):
                return v
        return "on"

    def set_site_state(self, host: str, state: str):
        host = host.lower().lstrip(".")
        if state == "on":
            self._site_toggles.pop(host, None)
        else:
            self._site_toggles[host] = state
        # Atomic swap so the IO-thread interceptor sees a consistent
        # view (no half-mutated dict).
        self._site_toggles_view = dict(self._site_toggles)
        cfg = Config()
        cfg.set("blocklist", "site_toggles", dict(self._site_toggles))
        try:
            cfg.save()
        except Exception as exc:
            log.warning("could not persist site_toggles: %s", exc)

    # -- network interception -----------------------------------------

    def intercept(self, info: QWebEngineUrlRequestInfo):
        if not self._enabled:
            return
        url = info.requestUrl()
        host = url.host().lower()
        if not host:
            return

        # Document-host for third-party + per-site lookup. Empty
        # doc_host happens for ``data:``/``blob:``/``about:blank`` —
        # we deliberately do NOT fall back to the request host for the
        # site_state lookup; falling back inverts the user's intent
        # ("disable blocking on news.site" must not mean "let every
        # tracker through on its own host").
        try:
            doc_host = info.firstPartyUrl().host().lower() or None
        except Exception:
            doc_host = None

        # No document host → can't honor per-site toggle; behave as "on"
        # (most restrictive). Allowlist is still checked below.
        state = self.site_state(doc_host) if doc_host else "on"
        if state == "off":
            return
        cosmetic_only = (state == "cosmetic-only")

        # Allowlist short-circuit applies regardless of network/cosmetic
        # mode (it's always-allow).
        allow_view = self._allow_hosts_view
        if host in allow_view or _suffix_match(host, allow_view):
            self._bump("allowed")
            return

        if cosmetic_only:
            return

        # Hosts-list block (frozen snapshot — see set_blocked_hosts).
        blocked_view = self._blocked_hosts_view
        if host in blocked_view or _suffix_match(host, blocked_view):
            self._bump("blocked")
            info.block(True)
            return

        # EasyList rules — exceptions first.
        url_str = url.toString()
        for rule in self._network_rules:
            if rule.is_exception and rule.matches(url_str, doc_host):
                self._bump("allowed")
                return
        for rule in self._network_rules:
            if rule.is_exception:
                continue
            if rule.matches(url_str, doc_host):
                self._bump("blocked")
                info.block(True)
                return

    # -- cosmetic CSS injection ---------------------------------------

    def cosmetic_css_for(self, doc_host: str) -> str:
        """Return a CSS snippet hiding all cosmetic selectors that apply
        to ``doc_host``. Used by ``on_load_finished``.
        """
        if not self._enabled or not doc_host:
            return ""
        state = self.site_state(doc_host)
        if state in ("off", "network-only"):
            return ""
        selectors: list[str] = []
        for rule in self._cosmetic_rules:
            if rule.host_suffix is None \
                    or _is_host_suffix(doc_host, rule.host_suffix):
                selectors.append(rule.selector)
        if not selectors:
            return ""
        # Chunked: large pages may have thousands of selectors; one rule
        # block is fine because the parser de-dupes via the engine.
        return ", ".join(selectors) + " { display: none !important; }"

    def on_load_finished(self, webview, ok: bool):
        if not ok:
            return
        host = _url_host(webview.url())
        css = self.cosmetic_css_for(host)
        if not css:
            return
        # Inject via JS — survives across SPA route changes if we re-run
        # on title changes (cheap enough).
        js = (
            "(function(css){"
            "var id='__qdb_cosmetic';"
            "var el=document.getElementById(id);"
            "if(!el){el=document.createElement('style');el.id=id;"
            "document.documentElement.appendChild(el);}"
            "el.textContent=css;"
            f"}})({_js_string(css)})"
        )
        try:
            webview.view.page().runJavaScript(js)
            self._bump("cosmetic_hidden")
        except Exception:
            pass

    # -- commands ------------------------------------------------------

    @property
    def stats(self):
        # Read each counter's current position via a no-op tee — there
        # is no peek API, so we track our own mirror behind a lock.
        with self._stats_lock:
            return dict(self._stats_view)

    def get_commands(self, window):
        wv = window._active_webview if window else None
        host = _url_host(wv.url()) if wv else ""
        out = [
            (f"Content blocker: {'ON' if self._enabled else 'OFF'} (toggle)",
             self._toggle),
            (f"Blocker stats ({self.stats['blocked']} blocked)",
             self._show_stats),
            ("Reload block lists", self._reload),
        ]
        if host:
            current = self.site_state(host)
            for state in ("on", "off", "cosmetic-only", "network-only"):
                marker = "● " if current == state else "○ "
                out.append((
                    f"Site blocking for {host}: {marker}{state}",
                    lambda h=host, s=state: self.set_site_state(h, s),
                ))
        return out

    def _toggle(self):
        self._enabled = not self._enabled

    def _show_stats(self):
        from PyQt6.QtWidgets import QMessageBox
        msg = (
            f"Enabled: {self._enabled}\n"
            f"Hosts loaded: {len(self._blocked_hosts)}\n"
            f"Network rules: {len(self._network_rules)}\n"
            f"Cosmetic rules: {len(self._cosmetic_rules)}\n"
            f"Blocked: {self.stats['blocked']}\n"
            f"Allowed: {self.stats['allowed']}\n"
            f"Cosmetic injections: {self.stats['cosmetic_hidden']}\n"
            f"Per-site toggles: {len(self._site_toggles)}"
        )
        QMessageBox.information(self._window, "Content blocker", msg)


def _js_string(s: str) -> str:
    """Encode for embedding inside a JS literal."""
    import json as _json
    return _json.dumps(s)

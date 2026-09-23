"""Thin wrapper around QWebEngineView that owns a per-tab profile and
emits a coherent set of signals (mirrors the wrapper-style of qterminator's
TerminalWidget around QTermWidget).

The wrapper supplies:
  - per-instance "group" (for tab stacks)
  - pinned / muted state
  - synchronous title/icon/url accessors
  - one place to inject the per-tab UrlRequestInterceptor

Profiles default to a shared off-the-record profile so private mode is
simple; persistent profiles (cookies, cache, history) attach via
``set_profile``.
"""

from __future__ import annotations

import logging
import os

from PyQt6.QtCore import (
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QIcon
from PyQt6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineUrlRequestInterceptor,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QSizePolicy, QVBoxLayout, QWidget

from .ca_bundle import _safe_profile_name
from .clipboard_silo import profile_silo_segment

log = logging.getLogger("qdbrowser.webview")

_PROFILES: dict = {}


# --- §6 User-agent: single source of truth --------------------------------
#
# Fingerprinting / silo-distinguishing risk: if every QWebEngineProfile
# computed its own User-Agent (or a page were allowed to drift it), pages
# could tell qdbrowser silos apart. We therefore resolve ONE UA string from
# config (``[general] user_agent``) and apply the identical value to every
# profile we mint via ``apply_user_agent`` (called from ``get_profile``).
#
# Accepted ``user_agent`` values:
#   ""        -> Qt/Chromium default (we leave the profile UA untouched;
#                Qt computes the same default for every profile in one build,
#                so this is already consistent).
#   "firefox" -> conservative Firefox preset.
#   "chrome"  -> conservative Chrome preset.
#   "edge"    -> conservative Edge preset.
#   <other>   -> taken verbatim as a custom UA (single source of truth, still
#                applied identically to every profile so there is no drift).
#
# The preset strings deliberately contain the tokens that
# security_interceptor's strict UA baseline accepts (``Firefox/``,
# ``Chrome/``, ``Edg/``) so a pinned preset is never self-rejected.

_UA_PRESETS = {
    "firefox": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
        "Gecko/20100101 Firefox/128.0"
    ),
    "chrome": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "edge": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
    ),
}


def resolve_user_agent(config=None) -> str | None:
    """Resolve the single configured User-Agent string, or ``None`` to mean
    "use the Qt/Chromium default" (consistent across profiles in one build).

    ``config`` may be a ``Config`` instance; when ``None`` the singleton is
    used. The lookup is ``[general] user_agent``. A preset name maps to a
    canned UA; any other non-empty value is returned verbatim.

    NOTE on interaction with ``[security] user_agent_policy="strict"``:
    the strict-mode validator in ``security_interceptor`` only tolerates
    UAs containing a token from its conservative baseline (``Firefox/``,
    ``Chrome/``, ``Edg/``, ``QtWebEngine``, ``Safari/``). The three
    presets here are deliberately built to contain those tokens. A
    *custom* UA that contains none of them is therefore incompatible with
    strict mode (its own requests would be blocked) — administrators must
    keep custom UAs inside the strict baseline or leave the policy at
    ``"default"``.
    """
    if config is None:
        from qdbrowser.config import Config
        config = Config()
    raw = config.get("general", "user_agent", default="")
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    preset = _UA_PRESETS.get(value.lower())
    if preset is not None:
        return preset
    return value


def apply_user_agent(profile, config=None) -> None:
    """Enforce the single source-of-truth UA on ``profile``.

    When a UA is configured the *same* string is pinned on every profile.
    When the resolved UA is ``None`` (Qt default), we explicitly set the
    empty string, which Qt interprets as "use the default UA" — this
    makes the enforcement authoritative in BOTH directions: reverting
    ``[general] user_agent`` to ``""`` re-pins a previously-customised
    profile back to the (consistent) Qt default rather than leaving it
    stale.
    """
    ua = resolve_user_agent(config)
    try:
        # Empty string -> Qt restores its built-in default UA, which is
        # identical for every profile in a single build, so this keeps
        # all profiles consistent.
        profile.setHttpUserAgent(ua or "")
    except Exception:
        # A missing setter on an exotic PyQt6 build must not break startup.
        pass


def pin_all_profiles(config=None) -> None:
    """Re-enforce the single source-of-truth UA on every profile that exists.

    Covers (a) every profile in our cache and (b) Qt's own
    ``defaultProfile()`` — which is constructed by Qt outside
    ``get_profile`` and would otherwise drift from qdbrowser profiles.
    Idempotent; safe to call at startup and again whenever
    ``[general] user_agent`` changes at runtime (including a revert to the
    Qt default). Enforces in both directions, so it never leaves a
    previously-pinned profile stale.
    """
    # QWebEngineProfile — including defaultProfile() — requires a running
    # QApplication. Touching it without one crashes the WebEngine C++ layer,
    # which is a native segfault rather than a catchable Python exception
    # (the try/except below cannot save us). With no QApplication there are
    # also no live WebEngine profiles to pin, so there is nothing to do.
    if QApplication.instance() is None:
        return
    seen = set()
    for prof in list(_PROFILES.values()):
        if id(prof) in seen:
            continue
        seen.add(id(prof))
        apply_user_agent(prof, config)
    try:
        default = QWebEngineProfile.defaultProfile()
        if default is not None and id(default) not in seen:
            apply_user_agent(default, config)
    except Exception:
        pass

# Subscribers (plain Python callables) for "a new profile was created"
# events. Used by downloads.py to wire ``downloadRequested`` on every
# profile we mint, without monkey-patching ``get_profile``.
_PROFILE_LISTENERS: list = []


def on_profile_created(callback) -> None:
    """Register ``callback(profile)`` to be invoked for every profile
    qdbrowser creates from now on. Also invoked retroactively for
    every profile already in the cache, so the subscriber doesn't
    miss the default profile that was minted before activation.
    """
    if callback not in _PROFILE_LISTENERS:
        _PROFILE_LISTENERS.append(callback)
    for prof in list(_PROFILES.values()):
        try:
            callback(prof)
        except Exception:
            pass


def off_profile_created(callback) -> None:
    """Unregister a ``callback`` previously passed to
    ``on_profile_created``."""
    try:
        _PROFILE_LISTENERS.remove(callback)
    except ValueError:
        pass


def _notify_profile_created(profile) -> None:
    for cb in list(_PROFILE_LISTENERS):
        try:
            cb(profile)
        except Exception:
            pass


# Monotonic webview id source. ``id()`` is unsafe to expose to agents
# because Python may reuse the memory address after a tab is closed,
# so an agent holding a stale tab_id could end up driving an unrelated
# WebView. We assign a process-unique integer at construction time.
_NEXT_WEBVIEW_ID: int = 1


def _alloc_webview_id() -> int:
    global _NEXT_WEBVIEW_ID
    out = _NEXT_WEBVIEW_ID
    _NEXT_WEBVIEW_ID += 1
    return out


def get_profile(name: str = "default") -> QWebEngineProfile:
    """Return a singleton named profile. Pass ``"private"`` for an
    off-the-record profile.

    J8 (cross-silo isolation): a *persistent* profile's on-disk storage is
    isolated per silo. When ``$QDISTRO_SILO`` is set (the launcher injects it),
    storage lives under ``~/.local/share/qdbrowser/profiles/<silo>/<name>`` so
    two silos that share ``$HOME`` do NOT share one cookie jar / cache /
    localStorage — which would defeat the silo web-identity boundary. With no
    silo set (plain standalone use) the legacy flat
    ``~/.local/share/qdbrowser/profiles/<name>`` path is kept, so existing
    profiles aren't orphaned. The off-the-record ``"private"`` profile has no
    on-disk storage and is silo-independent by construction.

    The ``name`` component is interpolated into the on-disk path and reaches
    us from attacker-selectable sources (``--profile``, the agent RPC
    ``open_tab(profile=...)``, restored layouts). It MUST be a safe single
    path segment or a crafted name (``../beta/default``) would climb out of
    the ``<silo>/`` segment into a *sibling* silo's storage — defeating the
    very boundary this fix creates. We fail closed: reject anything that is
    not a safe basename (reusing ``ca_bundle._safe_profile_name``) before it
    reaches either the cache key or the path build.
    """
    if name != "private":
        safe = _safe_profile_name(name)
        if safe is None:
            raise ValueError(f"unsafe qdbrowser profile name: {name!r}")
        name = safe
    # The silo segment is part of the cache key so a single interpreter that
    # somehow saw two silos would not hand back the wrong silo's profile.
    # Both ``silo_seg`` (silo grammar) and ``name`` (basename check above) are
    # guaranteed to be single path segments, so the key is unambiguous.
    silo_seg = "" if name == "private" else profile_silo_segment()
    cache_key = f"{silo_seg}/{name}" if silo_seg else name
    if cache_key in _PROFILES:
        return _PROFILES[cache_key]
    if name == "private":
        prof = QWebEngineProfile()  # off-the-record, no name
    else:
        prof = QWebEngineProfile(name)
        if silo_seg:
            base = os.path.expanduser(
                f"~/.local/share/qdbrowser/profiles/{silo_seg}/{name}")
        else:
            base = os.path.expanduser(
                f"~/.local/share/qdbrowser/profiles/{name}")
        os.makedirs(base, exist_ok=True)
        prof.setPersistentStoragePath(os.path.join(base, "storage"))
        prof.setCachePath(os.path.join(base, "cache"))
        prof.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        prof.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
    # §6: pin the single source-of-truth UA on every profile (default,
    # named, and private) so silos can't be distinguished by UA drift.
    apply_user_agent(prof)
    _PROFILES[cache_key] = prof
    _notify_profile_created(prof)
    return prof


class _ChainInterceptor(QWebEngineUrlRequestInterceptor):
    """Fans every URL request through a list of UrlInterceptor plugins.

    Owned by ``WebView`` (so it lives at least as long as the page).
    Plugins register/unregister via ``add`` / ``remove``.
    """

    def __init__(self):
        super().__init__()
        self._handlers: list = []

    def add(self, handler):
        if handler not in self._handlers:
            self._handlers.append(handler)

    def remove(self, handler):
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass

    def interceptRequest(self, info):  # noqa: N802 (Qt API)
        for h in list(self._handlers):
            try:
                h.intercept(info)
            except Exception:
                # A misbehaving plugin must not break navigation.
                pass


class WebView(QWidget):
    """One web view + its title bar.

    Carries Qt signals the window connects to:
      title_changed, icon_changed, url_changed, load_started,
      load_progress, load_finished, focus_gained, close_requested.
    """

    title_changed = pyqtSignal(object, str)            # (self, title)
    icon_changed = pyqtSignal(object, object)          # (self, QIcon)
    url_changed = pyqtSignal(object, str)              # (self, url)
    load_started = pyqtSignal(object)                  # (self,)
    load_progress = pyqtSignal(object, int)            # (self, percent)
    load_finished = pyqtSignal(object, bool)           # (self, ok)
    focus_gained = pyqtSignal(object)                  # (self,)
    close_requested = pyqtSignal(object)               # (self,)

    def __init__(self,
                 url: str | None = None,
                 profile_name: str = "default",
                 parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._profile = get_profile(profile_name)
        self._profile_name = profile_name
        self._stable_id: int = _alloc_webview_id()
        self.group: str | None = None  # tab-stack name
        self.pinned: bool = False
        self.muted: bool = False
        self._zoom: float = 1.0
        self._page_load_seq: int = 0
        self._last_focus_seen = False

        # Per-page interceptor: owned by this WebView, dies with it. We
        # deliberately do NOT call setUrlRequestInterceptor on the
        # shared profile — that would clobber every other view's chain
        # and leave dangling pointers when the profile outlives the view.
        self._interceptor = _ChainInterceptor()

        self.view = QWebEngineView(self)
        page = QWebEnginePage(self._profile, self.view)
        # Error-path cert-pin hook. ``certificateError`` is a page-level
        # signal (never on the profile), so it must be connected here,
        # on every page, including the off-the-record "private" profile
        # (iso2 `13` E2). This only hard-rejects a pinned host whose
        # chain Chromium already refused; see cert_policy's docstring.
        try:
            from .cert_policy import install_cert_policy_on_page
            install_cert_policy_on_page(page)
        except Exception as exc:  # noqa: BLE001
            log.error("cert policy page wiring failed: %s", exc)
        try:
            page.setUrlRequestInterceptor(self._interceptor)
        except AttributeError:
            # Very old PyQt6 — fall back to profile, accepting the
            # cross-view clobber risk.
            try:
                self._profile.setUrlRequestInterceptor(self._interceptor)
            except AttributeError:
                pass
        self.view.setPage(page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.view)

        self._wire_signals()

        if url:
            self.navigate(url)

    # -- signals --------------------------------------------------------

    def _wire_signals(self):
        self.view.titleChanged.connect(self._on_title)
        self.view.iconChanged.connect(self._on_icon)
        self.view.urlChanged.connect(self._on_url)
        self.view.loadStarted.connect(self._on_load_started)
        self.view.loadProgress.connect(self._on_load_progress)
        self.view.loadFinished.connect(self._on_load_finished)

    def _on_title(self, title: str):
        self.title_changed.emit(self, title)

    def _on_icon(self, icon: QIcon):
        self.icon_changed.emit(self, icon)

    def _on_url(self, url: QUrl):
        self.url_changed.emit(self, url.toString())

    def _on_load_started(self):
        self.load_started.emit(self)

    def _on_load_progress(self, percent: int):
        self.load_progress.emit(self, percent)

    def _on_load_finished(self, ok: bool):
        self._page_load_seq += 1
        self.load_finished.emit(self, ok)

    # -- accessors ------------------------------------------------------

    @property
    def profile_name(self) -> str:
        return self._profile_name

    @property
    def is_off_the_record(self) -> bool:
        """True when this view is backed by an off-the-record (private)
        profile, i.e. nothing it does should be persisted to disk.

        Reads the live ``QWebEngineProfile`` flag rather than trusting
        the ``profile_name`` string, so the answer is correct even if a
        profile is wired up by some path other than ``get_profile`` (and
        so a renamed/aliased private profile can't slip past privacy
        gates).
        """
        try:
            return bool(self._profile.isOffTheRecord())
        except Exception:
            # Fall back to the name convention if the Qt accessor is
            # unavailable; fail closed toward "private" only for the
            # known private name, never the reverse.
            return self._profile_name == "private"

    @property
    def stable_id(self) -> int:
        """Process-unique webview id, safe to expose to agents."""
        return self._stable_id

    def title(self) -> str:
        return self.view.title() or self.url() or "New Tab"

    def url(self) -> str:
        return self.view.url().toString()

    def icon(self) -> QIcon:
        return self.view.icon()

    def is_loading(self) -> bool:
        # Qt doesn't expose a stable accessor in all versions; track
        # loadStarted / loadFinished if needed. Default false-on-no-info.
        try:
            return bool(self.view.page().loading())  # type: ignore[attr-defined]
        except Exception:
            return False

    def page_load_seq(self) -> int:
        return self._page_load_seq

    def can_go_back(self) -> bool:
        return self.view.history().canGoBack()

    def can_go_forward(self) -> bool:
        return self.view.history().canGoForward()

    # -- actions --------------------------------------------------------

    def navigate(self, url: str):
        url = url.strip()
        if not url:
            return
        # data: URLs carry no "://" and routinely contain spaces; without
        # this they were sent, content and all, to the search engine.
        if ("://" not in url and not url.startswith("about:")
                and not url.lower().startswith("data:")):
            if "." in url and " " not in url:
                url = "https://" + url
            else:
                # Treat as search query.
                from qdbrowser.config import Config
                engine = Config().get(
                    "general", "search_engine",
                    default="https://duckduckgo.com/?q={query}")
                from urllib.parse import quote_plus
                url = engine.replace("{query}", quote_plus(url))
        self.view.setUrl(QUrl(url))

    def reload(self):
        self.view.reload()

    def stop(self):
        self.view.stop()

    def go_back(self):
        self.view.back()

    def go_forward(self):
        self.view.forward()

    def set_zoom(self, factor: float):
        factor = max(0.25, min(5.0, factor))
        self._zoom = factor
        self.view.setZoomFactor(factor)

    def zoom(self) -> float:
        return self._zoom

    def set_muted(self, muted: bool):
        self.muted = muted
        try:
            self.view.page().setAudioMuted(muted)
        except Exception:
            pass

    def set_pinned(self, pinned: bool):
        self.pinned = pinned

    def add_interceptor(self, handler):
        self._interceptor.add(handler)

    def remove_interceptor(self, handler):
        self._interceptor.remove(handler)

    # -- focus ----------------------------------------------------------

    def focusInEvent(self, event):  # noqa: N802 (Qt)
        self.focus_gained.emit(self)
        super().focusInEvent(event)

"""Main window: tabs + recursive splits + toolbar + side panel.

Top to bottom:
  - Navigation toolbar (back/forward/reload, URL bar, side-panel toggle)
  - Tab strip (QTabWidget)
  - Active split container (one per tab) holding WebViews
  - Side panel (dockable left)
  - Status bar with hover URL + load progress

Keyboard shortcuts and split semantics are direct adapts of qterminator's
MainWindow.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("qdbrowser.window")

from PyQt6.QtCore import Qt, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QAction, QIcon, QKeySequence  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QSizePolicy,
    QStatusBar,
    QTabWidget,
    QToolBar,
)

from qdbrowser.config import CONFIG_DIR, Config  # noqa: E402
from qdbrowser.plugin import PluginManager  # noqa: E402
from qdbrowser.side_panel import SidePanel  # noqa: E402
from qdbrowser.splitter import SplitContainer  # noqa: E402
from qdbrowser.webview import WebView  # noqa: E402

# Autosave path. Lives under the same ``sessions/`` directory as named
# saves so a tester can find every session-shaped file in one place.
# The ``_`` prefix marks it as managed and keeps it out of the named
# sessions listing.
SESSION_PATH = os.path.join(CONFIG_DIR, "sessions", "_autosave.json")
# Legacy path; if it exists, prefer it once for restore and then
# migrate to the new location on next save.
_LEGACY_SESSION_PATH = os.path.join(CONFIG_DIR, "session.json")


class _UrlBar(QLineEdit):
    """URL bar with simple smart-completion hook (filled later by history plugin)."""

    submitted = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("Search or enter address")
        self.setClearButtonEnabled(True)
        self.returnPressed.connect(self._on_return)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def _on_return(self):
        self.submitted.emit(self.text())


class MainWindow(QMainWindow):
    """qdbrowser main window."""

    webview_added = pyqtSignal(object)        # WebView
    webview_removed = pyqtSignal(object)      # WebView
    active_webview_changed = pyqtSignal(object)
    navigation_event = pyqtSignal(object, str)  # (webview, url)

    def __init__(self, resolved_theme: str = "dark", parent=None):
        super().__init__(parent)
        self.setWindowTitle("qdbrowser")
        self._resolved_theme = resolved_theme
        self._config = Config()
        self._active_webview: WebView | None = None
        self._closed_tabs: list = []  # stack of {tree, name}
        self._last_find_text: str = ""

        self._build_tabs()
        self._build_toolbar()
        self._build_status()
        self._build_side_panel()
        self._install_shortcuts()

        general = self._config.general
        self.resize(int(general.get("window_width", 1280)),
                    int(general.get("window_height", 800)))

        # Plugins. `_pending_agent_contribs` is filled by plugins that
        # contribute RPC verbs to agent_control via
        # ``register_agent_methods_later`` from their own ``activate``.
        # We drain it after every plugin has had its first activate().
        self._pending_agent_contribs: list = []
        # Per-plugin signal connections — disconnected on disable so a
        # deactivated plugin stops receiving page events and stops
        # keeping itself alive through closures.
        self._plugin_connections: dict = {}  # plugin -> [(signal, conn), ...]
        self.plugins = PluginManager()
        self.plugins.discover()
        self._enable_default_plugins()
        self._install_side_panels()
        self._wire_pending_agent_contribs()

        # Security modules — wired after plugins so every existing
        # WebView and every future one gets the interceptor, and cert
        # pinning is installed on every profile.
        self._apply_security_modules()

    # -- construction ---------------------------------------------------

    def _build_tabs(self):
        self._tabs = QTabWidget()
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.setDocumentMode(True)
        self._tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tabs.currentChanged.connect(self._on_current_tab_changed)
        self.setCentralWidget(self._tabs)

    def _build_toolbar(self):
        tb = QToolBar("Navigation", self)
        tb.setMovable(False)
        tb.setFloatable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)

        self._act_back = QAction("◀", self)
        self._act_back.setToolTip("Back")
        self._act_back.triggered.connect(self._go_back)
        tb.addAction(self._act_back)

        self._act_forward = QAction("▶", self)
        self._act_forward.setToolTip("Forward")
        self._act_forward.triggered.connect(self._go_forward)
        tb.addAction(self._act_forward)

        self._act_reload = QAction("⟳", self)
        self._act_reload.setToolTip("Reload")
        self._act_reload.triggered.connect(self._reload)
        tb.addAction(self._act_reload)

        self._act_home = QAction("⌂", self)
        self._act_home.setToolTip("Home")
        self._act_home.triggered.connect(self._home)
        tb.addAction(self._act_home)

        tb.addSeparator()

        self._url_bar = _UrlBar(self)
        self._url_bar.submitted.connect(self._navigate_active)
        tb.addWidget(self._url_bar)

        tb.addSeparator()

        self._act_palette = QAction("⌘", self)
        self._act_palette.setToolTip("Command palette (Ctrl+E)")
        self._act_palette.triggered.connect(self._open_command_palette)
        tb.addAction(self._act_palette)

        self._act_panel = QAction("☰", self)
        self._act_panel.setToolTip("Toggle side panel (F4)")
        self._act_panel.triggered.connect(self._toggle_side_panel)
        tb.addAction(self._act_panel)

        self._toolbar = tb

    def _build_status(self):
        sb = QStatusBar(self)
        self._hover_label = QLabel("")
        self._hover_label.setStyleSheet("padding: 0 6px;")
        sb.addWidget(self._hover_label, 1)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setFixedWidth(120)
        self._progress.setVisible(False)
        sb.addPermanentWidget(self._progress)
        self.setStatusBar(sb)

    def _build_side_panel(self):
        self._side_panel = SidePanel(self)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._side_panel)
        if not self._config.get("general", "show_side_panel", default=True):
            self._side_panel.hide()

    # -- plugin wiring --------------------------------------------------

    def _enable_default_plugins(self):
        """Enable plugins. Order matters in one specific way:
        ``agent_control`` activates *first* so that other plugins
        (``picture_in_picture``, ``translate``) can hand it RPC
        contributions; agent_control is opt-in via env var / config and
        is a no-op if disabled.
        """
        # Agent control first so the contribution drain at the end of
        # __init__ has somewhere to deliver verbs.
        try:
            self.plugins.enable("agent_control", app_controller=self)
        except Exception as exc:
            log.exception("agent_control disabled: %s", exc)

        always_on = [
            "history", "bookmarks", "downloads", "notes",
            "command_palette", "content_blocker", "page_actions",
            "sessions", "workspaces", "screenshot", "tab_stacks",
            "reader_mode", "mouse_gestures", "web_panels",
            "dark_mode", "picture_in_picture", "tab_list", "translate",
            "quarantine_panel",
            # Preview an untrusted URL in a throwaway tier-2 disposable
            # (command palette). Thin consumer of the shipped
            # open_in_disposable SDK; fail-closed (no command unless the SDK +
            # preview class are available and the URL is an eligible http(s)
            # link).
            "open_in_disposable",
            # track-04 Phase-1: tag clipboard writes with origin URL
            # + tab id so the qdshell ClipboardGate sees them as
            # extra MIME types on selection_set.
            "clipboard",
        ]
        for name in always_on:
            try:
                self.plugins.enable(name, app_controller=self)
            except Exception as exc:
                log.exception("plugin %s failed: %s", name, exc)

        if self._should_enable_bridge_adapter():
            try:
                self.plugins.enable("bridge_adapter", app_controller=self)
            except Exception as exc:
                log.exception("plugin bridge_adapter failed: %s", exc)

    def _should_enable_bridge_adapter(self) -> bool:
        try:
            from qdbrowser.plugins.bridge_adapter import (
                _daemons_available,
                _enabled_config_override,
            )
            explicit = _enabled_config_override()
            if explicit is not None:
                return explicit
            return _daemons_available()
        except Exception as exc:
            log.debug("bridge_adapter daemon probe failed: %s", exc)
            return False

    def register_agent_methods_later(self, plugin) -> None:
        """Plugins call this from ``activate`` to defer RPC registration
        until after every plugin has had its turn. ``plugin`` must
        define ``contribute_agent_methods(agent_control)``.
        """
        self._pending_agent_contribs.append(plugin)

    def disconnect_plugin(self, plugin) -> None:
        """Disconnect every signal we wired for ``plugin``. Called from
        ``PluginManager.disable`` so a deactivated plugin stops
        receiving page events and isn't pinned by lambda closures.
        """
        conns = self._plugin_connections.pop(plugin, None)
        if not conns:
            return
        for signal, conn in conns:
            try:
                signal.disconnect(conn)
            except (RuntimeError, TypeError):
                pass

    def _wire_pending_agent_contribs(self) -> None:
        ac = getattr(self, "agent_control", None)
        if ac is None:
            self._pending_agent_contribs.clear()
            return
        while self._pending_agent_contribs:
            plug = self._pending_agent_contribs.pop(0)
            try:
                plug.contribute_agent_methods(ac)
            except Exception as exc:
                log.exception("plugin %s contribute_agent_methods failed: %s",
                              type(plug).__name__, exc)

    def _apply_security_modules(self):
        """Wire the security interceptor and the cert-pin store.

        Called once at the end of ``__init__``. Both modules are
        fault-tolerant: missing pin files mean an empty store and the
        browser still starts.

        Scope of the cert-pin control (iso2 `13` E2): it is an
        error-path hook, connected per page in ``WebView.__init__``,
        that hard-rejects a pinned host whose chain Chromium ALREADY
        refused. It does not inspect CA-valid certificates and is not
        HPKP. See ``cert_policy`` for the full limitation note.
        """
        # 1. Security interceptor (HTTPS-only, DNT, UA validation).
        try:
            from qdbrowser.security_interceptor import apply_security_policy
            self._security_interceptor = apply_security_policy(self)
        except Exception as exc:
            log.exception("security interceptor failed: %s", exc)

        # 1b. §6: pin the single source-of-truth UA on every profile that
        #     already exists (including Qt's defaultProfile, created
        #     outside get_profile). New profiles are pinned on creation in
        #     get_profile. This eliminates per-profile UA drift between
        #     silos.
        try:
            from qdbrowser import webview as wv_mod
            wv_mod.pin_all_profiles()
        except Exception as exc:
            log.warning("user-agent pinning failed: %s", exc)

        # 2. Cert-pin store. Load the admin pin files and register the
        #    store as active. ``install_cert_policy`` is profile-level
        #    bookkeeping only: ``certificateError`` is a page signal,
        #    so the actual hook is connected in ``WebView.__init__``
        #    via ``install_cert_policy_on_page``. The profile-created
        #    subscription is kept so any future profile-level signal
        #    would get wired too.
        try:
            from qdbrowser import webview as wv_mod
            from qdbrowser.cert_policy import install_cert_policy, load_pin_store
            sec = self._config.get("security", default={}) or {}
            pin_store = load_pin_store(
                system_path=sec.get("cert_pins_path"),
                user_path=sec.get("cert_pins_user_path"),
                overrides_path=sec.get("cert_overrides_path"),
            )
            self._pin_store = pin_store

            def _wire_cert_policy(profile, _store=pin_store):
                try:
                    install_cert_policy(profile, _store)
                except Exception as exc:
                    log.warning("cert policy install failed on profile: %s", exc)

            wv_mod.on_profile_created(_wire_cert_policy)
            self._cert_policy_listener = _wire_cert_policy
        except Exception as exc:
            log.exception("cert pinning setup failed: %s", exc)

    def _install_side_panels(self):
        for provider in self.plugins.get_side_panel_providers():
            try:
                widget = provider.build_panel(self)
            except Exception as exc:
                log.warning("panel %s failed: %s",
                            provider.panel_id, exc)
                continue
            self._side_panel.add_panel(
                provider.panel_id,
                provider.panel_label,
                provider.panel_icon,
                widget,
            )

    def _connect_command_palette(self):
        # The command_palette plugin registers an open() method on the
        # window so other components can trigger it.
        pass  # bound below

    # -- shortcuts ------------------------------------------------------

    def _install_shortcuts(self):
        kb = self._config.keybindings
        defs = [
            ("new_tab", lambda: self.new_tab()),
            ("new_window", self._new_window),
            ("close_tab", self._close_current_tab),
            ("reopen_tab", self._reopen_last_tab),
            ("next_tab", lambda: self._cycle_tab(1)),
            ("prev_tab", lambda: self._cycle_tab(-1)),
            ("address_bar", lambda: (self._url_bar.setFocus(),
                                     self._url_bar.selectAll())),
            ("back", self._go_back),
            ("forward", self._go_forward),
            ("reload", self._reload),
            ("hard_reload", self._hard_reload),
            ("stop", self._stop),
            ("home", self._home),
            ("find", self._find_in_page),
            ("split_horizontal",
             lambda: self._split(Qt.Orientation.Vertical)),  # H-split == vertical splitter line
            ("split_vertical",
             lambda: self._split(Qt.Orientation.Horizontal)),
            ("close_split", self._close_active_split),
            ("navigate_left", lambda: self._navigate_split("left")),
            ("navigate_right", lambda: self._navigate_split("right")),
            ("navigate_up", lambda: self._navigate_split("up")),
            ("navigate_down", lambda: self._navigate_split("down")),
            ("command_palette", self._open_command_palette),
            ("toggle_devtools", self._toggle_devtools),
            ("view_source", self._view_source),
            ("fullscreen", self._toggle_fullscreen),
            ("zoom_in", lambda: self._zoom_step(0.1)),
            ("zoom_out", lambda: self._zoom_step(-0.1)),
            ("zoom_reset", lambda: self._zoom_set(1.0)),
            ("reader_mode", self._toggle_reader),
            ("toggle_side_panel", self._toggle_side_panel),
            ("save_session", self.save_session),
            ("quit", self.close),
            # Tab affordances.
            ("pin_tab", self._toggle_pin_active),
            ("mute_tab", self._toggle_mute_active),
            ("find_next", lambda: self._find_repeat(False)),
            ("find_prev", lambda: self._find_repeat(True)),
            # Side-panel direct switches.
            ("panel_bookmarks",
             lambda: self._side_panel.show_panel("bookmarks")),
            ("panel_history",
             lambda: self._side_panel.show_panel("history")),
            ("panel_downloads",
             lambda: self._side_panel.show_panel("downloads")),
            ("panel_notes",
             lambda: self._side_panel.show_panel("notes")),
            # Screenshot of the current tab.
            ("take_screenshot", self._take_screenshot_visible),
        ]
        for action_name, slot in defs:
            seq = kb.get(action_name)
            if not seq:
                continue
            act = QAction(self)
            act.setShortcut(QKeySequence(seq))
            act.triggered.connect(slot)
            self.addAction(act)

        # Tab switching Alt+1..9
        for i in range(1, 10):
            seq = kb.get(f"switch_to_tab_{i}")
            if not seq:
                continue
            act = QAction(self)
            act.setShortcut(QKeySequence(seq))
            act.triggered.connect(lambda _=False, idx=i - 1: self._switch_to_tab(idx))
            self.addAction(act)

    # -- tab / split lifecycle ------------------------------------------

    def new_tab(self, url=None, profile_name="default", background=False):
        split = SplitContainer(Qt.Orientation.Horizontal)
        wv = WebView(url=url or self._config.get("general", "homepage",
                                                 default="about:blank"),
                     profile_name=profile_name)
        self._connect_webview(wv)
        split.add_webview(wv)
        idx = self._tabs.addTab(split, wv.title() or "New Tab")
        if not background:
            self._tabs.setCurrentIndex(idx)
            wv.setFocus()
            self._set_active_webview(wv)
        self.webview_added.emit(wv)
        return wv

    def _connect_webview(self, wv: WebView):
        wv.title_changed.connect(self._on_wv_title)
        wv.icon_changed.connect(self._on_wv_icon)
        wv.url_changed.connect(self._on_wv_url)
        wv.load_started.connect(self._on_wv_load_started)
        wv.load_progress.connect(self._on_wv_load_progress)
        wv.load_finished.connect(self._on_wv_load_finished)
        wv.focus_gained.connect(self._set_active_webview)
        # Plugin observers — track each connection by plugin so we can
        # disconnect them when the plugin is disabled. Otherwise the
        # lambdas keep deactivated plugins alive and they keep
        # receiving events.
        #
        # Private (off-the-record) webviews must leave no persistent
        # trace, so observers that record/persist page activity (history
        # log, etc.) are not wired to them. Ephemeral observers
        # (dark-mode, clipboard tagging, content blocking) are
        # ``persistent = False`` and keep running in private mode.
        is_otr = bool(getattr(wv, "is_off_the_record", False))
        for obs in self.plugins.get_page_observers():
            if is_otr and getattr(obs, "persistent", False):
                log.debug("skipping persistent observer %s for OTR webview",
                          type(obs).__name__)
                continue
            try:
                s1 = wv.url_changed.connect(
                    lambda _wv, u, _obs=obs: _obs.on_navigation(_wv, u))
                s2 = wv.load_finished.connect(
                    lambda _wv, ok, _obs=obs: _obs.on_load_finished(_wv, ok))
                s3 = wv.title_changed.connect(
                    lambda _wv, t, _obs=obs: _obs.on_title_changed(_wv, t))
            except Exception as exc:
                log.warning("page observer wiring for %s failed: %s",
                            type(obs).__name__, exc)
                continue
            conns = self._plugin_connections.setdefault(obs, [])
            conns.append((wv.url_changed, s1))
            conns.append((wv.load_finished, s2))
            conns.append((wv.title_changed, s3))
        # URL interceptors
        for interc in self.plugins.get_url_interceptors():
            wv.add_interceptor(interc)
        # Security interceptor — also installed via webview_added, but
        # we attach eagerly here so the interceptor is in place before
        # Qt processes the queued navigation from WebView.__init__.
        si = getattr(self, "_security_interceptor", None)
        if si is not None:
            wv.add_interceptor(si)

    def _find_tab_for_webview(self, wv):
        for i in range(self._tabs.count()):
            split = self._tabs.widget(i)
            if isinstance(split, SplitContainer) and wv in split.find_webviews():
                return i, split
        return -1, None

    def _on_tab_close_requested(self, index):
        split = self._tabs.widget(index)
        if isinstance(split, SplitContainer):
            views = split.find_webviews()
            self._closed_tabs.append({
                "name": self._tabs.tabText(index),
                "urls": [wv.url() for wv in views],
            })
            for wv in views:
                # Clear active-pointer to a webview that's about to be
                # destroyed — otherwise any listener that calls
                # ``window._active_webview.url()`` between this point
                # and the next ``currentChanged`` will hit a dangling
                # C++ object.
                if self._active_webview is wv:
                    self._active_webview = None
                self.webview_removed.emit(wv)
        self._tabs.removeTab(index)
        if self._tabs.count() == 0:
            self.new_tab()

    def _close_current_tab(self):
        idx = self._tabs.currentIndex()
        if idx >= 0:
            self._on_tab_close_requested(idx)

    def _reopen_last_tab(self):
        if not self._closed_tabs:
            return
        entry = self._closed_tabs.pop()
        urls = entry.get("urls") or ["about:blank"]
        first = self.new_tab(url=urls[0])
        # Restore additional splits as horizontal splits.
        idx, split = self._find_tab_for_webview(first)
        for u in urls[1:]:
            split.split(first, Qt.Orientation.Horizontal, url=u)
        if entry.get("name"):
            self._tabs.setTabText(self._tabs.currentIndex(), entry["name"])

    def _cycle_tab(self, delta):
        count = self._tabs.count()
        if count == 0:
            return
        idx = (self._tabs.currentIndex() + delta) % count
        self._tabs.setCurrentIndex(idx)

    def _switch_to_tab(self, idx):
        if 0 <= idx < self._tabs.count():
            self._tabs.setCurrentIndex(idx)

    def _split(self, orientation):
        wv = self._active_webview
        if wv is None:
            return
        idx, split = self._find_tab_for_webview(wv)
        if split is None:
            return
        new_wv = split.split(wv, orientation,
                             url=self._config.get("general", "homepage",
                                                  default="about:blank"))
        if new_wv is not None:
            self._connect_webview(new_wv)
            self._set_active_webview(new_wv)
            new_wv.setFocus()
            self.webview_added.emit(new_wv)

    def _close_active_split(self):
        wv = self._active_webview
        if wv is None:
            return
        idx, split = self._find_tab_for_webview(wv)
        if split is None:
            return
        # Clear the active pointer first so any listener firing on
        # webview_removed.emit doesn't reach into a now-deleted view.
        self._active_webview = None
        empty = split.remove_webview(wv)
        self.webview_removed.emit(wv)
        if empty:
            self._tabs.removeTab(idx)
            if self._tabs.count() == 0:
                self.new_tab()
            return
        remaining = split.find_webviews()
        if remaining:
            self._set_active_webview(remaining[0])
            remaining[0].setFocus()

    def _navigate_split(self, direction):
        wv = self._active_webview
        if wv is None:
            return
        _, split = self._find_tab_for_webview(wv)
        if split is None:
            return
        nxt = split.find_next_webview(wv, direction)
        if nxt is not None:
            self._set_active_webview(nxt)
            nxt.setFocus()

    # -- active webview tracking ----------------------------------------

    def _on_current_tab_changed(self, index):
        if index < 0:
            return
        split = self._tabs.widget(index)
        if not isinstance(split, SplitContainer):
            return
        views = split.find_webviews()
        if views:
            self._set_active_webview(views[0])

    def _set_active_webview(self, wv):
        if not isinstance(wv, WebView):
            return
        if self._active_webview is wv:
            return
        self._active_webview = wv
        self._url_bar.setText(wv.url())
        self._update_nav_actions()
        self.setWindowTitle(f"{wv.title() or 'qdbrowser'} — qdbrowser")
        self.active_webview_changed.emit(wv)

    def _update_nav_actions(self):
        wv = self._active_webview
        if wv is None:
            self._act_back.setEnabled(False)
            self._act_forward.setEnabled(False)
            return
        self._act_back.setEnabled(wv.can_go_back())
        self._act_forward.setEnabled(wv.can_go_forward())

    # -- webview signal handlers ----------------------------------------

    def _on_wv_title(self, wv, title):
        idx, _ = self._find_tab_for_webview(wv)
        if idx >= 0:
            self._tabs.setTabText(idx, title or "New Tab")
        if wv is self._active_webview:
            self.setWindowTitle(f"{title or 'qdbrowser'} — qdbrowser")

    def _on_wv_icon(self, wv, icon):
        idx, _ = self._find_tab_for_webview(wv)
        if idx >= 0 and isinstance(icon, QIcon) and not icon.isNull():
            self._tabs.setTabIcon(idx, icon)

    def _on_wv_url(self, wv, url):
        if wv is self._active_webview:
            self._url_bar.setText(url)
            self._update_nav_actions()
        self.navigation_event.emit(wv, url)

    def _on_wv_load_started(self, wv):
        if wv is self._active_webview:
            self._progress.setValue(0)
            self._progress.setVisible(True)

    def _on_wv_load_progress(self, wv, percent):
        if wv is self._active_webview:
            self._progress.setValue(percent)

    def _on_wv_load_finished(self, wv, ok):
        if wv is self._active_webview:
            self._progress.setVisible(False)
            self._update_nav_actions()

    # -- toolbar slots --------------------------------------------------

    def _navigate_active(self, url):
        if self._active_webview is None:
            self.new_tab(url=url)
        else:
            self._active_webview.navigate(url)

    def _go_back(self):
        if self._active_webview:
            self._active_webview.go_back()

    def _go_forward(self):
        if self._active_webview:
            self._active_webview.go_forward()

    def _reload(self):
        if self._active_webview:
            self._active_webview.reload()

    def _hard_reload(self):
        if self._active_webview:
            self._active_webview.view.triggerPageAction(
                self._active_webview.view.page().WebAction.ReloadAndBypassCache)

    def _stop(self):
        if self._active_webview:
            self._active_webview.stop()

    def _home(self):
        if self._active_webview:
            self._active_webview.navigate(
                self._config.get("general", "homepage", default="about:blank"))

    def _find_in_page(self):
        # Lightweight inline find prompt via QInputDialog. A full find
        # bar (forward/back/highlight all) could be a future plugin.
        text, ok = _quick_input(self, "Find in page:")
        if ok and self._active_webview:
            self._last_find_text = text
            self._active_webview.view.findText(text)

    def _toggle_devtools(self):
        wv = self._active_webview
        if wv is None:
            return
        # QtWebEngine exposes a dev tools view by attaching another page.
        try:
            page = wv.view.page()
            if not hasattr(self, "_devtools_view"):
                from PyQt6.QtWebEngineWidgets import QWebEngineView as _DTV
                self._devtools_view = _DTV()
                self._devtools_view.setWindowTitle("DevTools")
                self._devtools_view.resize(900, 600)
            page.setDevToolsPage(self._devtools_view.page())
            self._devtools_view.show()
            self._devtools_view.raise_()
        except Exception as exc:
            log.warning("DevTools open failed: %s", exc)

    def _view_source(self):
        if self._active_webview:
            url = self._active_webview.url()
            self.new_tab(url=f"view-source:{url}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _zoom_step(self, delta):
        if self._active_webview:
            self._active_webview.set_zoom(
                self._active_webview.zoom() + delta)

    def _zoom_set(self, factor):
        if self._active_webview:
            self._active_webview.set_zoom(factor)

    def _toggle_reader(self):
        # Defer to reader_mode plugin if it registered a handler.
        plug = self.plugins._instances.get("reader_mode")
        if plug is not None and hasattr(plug, "toggle"):
            plug.toggle(self._active_webview)

    def _toggle_side_panel(self):
        self._side_panel.setVisible(not self._side_panel.isVisible())

    def _toggle_pin_active(self):
        wv = self._active_webview
        if wv is not None:
            wv.set_pinned(not wv.pinned)

    def _toggle_mute_active(self):
        wv = self._active_webview
        if wv is not None:
            wv.set_muted(not wv.muted)

    def _find_repeat(self, backward: bool):
        wv = self._active_webview
        if wv is None or not self._last_find_text:
            return
        from PyQt6.QtWebEngineCore import QWebEnginePage
        flags = QWebEnginePage.FindFlag.FindBackward if backward else \
            QWebEnginePage.FindFlag(0)
        wv.view.findText(self._last_find_text, flags)

    def _take_screenshot_visible(self):
        plug = self.plugins._instances.get("screenshot")
        if plug is not None and hasattr(plug, "capture_viewport"):
            plug.capture_viewport(self._active_webview)

    def _open_command_palette(self):
        plug = self.plugins._instances.get("command_palette")
        if plug is not None and hasattr(plug, "open"):
            plug.open()

    def _new_window(self):
        # A second top-level window in the same process; sessions are
        # tracked per-window.
        win = MainWindow(resolved_theme=self._resolved_theme)
        win.new_tab()
        win.show()

    # -- session save / restore -----------------------------------------

    def save_session(self):
        from qdbrowser.layout import serialize_layout
        os.makedirs(os.path.dirname(SESSION_PATH), exist_ok=True)
        data = serialize_layout(self._tabs)
        with open(SESSION_PATH, "w") as f:
            json.dump(data, f, indent=2)

    def restore_session(self):
        path = (SESSION_PATH if os.path.exists(SESSION_PATH)
                else (_LEGACY_SESSION_PATH
                      if os.path.exists(_LEGACY_SESSION_PATH) else None))
        if path is None:
            return False
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            return False
        from qdbrowser.layout import restore_layout
        while self._tabs.count() > 0:
            self._tabs.removeTab(0)
        restore_layout(self, data)
        return self._tabs.count() > 0

    def closeEvent(self, event):  # noqa: N802 (Qt)
        if self._config.get("general", "save_session", default=True):
            try:
                self.save_session()
            except Exception as exc:
                log.warning("save_session failed: %s", exc)
        super().closeEvent(event)


def _quick_input(parent, label):
    from PyQt6.QtWidgets import QInputDialog
    text, ok = QInputDialog.getText(parent, "qdbrowser", label)
    return text, ok

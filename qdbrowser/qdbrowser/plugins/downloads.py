"""Downloads plugin — intake from every web profile, persistent log,
progress bar per item, pause/resume/cancel, open-on-finish.

Connects to ``downloadRequested`` on every ``QWebEngineProfile`` we
have created via ``qdbrowser.webview.get_profile`` — Qt only fires the
signal on the profile that owns the requesting page, so wiring just
``defaultProfile()`` (the old behaviour) silently dropped downloads
from private mode and any named profile.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time

log = logging.getLogger("qdbrowser.downloads")


def _xdg_open(path: str) -> None:
    """Safely open ``path`` via xdg-open. No shell, no quoting traps —
    the path is one argv element. Detached: parent does not wait.

    Server-suggested download filenames can contain shell metacharacters
    or `$(...)` substitutions; passing them through a shell is RCE.
    """
    if not path:
        return
    try:
        subprocess.Popen(
            ["xdg-open", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        log.warning("xdg-open failed for %r: %s", path, exc)

from PyQt6.QtCore import Qt, pyqtSignal  # noqa: E402
from PyQt6.QtWebEngineCore import QWebEngineDownloadRequest, QWebEngineProfile  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qdbrowser import webview as wv_mod  # noqa: E402
from qdbrowser.config import CONFIG_DIR, Config  # noqa: E402
from qdbrowser.plugin import CommandProvider, SidePanelProvider  # noqa: E402
from qdbrowser.quarantine import QuarantineStore, _sanitize_name  # noqa: E402

HISTORY_PATH = os.path.join(CONFIG_DIR, "downloads.json")


def _load_history() -> list:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_history(items: list):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w") as f:
        json.dump(items, f, indent=2)


class _DownloadItem(QWidget):
    """One row in the downloads panel: filename, progress, controls."""

    cancelled = pyqtSignal(object)  # self

    def __init__(self, request: QWebEngineDownloadRequest,
                 quarantined: bool = False, parent=None):
        super().__init__(parent)
        self._request = request
        self._path = os.path.join(request.downloadDirectory(),
                                   request.downloadFileName())
        self._finished = False
        self._quarantined = quarantined

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        self._name = QLabel(self._render_label())
        self._name.setStyleSheet("font-weight: 600;")
        layout.addWidget(self._name)

        row = QHBoxLayout()
        row.setSpacing(4)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setMaximumHeight(8)
        self._bar.setTextVisible(False)
        row.addWidget(self._bar, 1)

        self._pause_btn = QPushButton("⏸")
        self._pause_btn.setMaximumWidth(28)
        self._pause_btn.setToolTip("Pause / resume")
        self._pause_btn.clicked.connect(self._toggle_pause)
        row.addWidget(self._pause_btn)

        self._cancel_btn = QPushButton("✕")
        self._cancel_btn.setMaximumWidth(28)
        self._cancel_btn.setToolTip("Cancel")
        self._cancel_btn.clicked.connect(self._cancel)
        row.addWidget(self._cancel_btn)

        layout.addLayout(row)

        # Signal wiring — every QWebEngineDownloadRequest signal is
        # zero-arg; we read state synchronously when it fires.
        request.receivedBytesChanged.connect(self._on_progress)
        request.totalBytesChanged.connect(self._on_progress)
        request.stateChanged.connect(self._on_state_changed)
        request.isFinishedChanged.connect(self._on_finished)

    def path(self) -> str:
        return self._path

    def is_finished(self) -> bool:
        return self._finished

    def _render_label(self) -> str:
        return f"⬇  {os.path.basename(self._path)}"

    def _on_progress(self):
        total = self._request.totalBytes()
        received = self._request.receivedBytes()
        if total > 0:
            pct = int(received * 100 / total)
            self._bar.setValue(pct)
            self._name.setText(
                f"⬇  {os.path.basename(self._path)}  "
                f"({_human(received)} / {_human(total)})")
        else:
            # Unknown total: indeterminate-looking.
            self._bar.setRange(0, 0)
            self._name.setText(
                f"⬇  {os.path.basename(self._path)}  ({_human(received)})")

    def _on_state_changed(self):
        s = self._request.state()
        DR = QWebEngineDownloadRequest
        if s == DR.DownloadState.DownloadCancelled:
            self._name.setText(f"✕  {os.path.basename(self._path)}  (cancelled)")
            self._pause_btn.setEnabled(False)
            self._cancel_btn.setEnabled(False)
        elif s == DR.DownloadState.DownloadInterrupted:
            self._name.setText(f"⚠  {os.path.basename(self._path)}  (interrupted)")
        elif s == DR.DownloadState.DownloadCompleted:
            self._on_finished()

    def _on_finished(self):
        if self._finished:
            return
        # Qt fires isFinishedChanged on every state transition that
        # toggles isFinished; coalesce.
        if not self._request.isFinished():
            return
        self._finished = True
        self._bar.setRange(0, 100)
        self._bar.setValue(100)
        self._pause_btn.setEnabled(False)
        if self._quarantined:
            # File is in quarantine — don't expose a direct-open button
            # that would bypass the polkit-gated release flow.
            self._cancel_btn.setEnabled(False)
            self._cancel_btn.setToolTip("Quarantined — release to open")
            self._name.setText(
                f"🔒  {os.path.basename(self._path)}  (quarantined)")
        else:
            self._cancel_btn.setText("📂")
            try:
                self._cancel_btn.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._cancel_btn.clicked.connect(self._open_path)
            self._cancel_btn.setToolTip("Open file")
            self._name.setText(f"✓  {os.path.basename(self._path)}")

    def _toggle_pause(self):
        if self._request.isPaused():
            self._request.resume()
            self._pause_btn.setText("⏸")
        else:
            self._request.pause()
            self._pause_btn.setText("▶")

    def _cancel(self):
        self._request.cancel()
        self.cancelled.emit(self)

    def _open_path(self):
        if os.path.exists(self._path):
            _xdg_open(self._path)


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


class DownloadsPanel(QWidget):
    def __init__(self, window, history: list | None = None):
        super().__init__()
        self._window = window
        self._items: list = []
        self._history: list = history or []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._list = QListWidget()
        self._list.setSpacing(2)
        layout.addWidget(self._list, 1)

        row = QHBoxLayout()
        clear_btn = QPushButton("Clear finished")
        clear_btn.clicked.connect(self._clear_finished)
        row.addWidget(clear_btn)
        open_dir_btn = QPushButton("Open dir")
        open_dir_btn.clicked.connect(self._open_dir)
        row.addWidget(open_dir_btn)
        layout.addLayout(row)

        # Replay completed history as static rows so the panel isn't
        # empty after a restart.
        for entry in self._history[-50:]:
            item = QListWidgetItem(
                f"✓  {os.path.basename(entry.get('path',''))}  "
                f"({entry.get('size_str','')})")
            item.setData(Qt.ItemDataRole.UserRole,
                          {"path": entry.get("path"), "historical": True})
            self._list.addItem(item)

        # Open-on-double-click for historical rows.
        self._list.itemActivated.connect(self._on_activated)

    def add_active(self, request: QWebEngineDownloadRequest,
                   quarantined: bool = False, private: bool = False):
        widget = _DownloadItem(request, quarantined=quarantined)
        # 02/S9: tag the widget as private so the bridge DownloadsProxy.list
        # can keep it off the agent-visible surface (a private download's
        # filename/state/timing is the same class of leak as its origin URL).
        widget._private = bool(private)
        item = QListWidgetItem()
        item.setSizeHint(widget.sizeHint())
        item.setData(Qt.ItemDataRole.UserRole,
                      {"path": widget.path(), "historical": False})
        self._list.insertItem(0, item)
        self._list.setItemWidget(item, widget)
        self._items.append((item, widget))

        # When the request finishes, persist to history — but never for a
        # private (off-the-record) download: it lives only in this
        # session's panel and is gone when the window closes.
        if not private:
            request.isFinishedChanged.connect(
                lambda r=request, w=widget: self._on_finished_persist(r, w))

    def _on_finished_persist(self, request, widget):
        if not request.isFinished():
            return
        if request.state() != QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            return
        # Defence in depth: never write a private download to disk
        # history even if this slot is somehow connected for an OTR
        # request. Fail closed — if we can't tell the profile, skip the
        # write rather than risk leaking a private origin.
        try:
            if request.page().profile().isOffTheRecord():
                return
        except Exception:
            return
        entry = {
            "path": widget.path(),
            "size": request.totalBytes(),
            "size_str": _human(request.totalBytes()),
            "url": request.url().toString(),
            "ts": time.time(),
        }
        self._history.append(entry)
        _save_history(self._history)

    def _clear_finished(self):
        # Remove rows whose widget says finished OR rows whose userrole
        # says historical.
        to_remove = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            widget = self._list.itemWidget(item)
            if widget is None:
                # historical row
                if data.get("historical"):
                    to_remove.append(i)
            elif isinstance(widget, _DownloadItem) and widget.is_finished():
                to_remove.append(i)
        for i in reversed(to_remove):
            self._list.takeItem(i)

    def _open_dir(self):
        cfg = Config()
        target = cfg.get(
            "downloads", "release_dir",
            default=cfg.get("general", "downloads_dir",
                            default=os.path.expanduser("~/Downloads")))
        _xdg_open(target)

    def _on_activated(self, item):
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        p = data.get("path")
        if p and os.path.exists(p):
            _xdg_open(p)


class DownloadsPlugin(SidePanelProvider, CommandProvider):
    name = "downloads"
    description = "Track downloads from every profile."
    capabilities = ["side_panel", "command_provider"]
    panel_id = "downloads"
    panel_label = "Downloads"
    panel_icon = "↓"

    def __init__(self):
        super().__init__()
        self._panel: DownloadsPanel | None = None
        self._window = None
        self._wired_profiles: set = set()
        self._history = _load_history()
        self._quarantine: QuarantineStore | None = None
        # True when quarantine is enabled but its store could not be
        # opened: downloads are then refused rather than falling through
        # to a direct ~/Downloads write (iso2 `13` E3).
        self._quarantine_required = False

    def activate(self, window):
        self._window = window
        # Initialise quarantine store when the feature is enabled.
        # Downloads land here first; polkit-gated release moves them
        # to ~/Downloads.
        cfg = Config()
        quarantine_enabled = cfg.get(
            "downloads", "quarantine_enabled", default=True)
        if quarantine_enabled:
            try:
                q_dir = cfg.get("downloads", "quarantine_dir",
                                default=os.path.expanduser(
                                    "~/.local/share/qdbrowser/quarantine"))
                self._quarantine = QuarantineStore(q_dir)
            except Exception as exc:
                # Fail closed: quarantine is the claimed containment, so
                # when it is enabled but cannot be set up, downloads are
                # refused rather than silently landing in ~/Downloads
                # (iso2 `13` E3).
                log.error("quarantine store init failed; downloads are "
                          "REFUSED until it is fixed: %s", exc)
                self._quarantine = None
                self._quarantine_required = True
        else:
            self._quarantine = None
            self._quarantine_required = False
        # Subscribe to "profile created" so every present and future
        # QWebEngineProfile gets its ``downloadRequested`` signal
        # wired without rebinding ``wv_mod.get_profile``. The webview
        # module replays its current cache to us synchronously.
        wv_mod.on_profile_created(self._wire)
        # Qt creates a defaultProfile() of its own before any
        # ``get_profile`` call; include it explicitly.
        self._wire(QWebEngineProfile.defaultProfile())

    def deactivate(self):
        wv_mod.off_profile_created(self._wire)

    def _wire(self, profile: QWebEngineProfile):
        if id(profile) in self._wired_profiles:
            return
        try:
            profile.downloadRequested.connect(self._on_download_requested)
        except Exception as exc:
            log.warning("could not wire profile: %s", exc)
            return
        self._wired_profiles.add(id(profile))

    def build_panel(self, window):
        self._panel = DownloadsPanel(window, history=self._history)
        return self._panel

    def _on_download_requested(self, request: QWebEngineDownloadRequest):
        qs = self._quarantine

        # Private (off-the-record) downloads must leave no durable record
        # of where they came from: no source URL / profile in the
        # quarantine DB or sidecar, no downloads.json history entry, no
        # URL-bearing bridge events. The file is still quarantined and
        # scanned (operational security), just without the origin trail.
        otr = self._request_is_off_the_record(request)

        if self._quarantine_required and qs is None:
            log.error("download refused: quarantine is enabled but its "
                      "store failed to initialise")
            self._cancel_request(request)
            self._notify_blocked(
                "Quarantine is unavailable, so this download was blocked.\n"
                "Downloads stay disabled until the quarantine store can be "
                "opened again.")
            return

        row_id = None  # set if quarantine intake succeeds

        # Check auto_release_domains: if the download URL's host is in
        # the allowlist, skip quarantine entirely for this request.
        if qs is not None:
            try:
                cfg = Config()
                auto_domains = cfg.get(
                    "downloads", "auto_release_domains", default=[]) or []
                if auto_domains:
                    host = request.url().host() if hasattr(request, "url") else ""
                    if host and host in auto_domains:
                        # Never log a private download's host to the
                        # journal; redact it for OTR requests.
                        log.info("auto-release domain %r, skipping quarantine",
                                 "<private>" if otr else host)
                        qs = None
            except Exception:
                pass

        if qs is not None:
            # Quarantine path: redirect into the quarantine directory.
            row_id = None
            q_path = None
            suggested = None
            try:
                suggested = request.downloadFileName()
                # Use sanitized basename for the DB record so release()
                # cannot escape the release directory via a path-like
                # server-suggested filename.
                safe_name = _sanitize_name(suggested)
                q_path = qs.plan_path(suggested)
                request.setDownloadDirectory(os.path.dirname(q_path))
                request.setDownloadFileName(os.path.basename(q_path))

                # For private downloads, never persist the origin URL or
                # profile — store empty strings so the file is still
                # tracked/scanned but leaves no source trail.
                source_url = ""
                if not otr:
                    source_url = (request.url().toString()
                                  if hasattr(request, "url") else "")
                content_type = ""
                try:
                    content_type = request.mimeType() or ""
                except Exception:
                    pass
                profile_name = ""
                if not otr:
                    try:
                        profile_name = (
                            request.page().profile().storageName() or "")
                    except Exception:
                        pass

                row_id = qs.record(
                    quarantine_path=q_path,
                    filename=safe_name,
                    source_url=source_url,
                    content_type=content_type,
                    profile_name=profile_name,
                    scan_result="pending",
                )
                # Write the sidecar metadata file.
                try:
                    qs.write_sidecar(row_id, q_path, {
                        "source_url": source_url,
                        "filename": safe_name,
                        "profile": profile_name,
                        "content_type": content_type,
                        "fetched_at": int(time.time()),
                    })
                except Exception as exc:
                    log.warning("quarantine sidecar write failed: %s", exc)

                # When the download finishes, update the hash and size,
                # then run the scan.
                request.isFinishedChanged.connect(
                    lambda _r=request, _id=row_id, _qp=q_path:
                        self._quarantine_on_finished(_r, _id, _qp))
            except Exception as exc:
                # Fail closed (iso2 `13` E3): intake errors used to fall
                # back to a direct ~/Downloads write, removing the claimed
                # containment exactly when its setup failed.
                log.error("quarantine redirect failed; download refused: %s",
                          exc)
                # Clean up partial quarantine state so we don't leave
                # orphan DB rows for files that will never arrive.
                if row_id is not None:
                    try:
                        qs.update_scan_result(row_id, "intake_failed")
                    except Exception:
                        pass
                self._cancel_request(request)
                self._notify_blocked(
                    "This download was blocked because it could not be "
                    "placed in quarantine.")
                return
        else:
            self._set_direct_download_dir(request)

        # Track whether this request ended up in quarantine so the UI
        # knows not to offer a direct-open button.
        is_quarantined = qs is not None and row_id is not None

        if self._panel:
            self._panel.add_active(request, quarantined=is_quarantined,
                                   private=otr)
        # Bridge events carry the source URL out over D-Bus; suppress
        # them entirely for private downloads.
        if not otr:
            try:
                request.isFinishedChanged.connect(
                    lambda _r=request: self._notify_bridge_finished(_r))
            except Exception as exc:
                log.warning("bridge download-finished hook failed: %s", exc)
            # Notify bridge_adapter (if loaded and active) so it can fan
            # the event out over D-Bus to qdistro daemons. We look it up
            # via the plugin manager rather than importing the module so
            # qdbrowser still works when bridge_adapter is disabled.
            self._notify_bridge_started(request)
        request.accept()

    @staticmethod
    def _cancel_request(request) -> None:
        try:
            request.cancel()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not cancel refused download: %s", exc)

    def _notify_blocked(self, message: str) -> None:
        """Tell the user a download was refused (quarantine unavailable)."""
        window = self._window
        if window is None:
            return
        try:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(window, "Download blocked", message)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not show download-blocked notice: %s", exc)

    def _set_direct_download_dir(self, request: QWebEngineDownloadRequest):
        """Write directly to the user's downloads directory.

        Used only when quarantine is *disabled* or the URL host is in
        ``auto_release_domains`` — never as an error fallback: an intake
        failure with quarantine enabled cancels the download instead
        (iso2 `13` E3).

        Prefers ``[downloads] release_dir``, falls back to the legacy
        ``[general] downloads_dir`` so existing user configs are honoured.
        """
        cfg = Config()
        target_dir = cfg.get(
            "downloads", "release_dir",
            default=cfg.get("general", "downloads_dir",
                            default=os.path.expanduser("~/Downloads")))
        os.makedirs(target_dir, exist_ok=True)
        request.setDownloadDirectory(target_dir)

    def _quarantine_on_finished(self, request, row_id, q_path):
        """After download finishes, hash + scan the quarantined file."""
        if not request.isFinished():
            return
        if request.state() != QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
            return
        qs = self._quarantine
        if qs is None:
            return
        try:
            from qdbrowser.quarantine import hash_file, run_scan
            sha = hash_file(q_path) if os.path.exists(q_path) else ""
            size = os.path.getsize(q_path) if os.path.exists(q_path) else 0
            qs.update_after_finish(row_id, sha, size)
            scan_cmd = Config().get("downloads", "scan_command", default="")
            result = run_scan(scan_cmd, q_path)
            qs.update_scan_result(row_id, result)
        except Exception as exc:
            log.warning("quarantine post-finish failed id=%s: %s",
                        row_id, exc)

    @staticmethod
    def _request_is_off_the_record(
            request: QWebEngineDownloadRequest) -> bool:
        """True when the download originates from a private (off-the-record)
        profile. Such downloads must not leave a durable trace of their
        source URL or profile (history log, quarantine metadata/sidecar,
        bridge events) — the file itself may still be quarantined and
        scanned, that is operational security, not a privacy record.
        Fail closed: any error treats the request as private.
        """
        try:
            return bool(request.page().profile().isOffTheRecord())
        except Exception:
            return True

    @staticmethod
    def _download_id(request: QWebEngineDownloadRequest) -> int:
        return int(request.id()) if hasattr(request, "id") else id(request)

    @staticmethod
    def _download_state_int(request: QWebEngineDownloadRequest) -> int:
        state = request.state()
        return int(getattr(state, "value", state))

    @staticmethod
    def _download_filename(request: QWebEngineDownloadRequest) -> str:
        return os.path.basename(
            os.path.join(request.downloadDirectory(),
                         request.downloadFileName()))

    @staticmethod
    def _download_counter(request: QWebEngineDownloadRequest,
                          attr: str) -> int:
        try:
            value = getattr(request, attr)
        except Exception:
            return 0
        try:
            value = value() if callable(value) else value
            return int(value or 0)
        except Exception:
            return 0

    @staticmethod
    def _download_url(request: QWebEngineDownloadRequest) -> str:
        try:
            url = request.url()
            return url.toString() if hasattr(url, "toString") else str(url)
        except Exception:
            return ""

    @staticmethod
    def _download_mime(request: QWebEngineDownloadRequest) -> str:
        try:
            return str(request.mimeType())
        except Exception:
            return ""

    def _bridge_adapter(self):
        win = self._window
        if win is None or not hasattr(win, "plugins"):
            return None
        try:
            bridge = win.plugins._instances.get("bridge_adapter")
        except Exception:
            return None
        if bridge is None or not getattr(bridge, "active", False):
            return None
        return bridge

    def _notify_bridge_started(self, request: QWebEngineDownloadRequest) -> None:
        bridge = self._bridge_adapter()
        if bridge is None:
            return
        try:
            bridge.emit_download_started(
                self._download_id(request),
                self._download_filename(request),
                url=self._download_url(request),
                mime=self._download_mime(request),
                total_bytes=self._download_counter(request, "totalBytes"),
                bytes_received=self._download_counter(request, "receivedBytes"))
        except Exception as exc:
            log.warning("bridge download-started notify failed: %s", exc)

    def _notify_bridge_finished(self, request: QWebEngineDownloadRequest) -> None:
        try:
            if not request.isFinished():
                return
        except Exception:
            return
        bridge = self._bridge_adapter()
        if bridge is None:
            return
        try:
            forward = getattr(bridge, "forward_download_state", None)
            if forward is None:
                return
            forward(
                self._download_id(request),
                self._download_filename(request),
                state=self._download_state_int(request),
                url=self._download_url(request),
                mime=self._download_mime(request),
                total_bytes=self._download_counter(request, "totalBytes"),
                bytes_received=self._download_counter(request, "receivedBytes"))
        except Exception as exc:
            log.warning("bridge download-finished notify failed: %s", exc)

    def get_commands(self, window):
        return [
            ("Show downloads panel",
             lambda: window._side_panel.show_panel(self.panel_id)),
            ("Open downloads directory",
             lambda: self._panel._open_dir() if self._panel else None),
            ("Clear finished downloads",
             lambda: self._panel._clear_finished() if self._panel else None),
        ]

"""Downloads plugin: panel + command."""


class _Plugins:
    def __init__(self, bridge):
        self._instances = {"bridge_adapter": bridge}


class _Window:
    def __init__(self, bridge):
        self.plugins = _Plugins(bridge)


class _Bridge:
    active = True

    def __init__(self):
        self.calls = []
        self.forward_calls = []

    def emit_download_started(self, download_id, filename, state=0, **kw):
        self.calls.append((download_id, filename, state, kw))

    def forward_download_state(self, download_id, filename, state=0, **kw):
        self.forward_calls.append((download_id, filename, state, kw))


class _LegacyBridge:
    active = True

    def __init__(self):
        self.calls = []

    def emit_download_started(self, download_id, filename, state=0, **kw):
        self.calls.append((download_id, filename, state, kw))


class _Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self):
        for callback in self.callbacks:
            callback()


class _Url:
    def host(self):
        return "example.test"

    def toString(self):
        return "https://example.test/file.zip"


class _Profile:
    def __init__(self, otr=False):
        self._otr = otr

    def isOffTheRecord(self):
        return self._otr

    def storageName(self):
        return "" if self._otr else "default"


class _Page:
    def __init__(self, otr=False):
        self._profile = _Profile(otr)

    def profile(self):
        return self._profile


class _Request:
    def __init__(self, *, state=0, finished=False, finish_on_accept=False,
                 private=False):
        self._state = state
        self._finished = finished
        self._finish_on_accept = finish_on_accept
        self._page = _Page(private)
        self.accepted = False
        self.cancelled = False
        self.isFinishedChanged = _Signal()

    def page(self):
        return self._page

    def id(self):
        return 42

    def state(self):
        return self._state

    def isFinished(self):
        return self._finished

    def downloadDirectory(self):
        return "/tmp/downloads"

    def setDownloadDirectory(self, directory):
        self._directory = directory

    def downloadFileName(self):
        return "file.zip"

    def setDownloadFileName(self, filename):
        self._filename = filename

    def totalBytes(self):
        return 100

    def receivedBytes(self):
        return 100 if self._finished else 10

    def url(self):
        return _Url()

    def mimeType(self):
        return "application/zip"

    def cancel(self):
        self.cancelled = True

    def accept(self):
        self.accepted = True
        if self._finish_on_accept:
            self._state = 2
            self._finished = True
            self.isFinishedChanged.emit()


def test_panel_starts_empty(window):
    plug = window.plugins._instances["downloads"]
    panel = plug._panel
    assert panel is not None
    assert panel._list.count() == 0


def test_clear_finished_removes_historical_rows(window):
    """Historical (already-finished) rows go away on Clear; nothing
    else is left to assert about active rows since this test doesn't
    construct a real QWebEngineDownloadRequest."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QListWidgetItem

    plug = window.plugins._instances["downloads"]
    panel = plug._panel
    panel._list.clear()
    item = QListWidgetItem("✓ done.zip")
    item.setData(Qt.ItemDataRole.UserRole,
                 {"path": "/x/done.zip", "historical": True})
    panel._list.addItem(item)
    assert panel._list.count() == 1
    panel._clear_finished()
    assert panel._list.count() == 0


def test_commands_provided(window):
    plug = window.plugins._instances["downloads"]
    labels = [label for label, _ in plug.get_commands(window)]
    assert any("downloads" in label.lower() for label in labels)


def test_bridge_notifies_terminal_download_state(window):
    plug = window.plugins._instances["downloads"]
    bridge = _Bridge()
    plug._window = _Window(bridge)
    plug._notify_bridge_finished(_Request(state=2, finished=True))
    assert bridge.forward_calls == [
        (42, "file.zip", 2, {
            "url": "https://example.test/file.zip",
            "mime": "application/zip",
            "total_bytes": 100,
            "bytes_received": 100,
        })
    ]


def test_bridge_does_not_notify_unfinished_download(window):
    plug = window.plugins._instances["downloads"]
    bridge = _Bridge()
    plug._window = _Window(bridge)
    plug._notify_bridge_finished(_Request(state=1, finished=False))
    assert bridge.calls == []
    assert bridge.forward_calls == []


def test_bridge_finished_requires_forward_only_method(window):
    plug = window.plugins._instances["downloads"]
    bridge = _LegacyBridge()
    plug._window = _Window(bridge)
    plug._notify_bridge_finished(_Request(state=2, finished=True))
    assert bridge.calls == []


def test_bridge_finish_hook_connected_before_accept(window):
    plug = window.plugins._instances["downloads"]
    bridge = _Bridge()
    plug._window = _Window(bridge)
    plug._panel = None
    req = _Request(state=0, finished=False)
    plug._on_download_requested(req)
    assert req.accepted is True
    assert req.isFinishedChanged.callbacks


def test_bridge_fast_completion_does_not_duplicate_terminal_state(window):
    plug = window.plugins._instances["downloads"]
    bridge = _Bridge()
    plug._window = _Window(bridge)
    plug._panel = None
    req = _Request(state=0, finished=False, finish_on_accept=True)
    plug._on_download_requested(req)
    assert bridge.calls == [
        (42, "file.zip", 0, {
            "url": "https://example.test/file.zip",
            "mime": "application/zip",
            "total_bytes": 100,
            "bytes_received": 10,
        })
    ]
    assert bridge.forward_calls == [
        (42, "file.zip", 2, {
            "url": "https://example.test/file.zip",
            "mime": "application/zip",
            "total_bytes": 100,
            "bytes_received": 100,
        })
    ]


def test_request_otr_detection(window):
    plug = window.plugins._instances["downloads"]
    assert plug._request_is_off_the_record(_Request(private=True)) is True
    assert plug._request_is_off_the_record(_Request(private=False)) is False


def test_request_otr_fails_closed_without_page(window):
    # A request that can't reveal its profile is treated as private so
    # we never accidentally persist a private origin.
    plug = window.plugins._instances["downloads"]

    class _NoPage:
        def page(self):
            raise RuntimeError("no page")

    assert plug._request_is_off_the_record(_NoPage()) is True


def test_private_download_does_not_notify_bridge(window):
    plug = window.plugins._instances["downloads"]
    bridge = _Bridge()
    plug._window = _Window(bridge)
    plug._panel = None
    req = _Request(state=0, finished=False, finish_on_accept=True,
                   private=True)
    plug._on_download_requested(req)
    assert req.accepted is True
    # No URL-bearing bridge events for a private download (neither the
    # started emit nor the finished forward).
    assert bridge.calls == []
    assert bridge.forward_calls == []


def _fresh_quarantine(plug, tmp_path):
    from qdbrowser.quarantine import QuarantineStore
    qs = QuarantineStore(str(tmp_path / "quarantine"))
    plug._quarantine = qs
    return qs


def test_private_download_omits_source_url_in_quarantine(window, tmp_path):
    plug = window.plugins._instances["downloads"]
    plug._window = _Window(_Bridge())
    plug._panel = None
    qs = _fresh_quarantine(plug, tmp_path)
    req = _Request(state=0, finished=False, private=True)
    plug._on_download_requested(req)
    rows = qs.list_all()
    assert rows, "download should still be quarantined"
    # The origin URL must NOT be persisted for OTR downloads.
    assert rows[0].get("source_url", "") == ""
    assert rows[0].get("profile_name", "") == ""


def test_normal_download_keeps_source_url_in_quarantine(window, tmp_path):
    plug = window.plugins._instances["downloads"]
    plug._window = _Window(_Bridge())
    plug._panel = None
    qs = _fresh_quarantine(plug, tmp_path)
    req = _Request(state=0, finished=False, private=False)
    plug._on_download_requested(req)
    rows = qs.list_all()
    assert rows
    assert rows[0].get("source_url", "") == "https://example.test/file.zip"


class _FakeWidget:
    def path(self):
        return "/tmp/downloads/file.zip"


def _completed_state():
    from PyQt6.QtWebEngineCore import QWebEngineDownloadRequest
    return QWebEngineDownloadRequest.DownloadState.DownloadCompleted


def test_private_download_not_persisted_to_history(window, tmp_path,
                                                   monkeypatch):
    # Defence in depth: the persist slot itself refuses an OTR request
    # even if it is reached.
    import qdbrowser.plugins.downloads as d
    hist_path = tmp_path / "downloads.json"
    monkeypatch.setattr(d, "HISTORY_PATH", str(hist_path))
    panel = d.DownloadsPanel(window, history=[])
    req = _Request(state=_completed_state(), finished=True, private=True)
    panel._on_finished_persist(req, _FakeWidget())
    assert panel._history == []
    assert not hist_path.exists()


def test_normal_download_persisted_to_history(window, tmp_path, monkeypatch):
    import qdbrowser.plugins.downloads as d
    hist_path = tmp_path / "downloads.json"
    monkeypatch.setattr(d, "HISTORY_PATH", str(hist_path))
    panel = d.DownloadsPanel(window, history=[])
    req = _Request(state=_completed_state(), finished=True, private=False)
    panel._on_finished_persist(req, _FakeWidget())
    assert len(panel._history) == 1
    assert panel._history[0]["url"] == "https://example.test/file.zip"


def test_persist_fails_closed_without_page(window, tmp_path, monkeypatch):
    # If the profile can't be determined, the persist slot skips the
    # write rather than risk leaking a private origin.
    import qdbrowser.plugins.downloads as d
    hist_path = tmp_path / "downloads.json"
    monkeypatch.setattr(d, "HISTORY_PATH", str(hist_path))
    panel = d.DownloadsPanel(window, history=[])

    class _NoPageReq(_Request):
        def page(self):
            raise RuntimeError("no page")

    req = _NoPageReq(state=_completed_state(), finished=True)
    panel._on_finished_persist(req, _FakeWidget())
    assert panel._history == []
    assert not hist_path.exists()


def test_private_autorelease_host_redacted_in_log(window, tmp_path, caplog):
    import logging

    from qdbrowser.config import Config
    plug = window.plugins._instances["downloads"]
    plug._window = _Window(_Bridge())
    plug._panel = None
    _fresh_quarantine(plug, tmp_path)
    # example.test is the host of the fake request's URL.
    Config().set("downloads", "auto_release_domains", ["example.test"])
    try:
        with caplog.at_level(logging.INFO, logger="qdbrowser.downloads"):
            plug._on_download_requested(
                _Request(state=0, finished=False, private=True))
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "auto-release" in text
        # The private host must NOT appear in the journal-bound log.
        assert "example.test" not in text
        assert "<private>" in text
    finally:
        Config().set("downloads", "auto_release_domains", [])


def test_normal_autorelease_host_logged(window, tmp_path, caplog):
    import logging

    from qdbrowser.config import Config
    plug = window.plugins._instances["downloads"]
    plug._window = _Window(_Bridge())
    plug._panel = None
    _fresh_quarantine(plug, tmp_path)
    Config().set("downloads", "auto_release_domains", ["example.test"])
    try:
        with caplog.at_level(logging.INFO, logger="qdbrowser.downloads"):
            plug._on_download_requested(
                _Request(state=0, finished=False, private=False))
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "example.test" in text
    finally:
        Config().set("downloads", "auto_release_domains", [])


# --- iso2 `13` E3: quarantine failures must fail closed -----------------

def test_quarantine_init_failure_blocks_downloads(window, monkeypatch,
                                                  caplog):
    """Store init failing with quarantine enabled refuses downloads
    instead of writing straight to ~/Downloads."""
    import logging

    from qdbrowser.plugins import downloads as dl_mod

    plug = window.plugins._instances["downloads"]
    plug._window = _Window(_Bridge())
    plug._panel = None

    def _boom(*a, **kw):
        raise OSError("no space left on device")

    monkeypatch.setattr(dl_mod, "QuarantineStore", _boom)
    with caplog.at_level(logging.ERROR, logger="qdbrowser.downloads"):
        plug.activate(_Window(_Bridge()))
    assert plug._quarantine is None
    assert plug._quarantine_required is True

    plug._panel = None
    req = _Request(state=0, finished=False)
    with caplog.at_level(logging.ERROR, logger="qdbrowser.downloads"):
        plug._on_download_requested(req)
    assert req.cancelled is True
    assert req.accepted is False
    assert not hasattr(req, "_directory")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "refused" in text.lower()


def test_quarantine_disabled_still_downloads_direct(window, monkeypatch):
    """quarantine_enabled = false is unchanged: direct download."""
    from qdbrowser.config import Config

    plug = window.plugins._instances["downloads"]
    Config().set("downloads", "quarantine_enabled", False)
    try:
        plug.activate(_Window(_Bridge()))
        assert plug._quarantine is None
        assert plug._quarantine_required is False
        plug._window = _Window(_Bridge())
        plug._panel = None
        req = _Request(state=0, finished=False)
        plug._on_download_requested(req)
        assert req.accepted is True
        assert req.cancelled is False
        assert getattr(req, "_directory", None)
    finally:
        Config().set("downloads", "quarantine_enabled", True)


def test_quarantine_redirect_failure_cancels(window, tmp_path, caplog):
    """A per-request intake error cancels the download and marks the
    orphan row intake_failed instead of falling back to direct."""
    import logging

    plug = window.plugins._instances["downloads"]
    plug._window = _Window(_Bridge())
    plug._panel = None
    qs = _fresh_quarantine(plug, tmp_path)

    class _BadSignal(_Signal):
        def connect(self, callback):
            raise RuntimeError("intake exploded")

    req = _Request(state=0, finished=False)
    req.isFinishedChanged = _BadSignal()

    with caplog.at_level(logging.ERROR, logger="qdbrowser.downloads"):
        plug._on_download_requested(req)

    assert req.cancelled is True
    assert req.accepted is False
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "refused" in text.lower()
    # The orphan row is marked, not left pending.
    rows = qs.list_all()
    assert rows and rows[0].get("scan_result") == "intake_failed"

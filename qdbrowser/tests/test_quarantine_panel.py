"""Quarantine release-flow side panel.

The Qt widget (``QuarantinePanel``) is a thin shell; all release/delete
logic lives in ``QuarantineController``, which is Qt-free and tested here
headless. The polkit gate is injected as a callable so no ``pkcheck``
subprocess is spawned.
"""

import os


def _make_store(tmp_path):
    from qdbrowser.quarantine import QuarantineStore
    return QuarantineStore(str(tmp_path / "quar"))


def _quarantined(store, name="doc.pdf", scan="clean"):
    qpath = os.path.join(store.directory, name)
    with open(qpath, "wb") as f:
        f.write(b"contents")
    row_id = store.record(quarantine_path=qpath, filename=name,
                          source_url=f"https://x/{name}", scan_result=scan)
    return qpath, row_id


# ---------------------------------------------------------------------------
# list shaping
# ---------------------------------------------------------------------------


def test_row_summary_shapes_fields():
    from qdbrowser.plugins.quarantine_panel import _row_summary
    s = _row_summary({
        "id": 7, "filename": "a.bin", "source_url": "https://h/a.bin",
        "scan_result": "clean", "fetched_at": 0,
    })
    assert s["id"] == 7
    assert s["filename"] == "a.bin"
    assert s["source_url"] == "https://h/a.bin"
    assert s["scan_result"] == "clean"
    assert s["releasable"] is True


def test_row_summary_bad_scan_not_releasable():
    from qdbrowser.plugins.quarantine_panel import _row_summary
    s = _row_summary({"id": 1, "filename": "x", "source_url": "",
                      "scan_result": "bad", "fetched_at": 0})
    assert s["releasable"] is False


def test_row_summary_pending_not_releasable():
    from qdbrowser.plugins.quarantine_panel import _row_summary
    s = _row_summary({"id": 1, "filename": "x", "source_url": "",
                      "scan_result": "pending", "fetched_at": 0})
    assert s["releasable"] is False


def test_row_summary_scanner_error_not_releasable():
    from qdbrowser.plugins.quarantine_panel import _row_summary
    s = _row_summary({"id": 1, "filename": "x", "source_url": "",
                      "scan_result": "error", "fetched_at": 0})
    assert s["releasable"] is False


def test_row_summary_skipped_is_releasable():
    from qdbrowser.plugins.quarantine_panel import _row_summary
    s = _row_summary({"id": 1, "filename": "x", "source_url": "",
                      "scan_result": "skipped", "fetched_at": 0})
    assert s["releasable"] is True


def test_row_summary_handles_missing_fields():
    from qdbrowser.plugins.quarantine_panel import _row_summary
    s = _row_summary({"id": 2, "fetched_at": None})
    assert s["filename"] == "(unnamed)"
    assert s["source_url"] == ""
    assert s["scan_result"] == "pending"
    assert s["timestamp"] == "?"


def test_list_items_only_pending(tmp_path):
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    store = _make_store(tmp_path)
    _quarantined(store, "a.bin")
    _quarantined(store, "b.bin")
    ctl = QuarantineController(store)
    items = ctl.list_items()
    assert {i["filename"] for i in items} == {"a.bin", "b.bin"}
    store.close()


def test_list_items_empty(tmp_path):
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    ctl = QuarantineController(_make_store(tmp_path))
    assert ctl.list_items() == []


# ---------------------------------------------------------------------------
# release gating
# ---------------------------------------------------------------------------


def test_release_checks_authorization_before_moving(tmp_path):
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    store = _make_store(tmp_path)
    qpath, row_id = _quarantined(store)
    out = tmp_path / "Downloads"

    calls = []

    def gate():
        calls.append(1)
        return True

    ctl = QuarantineController(store)
    result = ctl.release(row_id, str(out), authorize=gate)
    assert calls == [1]
    assert result is not None
    assert os.path.exists(result)
    assert not os.path.exists(qpath)
    assert store.get(row_id)["released"] == 1
    store.close()


def test_release_denied_leaves_file(tmp_path):
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    store = _make_store(tmp_path)
    qpath, row_id = _quarantined(store)
    out = tmp_path / "Downloads"

    ctl = QuarantineController(store)
    result = ctl.release(row_id, str(out), authorize=lambda: False)
    assert result is None
    assert os.path.exists(qpath)            # not moved
    assert not out.exists()                 # never even created
    assert store.get(row_id)["released"] == 0
    store.close()


def test_release_does_not_call_move_when_denied(tmp_path, monkeypatch):
    """Belt-and-braces: when the gate denies, the underlying
    quarantine.release helper must not be invoked at all."""
    from qdbrowser.plugins import quarantine_panel
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    store = _make_store(tmp_path)
    _qpath, row_id = _quarantined(store)

    called = []
    monkeypatch.setattr(quarantine_panel.quar_mod, "release",
                        lambda *a, **k: called.append(1))
    ctl = QuarantineController(store)
    ctl.release(row_id, str(tmp_path / "out"), authorize=lambda: False)
    assert called == []
    store.close()


def test_release_empty_target_dir_noop(tmp_path):
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    store = _make_store(tmp_path)
    qpath, row_id = _quarantined(store)
    ctl = QuarantineController(store)
    # User cancelled the file chooser → empty string.
    assert ctl.release(row_id, "", authorize=lambda: True) is None
    assert os.path.exists(qpath)
    store.close()


def test_release_bad_scan_refused_even_if_authorized(tmp_path):
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    store = _make_store(tmp_path)
    qpath, row_id = _quarantined(store, "evil.exe", scan="bad")
    ctl = QuarantineController(store)
    result = ctl.release(row_id, str(tmp_path / "out"), authorize=lambda: True)
    assert result is None
    assert os.path.exists(qpath)
    store.close()


def test_release_pending_refused_even_if_authorized(tmp_path):
    """A still-pending (unfinished/unscanned) download must not leave
    quarantine even with authorization."""
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    store = _make_store(tmp_path)
    qpath, row_id = _quarantined(store, "partial.iso", scan="pending")
    ctl = QuarantineController(store)
    result = ctl.release(row_id, str(tmp_path / "out"), authorize=lambda: True)
    assert result is None
    assert os.path.exists(qpath)
    store.close()


def test_release_confined_to_quarantine_dir(tmp_path):
    """A forged row whose quarantine_path escapes the quarantine dir must
    not let release() move an arbitrary file even when authorized."""
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    store = _make_store(tmp_path)
    outside = tmp_path / "secret.key"
    outside.write_text("private")
    row_id = store.record(quarantine_path=str(outside), filename="secret.key",
                          source_url="https://x", scan_result="clean")
    ctl = QuarantineController(store)
    result = ctl.release(row_id, str(tmp_path / "out"), authorize=lambda: True)
    assert result is None
    assert outside.exists()                 # not moved
    assert store.get(row_id)["released"] == 0
    store.close()


def test_release_refuses_directory_path(tmp_path):
    """A forged row whose quarantine_path is a directory inside the
    quarantine tree must not let release() move the whole tree."""
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    store = _make_store(tmp_path)
    subdir = os.path.join(store.directory, "sub")
    os.makedirs(subdir)
    open(os.path.join(subdir, "f.bin"), "wb").close()
    row_id = store.record(quarantine_path=subdir, filename="sub",
                          source_url="https://x", scan_result="clean")
    ctl = QuarantineController(store)
    result = ctl.release(row_id, str(tmp_path / "out"), authorize=lambda: True)
    assert result is None
    assert os.path.isdir(subdir)            # not moved
    store.close()


def test_release_default_authorize_is_polkit_gate(tmp_path, monkeypatch):
    """With no authorize callable, the controller must consult the real
    quarantine.check_release_authorized entry point."""
    from qdbrowser.plugins import quarantine_panel
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    store = _make_store(tmp_path)
    _qpath, row_id = _quarantined(store)

    seen = []
    monkeypatch.setattr(quarantine_panel.quar_mod, "check_release_authorized",
                        lambda: seen.append(1) or False)
    ctl = QuarantineController(store)
    assert ctl.release(row_id, str(tmp_path / "out")) is None
    assert seen == [1]
    store.close()


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_row(tmp_path):
    from qdbrowser.plugins.quarantine_panel import QuarantineController
    store = _make_store(tmp_path)
    qpath, row_id = _quarantined(store)
    ctl = QuarantineController(store)
    assert ctl.delete(row_id) is True
    assert not os.path.exists(qpath)
    assert ctl.list_items() == []
    store.close()


# ---------------------------------------------------------------------------
# Qt widget (headless / offscreen)
# ---------------------------------------------------------------------------


def test_panel_registered_in_window(window):
    assert "quarantine" in window._side_panel.panel_ids()


def test_widget_renders_and_refreshes(qtbot, tmp_path):
    from qdbrowser.plugins.quarantine_panel import QuarantineController, QuarantinePanel
    store = _make_store(tmp_path)
    ctl = QuarantineController(store)
    panel = QuarantinePanel(None, ctl)
    qtbot.addWidget(panel)

    # Empty state: placeholder visible, list hidden.
    assert panel._empty.isVisibleTo(panel)
    assert panel._list.count() == 0

    _quarantined(store, "a.bin")
    panel.refresh()
    assert panel._list.count() == 1
    assert not panel._empty.isVisibleTo(panel)

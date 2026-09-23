"""Session lock file (per-notebook)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings
from qnotebook import locks
from qnotebook.window import MainWindow


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path_factory):
    d = tmp_path_factory.mktemp("qsettings")
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(d))
    s = QSettings("qnotebook", "qnotebook")
    s.clear(); s.sync()
    yield


def test_lock_acquire_creates_file(tmp_path):
    root = tmp_path / "nb"
    root.mkdir()
    (root / ".qnotebook").mkdir()
    ok, prev = locks.acquire(root)
    assert ok
    assert prev is None
    data = locks.read(root)
    assert data["pid"] == os.getpid()


def test_lock_stale_pid_is_reclaimable(tmp_path):
    root = tmp_path / "nb"
    root.mkdir()
    (root / ".qnotebook").mkdir()
    import socket
    (root / ".qnotebook" / "lock").write_text(
        json.dumps({"pid": 999999, "host": socket.gethostname()}),
        encoding="utf-8",
    )
    assert locks.is_stale(locks.read(root))
    ok, prev = locks.acquire(root)
    assert ok
    assert prev is not None
    assert prev["pid"] == 999999


def test_opening_locked_notebook_prompts(qapp, tmp_notebook: Path, qtbot, monkeypatch):
    import socket
    # Seed lock with init (pid 1) which is always alive and not us.
    (tmp_notebook / ".qnotebook").mkdir(exist_ok=True)
    (tmp_notebook / ".qnotebook" / "lock").write_text(
        json.dumps({"pid": 1, "host": socket.gethostname()}),
        encoding="utf-8",
    )
    w = MainWindow()
    qtbot.addWidget(w)
    calls = {"prompt": 0}

    def stub(existing):
        calls["prompt"] += 1
        return "readonly"

    monkeypatch.setattr(w, "_prompt_lock_conflict", stub)
    w.open_notebook(str(tmp_notebook))
    assert calls["prompt"] == 1
    assert w._read_only is True

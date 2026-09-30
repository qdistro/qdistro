"""The admin app's first-paint marker appears only after an exposed paint.

The GUI test launcher (deploy/start-admin-app.sh) returns once this marker
exists, so S1 screenshots see a painted window instead of the bare desktop
(full-20260930T051422Z-65193 permissions-gui/22 and /47).
"""
import os
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication, QMainWindow
from qdistro_admin_app import _FirstPaintMarker

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_marker_is_written_after_first_paint_not_at_construction(app, tmp_path):
    marker = tmp_path / "painted"
    win = QMainWindow()
    _FirstPaintMarker(win, str(marker))
    _spin(50)
    assert not marker.exists(), "no marker before the window is shown and painted"
    win.show()
    for _ in range(40):
        if marker.exists():
            break
        _spin(50)
    assert marker.read_text() == f"painted pid={os.getpid()}\n"
    # One marker per launch: later repaints never rewrite or fail on it.
    marker.write_text("kept\n")
    win.update()
    _spin(100)
    assert marker.read_text() == "kept\n"
    win.close()


def test_hidden_window_never_marks(app, tmp_path):
    marker = tmp_path / "painted"
    win = QMainWindow()
    _FirstPaintMarker(win, str(marker))
    _spin(200)
    assert not marker.exists()
    win.deleteLater()


def _fake_launcher(tmp_path, app_body: str):
    """Run the real launcher against a stand-in app script."""
    home = tmp_path / "home"
    app_dir = home / "qdistro" / "admin_app"
    app_dir.mkdir(parents=True)
    (app_dir / "qdistro_admin_app.py").write_text(app_body)
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    launcher = (ROOT / "deploy" / "start-admin-app.sh").read_text()
    # The launcher pins XDG_RUNTIME_DIR to uid 1000's; point it at the test dir.
    launcher = launcher.replace("export XDG_RUNTIME_DIR=/run/user/1000",
                                f"export XDG_RUNTIME_DIR={runtime}")
    # ... and its app path (resolved from getent passwd) at the stand-in.
    app_line = 'APP_PY="$ADMIN_HOME/qdistro/admin_app/qdistro_admin_app.py"'
    assert app_line in launcher
    launcher = launcher.replace(app_line, f'APP_PY="{app_dir}/qdistro_admin_app.py"')
    script = tmp_path / "start-admin-app.sh"
    script.write_text(launcher)
    env = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(tmp_path / "state"),
           "QDISTRO_ADMIN_APP_READY_TIMEOUT": "5"}
    return script, env, runtime


def _run_as_self(script, env):
    if os.geteuid() == 0:
        pytest.skip("launcher drops root to uid 1000")
    return subprocess.run(["bash", str(script)], env=env, capture_output=True,
                          text=True, timeout=30)


def test_launcher_waits_for_marker_then_prints_pid(tmp_path):
    script, env, runtime = _fake_launcher(tmp_path, (
        "import os, time\n"
        "time.sleep(1)\n"
        "open(os.path.join(os.path.dirname(__file__), 'painted'), 'w').close()\n"
        "open(os.environ['QDISTRO_ADMIN_APP_READY_FILE'], 'x').write('painted\\n')\n"
        "time.sleep(30)\n"))
    proc = _run_as_self(script, env)
    try:
        assert proc.returncode == 0, proc.stderr
        # It returned only after the app painted, not straight after the fork.
        assert (tmp_path / "home" / "qdistro" / "admin_app" / "painted").exists()
        pid = int(proc.stdout.strip().splitlines()[-1])
        os.kill(pid, 0)
        assert not list(runtime.glob("qdistro-admin-app-ready.*")), "ready dir removed"
    finally:
        subprocess.run(["pkill", "-f", str(tmp_path / "home")], check=False)


def test_launcher_fails_when_app_dies_before_painting(tmp_path):
    script, env, _ = _fake_launcher(tmp_path, (
        "import os\n"
        "open(os.path.join(os.path.dirname(__file__), 'ran'), 'w').close()\n"
        "raise SystemExit(1)\n"))
    proc = _run_as_self(script, env)
    assert proc.returncode == 3
    assert "exited before painting its window" in proc.stderr
    assert proc.stdout.strip().isdigit()
    assert (tmp_path / "home" / "qdistro" / "admin_app" / "ran").exists(), "stand-in app ran"


def test_launcher_times_out_when_app_never_paints(tmp_path):
    script, env, _ = _fake_launcher(tmp_path, "import time\ntime.sleep(30)\n")
    env["QDISTRO_ADMIN_APP_READY_TIMEOUT"] = "1"
    proc = _run_as_self(script, env)
    try:
        assert proc.returncode == 3
        assert "painted no window within 1s" in proc.stderr
    finally:
        subprocess.run(["pkill", "-f", str(tmp_path / "home")], check=False)

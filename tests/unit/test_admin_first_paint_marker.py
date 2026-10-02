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


def test_marker_syncs_qt_display_connection_before_the_file_appears(app, tmp_path, monkeypatch):
    """X orders requests per connection only: the round trip that proves the
    server has the frame must run on Qt's own connection, before the file."""
    import qdistro_admin_app as mod

    marker = tmp_path / "painted"
    seen = []

    class _App:
        @staticmethod
        def sync():
            seen.append(marker.exists())

    monkeypatch.setattr(mod, "QApplication", _App)
    win = QMainWindow()
    _FirstPaintMarker(win, str(marker))
    win.show()
    for _ in range(40):
        if marker.exists():
            break
        _spin(50)
    assert marker.exists()
    assert seen == [False], "synced exactly once, before the marker existed"
    win.close()


def test_paint_before_exposure_is_rechecked_when_the_window_is_exposed(app, tmp_path):
    """Qt may paint before exposure and present that store without a second
    paint; the marker must request a repaint on Expose, not wait forever."""
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QPaintEvent, QWindow

    marker = tmp_path / "painted"
    win = QMainWindow()
    native = QWindow()  # stands in for win's handle; unexposed until shown
    win.windowHandle = lambda: native
    repaints = []
    win.update = lambda: repaints.append(1)
    _FirstPaintMarker(win, str(marker))

    app.sendEvent(win, QPaintEvent(QRect(0, 0, 10, 10)))  # pre-exposure paint
    _spin(50)
    assert not marker.exists(), "an unexposed paint is not a frame"
    assert repaints == []

    native.show()  # exposure without any further paint of win
    for _ in range(40):
        if repaints:
            break
        _spin(50)
    assert repaints, "exposure requested a repaint"

    app.sendEvent(win, QPaintEvent(QRect(0, 0, 10, 10)))  # the requested repaint
    _spin(50)
    assert marker.read_text() == f"painted pid={os.getpid()}\n"
    native.close()


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


# The SHIPPED native-Wayland launcher (deploy/start-admin-app-wayland.sh) has
# the same contract behind QDISTRO_ADMIN_APP_WAIT_PAINTED=1: the qdwin GUI lane
# launches the admin app through it and captures right after it returns.
def _fake_wayland_launcher(tmp_path, app_body: str):
    """Run the real Wayland launcher against a stand-in app as 'uid 1000'."""
    import socket

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "id").write_text("#!/bin/bash\nprintf '1000\\n'\n")
    (bindir / "id").chmod(0o755)
    app = tmp_path / "stand-in-app.py"
    app.write_text(app_body)
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(runtime / "wayland-test"))
    except OSError as exc:  # pragma: no cover - sandboxed hosts
        pytest.skip(f"cannot bind a unix socket here: {exc}")
    launcher = (ROOT / "deploy" / "start-admin-app-wayland.sh").read_text()
    app_path = "/usr/local/bin/qdistro-admin-approval-app"
    assert launcher.count(app_path) == 3  # two detached starts + the exec
    script = tmp_path / "start-admin-app-wayland.sh"
    script.write_text(launcher.replace(app_path, str(app)))
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
           "XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": "wayland-test",
           "QDISTRO_ADMIN_APP_READY_TIMEOUT": "5"}
    return script, env, runtime, sock


def test_wayland_launcher_wait_mode_returns_pid_after_marker(tmp_path):
    script, env, runtime, sock = _fake_wayland_launcher(tmp_path, (
        "import os, sys, time\n"
        "assert os.environ['QT_QPA_PLATFORM'] == 'wayland'\n"
        "print('app output must not reach the launcher stdout', flush=True)\n"
        "time.sleep(1)\n"
        "open(sys.argv[0] + '.painted', 'w').close()\n"
        "open(os.environ['QDISTRO_ADMIN_APP_READY_FILE'], 'x').write('painted\\n')\n"
        "time.sleep(30)\n"))
    env["QDISTRO_ADMIN_APP_WAIT_PAINTED"] = "1"
    try:
        proc = subprocess.run(["bash", str(script)], env=env, capture_output=True,
                              text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
        # It returned only after the app painted, not straight after the fork.
        assert (tmp_path / "stand-in-app.py.painted").exists()
        # stdout carries the pid and nothing else; the app's output is in its log.
        assert proc.stdout.strip().isdigit(), proc.stdout
        os.kill(int(proc.stdout.strip()), 0)
        logs = list(runtime.glob("qdistro-admin-app.*.log"))
        assert len(logs) == 1 and str(logs[0]) in proc.stderr
        assert not list(runtime.glob("qdistro-admin-app-ready.*")), "ready dir removed"
    finally:
        sock.close()
        subprocess.run(["pkill", "-f", str(tmp_path / "stand-in-app.py")], check=False)
    assert "app output must not reach" in logs[0].read_text()


def test_wayland_launcher_wait_mode_fails_when_app_dies_before_painting(tmp_path):
    script, env, _, sock = _fake_wayland_launcher(tmp_path, "raise SystemExit(1)\n")
    env["QDISTRO_ADMIN_APP_WAIT_PAINTED"] = "1"
    try:
        proc = subprocess.run(["bash", str(script)], env=env, capture_output=True,
                              text=True, timeout=30)
    finally:
        sock.close()
    assert proc.returncode == 3
    assert "exited before painting its window" in proc.stderr
    assert proc.stdout.strip().isdigit()


def test_wayland_launcher_wait_mode_times_out_when_app_never_paints(tmp_path):
    script, env, _, sock = _fake_wayland_launcher(tmp_path, "import time\ntime.sleep(30)\n")
    env["QDISTRO_ADMIN_APP_WAIT_PAINTED"] = "1"
    env["QDISTRO_ADMIN_APP_READY_TIMEOUT"] = "1"
    try:
        proc = subprocess.run(["bash", str(script)], env=env, capture_output=True,
                              text=True, timeout=30)
        assert proc.returncode == 3
        assert "painted no window within 1s" in proc.stderr
    finally:
        sock.close()
        subprocess.run(["pkill", "-f", str(tmp_path / "stand-in-app.py")], check=False)


def test_wayland_launcher_desktop_mode_execs_the_app(tmp_path):
    # Without the opt-in the launcher must stay an exec: the pid the shell
    # started IS the app, and its exit status is the app's.
    script, env, _, sock = _fake_wayland_launcher(tmp_path, (
        "import os, sys\n"
        "print('pid', os.getpid(), 'ppid', os.getppid(),"
        " 'ready', os.environ.get('QDISTRO_ADMIN_APP_READY_FILE', '-'), sys.argv[1:])\n"
        "raise SystemExit(7)\n"))
    try:
        proc = subprocess.Popen(["bash", str(script), "approval-test"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = proc.communicate(timeout=30)
    finally:
        sock.close()
    assert proc.returncode == 7, err
    assert out.split()[:2] == ["pid", str(proc.pid)], out
    assert "ready -" in out and "['approval-test']" in out

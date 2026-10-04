#!/usr/bin/env python3
"""Host-runnable four-app live presentation follow.

Starts the qfileman, qdterm, qdbrowser, and qnotebook chrome adapters as
separate offscreen processes against one snapshot directory. Boot workers
also keep two top-level windows and one Preferences/Settings dialog open.
Missing file is fallback; publish A then B is followed without calling
_reload; a late join reads B; malformed JSON keeps B; publish C is
followed; a font/scale publish restyles existing chrome; deletion keeps C;
a post-delete join is fallback; enabled:false restores native.
This is not the live compositor GUI matrix and does not write
/var/lib/qdistro.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

APPS = (
    ("qfileman", "qdfileman", "qfileman.theme"),
    ("qdterm", "qdterm", "qterminator.theme"),
    ("qdbrowser", "qdbrowser", "qdbrowser.theme"),
    ("qnotebook", "qnotebook", "qnotebook.theme"),
)
COLOR_A = "#112233"
COLOR_B = "#445566"
COLOR_C = "#223344"
NATIVE = "#fedcba"
FONT_FAMILY = "DejaVu Sans"
FONT_FAMILY_TOKEN = "DejaVu_Sans"
FONT_UI_SCALE = 1.25
FONT_SIZE = 11.0 * FONT_UI_SCALE
FONT_SIZE_TOKEN = f"{FONT_SIZE:.2f}"
BASE_FAMILY_TOKEN = "Sans_Serif"
SIZE_TOLERANCE = 0.05
PHASE_TIMEOUT = 30.0
SETTLE = 0.5
SETTLE_PHASES = frozenset({"malformed", "delete"})
# disable drops the shared layer; adapters may then paint their own
# system fallback, so the window color is any non-snapshot value.
PHASE_EXPECT = {
    "A": ("1", COLOR_A),
    "B": ("1", COLOR_B),
    "malformed": ("1", COLOR_B),
    "C": ("1", COLOR_C),
    "fonts": ("1", COLOR_C),
    "delete": ("1", COLOR_C),
    "disable": ("0", None),
}
SNAPSHOT_COLORS = (COLOR_A, COLOR_B, COLOR_C)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def parse_status(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in text.split():
        if "=" in part:
            key, value = part.split("=", 1)
            out[key] = value
    return out


class Cfg:
    def get(self, *keys, default=None):
        if keys[:2] == ("general", "theme_mode"):
            return "system"
        if keys == ("appearance",):
            return {}
        return default


def window_hex(app) -> str:
    from PyQt6.QtGui import QPalette

    return app.palette().color(QPalette.ColorRole.Window).name().lower()


def widget_hex(widget) -> str:
    from PyQt6.QtGui import QPalette

    return widget.palette().color(QPalette.ColorRole.Window).name().lower()


def family_token(widget) -> str:
    return widget.font().family().replace(" ", "_")


def size_token(widget) -> str:
    return f"{widget.font().pointSizeF():.2f}"


def size_matches(widget, want: float) -> bool:
    return abs(widget.font().pointSizeF() - want) <= SIZE_TOLERANCE


def isolate_home(root: Path) -> None:
    home = root / "home"
    runtime = home / "run"
    home.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    os.chmod(runtime, 0o700)
    os.environ["HOME"] = str(home)
    os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
    os.environ["XDG_CACHE_HOME"] = str(home / ".cache")
    os.environ["XDG_DATA_HOME"] = str(home / ".local" / "share")
    os.environ["XDG_RUNTIME_DIR"] = str(runtime)


def ensure_qtermwidget() -> None:
    """Host pytest uses the same fallback when the SIP binding is absent."""
    if "QTermWidget" in sys.modules:
        return
    try:
        import QTermWidget  # noqa: F401
        return
    except ImportError:
        pass
    import types

    from PyQt6.QtCore import pyqtSignal
    from PyQt6.QtWidgets import QWidget

    mod = types.ModuleType("QTermWidget")

    class QTermWidget(QWidget):
        _QTERMINATOR_FAKE = True
        finished = pyqtSignal()
        titleChanged = pyqtSignal()
        termGetFocus = pyqtSignal()
        activity = pyqtSignal()
        silence = pyqtSignal()
        bell = pyqtSignal(str)
        urlActivated = pyqtSignal(object, bool)
        copyAvailable = pyqtSignal(bool)
        termKeyPressed = pyqtSignal(object)
        receivedData = pyqtSignal(str)
        sendData = pyqtSignal(bytes, int)

        class ScrollBarPosition:
            NoScrollBar = 0
            ScrollBarLeft = 1
            ScrollBarRight = 2

        @staticmethod
        def availableColorSchemes():
            return ["BlackOnWhite", "Linux", "WhiteOnBlack"]

    mod.QTermWidget = QTermWidget
    sys.modules["QTermWidget"] = mod


def prepare_qdbrowser() -> None:
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    required = (
        "--no-sandbox",
        "--disable-gpu",
        "--headless",
        "--in-process-gpu",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-domain-reliability",
        "--disable-sync",
        "--metrics-recording-only",
        "--disable-default-apps",
    )
    parts = flags.split()
    for flag in required:
        if flag not in parts:
            parts.append(flag)
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(parts)
    import PyQt6.QtWebEngineWidgets  # noqa: F401


def open_chrome(app_name: str, resolved_theme: str):
    if app_name == "qfileman":
        from qfileman.config import Config
        from qfileman.preferences import PreferencesDialog
        from qfileman.window import FileManagerWindow

        w0 = FileManagerWindow()
        w1 = FileManagerWindow()
        dlg = PreferencesDialog(Config(), parent=w0)
    elif app_name == "qdterm":
        ensure_qtermwidget()
        from qterminator.preferences import PreferencesDialog
        from qterminator.window import MainWindow

        w0 = MainWindow(resolved_theme=resolved_theme, create_initial_tab=False)
        w1 = MainWindow(resolved_theme=resolved_theme, create_initial_tab=False)
        dlg = PreferencesDialog(w0)
    elif app_name == "qdbrowser":
        from PyQt6.QtWidgets import QMainWindow
        from qdbrowser.preferences import PreferencesDialog
        from qdbrowser.window import MainWindow

        # A second WebEngine MainWindow aborts offscreen Chromium; the extra
        # window is still a top-level chrome surface in this process.
        w0 = MainWindow(resolved_theme=resolved_theme)
        w1 = QMainWindow()
        w1.setWindowTitle("qdbrowser-chrome-2")
        dlg = PreferencesDialog(w0._config, parent=w0)
    elif app_name == "qnotebook":
        from qnotebook.settings_dialog import SettingsDialog
        from qnotebook.window import MainWindow

        w0 = MainWindow()
        w1 = MainWindow()
        dlg = SettingsDialog(w0)
    else:
        raise SystemExit(f"FAIL: unknown app {app_name}")
    w0.show()
    w1.show()
    dlg.show()
    return w0, w1, dlg


def chrome_status(windows) -> str:
    if windows is None:
        return ""
    w0, w1, dlg = windows
    if not (w0.isVisible() and w1.isVisible() and dlg.isVisible()):
        return " chrome=hidden"
    return (
        f" win0={widget_hex(w0)} win1={widget_hex(w1)} dlg={widget_hex(dlg)}"
        f" uifamily={family_token(w0)} uisize={size_token(w0)}"
    )


def chrome_matches(windows, want_window: str | None, *, fonts: bool) -> bool:
    if windows is None:
        return True
    w0, w1, dlg = windows
    if not (w0.isVisible() and w1.isVisible() and dlg.isVisible()):
        return False
    if want_window is not None:
        if widget_hex(w0) != want_window:
            return False
        if widget_hex(w1) != want_window:
            return False
        if widget_hex(dlg) != want_window:
            return False
    if fonts:
        for widget in (w0, w1, dlg):
            if family_token(widget) != FONT_FAMILY_TOKEN:
                return False
            if family_token(widget) == BASE_FAMILY_TOKEN:
                return False
            if not size_matches(widget, FONT_SIZE):
                return False
    return True


def surface_of(ctrl) -> str:
    if not ctrl.state.using_shared_palette:
        return "-"
    return ctrl.state.colors.mSurface


def run_worker(
    app_name: str,
    module_name: str,
    snap_dir: Path,
    status_path: Path,
    cmd_path: Path,
    expect_shared: str,
    expect_window: str,
    with_windows: bool,
) -> int:
    isolate_home(status_path.parent / f"home-{status_path.stem}")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QDISTRO_PRESENTATION_FILE"] = str(snap_dir / "current.json")
    if app_name == "qdbrowser":
        prepare_qdbrowser()

    from PyQt6.QtGui import QColor, QPalette
    from PyQt6.QtWidgets import QApplication

    theme = __import__(module_name, fromlist=["attach_presentation", "current_controller"])
    attach = theme.attach_presentation
    current_controller = theme.current_controller

    app = QApplication.instance() or QApplication([f"presentation-four-apps-{app_name}"])
    native = QPalette(app.palette())
    native.setColor(QPalette.ColorRole.Window, QColor(NATIVE))
    app.setPalette(native)
    app.setStyleSheet(f"QWidget {{ background: {NATIVE}; }}")

    attach(app, Cfg())
    ctrl = current_controller()
    if ctrl is None:
        print(f"FAIL: {app_name} attach returned no controller", file=sys.stderr)
        return 1
    shared = "1" if ctrl.state.using_shared_palette else "0"
    color = window_hex(app)
    if shared != expect_shared:
        print(
            f"FAIL: {app_name} start shared={shared} window={color} "
            f"want shared={expect_shared} window={expect_window}",
            file=sys.stderr,
        )
        return 1
    if expect_window == "-":
        if color in (COLOR_A, COLOR_B, COLOR_C):
            print(
                f"FAIL: {app_name} fallback already painted snapshot color {color}",
                file=sys.stderr,
            )
            return 1
    elif color != expect_window:
        print(
            f"FAIL: {app_name} start shared={shared} window={color} "
            f"want shared={expect_shared} window={expect_window}",
            file=sys.stderr,
        )
        return 1
    windows = None
    if with_windows:
        resolved = ctrl.state.snapshot.mode if ctrl.state.snapshot is not None else "dark"
        windows = open_chrome(app_name, resolved)
        app.processEvents()
        if expect_window not in ("-",) and not chrome_matches(
            windows, expect_window if expect_window != "-" else None, fonts=False
        ):
            print(
                f"FAIL: {app_name} chrome at start "
                f"{chrome_status(windows).strip()} want window={expect_window}",
                file=sys.stderr,
            )
            return 1
    atomic_write(
        status_path,
        f"phase=ready shared={shared} window={color}{chrome_status(windows)}\n",
    )

    seen = "idle"
    deadline = 0.0
    settle_started = 0.0
    while True:
        app.processEvents()
        cmd = read_text(cmd_path) or "idle"
        if cmd == "quit":
            ctrl.stop()
            if windows is not None:
                from PyQt6.QtCore import QCoreApplication, QEvent

                # No app.exec() runs in this probe, so drain deferred Qt
                # deletion before Python unloads qdbrowser's WebEngine types.
                for widget in reversed(windows):
                    widget.close()
                    widget.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            return 0
        if cmd != seen:
            seen = cmd
            deadline = time.monotonic() + PHASE_TIMEOUT
            settle_started = time.monotonic() if cmd in SETTLE_PHASES else 0.0
        if cmd not in PHASE_EXPECT:
            time.sleep(0.05)
            continue
        want_shared, want_window = PHASE_EXPECT[cmd]
        now = time.monotonic()
        shared = "1" if ctrl.state.using_shared_palette else "0"
        color = window_hex(app)
        if want_window is None:
            matched = shared == want_shared and color not in SNAPSHOT_COLORS
            matched = matched and chrome_matches(windows, None, fonts=False)
        else:
            matched = shared == want_shared and color == want_window
            if want_shared == "1":
                matched = matched and ctrl.state.colors.mSurface == want_window
            matched = matched and chrome_matches(
                windows, want_window, fonts=(cmd == "fonts")
            )
        extra = chrome_status(windows)
        gen = ctrl.state.generation or "-"
        line = f"phase={cmd} shared={shared} window={color} gen={gen}{extra}\n"
        if cmd in SETTLE_PHASES:
            if now - settle_started < SETTLE:
                time.sleep(0.05)
                continue
            if matched:
                atomic_write(status_path, line)
                seen = f"{cmd}-done"
            else:
                print(
                    f"FAIL: {app_name} lost last-known-good on {cmd}: "
                    f"shared={shared} window={color} surface={surface_of(ctrl)}"
                    f" gen={gen}{extra}",
                    file=sys.stderr,
                )
                return 1
        elif matched:
            atomic_write(status_path, line)
            seen = f"{cmd}-done"
        elif now > deadline:
            print(
                f"FAIL: {app_name} did not follow {cmd}: shared={shared} "
                f"window={color} surface={surface_of(ctrl)} gen={gen}{extra}",
                file=sys.stderr,
            )
            return 1
        time.sleep(0.05)


def publish(directory: Path, surface: str) -> str:
    from qdistro_presentation.model import example_snapshot
    from qdistro_presentation.publish import write_snapshot

    snap = example_snapshot()
    snap = replace(snap, colors=replace(snap.colors, mSurface=surface), enabled=True)
    result = write_snapshot(str(directory), snap, skip_unchanged=False)
    if not result.wrote:
        raise SystemExit(f"FAIL: publish {surface} did not write ({result.reason})")
    return result.generation


def publish_disabled(directory: Path) -> None:
    from qdistro_presentation.model import example_snapshot
    from qdistro_presentation.publish import write_disabled_envelope

    result = write_disabled_envelope(str(directory), example_snapshot())
    if not result.wrote:
        raise SystemExit(f"FAIL: enabled:false did not write ({result.reason})")


def publish_fonts(directory: Path, surface: str) -> str:
    from qdistro_presentation.model import example_snapshot
    from qdistro_presentation.publish import write_snapshot

    snap = example_snapshot()
    snap = replace(
        snap,
        colors=replace(snap.colors, mSurface=surface),
        fonts=replace(snap.fonts, ui_family=FONT_FAMILY, ui_scale=FONT_UI_SCALE),
        enabled=True,
    )
    result = write_snapshot(str(directory), snap, skip_unchanged=False)
    if not result.wrote:
        raise SystemExit(f"FAIL: publish fonts did not write ({result.reason})")
    return result.generation


def wait_phase(
    statuses: dict[str, Path],
    procs: dict[str, subprocess.Popen],
    phase: str,
    timeout: float,
    *,
    shared: str,
    window: str | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for name, proc in procs.items():
            if proc.poll() is not None:
                raise SystemExit(f"FAIL: {name} worker exited {proc.returncode} before {phase}")
        done = True
        for name, path in statuses.items():
            parsed = parse_status(read_text(path))
            if parsed.get("phase") != phase:
                done = False
                break
            if parsed.get("shared") != shared:
                done = False
                break
            if window is not None and parsed.get("window") != window:
                done = False
                break
        if done:
            return
        time.sleep(0.05)
    details = {name: read_text(path) for name, path in statuses.items()}
    raise SystemExit(f"FAIL: timeout waiting for phase {phase}: {details}")


def assert_status(
    statuses: dict[str, Path],
    *,
    phase: str,
    shared: str,
    window: str,
) -> None:
    for name, path in statuses.items():
        parsed = parse_status(read_text(path))
        if (
            parsed.get("phase") != phase
            or parsed.get("shared") != shared
            or parsed.get("window") != window
        ):
            raise SystemExit(f"FAIL: {name} expected {phase} shared={shared} window={window}: {read_text(path)}")


def assert_chrome(
    statuses: dict[str, Path],
    *,
    window: str,
    family: str | None = None,
    size: str | None = None,
    generation: str | None = None,
) -> None:
    for name, path in statuses.items():
        parsed = parse_status(read_text(path))
        text = read_text(path)
        if (
            parsed.get("win0") != window
            or parsed.get("win1") != window
            or parsed.get("dlg") != window
        ):
            raise SystemExit(
                f"FAIL: {name} chrome did not follow window={window}: {text}"
            )
        if family is not None and parsed.get("uifamily") != family:
            raise SystemExit(
                f"FAIL: {name} chrome uifamily want {family}: {text}"
            )
        if size is not None and parsed.get("uisize") != size:
            raise SystemExit(
                f"FAIL: {name} chrome uisize want {size}: {text}"
            )
        if generation is not None and parsed.get("gen") != generation:
            raise SystemExit(
                f"FAIL: {name} chrome gen want {generation}: {text}"
            )


def assert_chrome_self_test() -> int:
    """Negative control: stale generation must fail even when palette/size match."""
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="p7-chrome-self-"))
    try:
        gen = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        stale = "00000000-0000-0000-0000-000000000000"
        common = (
            f"phase=fonts shared=1 window={COLOR_C} "
            f"win0={COLOR_C} win1={COLOR_C} dlg={COLOR_C} "
            f"uifamily={FONT_FAMILY_TOKEN} uisize={FONT_SIZE_TOKEN}"
        )
        good = work / "good.status"
        bad = work / "stale.status"
        atomic_write(good, f"{common} gen={gen}\n")
        atomic_write(bad, f"{common} gen={stale}\n")
        assert_chrome(
            {"good": good},
            window=COLOR_C,
            family=FONT_FAMILY_TOKEN,
            size=FONT_SIZE_TOKEN,
            generation=gen,
        )
        try:
            assert_chrome(
                {"stale": bad},
                window=COLOR_C,
                family=FONT_FAMILY_TOKEN,
                size=FONT_SIZE_TOKEN,
                generation=gen,
            )
        except SystemExit:
            print("ok")
            return 0
        print("FAIL: stale generation did not fail assert_chrome", file=sys.stderr)
        return 1
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


def pythonpath(repo: Path) -> str:
    parts = [
        str(repo / "sdk" / "presentation"),
        str(repo / "qdfileman"),
        str(repo / "qdterm"),
        str(repo / "qdbrowser"),
        str(repo / "qnotebook"),
    ]
    return os.pathsep.join(parts)


def spawn_workers(
    *,
    suffix: str,
    expect_shared: str,
    expect_window: str,
    work: Path,
    snap: Path,
    cmd_path: Path,
    env: dict[str, str],
    repo: Path,
    procs: dict[str, subprocess.Popen],
    statuses: dict[str, Path],
    logs: dict[str, Path],
    with_windows: bool = False,
) -> dict[str, Path]:
    group: dict[str, Path] = {}
    probe = str(Path(__file__).resolve())
    for name, _src, module in APPS:
        key = name if not suffix else f"{name}-{suffix}"
        status_path = work / f"{key}.status"
        log_path = work / f"{key}.log"
        statuses[key] = status_path
        logs[key] = log_path
        group[key] = status_path
        log_f = open(log_path, "w", encoding="utf-8")
        argv = [
            sys.executable,
            probe,
            "--worker",
            name,
            module,
            str(snap),
            str(status_path),
            str(cmd_path),
            expect_shared,
            expect_window,
        ]
        if with_windows:
            argv.append("windows")
        procs[key] = subprocess.Popen(
            argv,
            cwd=str(repo),
            env=env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )
        log_f.close()
    return group


def run_orchestrator() -> int:
    repo = repo_root()
    for part in pythonpath(repo).split(os.pathsep):
        if part not in sys.path:
            sys.path.insert(0, part)
    raw_tmp = os.environ.get("PRESENTATION_FOUR_APPS_TMP", "").strip()
    owned_tmp = False
    if raw_tmp:
        work = Path(raw_tmp)
        if not work.is_absolute():
            raise SystemExit("FAIL: PRESENTATION_FOUR_APPS_TMP must be an absolute path")
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
    else:
        import tempfile

        work = Path(tempfile.mkdtemp(prefix="p7-four-apps-"))
        os.chmod(work, 0o700)
        owned_tmp = True
    snap = work / "snap"
    snap.mkdir(mode=0o700, exist_ok=False)
    cmd_path = work / "cmd"
    atomic_write(cmd_path, "idle\n")

    env = os.environ.copy()
    env["PYTHONPATH"] = pythonpath(repo)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONSAFEPATH"] = "1"
    env.pop("QDISTRO_PRESENTATION_FILE", None)

    procs: dict[str, subprocess.Popen] = {}
    statuses: dict[str, Path] = {}
    logs: dict[str, Path] = {}
    try:
        boot = spawn_workers(
            suffix="",
            expect_shared="0",
            expect_window="-",
            work=work,
            snap=snap,
            cmd_path=cmd_path,
            env=env,
            repo=repo,
            procs=procs,
            statuses=statuses,
            logs=logs,
            with_windows=True,
        )
        wait_phase(boot, procs, "ready", PHASE_TIMEOUT, shared="0")

        gen_a = publish(snap, COLOR_A)
        atomic_write(cmd_path, "A\n")
        wait_phase(boot, procs, "A", PHASE_TIMEOUT, shared="1", window=COLOR_A)
        assert_chrome(boot, window=COLOR_A)

        gen_b = publish(snap, COLOR_B)
        if gen_a == gen_b:
            raise SystemExit("FAIL: A and B reused generation")
        atomic_write(cmd_path, "B\n")
        wait_phase(boot, procs, "B", PHASE_TIMEOUT, shared="1", window=COLOR_B)
        assert_chrome(boot, window=COLOR_B)

        atomic_write(cmd_path, "idle\n")
        join = spawn_workers(
            suffix="join",
            expect_shared="1",
            expect_window=COLOR_B,
            work=work,
            snap=snap,
            cmd_path=cmd_path,
            env=env,
            repo=repo,
            procs=procs,
            statuses=statuses,
            logs=logs,
        )
        wait_phase(join, procs, "ready", PHASE_TIMEOUT, shared="1", window=COLOR_B)
        running = {**boot, **join}

        atomic_write(snap / "current.json", "{not json\n")
        atomic_write(cmd_path, "malformed\n")
        wait_phase(running, procs, "malformed", PHASE_TIMEOUT, shared="1", window=COLOR_B)

        gen_c = publish(snap, COLOR_C)
        if gen_c in (gen_a, gen_b):
            raise SystemExit("FAIL: C reused generation")
        atomic_write(cmd_path, "C\n")
        wait_phase(running, procs, "C", PHASE_TIMEOUT, shared="1", window=COLOR_C)
        assert_chrome(boot, window=COLOR_C)

        gen_fonts = publish_fonts(snap, COLOR_C)
        if gen_fonts in (gen_a, gen_b, gen_c):
            raise SystemExit("FAIL: fonts reused generation")
        atomic_write(cmd_path, "fonts\n")
        wait_phase(running, procs, "fonts", PHASE_TIMEOUT, shared="1", window=COLOR_C)
        assert_chrome(
            boot,
            window=COLOR_C,
            family=FONT_FAMILY_TOKEN,
            size=FONT_SIZE_TOKEN,
            generation=gen_fonts,
        )

        (snap / "current.json").unlink()
        atomic_write(cmd_path, "delete\n")
        wait_phase(running, procs, "delete", PHASE_TIMEOUT, shared="1", window=COLOR_C)

        atomic_write(cmd_path, "idle\n")
        post = spawn_workers(
            suffix="post",
            expect_shared="0",
            expect_window="-",
            work=work,
            snap=snap,
            cmd_path=cmd_path,
            env=env,
            repo=repo,
            procs=procs,
            statuses=statuses,
            logs=logs,
        )
        wait_phase(post, procs, "ready", PHASE_TIMEOUT, shared="0")
        assert_status(running, phase="delete", shared="1", window=COLOR_C)

        publish_disabled(snap)
        atomic_write(cmd_path, "disable\n")
        wait_phase(running, procs, "disable", PHASE_TIMEOUT, shared="0")

        atomic_write(cmd_path, "quit\n")
        for name, proc in procs.items():
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise SystemExit(f"FAIL: {name} worker did not exit")
            if rc != 0:
                raise SystemExit(
                    f"FAIL: {name} worker exit {rc}: {logs[name].read_text(encoding='utf-8')[-500:]}"
                )
        print("ok")
        return 0
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        atomic_write(cmd_path, "quit\n")
        for proc in procs.values():
            if proc.poll() is None:
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        return 1
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
        if owned_tmp:
            import shutil

            shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str]) -> int:
    if argv[:1] == ["--worker"]:
        if len(argv) not in (8, 9):
            print("FAIL: worker argv", file=sys.stderr)
            return 2
        _, name, module, snap, status, cmd, expect_shared, expect_window = argv[:8]
        with_windows = len(argv) == 9 and argv[8] == "windows"
        if len(argv) == 9 and argv[8] != "windows":
            print("FAIL: worker argv", file=sys.stderr)
            return 2
        return run_worker(
            name,
            module,
            Path(snap),
            Path(status),
            Path(cmd),
            expect_shared,
            expect_window,
            with_windows,
        )
    if argv[:1] == ["--assert-chrome-self-test"]:
        if len(argv) != 1:
            print("FAIL: unexpected argv", file=sys.stderr)
            return 2
        return assert_chrome_self_test()
    if argv:
        print("FAIL: unexpected argv", file=sys.stderr)
        return 2
    return run_orchestrator()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

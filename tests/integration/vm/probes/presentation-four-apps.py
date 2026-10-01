#!/usr/bin/env python3
"""Host-runnable four-app live presentation follow.

Starts the qfileman, qdterm, qdbrowser, and qnotebook chrome adapters as
separate offscreen processes against one snapshot directory. Missing file
is fallback; publish A then B is followed without calling _reload; deletion
keeps B. This is not the live compositor GUI matrix and does not write
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
NATIVE = "#fedcba"
PHASE_TIMEOUT = 10.0
DELETE_SETTLE = 0.5


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


def run_worker(app_name: str, module_name: str, snap_dir: Path, status_path: Path, cmd_path: Path) -> int:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QDISTRO_PRESENTATION_FILE"] = str(snap_dir / "current.json")

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
    if ctrl.state.using_shared_palette:
        print(f"FAIL: {app_name} used shared palette before publish", file=sys.stderr)
        return 1
    atomic_write(
        status_path,
        f"phase=ready shared=0 window={window_hex(app)}\n",
    )

    seen = "idle"
    deadline = 0.0
    delete_started = 0.0
    while True:
        app.processEvents()
        cmd = read_text(cmd_path) or "idle"
        if cmd == "quit":
            ctrl.stop()
            return 0
        if cmd != seen:
            seen = cmd
            deadline = time.monotonic() + PHASE_TIMEOUT
            delete_started = time.monotonic() if cmd == "delete" else 0.0
        now = time.monotonic()
        shared = "1" if ctrl.state.using_shared_palette else "0"
        color = window_hex(app)
        if cmd == "A":
            if shared == "1" and color == COLOR_A and ctrl.state.colors.mSurface == COLOR_A:
                atomic_write(status_path, f"phase=A shared=1 window={COLOR_A}\n")
                seen = "A-done"
            elif now > deadline:
                print(
                    f"FAIL: {app_name} did not follow A: shared={shared} window={color} "
                    f"surface={ctrl.state.colors.mSurface if ctrl.state.using_shared_palette else '-'}",
                    file=sys.stderr,
                )
                return 1
        elif cmd == "B":
            if shared == "1" and color == COLOR_B and ctrl.state.colors.mSurface == COLOR_B:
                atomic_write(status_path, f"phase=B shared=1 window={COLOR_B}\n")
                seen = "B-done"
            elif now > deadline:
                print(
                    f"FAIL: {app_name} did not follow B without _reload: shared={shared} "
                    f"window={color} surface={ctrl.state.colors.mSurface if ctrl.state.using_shared_palette else '-'}",
                    file=sys.stderr,
                )
                return 1
        elif cmd == "delete":
            if now - delete_started >= DELETE_SETTLE:
                if shared == "1" and color == COLOR_B:
                    atomic_write(status_path, f"phase=delete shared=1 window={COLOR_B}\n")
                    seen = "delete-done"
                else:
                    print(
                        f"FAIL: {app_name} dropped last-known-good after delete: "
                        f"shared={shared} window={color}",
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


def wait_phase(statuses: dict[str, Path], procs: dict[str, subprocess.Popen], phase: str, timeout: float) -> None:
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
            if phase in ("A", "B", "delete") and parsed.get("shared") != "1":
                done = False
                break
            if phase == "A" and parsed.get("window") != COLOR_A:
                done = False
                break
            if phase in ("B", "delete") and parsed.get("window") != COLOR_B:
                done = False
                break
        if done:
            return
        time.sleep(0.05)
    details = {name: read_text(path) for name, path in statuses.items()}
    raise SystemExit(f"FAIL: timeout waiting for phase {phase}: {details}")


def pythonpath(repo: Path) -> str:
    parts = [
        str(repo / "sdk" / "presentation"),
        str(repo / "qdfileman"),
        str(repo / "qdterm"),
        str(repo / "qdbrowser"),
        str(repo / "qnotebook"),
    ]
    return os.pathsep.join(parts)


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
        for name, _src, module in APPS:
            status_path = work / f"{name}.status"
            log_path = work / f"{name}.log"
            statuses[name] = status_path
            logs[name] = log_path
            log_f = open(log_path, "w", encoding="utf-8")
            procs[name] = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    name,
                    module,
                    str(snap),
                    str(status_path),
                    str(cmd_path),
                ],
                cwd=str(repo),
                env=env,
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )
            log_f.close()

        wait_phase(statuses, procs, "ready", PHASE_TIMEOUT)
        for name, path in statuses.items():
            parsed = parse_status(read_text(path))
            if parsed.get("shared") != "0":
                raise SystemExit(f"FAIL: {name} shared palette before publish ({read_text(path)})")

        gen_a = publish(snap, COLOR_A)
        atomic_write(cmd_path, "A\n")
        wait_phase(statuses, procs, "A", PHASE_TIMEOUT)

        gen_b = publish(snap, COLOR_B)
        if gen_a == gen_b:
            raise SystemExit("FAIL: A and B reused generation")
        atomic_write(cmd_path, "B\n")
        wait_phase(statuses, procs, "B", PHASE_TIMEOUT)

        current = snap / "current.json"
        current.unlink()
        atomic_write(cmd_path, "delete\n")
        wait_phase(statuses, procs, "delete", PHASE_TIMEOUT)

        atomic_write(cmd_path, "quit\n")
        for name, proc in procs.items():
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise SystemExit(f"FAIL: {name} worker did not exit")
            if rc != 0:
                raise SystemExit(f"FAIL: {name} worker exit {rc}: {logs[name].read_text(encoding='utf-8')[-500:]}")
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
        if len(argv) != 6:
            print("FAIL: worker argv", file=sys.stderr)
            return 2
        _, name, module, snap, status, cmd = argv
        return run_worker(name, module, Path(snap), Path(status), Path(cmd))
    if argv:
        print("FAIL: unexpected argv", file=sys.stderr)
        return 2
    return run_orchestrator()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

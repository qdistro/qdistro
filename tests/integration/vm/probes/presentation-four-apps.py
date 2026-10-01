#!/usr/bin/env python3
"""Host-runnable four-app live presentation follow.

Starts the qfileman, qdterm, qdbrowser, and qnotebook chrome adapters as
separate offscreen processes against one snapshot directory. Missing file
is fallback; publish A then B is followed without calling _reload; a late
join reads B; malformed JSON keeps B; publish C is followed; deletion
keeps C; a post-delete join is fallback; enabled:false restores native.
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
PHASE_TIMEOUT = 10.0
SETTLE = 0.5
SETTLE_PHASES = frozenset({"malformed", "delete"})
# disable drops the shared layer; adapters may then paint their own
# system fallback, so the window color is any non-snapshot value.
PHASE_EXPECT = {
    "A": ("1", COLOR_A),
    "B": ("1", COLOR_B),
    "malformed": ("1", COLOR_B),
    "C": ("1", COLOR_C),
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
) -> int:
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
    atomic_write(
        status_path,
        f"phase=ready shared={shared} window={color}\n",
    )

    seen = "idle"
    deadline = 0.0
    settle_started = 0.0
    while True:
        app.processEvents()
        cmd = read_text(cmd_path) or "idle"
        if cmd == "quit":
            ctrl.stop()
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
        else:
            matched = shared == want_shared and color == want_window
            if want_shared == "1":
                matched = matched and ctrl.state.colors.mSurface == want_window
        if cmd in SETTLE_PHASES:
            if now - settle_started < SETTLE:
                time.sleep(0.05)
                continue
            if matched:
                atomic_write(status_path, f"phase={cmd} shared={shared} window={color}\n")
                seen = f"{cmd}-done"
            else:
                print(
                    f"FAIL: {app_name} lost last-known-good on {cmd}: "
                    f"shared={shared} window={color} surface={surface_of(ctrl)}",
                    file=sys.stderr,
                )
                return 1
        elif matched:
            atomic_write(status_path, f"phase={cmd} shared={shared} window={color}\n")
            seen = f"{cmd}-done"
        elif now > deadline:
            print(
                f"FAIL: {app_name} did not follow {cmd}: shared={shared} "
                f"window={color} surface={surface_of(ctrl)}",
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
        procs[key] = subprocess.Popen(
            [
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
            ],
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
        )
        wait_phase(boot, procs, "ready", PHASE_TIMEOUT, shared="0")

        gen_a = publish(snap, COLOR_A)
        atomic_write(cmd_path, "A\n")
        wait_phase(boot, procs, "A", PHASE_TIMEOUT, shared="1", window=COLOR_A)

        gen_b = publish(snap, COLOR_B)
        if gen_a == gen_b:
            raise SystemExit("FAIL: A and B reused generation")
        atomic_write(cmd_path, "B\n")
        wait_phase(boot, procs, "B", PHASE_TIMEOUT, shared="1", window=COLOR_B)

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
        if len(argv) != 8:
            print("FAIL: worker argv", file=sys.stderr)
            return 2
        _, name, module, snap, status, cmd, expect_shared, expect_window = argv
        return run_worker(
            name,
            module,
            Path(snap),
            Path(status),
            Path(cmd),
            expect_shared,
            expect_window,
        )
    if argv:
        print("FAIL: unexpected argv", file=sys.stderr)
        return 2
    return run_orchestrator()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

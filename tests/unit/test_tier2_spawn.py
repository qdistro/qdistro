from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPAWN = ROOT / "tier2" / "spawn-tier2.sh"


def _link_tool(bindir: Path, name: str) -> None:
    target = Path("/usr/bin") / name
    if not target.exists():
        target = Path("/bin") / name
    (bindir / name).symlink_to(target)


def _tool_path(tmp_path: Path, *, dbus_mode: str | None) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in (
        "bash",
        "basename",
        "chmod",
        "date",
        "dirname",
        "env",
        "grep",
        "head",
        "mkdir",
        "od",
        "readlink",
        "rm",
        "tr",
    ):
        _link_tool(bindir, name)

    podman = bindir / "podman"
    # Records the final `podman run ...` argv to $PODMAN_ARGV_FILE (if set)
    # so tests can assert the resolved container flags. `container exists`
    # returns 1 (absent) so the disposable same-second collision path is not
    # triggered.
    podman.write_text(
        "#!/bin/sh\n"
        "case \"$1 $2\" in\n"
        "  'image exists') exit 0 ;;\n"
        "  'ps -a') exit 0 ;;\n"
        "  'container exists') exit 1 ;;\n"
        "esac\n"
        "if [ \"$1\" = run ]; then\n"
        "  [ -n \"$PODMAN_ARGV_FILE\" ] && printf '%s\\n' \"$*\" > \"$PODMAN_ARGV_FILE\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    podman.chmod(0o755)

    if dbus_mode is not None:
        dbus = bindir / "dbus-send"
        dbus.write_text(
            "#!/bin/sh\n"
            "if [ -n \"$FAKE_EXPECT_ACTION\" ]; then\n"
            "  found=0\n"
            "  for arg in \"$@\"; do\n"
            "    [ \"$arg\" = \"string:$FAKE_EXPECT_ACTION\" ] && found=1\n"
            "  done\n"
            "  if [ \"$found\" -ne 1 ]; then\n"
            "    echo \"unexpected action; expected $FAKE_EXPECT_ACTION\" >&2\n"
            "    exit 3\n"
            "  fi\n"
            "fi\n"
            "case \"$FAKE_DBUS_MODE\" in\n"
            "  allow) echo 'string \"allow\"'; exit 0 ;;\n"
            "  deny) echo 'string \"deny\"'; exit 0 ;;\n"
            "  unknown) echo 'string \"unknown\"'; exit 0 ;;\n"
            "  error) echo 'broker unavailable' >&2; exit 1 ;;\n"
            "  disallow) echo 'string \"disallow\"'; exit 0 ;;\n"
            "  *) echo \"bad fake mode: $FAKE_DBUS_MODE\" >&2; exit 2 ;;\n"
            "esac\n"
        )
        dbus.chmod(0o755)

    return str(bindir)


def _run_spawn(
    tmp_path: Path,
    *,
    dbus_mode: str | None,
) -> subprocess.CompletedProcess[str]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    qdwin_shell = tmp_path / "qdwin-shell.so"
    qdwin_shell.write_text("stub\n")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(runtime / "wayland-1"))
    sock.listen(1)
    try:
        env = os.environ.copy()
        env.update({
            "FAKE_DBUS_MODE": dbus_mode or "",
            "FAKE_EXPECT_ACTION": "qdistro.tier2.spawn:weston-terminal/weston-terminal",
            "HOME": str(tmp_path / "home"),
            "PATH": _tool_path(tmp_path, dbus_mode=dbus_mode),
            "TIER2_OUTER_DISPLAY": "wayland-1",
            "TIER2_QDWIN_SHELL_SO": str(qdwin_shell),
            "TIER2_USE_SECCTX": "0",
            "XDG_RUNTIME_DIR": str(runtime),
        })
        return subprocess.run(
            [
                "/bin/bash",
                str(SPAWN),
                "tier2-c1",
                "weston-terminal",
                "--",
                "weston-terminal",
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        sock.close()


def test_tier2_spawn_requires_explicit_broker_allow(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="allow")

    assert result.returncode == 0, result.stderr
    assert "LAUNCH_TOKEN=" in result.stdout


def test_tier2_spawn_fails_closed_on_unknown(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="unknown")

    assert result.returncode == 2
    assert "no allow rule" in result.stderr
    assert "qdistro.tier2.spawn:weston-terminal/weston-terminal" in result.stderr
    assert "LAUNCH_TOKEN=" not in result.stdout


def test_tier2_spawn_fails_closed_on_broker_error(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="error")

    assert result.returncode == 2
    assert "broker authorization failed" in result.stderr


def test_tier2_spawn_rejects_malformed_allow_substring(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="disallow")

    assert result.returncode == 2
    assert "unsupported verdict" in result.stderr


def test_tier2_spawn_fails_closed_without_dbus_send(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode=None)

    assert result.returncode == 2
    assert "dbus-send not found" in result.stderr


# --- disposable (--disposable) variant (07-disposables-plan P1) -----------

def _run_disposable(
    tmp_path: Path,
    *,
    workload: str = "pdf",
    dbus_mode: str | None = "allow",
    print_plan: bool = False,
    record_podman: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    qdwin_shell = tmp_path / "qdwin-shell.so"
    qdwin_shell.write_text("stub\n")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(runtime / "wayland-1"))
    sock.listen(1)
    try:
        env = os.environ.copy()
        env.update({
            "FAKE_DBUS_MODE": dbus_mode or "",
            "FAKE_EXPECT_ACTION": f"qdistro.dispose.spawn:{workload}",
            "HOME": str(tmp_path / "home"),
            "PATH": _tool_path(tmp_path, dbus_mode=dbus_mode),
            "TIER2_OUTER_DISPLAY": "wayland-1",
            "TIER2_QDWIN_SHELL_SO": str(qdwin_shell),
            "TIER2_USE_SECCTX": "0",
            "XDG_RUNTIME_DIR": str(runtime),
        })
        if print_plan:
            env["TIER2_PRINT_PLAN"] = "1"
        if record_podman:
            env["PODMAN_ARGV_FILE"] = str(tmp_path / "podman-argv")
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["/bin/bash", str(SPAWN), "--disposable", workload,
             "--", "mupdf", "/tmp/doc.pdf"],
            cwd=str(ROOT), env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    finally:
        sock.close()


def _plan(result: subprocess.CompletedProcess[str]) -> dict[str, str]:
    out = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_disposable_plan_identity(tmp_path: Path) -> None:
    """Generated name disp-<workload>-<ts>, secctx app_id qdistro.disp.<token>,
    the dispose.spawn gate action, and no persistent state."""
    result = _run_disposable(tmp_path, print_plan=True)
    assert result.returncode == 0, result.stderr
    plan = _plan(result)
    assert plan["DISPOSABLE"] == "1"
    assert re.match(r"^disp-pdf-\d{8}-\d{6}$", plan["CONTAINER"]), plan
    assert re.match(r"^qdistro\.disp\.[0-9a-f]{32}$", plan["APP_ID"]), plan
    assert plan["SPAWN_ACTION"] == "qdistro.dispose.spawn:pdf"
    assert plan["ENGINE"] == "qdistro.tier2"
    assert plan["STATE"] == "none"


def test_disposable_rejects_state_binding(tmp_path: Path) -> None:
    result = _run_disposable(tmp_path, print_plan=True,
                             extra_env={"TIER2_SILO": "mysilo"})
    assert result.returncode != 0
    assert "incompatible with TIER2_SILO" in result.stderr


def test_disposable_rejects_bad_workload(tmp_path: Path) -> None:
    result = _run_disposable(tmp_path, workload="Bad_Name", print_plan=True)
    assert result.returncode != 0
    assert "invalid disposable workload" in result.stderr


def test_disposable_uses_dispose_gate_and_fails_closed(tmp_path: Path) -> None:
    # The fake broker asserts the action is qdistro.dispose.spawn:pdf; unknown
    # must fail closed (no LAUNCH_TOKEN emitted).
    result = _run_disposable(tmp_path, dbus_mode="unknown")
    assert result.returncode == 2
    assert "qdistro.dispose.spawn:pdf" in result.stderr
    assert "LAUNCH_TOKEN=" not in result.stdout


def test_disposable_podman_argv(tmp_path: Path) -> None:
    """The resolved podman run carries --rm, the disp- name, a tmpfs
    /home/admin, and NO persistent state bind."""
    result = _run_disposable(tmp_path, dbus_mode="allow", record_podman=True)
    assert result.returncode == 0, result.stderr
    argv = (tmp_path / "podman-argv").read_text()
    assert "--rm" in argv
    assert re.search(r"--name disp-pdf-\d{8}-\d{6}", argv), argv
    assert "type=tmpfs,destination=/home/admin," in argv
    # authoritative reaper marker (the session-manager sweep filters by label)
    assert "--label qdistro_disposable=1" in argv
    # no persistent-state bind into /home/admin
    assert ":/home/admin:rw" not in argv

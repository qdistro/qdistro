from __future__ import annotations

import os
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
        "dirname",
        "env",
        "head",
        "mkdir",
        "od",
        "readlink",
        "rm",
        "tr",
    ):
        _link_tool(bindir, name)

    podman = bindir / "podman"
    podman.write_text(
        "#!/bin/sh\n"
        "case \"$1 $2\" in\n"
        "  'image exists') exit 0 ;;\n"
        "  'ps -a') exit 0 ;;\n"
        "  'run --name') exit 0 ;;\n"
        "esac\n"
        "if [ \"$1\" = run ]; then exit 0; fi\n"
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

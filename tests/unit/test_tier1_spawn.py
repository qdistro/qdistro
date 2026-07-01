from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPAWN = ROOT / "selinux" / "tier1" / "spawn-tier1.sh"


def _tool_path(tmp_path: Path, *, dbus_mode: str | None) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("basename", "dirname", "env", "id", "mkdir", "readlink", "tr"):
        target = Path("/usr/bin") / name
        if not target.exists():
            target = Path("/bin") / name
        (bindir / name).symlink_to(target)
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
            "  empty) exit 0 ;;\n"
            "  error) echo 'broker unavailable' >&2; exit 1 ;;\n"
            "  disallow) echo 'string \"disallow\"'; exit 0 ;;\n"
            "  *) echo \"bad fake mode: $FAKE_DBUS_MODE\" >&2; exit 2 ;;\n"
            "esac\n"
        )
        dbus.chmod(0o755)
    return str(bindir)


def _tier1_exec(tmp_path: Path) -> Path:
    exe = tmp_path / "qdistro-tier1-exec"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


def _run_spawn(tmp_path: Path, *, dbus_mode: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        "FAKE_DBUS_MODE": dbus_mode or "",
        "FAKE_EXPECT_ACTION": "qdistro.tier1.spawn:/usr/bin/true",
        "HOME": str(tmp_path / "home"),
            "PATH": _tool_path(tmp_path, dbus_mode=dbus_mode),
            "QDISTRO_TIER1_EXEC": str(_tier1_exec(tmp_path)),
            "QDISTRO_PROFILE": "dev",
            "TIER1_USE_SECCTX": "0",
        })
    return subprocess.run(
        ["/bin/bash", str(SPAWN), "work", "--", "/usr/bin/true"],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_tier1_spawn_requires_explicit_broker_allow(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="allow")

    assert result.returncode == 0, result.stderr


def test_tier1_spawn_fails_closed_on_unknown(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="unknown")

    assert result.returncode == 1
    assert "no allow rule" in result.stderr
    assert "qdistro.tier1.spawn:/usr/bin/true" in result.stderr


def test_tier1_spawn_fails_closed_on_broker_error(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="error")

    assert result.returncode == 1
    assert "broker authorization failed" in result.stderr


def test_tier1_spawn_rejects_malformed_allow_substring(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode="disallow")

    assert result.returncode == 1
    assert "unsupported verdict" in result.stderr


def test_tier1_spawn_fails_closed_without_dbus_send(tmp_path: Path) -> None:
    result = _run_spawn(tmp_path, dbus_mode=None)

    assert result.returncode == 1
    assert "dbus-send not found" in result.stderr


def test_tier1_hardened_rejects_secctx_disabled(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.update({
        "FAKE_DBUS_MODE": "allow",
        "FAKE_EXPECT_ACTION": "qdistro.tier1.spawn:/usr/bin/true",
        "HOME": str(tmp_path / "home"),
        "PATH": _tool_path(tmp_path, dbus_mode="allow"),
        "QDISTRO_TIER1_EXEC": str(_tier1_exec(tmp_path)),
        "QDISTRO_PROFILE": "release",
        "TIER1_USE_SECCTX": "0",
    })
    result = subprocess.run(
        ["/bin/bash", str(SPAWN), "work", "--", "/usr/bin/true"],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 2
    assert "dev/test-only" in result.stderr


def test_tier1_hardened_rejects_direct_untagged_launch(tmp_path: Path) -> None:
    bindir = Path(_tool_path(tmp_path, dbus_mode="allow"))
    secctx = bindir / "qdistro-secctx-exec"
    secctx.write_text("#!/bin/sh\nexit 99\n")
    secctx.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "FAKE_DBUS_MODE": "allow",
        "FAKE_EXPECT_ACTION": "qdistro.tier1.spawn:/usr/bin/true",
        "HOME": str(tmp_path / "home"),
        "PATH": str(bindir),
        "QDISTRO_TIER1_EXEC": str(_tier1_exec(tmp_path)),
        "QDISTRO_PROFILE": "release",
    })
    result = subprocess.run(
        ["/bin/bash", str(SPAWN), "work", "--", "/usr/bin/true"],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 2
    assert "direct Tier-1 launch is dev-only" in result.stderr

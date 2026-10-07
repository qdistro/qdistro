"""Headless TCB no-network tripwires for non-broker qdistro units."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
POLKIT_SERVICE = ROOT / "polkit/qdistro-polkit-agent.service"
POLKIT_DIR = ROOT / "polkit"
SESSION_MANAGER_DIR = ROOT / "session_manager"


def _service_directives(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def test_polkit_agent_unit_has_no_network_runtime_hardening() -> None:
    directives = _service_directives(POLKIT_SERVICE)

    assert directives.get("PrivateNetwork") == "yes"
    # This is a user unit (installed to /etc/systemd/user). IP firewalling
    # needs the system manager: a user manager applies nothing and only logs
    # "unit configures an IP firewall, but not running as root". The
    # enforcement is PrivateNetwork= plus the address-family allowlist, so an
    # IPAddressDeny= here would be a no-op that reads as a control.
    assert "IPAddressDeny" not in directives
    families = directives.get("RestrictAddressFamilies", "")
    assert "AF_UNIX" in families
    assert "AF_INET" not in families
    assert "AF_INET6" not in families
    assert "AF_VSOCK" not in families


def test_polkit_agent_sources_stay_unix_only() -> None:
    offenders: list[str] = []
    for py in POLKIT_DIR.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        for family in ("AF_INET", "AF_INET6", "AF_VSOCK"):
            if f"socket.{family}" in text:
                offenders.append(f"{py.name}:socket.{family}")
    assert not offenders


def test_session_manager_no_network_exception_is_still_explicit() -> None:
    """The session manager still owns the netvm ubus-over-HTTP client.

    This pins why S5 cannot blindly copy the broker/polkit systemd
    PrivateNetwork profile onto session-manager until that control plane is
    split out or moved behind a non-IP transport.
    """
    netvm = (SESSION_MANAGER_DIR / "qdistro_netvm_client.py").read_text(
        encoding="utf-8"
    )
    assert "urllib.request" in netvm
    assert "ubus-over-HTTP" in netvm

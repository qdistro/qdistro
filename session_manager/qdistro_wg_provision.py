"""Admin-side WireGuard tunnel provisioning for per-silo egress (task 3).

A tunnel named `<name>` (referenced by a silo's egress policy `wg:<name>`) has
two halves:

  * a NON-secret config — the *peer* (VPN server) public key, endpoint, the
    silo-side tunnel address, and the tunnel resolver — written to
    `/etc/qdistro/wg/<name>.conf` (read by the session-manager at silo start);
  * the silo end's PRIVATE key — generated here and stored in qdistro-pwd,
    pinned so only root (the session-manager, TCB) can read it. It NEVER lands
    on disk (least of all in a silo home) and is NEVER written to the conf.

`provision()` generates the silo end's keypair, stores the private key, writes
the conf, and returns the silo end's *public* key for the admin to register
with the VPN provider. The core is dependency-injected so it is unit-testable
without `wg`, a pwd bus, or root.
"""
from __future__ import annotations

import argparse
import os
import re as _re
import subprocess
import sys
from pathlib import Path

# Same tunnel-name shape the egress backend accepts (egress wg:<name>): keep a
# crafted name from escaping WG_CONFIG_DIR or producing a tag no silo can ref.
_TUNNEL_NAME_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

# Mirror the session-manager's constants without importing it (keeps this admin
# tool independent of the daemon's heavy dbus import path).
WG_CONFIG_DIR = Path("/etc/qdistro/wg")
WG_PWD_VAULT = "wireguard"
PWD_BUS_NAME = "org.qdistro.Pwd1"
PWD_OBJ_PATH = "/org/qdistro/Pwd1"

# Canonical SELinux label of the session-manager domain (selinux/session_manager
# module, Opt 3-B). Pinning a wg key to this label tightens custody from "any
# root process" (pin_uid=0) to "only the session-manager domain". Only effective
# once that module is installed AND the daemon exec is relabelled+restarted
# (install-policy.sh does both) — otherwise the daemon runs unconfined and the
# pin would lock out its own reads, which is why it stays opt-in.
SESSION_MANAGER_SELINUX_LABEL = "system_u:system_r:qdistro_sessmgr_t:s0"

# Conf keys consumed by qdistro_session_manager._default_tunnel_resolver.
_CONF_KEYS = ("public_key", "endpoint", "address", "dns", "allowed_ips",
              "keepalive")


def _wg_key_tag(name: str) -> str:
    return f"wg/{name}/private-key"


def render_tunnel_conf(*, peer_public_key: str, endpoint: str, address: str,
                       dns: str | None = None,
                       allowed_ips: str = "0.0.0.0/0, ::/0",
                       keepalive: int | None = 25) -> str:
    """Render the NON-secret tunnel config. `public_key` is the *peer* (server)
    key — the silo end's private key is deliberately absent."""
    lines = [
        "# qdistro per-silo WireGuard tunnel (non-secret). The silo end's",
        "# PRIVATE key lives in qdistro-pwd (vault 'wireguard'), not here.",
        f"public_key = {peer_public_key}",
        f"endpoint = {endpoint}",
        f"address = {address}",
    ]
    if dns:
        lines.append(f"dns = {dns}")
    lines.append(f"allowed_ips = {allowed_ips}")
    if keepalive:
        lines.append(f"keepalive = {int(keepalive)}")
    return "\n".join(lines) + "\n"


def _real_keygen() -> tuple[str, str]:
    """Generate a WireGuard keypair via `wg`. Returns (private, public),
    base64. The private key is held only in memory here."""
    priv = subprocess.run(["wg", "genkey"], check=True,
                          capture_output=True, text=True).stdout.strip()
    pub = subprocess.run(["wg", "pubkey"], check=True, input=priv + "\n",
                         capture_output=True, text=True).stdout.strip()
    return priv, pub


def _real_store_key(name: str, private_key: str, *,
                    pin_selinux: str = "") -> None:
    """Store the private key in qdistro-pwd, pinned to root (the
    session-manager). pin_uid=0 is the always-on gate; pin_selinux additionally
    requires the caller to run in the session-manager's SELinux domain
    (SESSION_MANAGER_SELINUX_LABEL, Opt 3-B) — pass it once that module is
    installed to tighten custody beyond "any root process"."""
    import dbus  # local import: optional dependency
    bus = dbus.SystemBus()
    proxy = bus.get_object(PWD_BUS_NAME, PWD_OBJ_PATH)
    # AddItem(vault, tag, value, pin_app_exe, pin_selinux, pin_uid)
    proxy.AddItem(WG_PWD_VAULT, _wg_key_tag(name), private_key,
                  "", pin_selinux, "0", dbus_interface=PWD_BUS_NAME)


def _real_write_conf(name: str, conf_text: str) -> Path:
    WG_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = WG_CONFIG_DIR / f"{name}.conf"
    # 0644: non-secret, read by the root session-manager at silo start.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, conf_text.encode())
    finally:
        os.close(fd)
    return path


def provision(name: str, *, peer_public_key: str, endpoint: str, address: str,
              dns: str | None = None, allowed_ips: str = "0.0.0.0/0, ::/0",
              keepalive: int | None = 25, pin_selinux: str = "",
              keygen=_real_keygen, store_key=None, write_conf=None) -> str:
    """Provision tunnel `<name>`: generate the silo end's keypair, store its
    private key in pwd, write the non-secret conf. Returns the silo end's
    PUBLIC key (register it with the VPN provider). Deps are injectable."""
    if not _TUNNEL_NAME_RE.match(name) or ".." in name:
        raise ValueError(
            f"invalid tunnel name {name!r} (lowercase alnum/_/-, "
            f"must start alnum, <=31 chars)")
    if store_key is None:
        store_key = lambda n, k: _real_store_key(n, k, pin_selinux=pin_selinux)
    if write_conf is None:
        write_conf = _real_write_conf
    private_key, public_key = keygen()
    # Store the secret FIRST: if the conf landed but the key store failed, the
    # session-manager would try (and fail) to bring the tunnel up. Storing the
    # key first means a later conf failure leaves an orphan key (harmless) not
    # an orphan conf (a silo that can never come up live).
    store_key(name, private_key)
    conf = render_tunnel_conf(
        peer_public_key=peer_public_key, endpoint=endpoint, address=address,
        dns=dns, allowed_ips=allowed_ips, keepalive=keepalive)
    write_conf(name, conf)
    return public_key


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="qdistro-wg-provision",
        description="Provision a per-silo WireGuard tunnel (task 3).")
    ap.add_argument("name", help="tunnel name (referenced as egress wg:<name>)")
    ap.add_argument("--peer-public-key", required=True,
                    help="the VPN server's public key")
    ap.add_argument("--endpoint", required=True, help="server host:port")
    ap.add_argument("--address", required=True,
                    help="silo-side tunnel address, e.g. 10.7.0.2/32")
    ap.add_argument("--dns", default=None, help="tunnel-side resolver IP")
    ap.add_argument("--allowed-ips", default="0.0.0.0/0, ::/0")
    ap.add_argument("--keepalive", type=int, default=25)
    ap.add_argument("--pin-selinux", default="",
                    help="optional SELinux label pin for key retrieval")
    ap.add_argument("--pin-session-manager", action="store_true",
                    help="pin the key to the session-manager SELinux domain "
                         f"({SESSION_MANAGER_SELINUX_LABEL}); requires the "
                         "selinux/session_manager module to be installed")
    args = ap.parse_args(argv)
    if args.pin_session_manager:
        if args.pin_selinux and args.pin_selinux != SESSION_MANAGER_SELINUX_LABEL:
            print("error: --pin-session-manager conflicts with an explicit "
                  "--pin-selinux", file=sys.stderr)
            return 1
        args.pin_selinux = SESSION_MANAGER_SELINUX_LABEL
    if os.geteuid() != 0:
        print("qdistro-wg-provision must run as root (stores the key pinned "
              "to root and writes /etc/qdistro/wg)", file=sys.stderr)
        return 1
    if not args.pin_selinux:
        # uid=0 alone means "any root process may read the key". Root is TCB, so
        # this is acceptable, but the session-manager SELinux domain
        # (selinux/session_manager, Opt 3-B) tightens it to that one domain.
        # Warn so the operator knows the stronger pin is available.
        print("WARNING: storing the key pinned to uid=0 only (any root process "
              "can read it). Pass --pin-session-manager to tighten to the "
              "session-manager SELinux domain (install selinux/session_manager "
              "first).", file=sys.stderr)
    pub = provision(
        args.name, peer_public_key=args.peer_public_key,
        endpoint=args.endpoint, address=args.address, dns=args.dns,
        allowed_ips=args.allowed_ips, keepalive=args.keepalive,
        pin_selinux=args.pin_selinux)
    print(f"tunnel {args.name!r} provisioned. Register this PUBLIC key with "
          f"the VPN provider:\n{pub}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

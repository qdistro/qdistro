"""Real qci ``VMBackend`` for :func:`scenario.run_viewer_slice`.

Codifies the live two-VM apparatus proven in session 2 (PLAN A, codex impl-4):
VM-A runs a dedicated headless qdwin + the shipped per-view RDP path; VM-B decodes
on its own DRM head under a kiosk-shell weston; the decoded head is captured
host-side via ``virsh screenshot`` (QMP). RDP bytes chain over two SLIRP NATs
meeting at host loopback (a one-time QMP ``hostfwd_add``). See ``vm/README.md`` and
``vm/{source,decoder}-stack.sh`` for the proven shell.

The orchestration in :func:`scenario.run_viewer_slice` is mock-validated; this is
the thin adapter that runs the *same* logic against real VMs. Protocol-touching
methods shell out (``scripts/vm/vm-exec``, ``virsh``); the parsing/argv logic that
is worth pinning is factored into pure module functions with unit tests.

Lifecycle note: this backend wraps **pre-provisioned** VMs (spin maps a logical
name to a real domain and ensures it is running; destroy stops the per-run units
but does NOT undefine the domain — the caller owns VM teardown).
"""
from __future__ import annotations

import base64
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..bridge import ViewStreamApproved

# ---------------------------------------------------------------------------
# pure helpers (unit-tested; no subprocess)
# ---------------------------------------------------------------------------
_APPROVED_RE = {
    "rdp_port": re.compile(r"^RDP_PORT=(\d+)", re.M),
    "password": re.compile(r"^RDP_PASSWORD=(\S+)", re.M),
    "pw_node": re.compile(r"^PIPEWIRE_NODE_NAME=(\S+)", re.M),
}


def parse_approved(bystander_out: str) -> dict:
    """Extract the approved endpoint from ``qdwin-bystander`` stdout.

    Returns a dict with int ``rdp_port`` and str ``password``/``pw_node``. Raises
    if the stream was never approved (no ``RDP_PORT=`` line)."""
    m = _APPROVED_RE["rdp_port"].search(bystander_out)
    if not m:
        raise ValueError("no RDP_PORT in bystander output (stream not approved)")
    pw = _APPROVED_RE["password"].search(bystander_out)
    node = _APPROVED_RE["pw_node"].search(bystander_out)
    return {
        "rdp_port": int(m.group(1)),
        "password": pw.group(1) if pw else "",
        "pw_node": node.group(1) if node else "",
    }


def hostfwd_add_hmp(netdev: str, port: int, host_addr: str = "127.0.0.1") -> str:
    """The QMP/HMP command that exposes guest ``port`` on host ``host_addr:port``
    so the peer VM reaches it via 10.0.2.2 over its own SLIRP NAT."""
    return f"hostfwd_add {netdev} tcp:{host_addr}:{port}-:{port}"


def is_marker_argv(argv: list[str]) -> bool:
    return bool(argv) and argv[0] == "qdwin-marker-client"


def arg_value(argv: list[str], flag: str, default: str | None = None) -> str | None:
    """Value following ``flag`` in an argv list (``--generation 7`` -> '7')."""
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
    return default


# ---------------------------------------------------------------------------
# the backend
# ---------------------------------------------------------------------------
@dataclass
class QciVMBackend:
    """Drives the proven apparatus for ``run_viewer_slice``.

    ``vm_a``/``vm_b`` are the real libvirt domain names; ``run_viewer_slice``
    addresses them by the logical names in its ``Topology`` (default "vm-a"/"vm-b")
    which we map here. Scripts are pushed from the repo (``vm/{source,decoder}-stack.sh``).
    """

    vm_a: str                                   # real domain for logical vm-a
    vm_b: str                                   # real domain for logical vm-b
    repo_dir: Path                              # qdistro/ (for scripts/vm/vm-exec)
    libvirt_uri: str = "qemu:///session"
    relay_port: int = 5555
    netdev: str = "hostnet0"
    out_w: int = 1280
    out_h: int = 800
    logical_a: str = "vm-a"                     # logical name used by Topology
    logical_b: str = "vm-b"
    _logical: dict = field(default_factory=dict)   # logical -> real
    _approved: ViewStreamApproved | None = None

    def __post_init__(self):
        # map the Topology's logical names onto the real libvirt domains.
        self._logical = {self.logical_a: self.vm_a, self.logical_b: self.vm_b}

    # ---- low-level -------------------------------------------------------
    def _real(self, name: str) -> str:
        return self._logical.get(name, name)

    def _vmexec(self, vm: str, command: str, timeout: int = 300) -> str:
        # vm-exec relays the guest command's output across stdout+stderr; merge
        # them (as a manual `2>&1` would) so parsing sees the guest's stdout.
        out = subprocess.run(
            [str(self.repo_dir / "scripts/vm/vm-exec"), vm, command],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=timeout)
        return out.stdout

    def _virsh(self, *args: str, timeout: int = 60) -> str:
        return subprocess.run(
            ["virsh", "-c", self.libvirt_uri, *args],
            capture_output=True, text=True, timeout=timeout).stdout

    def _push(self, vm: str, local: Path, guest: str) -> None:
        b64 = base64.b64encode(local.read_bytes()).decode()
        self._vmexec(vm, f"printf '%s' '{b64}' | base64 -d > {guest}; chmod 0644 {guest}")

    def _as_admin(self, body: str) -> str:
        # run BODY as the admin uid with the session bus + runtime dir wired.
        return ("runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 "
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus " + body)

    # ---- VMBackend protocol ---------------------------------------------
    def spin(self, name: str) -> str:
        real = self._real(name)
        # ensure running (idempotent).
        self._virsh("start", real)
        if real == self.vm_a:
            # one-time SLIRP hostfwd so vm-b reaches the relay via host loopback.
            # idempotent: a second add fails ("already in use") — that is fine.
            self._virsh("qemu-monitor-command", real, "--hmp",
                        hostfwd_add_hmp(self.netdev, self.relay_port))
        return real

    def exec(self, vm: str, argv: list[str]) -> str:
        real = self._real(vm)
        if is_marker_argv(argv):
            # bring up the whole VM-A source stack (qdwin + marker + bystander +
            # relay). subscribe_view_stream reads its result.
            self._push(real, self.repo_dir / "multimachine/harness/vm/source-stack.sh",
                       "/tmp/mm-source-stack.sh")
            gen = arg_value(argv, "--generation", "1")
            env = f"W={self.out_w} H={self.out_h} GEN={gen} FS=1 RELAY_PORT={self.relay_port}"
            return self._vmexec(real, self._as_admin(
                f"{env} bash /tmp/mm-source-stack.sh"), timeout=180)
        # generic exec (e.g. the source-survival pgrep on teardown).
        return self._vmexec(real, " ".join(argv))

    def subscribe_view_stream(self, vm: str, handle: int) -> ViewStreamApproved:
        real = self._real(vm)
        # the source stack restarts the bystander; tolerate a brief race where
        # bystander.out is mid-rewrite by retrying until RDP_PORT appears.
        info = None
        for _ in range(20):
            out = self._vmexec(real, self._as_admin(
                "cat /run/user/1000/bystander.out 2>/dev/null"))
            try:
                info = parse_approved(out)
                break
            except ValueError:
                time.sleep(0.5)
        if info is None:
            raise RuntimeError("subscribe: no approved stream in bystander.out")
        # the viewer reaches the relay (not the dynamic port) via host loopback;
        # expose relay_port as the rdp_port the bridge builds the client argv from.
        self._approved = ViewStreamApproved(
            info["pw_node"], self.relay_port, "", info["password"])
        return self._approved

    def screenshot(self, vm: str, screen: int, dest: Path) -> Path:
        real = self._real(vm)
        if self._approved is None:
            raise RuntimeError("screenshot before subscribe_view_stream")
        self._push(real, self.repo_dir / "multimachine/harness/vm/decoder-stack.sh",
                   "/tmp/mm-decoder-stack.sh")
        otp = self._approved.rdp_password
        self._vmexec(real, f"OTP={otp} W={self.out_w} H={self.out_h} "
                     f"bash /tmp/mm-decoder-stack.sh", timeout=120)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._virsh("screenshot", real, "--screen", str(screen), str(dest))
        return dest

    def apply_netem(self, vm: str, dev: str, profile_name: str) -> None:
        # NB (codex impl-4): the loopback relay leg bypasses this; netem here
        # models link impairment on the SLIRP-facing dev, it is not a bridged link.
        from .netem import profile
        prof = profile(profile_name)
        self._vmexec(self._real(vm), " ".join(prof.tc_add(dev)))

    def clear_netem(self, vm: str, dev: str) -> None:
        self._vmexec(self._real(vm), f"tc qdisc del dev {dev} root 2>/dev/null || true")

    def destroy(self, vm: str) -> None:
        # stop the per-run units; leave the domain defined+running for reuse.
        real = self._real(vm)
        if real == self.vm_a:
            self._vmexec(real, self._as_admin(
                "systemctl --user stop mm-qdwin mm-marker mm-bystander mm-relay 2>/dev/null || true"))
        else:
            self._vmexec(real, "systemctl stop mm-weston mm-viewer 2>/dev/null || true")

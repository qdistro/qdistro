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
import shlex
import subprocess
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


def hostfwd_present(usernet: str, port: int, host_addr: str = "127.0.0.1") -> bool:
    """True if ``info usernet`` already lists a TCP host-forward on ``port``.

    ``info usernet`` formats each rule as a columnar row, e.g.::

        TCP[HOST_FORWARD] 138       127.0.0.1  5555       10.0.2.15  5555 0 0

    so the port is a bare whitespace-delimited field — NOT ``:5555``. The earlier
    ``":%d" in net`` check never matched this format, so a pre-existing rule was
    neither detected nor re-addable (it raised on the duplicate); found by the
    session-3 live ``run_viewer_slice`` re-validation."""
    p = str(port)
    for line in usernet.splitlines():
        if "HOST_FORWARD" not in line:
            continue
        fields = line.split()
        if p in fields and (host_addr in fields or host_addr == ""):
            return True
    return False


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
    # VM-A-served mm-control (impl-12) binds its OWN dedicated port, distinct from
    # the host-served ControlServer's 5556 (used by the host-served managed +
    # input-confinement slices). They never run together, but the VM-A control port
    # is exposed by a persistent QEMU hostfwd on host loopback — a shared 5556 would
    # collide with any host-served ControlServer (incl. unit tests) on the same host.
    control_port: int = 5557
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

    def _vmexec(self, vm: str, command: str, timeout: int = 300,
                check: bool = True) -> str:
        # vm-exec relays the guest command's output across stdout+stderr; merge
        # them (as a manual `2>&1` would) so parsing sees the guest's stdout.
        # check=True (default): a non-zero guest exit raises — a failed setup step
        # must never quietly let a later step "pass" (codex impl-6 H1). Use
        # check=False only for inherently-tolerant probes (pgrep, stop-units).
        out = subprocess.run(
            [str(self.repo_dir / "scripts/vm/vm-exec"), vm, command],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=timeout)
        if check and out.returncode != 0:
            raise RuntimeError(
                f"vm-exec {vm} failed (rc={out.returncode}): {command}\n{out.stdout}")
        return out.stdout

    def _virsh(self, *args: str, timeout: int = 60, check: bool = True) -> str:
        out = subprocess.run(
            ["virsh", "-c", self.libvirt_uri, *args],
            capture_output=True, text=True, timeout=timeout)
        if check and out.returncode != 0:
            raise RuntimeError(
                f"virsh {' '.join(args)} failed (rc={out.returncode}): "
                f"{out.stderr.strip() or out.stdout.strip()}")
        return out.stdout

    def _push(self, vm: str, local: Path, guest: str) -> None:
        b64 = base64.b64encode(local.read_bytes()).decode()
        g = shlex.quote(guest)
        self._vmexec(vm, f"printf '%s' '{b64}' | base64 -d > {g} && chmod 0644 {g}")

    def _guest_link_dev(self, vm: str) -> str:
        """The guest's default-route NIC (e.g. ens2) — the configured
        ``link_dev`` ('eth0') is often wrong for this image (codex impl-6 M6)."""
        out = self._vmexec(vm, "ip -o route get 10.0.2.2 2>/dev/null "
                           "| sed -n 's/.* dev \\([^ ]*\\).*/\\1/p' | head -1")
        dev = out.strip().splitlines()[-1].strip() if out.strip() else ""
        return dev or "eth0"

    def _as_admin(self, body: str) -> str:
        # run BODY as the admin uid with the session bus + runtime dir wired.
        return ("runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 "
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus " + body)

    # ---- VMBackend protocol ---------------------------------------------
    def spin(self, name: str) -> str:
        real = self._real(name)
        self._virsh("start", real, check=False)   # tolerate "already active"
        # confirm it is actually running (start may have failed for real).
        st = self._virsh("domstate", real)
        if "running" not in st:
            raise RuntimeError(f"{real}: not running after spin ({st.strip()})")
        if real == self.vm_a:
            self._ensure_hostfwd(real)
        return real

    def _ensure_hostfwd(self, vm: str, port: int | None = None) -> None:
        """Add the SLIRP hostfwd for ``port`` (default the RDP relay) only if an
        exact-matching rule isn't already present; any OTHER QMP failure is fatal
        (codex impl-6 M5 — don't treat every error as the benign 'already in
        use'). Used for both the RDP relay (5555) and the VM-A-served mm-control
        port (5556, impl-12) — symmetric host-loopback bridging."""
        port = self.relay_port if port is None else port
        net = self._virsh("qemu-monitor-command", vm, "--hmp", "info usernet")
        if hostfwd_present(net, port):
            return                                  # a forward on the port exists
        out = self._virsh("qemu-monitor-command", vm, "--hmp",
                          hostfwd_add_hmp(self.netdev, port), check=False)
        # re-query: success means the rule now exists.
        net = self._virsh("qemu-monitor-command", vm, "--hmp", "info usernet")
        if not hostfwd_present(net, port):
            raise RuntimeError(f"hostfwd_add did not install :{port}: {out}")

    def exec(self, vm: str, argv: list[str]) -> str:
        real = self._real(vm)
        if is_marker_argv(argv):
            # bring up the whole VM-A source stack (qdwin + marker + bystander +
            # relay). subscribe_view_stream reads its result.
            self._push(real, self.repo_dir / "multimachine/harness/vm/source-stack.sh",
                       "/tmp/mm-source-stack.sh")
            gen = int(arg_value(argv, "--generation", "1"))
            # keep the marker ANIMATING: a static surface lets the output dim
            # (no repaints → the barcode darkens, ~½ brightness, CRC fails); an
            # animating marker stays bright. Capture tearing is handled by the
            # scenario's capture-retry (it re-captures until the oracle decodes).
            env = (f"W={self.out_w} H={self.out_h} GEN={gen} FS=1 "
                   f"RELAY_PORT={self.relay_port}")
            out = self._vmexec(real, self._as_admin(
                f"{env} bash /tmp/mm-source-stack.sh"), timeout=180)
            if "SETUP_OK" not in out:               # explicit success token (H1)
                raise RuntimeError(f"source-stack did not report SETUP_OK:\n{out}")
            # bind the approval to THIS run from the same SETUP_OK output, not a
            # possibly-stale bystander.out (codex impl-6 H2).
            self._approved = self._parse_setup(out, gen)
            return out
        # generic exec (e.g. the source-survival pgrep on teardown) — tolerant.
        return self._vmexec(real, " ".join(shlex.quote(a) for a in argv),
                            check=False)

    def _parse_setup(self, source_stack_out: str, gen: int) -> ViewStreamApproved:
        info = parse_approved(source_stack_out)
        if not info["password"]:
            raise RuntimeError("source-stack approval has empty RDP_PASSWORD")
        # viewer reaches the relay (not the dynamic port) via host loopback.
        return ViewStreamApproved(info["pw_node"], self.relay_port, "",
                                  info["password"])

    def subscribe_view_stream(self, vm: str, handle: int) -> ViewStreamApproved:
        # exec() already brought up the source stack and bound the approval from
        # this run's SETUP_OK output. Just return it (no stale-file re-read).
        if self._approved is None:
            raise RuntimeError("subscribe before exec(marker) / source-stack")
        return self._approved

    def screenshot(self, vm: str, screen: int, dest: Path) -> Path:
        real = self._real(vm)
        if self._approved is None:
            raise RuntimeError("screenshot before subscribe_view_stream")
        self._push(real, self.repo_dir / "multimachine/harness/vm/decoder-stack.sh",
                   "/tmp/mm-decoder-stack.sh")
        otp = shlex.quote(self._approved.rdp_password)
        out = self._vmexec(real, f"OTP={otp} W={self.out_w} H={self.out_h} "
                           f"bash /tmp/mm-decoder-stack.sh", timeout=120)
        if "VMB_SETUP_OK" not in out:               # decoder really came up (H1)
            raise RuntimeError(f"decoder-stack did not report VMB_SETUP_OK:\n{out}")
        return self.capture(vm, screen, dest)

    def capture(self, vm: str, screen: int, dest: Path) -> Path:
        """Host-side ``virsh screenshot`` ONLY (no decoder/viewer bring-up). The
        managed-toplevel gate launches the viewer separately via
        :meth:`launch_viewer`, so it must capture the already-live VM-B head
        without re-running decoder-stack (which would fight that viewer)."""
        real = self._real(vm)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()                            # never evaluate a stale capture
        self._virsh("screenshot", real, "--screen", str(screen), str(dest))
        if not dest.exists() or dest.stat().st_size == 0:
            raise RuntimeError(f"virsh screenshot produced no image at {dest}")
        return dest

    # ---- managed-viewer ops (scenario-2, codex impl-9) ------------------
    def _push_mm_package(self, vm: str, guest_dir: str = "/tmp/mm") -> None:
        """Push the minimal ``multimachine`` package the guest viewer (VM-B) and
        ``mm-control`` (VM-A) import (``__init__``/``sidechannel``/``bridge``/
        ``viewer``/``control_source`` — no ``generation``) so ``python3 -m
        multimachine.viewer`` and ``... .control_source`` run in-guest."""
        pkg = self.repo_dir / "multimachine"
        self._vmexec(vm, f"mkdir -p {shlex.quote(guest_dir)}/multimachine")
        for mod in ("__init__.py", "sidechannel.py", "bridge.py", "viewer.py",
                    "control_source.py"):
            self._push(vm, pkg / mod, f"{guest_dir}/multimachine/{mod}")

    def launch_viewer(self, vm: str, *, control_host: str, control_port: int,
                      rdp_host: str, rdp_port: int, generation: int, otp: str,
                      size: str, status_file: str) -> None:
        """Bring up the VM-B managed-viewer stack: kiosk weston + the real
        ``mm-viewer-launch`` connecting to the host control side-channel (impl-9
        Q2). It decodes (fullscreen) only once the host sends Announce."""
        real = self._real(vm)
        self._push_mm_package(real)
        self._push(real, self.repo_dir / "multimachine/harness/vm/viewer-stack.sh",
                   "/tmp/mm-viewer-stack.sh")
        env = (f"CONTROL_HOST={shlex.quote(control_host)} CONTROL_PORT={control_port} "
               f"RDP_HOST={shlex.quote(rdp_host)} RDP_PORT={rdp_port} "
               f"GEN={generation} OTP={shlex.quote(otp)} "
               f"W={self.out_w} H={self.out_h} RDP_USER=mm MMDIR=/tmp/mm "
               f"STATUS_FILE={shlex.quote(status_file)}")
        out = self._vmexec(real, f"{env} bash /tmp/mm-viewer-stack.sh", timeout=120)
        if "VMB_VIEWER_OK" not in out:
            raise RuntimeError(f"viewer-stack did not report VMB_VIEWER_OK:\n{out}")
        self._viewer_status_file = status_file
        self._freerdp_log = "/run/mm-b/freerdp.log"

    def await_decode(self, vm: str, timeout: int = 25) -> bool:
        """Wait until the launched ``sdl-freerdp`` has actually negotiated the
        decoded channel and rendered, so the capture is a real frame, not a black
        pre-first-frame head. The viewer status flips to ``connected`` on Announce
        (before pixels flow), so a capture right after that races the first frame
        (observed: a blank ``\\x00\\x00`` capture). Mirrors the proven decoder-stack
        readiness: grep the freerdp log for the rdpgfx channel, then settle."""
        import time
        log = getattr(self, "_freerdp_log", "/run/mm-b/freerdp.log")
        real = self._real(vm)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self._vmexec(
                real, f"grep -c 'Loading Dynamic Virtual Channel rdpgfx' "
                f"{shlex.quote(log)} 2>/dev/null || true", check=False)
            if out.strip() and out.strip().splitlines()[-1].strip() not in ("0", ""):
                time.sleep(2)                        # settle one repaint, as proven
                return True
            time.sleep(0.5)
        return False

    def viewer_status(self, vm: str) -> dict:
        """Read + parse the viewer's machine-readable status file from the guest
        (empty dict if absent — e.g. before it is written)."""
        import json
        sf = getattr(self, "_viewer_status_file", "/run/mm-b/viewer-status.json")
        out = self._vmexec(self._real(vm), f"cat {shlex.quote(sf)} 2>/dev/null",
                          check=False)
        out = out.strip()
        if not out:
            return {}
        try:
            return json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            return {}

    def stop_viewer(self, vm: str) -> None:
        """Viewer-side close: stop the mm-viewer unit (kills the python + its
        sdl-freerdp child in the unit cgroup), tearing down the RDP client."""
        self._vmexec(self._real(vm),
                     "systemctl stop mm-viewer 2>/dev/null || true", check=False)

    def resubscribe(self, vm: str) -> ViewStreamApproved | None:
        """Run the source-stack ``MODE=resubscribe`` path on VM-A: a fresh
        bystander export (new dynamic port + fresh single-use OTP) repointed onto
        the fixed relay. Returns the fresh approval, or None if not approved."""
        real = self._real(vm)
        self._push(real, self.repo_dir / "multimachine/harness/vm/source-stack.sh",
                   "/tmp/mm-source-stack.sh")
        env = (f"MODE=resubscribe W={self.out_w} H={self.out_h} "
               f"RELAY_PORT={self.relay_port}")
        out = self._vmexec(real, self._as_admin(
            f"{env} bash /tmp/mm-source-stack.sh"), timeout=120, check=False)
        if "SETUP_OK" not in out:
            return None
        try:
            fresh = self._parse_setup(out, 0)
        except RuntimeError:
            return None
        self._approved = fresh
        return fresh

    def source_alive(self, vm: str) -> bool:
        """True if the VM-A marker (the source toplevel app) is still running."""
        out = self._vmexec(self._real(vm), "pgrep -f qdwin-marker-client",
                          check=False)
        return bool(out.strip())

    # ---- VM-A-served control ops (scenario-2 product shape, codex impl-12) --
    _MM_CONTROL_LOG = "/run/user/1000/mm-control.jsonl"
    _MM_CONTROL_STATUS = "/run/user/1000/mm-control-status.json"

    def launch_control(self, vm: str, *, generation: int, window_id: int,
                       source_machine: str, title: str, app_id: str,
                       req_w: int, req_h: int, marker_unit: str = "mm-marker",
                       ) -> str:
        """Start the VM-A ``mm-control`` unit (impl-12): the control side-channel
        now ORIGINATES in VM-A, not on the host. Pushes the ``multimachine`` pkg +
        adds the SLIRP hostfwd for the control port (mirroring the RDP relay), then
        runs ``python3 -m multimachine.control_source`` as a ``systemd --user``
        unit (so its ``systemctl --user show mm-marker`` source-death probe sees the
        marker's unit). Returns the in-guest-minted ``stream_id``.

        The viewer reaches it at ``10.0.2.2:control_port`` over VM-B's own NAT →
        host loopback → this VM-A hostfwd → mm-control. No host ``ControlServer``.

        The stream_id + terminal reason are read from a FILE the unit writes
        (``--status-file``), not the unit's journal — robust against the
        ``--collect`` transient unit being reaped (codex impl-13)."""
        import time
        real = self._real(vm)
        self._push_mm_package(real)
        self._ensure_hostfwd(real, self.control_port)        # control port bridge
        # fresh log + status + reusable unit name across relaunches (so a stale
        # status file can never be mistaken for THIS launch).
        self._vmexec(real, self._as_admin(
            "systemctl --user stop mm-control 2>/dev/null; "
            "systemctl --user reset-failed mm-control 2>/dev/null; "
            f"rm -f {self._MM_CONTROL_LOG} {self._MM_CONTROL_STATUS}; true"),
            check=False)
        argv = (
            "python3 -m multimachine.control_source "
            f"--port {self.control_port} --generation {generation} "
            f"--window-id {window_id} "
            f"--source-machine {shlex.quote(source_machine)} "
            f"--title {shlex.quote(title)} --app-id {shlex.quote(app_id)} "
            f"--req-w {req_w} --req-h {req_h} --marker-unit {shlex.quote(marker_unit)} "
            f"--emit-log {self._MM_CONTROL_LOG} "
            f"--status-file {self._MM_CONTROL_STATUS}")
        out = self._vmexec(real, self._as_admin(
            "systemd-run --user --collect --unit=mm-control "
            "--setenv=PYTHONPATH=/tmp/mm "
            f"{argv}"), check=False)
        if "Running as unit" not in out and "mm-control.service" not in out:
            raise RuntimeError(f"systemd-run did not start mm-control:\n{out}")
        # wait for the unit to publish its listening status file (the bind succeeded
        # + the in-guest-minted stream_id is available).
        for _ in range(40):
            st = self._read_control_status(real)
            if st.get("state") == "listening" and st.get("stream_id"):
                return st["stream_id"]
            time.sleep(0.25)
        raise RuntimeError("mm-control never published a listening status file")

    def _read_control_status(self, real: str) -> dict:
        import json
        raw = self._vmexec(real, self._as_admin(
            f"cat {self._MM_CONTROL_STATUS} 2>/dev/null"), check=False).strip()
        if not raw:
            return {}
        try:
            return json.loads(raw.splitlines()[-1])
        except (ValueError, IndexError):
            return {}

    def control_log(self, vm: str) -> dict:
        """What VM-A's mm-control PRODUCED: the JSON-lines it sent (the
        source-derived Announce, and a source-driven Closed if the marker died) +
        the watcher's terminal reason. The honesty evidence that the control bytes
        + lifecycle originate in VM-A. Entirely FILE-based (impl-13)."""
        import json
        real = self._real(vm)
        raw = self._vmexec(real, self._as_admin(
            f"cat {self._MM_CONTROL_LOG} 2>/dev/null"), check=False)
        sent = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                sent.append(json.loads(line))
            except ValueError:
                continue
        st = self._read_control_status(real)
        reason = st.get("reason", "") if st.get("state") == "done" else ""
        return {"sent": sent, "reason": reason}

    def stop_control(self, vm: str) -> None:
        self._vmexec(self._real(vm), self._as_admin(
            "systemctl --user stop mm-control 2>/dev/null || true"), check=False)

    def kill_marker(self, vm: str) -> None:
        """Source toplevel death: stop the marker's OWN systemd --user unit so
        mm-control's liveness probe sees it go inactive and emits Closed (impl-12
        source-driven teardown)."""
        self._vmexec(self._real(vm), self._as_admin(
            "systemctl --user stop mm-marker 2>/dev/null || true"), check=False)

    # ---- input-confinement ops (scenario-3, codex impl-10) --------------
    def setup_confinement_source(
        self, vm: str, *, generation: int, width: int, height: int,
        exported_telemetry: str, sentinel_telemetry: str,
        exported_label: str, sentinel_label: str,
        allow_input: int = 1) -> ViewStreamApproved:
        """Bring up the confinement source on VM-A: the EXPORTED marker (fullscreen,
        subscribed, writing per-seat input telemetry) + a subscribe. The SENTINEL is
        launched separately (:meth:`launch_sentinel`) AFTER the oracle, since a
        visible sentinel overlaps the per-view capture.

        ``allow_input=1`` (default) requests an ``--allow-input`` subscription so the
        forward gets the inject channel (the positive confinement gate). ``0`` is the
        read-only **negative control** (codex impl-11/13): the SAME injection is
        attempted but the server-side permission bit must gate it, so NOTHING
        receives the presses."""
        real = self._real(vm)
        self._sentinel_label = sentinel_label
        self._push(real, self.repo_dir / "multimachine/harness/vm/source-stack.sh",
                   "/tmp/mm-source-stack.sh")
        # qdwin now spawns qdistro-forward with `--wayland-display <its own socket>`
        # (read from WAYLAND_DISPLAY, qdwin.c), so the forward claims the input
        # channel on whatever socket our mm-qdwin listens on. No longer forced onto
        # wayland-0 — the default private SOCK=wayland-mm works (session-5 qdwin fix).
        env = (f"W={width} H={height} GEN={generation} FS=1 ANIMATE_MS=200 "
               f"RELAY_PORT={self.relay_port} ALLOW_INPUT={int(allow_input)} "
               f"EXPORTED_TELEMETRY={shlex.quote(exported_telemetry)} "
               f"EXPORTED_LABEL={shlex.quote(exported_label)}")
        out = self._vmexec(real, self._as_admin(
            f"{env} bash /tmp/mm-source-stack.sh"), timeout=180)
        if "SETUP_OK" not in out:
            raise RuntimeError(f"confinement source-stack did not report SETUP_OK:\n{out}")
        self._approved = self._parse_setup(out, generation)
        return self._approved

    def launch_sentinel(self, vm: str, *, generation: int,
                        sentinel_telemetry: str, sentinel_label: str) -> None:
        """Launch the LOCAL unexported sentinel marker on the live qdwin (MODE=
        sentinel), AFTER the oracle. It must be up + binding seats during injection;
        injected input must never reach it (the confinement detector)."""
        real = self._real(vm)
        # default SOCK=wayland-mm (matches setup_confinement_source; the qdwin
        # WAYLAND_DISPLAY fix removed the wayland-0 coupling).
        env = (f"MODE=sentinel GEN={generation} ANIMATE_MS=200 "
               f"SENTINEL_TELEMETRY={shlex.quote(sentinel_telemetry)} "
               f"SENTINEL_LABEL={shlex.quote(sentinel_label)}")
        out = self._vmexec(real, self._as_admin(
            f"{env} bash /tmp/mm-source-stack.sh"), timeout=60)
        if "SENTINEL_OK" not in out:
            raise RuntimeError(f"sentinel did not start:\n{out}")

    def read_telemetry(self, vm: str, path: str) -> dict:
        """Read + parse a marker's per-seat input telemetry JSON from the guest
        (empty dict if absent/unwritten)."""
        import json
        out = self._vmexec(self._real(vm), f"cat {shlex.quote(path)} 2>/dev/null",
                          check=False).strip()
        if not out:
            return {}
        try:
            return json.loads(out.splitlines()[-1])
        except (ValueError, IndexError):
            return {}

    def inject_input(self, vm: str) -> None:
        """Inject input at the VM-B viewer via ydotool — the HONEST end-to-end path
        (ydotool → kiosk weston seat → sdl-freerdp → RDP → qdistro-forward →
        per-stream seat → exported marker). Ensures ydotoold, lets weston hotplug
        its uinput device, then moves the pointer into the fullscreen surface,
        clicks, and sends a key (codex impl-10 Q2: motion→click→key)."""
        real = self._real(vm)
        # viewer-stack.sh already started OUR ydotoold at this socket BEFORE weston
        # (so weston enumerated the uinput device at startup). Just drive ydotool.
        sock = "/run/.ydotool_socket"
        script = (
            f"export YDOTOOL_SOCKET={sock}; "
            f"[ -S {sock} ] || {{ echo 'NO_YDOTOOL_SOCKET'; exit 1; }}; "
            # center the pointer (relative: large move to a corner, then toward
            # center), click TWICE (a missed-focus first click still leaves a
            # delivered press), and send a key.
            f"ydotool mousemove -- -5000 -5000; sleep 0.3; "
            f"ydotool mousemove -- {self.out_w // 2} {self.out_h // 2}; sleep 0.3; "
            f"ydotool click 0xC0; sleep 0.3; "       # left press+release
            f"ydotool click 0xC0; sleep 0.3; "
            f"ydotool key 30:1 30:0; sleep 0.3; "    # 'a' press+release
            f"echo YDOTOOL_DONE")
        out = self._vmexec(real, script, check=False)
        if "YDOTOOL_DONE" not in out:
            raise RuntimeError(f"ydotool injection failed on {vm}:\n{out}")

    def apply_netem(self, vm: str, dev: str, profile_name: str) -> None:
        # NB (codex impl-4/impl-6 M6): the loopback relay leg BYPASSES this — it
        # models link impairment on the SLIRP-facing dev, NOT a bridged inter-VM
        # link. The configured ``dev`` ('eth0') is wrong for this image, so resolve
        # the guest's real default-route NIC and apply (checked) there.
        from .netem import profile
        real = self._real(vm)
        gdev = self._guest_link_dev(real)
        prof = profile(profile_name)
        self._vmexec(real, "tc qdisc del dev %s root 2>/dev/null; %s"
                     % (shlex.quote(gdev), " ".join(prof.tc_add(gdev))))
        self._netem_dev = gdev

    def clear_netem(self, vm: str, dev: str) -> None:
        gdev = getattr(self, "_netem_dev", None) or self._guest_link_dev(self._real(vm))
        self._vmexec(self._real(vm),
                     f"tc qdisc del dev {shlex.quote(gdev)} root 2>/dev/null || true",
                     check=False)

    def destroy(self, vm: str) -> None:
        # stop the per-run units; leave the domain defined+running for reuse.
        real = self._real(vm)
        if real == self.vm_a:
            self._vmexec(real, self._as_admin(
                "systemctl --user stop mm-qdwin mm-marker mm-sentinel mm-bystander "
                "mm-relay mm-control 2>/dev/null || true"), check=False)
        else:
            self._vmexec(real, "systemctl stop mm-weston mm-viewer 2>/dev/null || true",
                         check=False)

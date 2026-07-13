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
import os
import re
import shlex
import subprocess
import tempfile
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

    def _push(self, vm: str, local: Path, guest: str,
              mode: int = 0o644) -> None:
        b64 = base64.b64encode(local.read_bytes()).decode()
        g = shlex.quote(guest)
        # Separate harness processes may stage to the same pre-provisioned VM.
        # A per-process pathname keeps their atomic renames independent.
        temporary = shlex.quote(
            guest + f".qdistro-push-{os.getpid()}-tmp")
        # vm-exec currently carries the transfer command through QGA argv, so
        # this remains test-apparatus transport rather than a production secret
        # channel.  Stage mode-0600 credentials atomically: there is no window
        # where their final pathname exists with the generic 0644 push mode.
        self._vmexec(
            vm, f"umask 077; rm -f {temporary}; "
            f"printf '%s' '{b64}' | base64 -d > {temporary} && "
            f"chmod {mode:04o} {temporary} && mv -f {temporary} {g}")

    def _push_large(self, vm: str, local: Path, guest: str,
                    mode: int = 0o644, chunk_size: int = 48 * 1024) -> None:
        """Atomically stage a file without exceeding QGA's argv limit.

        ``_push`` is intentionally simple but base64-expands its whole payload
        into one guest command.  Large compositor sources exceed the host's
        exec argument limit before vm-exec can run.  Transfer bounded chunks
        with the existing atomic primitive, append them in order in the guest,
        then publish the completed file with one rename.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        data = local.read_bytes()
        if len(data) <= chunk_size:
            self._push(vm, local, guest, mode=mode)
            return

        guest_tmp = guest + f".qdistro-large-{os.getpid()}-tmp"
        guest_chunk = guest_tmp + ".chunk"
        quoted_tmp = shlex.quote(guest_tmp)
        quoted_chunk = shlex.quote(guest_chunk)
        quoted_guest = shlex.quote(guest)
        self._vmexec(vm, f"umask 077; rm -f {quoted_tmp} {quoted_chunk}")
        with tempfile.TemporaryDirectory(prefix="qdistro-vm-push-") as tmp:
            local_chunk = Path(tmp) / "chunk"
            for offset in range(0, len(data), chunk_size):
                local_chunk.write_bytes(data[offset:offset + chunk_size])
                self._push(vm, local_chunk, guest_chunk, mode=0o600)
                self._vmexec(
                    vm, f"dd if={quoted_chunk} of={quoted_tmp} "
                        "oflag=append conv=notrunc status=none && "
                        f"rm -f {quoted_chunk}")
        self._vmexec(
            vm, f"chmod {mode:04o} {quoted_tmp} && "
                f"mv -f {quoted_tmp} {quoted_guest}")

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
        self._vmexec(
            vm, f"mkdir -p {shlex.quote(guest_dir)}/multimachine/harness")
        for mod in (
                "__init__.py", "sidechannel.py", "bridge.py", "viewer.py",
                "control_source.py", "mm_broker.py", "mm_pairing_authority.py",
                "mm_remote_session_authority.py",
                "mm_remote_session_launcher.py", "mm_session_launcher.py",
                "origin_authority.py", "rdp_client_wrapper.py",
                "remote_adapter.py", "remote_adapter_transport.py",
                "remote_nested_protocol.py", "remote_nested_service.py",
                "remote_nested_supervisor.py"):
            self._push(vm, pkg / mod, f"{guest_dir}/multimachine/{mod}")
        for mod in ("__init__.py", "viewer_broker.py"):
            self._push(
                vm, pkg / "harness" / mod,
                f"{guest_dir}/multimachine/harness/{mod}")

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

    def setup_calibration_probe(self, vm: str, *, generation: int,
                                telemetry: str = "/run/mm-b/calib-probe.json",
                                label: str = "calib") -> str:
        """Bring up the VM-B coordinate-CALIBRATION probe (A2, codex impl-21): the
        SAME kiosk weston + ydotoold recipe as the managed viewer, but a FULLSCREEN
        ``qdwin-marker-client`` with telemetry INSTEAD of sdl-freerdp. The harness
        injects ``ydotool --absolute`` and reads this probe's received coords =
        ``T_apparatus(p)`` — the ydotool→uinput→kiosk-pointer map, measured WITHOUT
        qdistro-forward/RDP/the source in the path. Returns the probe telemetry path.
        Tear it down with :meth:`stop_calibration_probe` BEFORE the product phase."""
        real = self._real(vm)
        self._push(real, self.repo_dir / "multimachine/harness/vm/calib-probe.sh",
                   "/tmp/mm-calib-probe.sh")
        env = (f"W={self.out_w} H={self.out_h} GEN={generation} ANIMATE_MS=200 "
               f"TELEMETRY={shlex.quote(telemetry)} LABEL={shlex.quote(label)}")
        out = self._vmexec(real, f"{env} bash /tmp/mm-calib-probe.sh", timeout=120)
        if "CALIB_OK" not in out:
            raise RuntimeError(f"calib-probe did not report CALIB_OK:\n{out}")
        return telemetry

    def stop_calibration_probe(self, vm: str) -> None:
        """Tear down the calibration probe + its kiosk weston so the product phase
        brings up a FRESH viewer on the SAME geometry (phase isolation: the probe
        must not retain fullscreen/focus while sdl-freerdp maps). WAITS for the units
        to actually go inactive + the wayland socket to disappear before returning so
        the product viewer can't race a lingering compositor (codex impl-22)."""
        real = self._real(vm)
        self._vmexec(
            real,
            "systemctl stop mm-calib mm-weston 2>/dev/null || true; "
            "systemctl reset-failed mm-calib mm-weston 2>/dev/null || true; "
            "for _ in $(seq 1 30); do "
            "  systemctl is-active mm-weston >/dev/null 2>&1 || break; "
            "  sleep 0.2; "
            "done; rm -f /run/mm-b/wayland-b 2>/dev/null; true", check=False)

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

    # ---- Phase-2 rung-1 viewer-qdwin spike ops (codex impl-30 Option B) -----
    def launch_viewer_qdwin(self, vm: str, *, rdp_host: str,
                            port_a: int, otp_a: str, port_b: int, otp_b: str,
                            stream_a: str = "streamA", stream_b: str = "streamB",
                            origin: str = "vm-a") -> str:
        """Bring up the VM-B Phase-2 rung-1 viewer stack: a REAL qdwin
        (shell=qdwin-shell.so) + qdwin-bystander as the bound shell client + TWO
        *windowed* secctx-tagged FreeRDP clients, each decoding one source stream
        into its own managed qdwin toplevel (viewer-qdwin-stack.sh; impl-30 Q6).
        Asserts the VMB_QDWIN_OK token, which proves a WEAK readiness condition:
        the gfx channel loaded on both RDP connections AND both per-stream secctx
        app_ids were observed at least once. It does NOT prove the current LIVE set
        has exactly one toplevel per stream (SDL3 churns toplevels) — that stronger
        check is the caller's job via :meth:`viewer_qdwin_toplevels` (live add−removed
        set). Returns the script's stdout (carries the observed toplevel lines)."""
        real = self._real(vm)
        self._push(real,
                   self.repo_dir / "multimachine/harness/vm/viewer-qdwin-stack.sh",
                   "/tmp/mm-viewer-qdwin-stack.sh")
        env = (f"RDP_HOST={shlex.quote(rdp_host)} "
               f"RDP_PORT_A={int(port_a)} OTP_A={shlex.quote(otp_a)} "
               f"RDP_PORT_B={int(port_b)} OTP_B={shlex.quote(otp_b)} "
               f"STREAM_A={shlex.quote(stream_a)} STREAM_B={shlex.quote(stream_b)} "
               f"ORIGIN={shlex.quote(origin)} W={self.out_w} H={self.out_h} "
               f"RDP_USER=mm")
        out = self._vmexec(real, f"{env} bash /tmp/mm-viewer-qdwin-stack.sh",
                           timeout=240)
        if "VMB_QDWIN_OK" not in out:
            raise RuntimeError(f"viewer-qdwin-stack did not report VMB_QDWIN_OK:\n{out}")
        return out

    def viewer_qdwin_toplevels(self, vm: str) -> dict[int, dict]:
        """Parse the VM-B bound shell client's (qdwin-bystander) observations into
        the LIVE ``handle -> {engine, app_id, instance_id}`` set, from its
        ``toplevel_added`` / ``toplevel_security_context`` / ``toplevel_removed``
        lines. This is the load-bearing handle<->stream attribution (impl-30 Q6:
        secctx, NOT window title / client pixels).

        The SDL3 FreeRDP frontend churns several short-lived toplevels (create →
        destroy) before settling on its final window, so we MUST honour
        ``toplevel_removed`` and return only handles that are currently live —
        otherwise dead transient handles would be mis-attributed as peers."""
        raw = self._vmexec(self._real(vm),
                           "cat /run/mm-vb/bystander.out 2>/dev/null || true",
                           check=False)
        rx_secctx = re.compile(
            r'toplevel_security_context handle=(\d+) engine="([^"]*)" '
            r'app_id="([^"]*)" instance_id="([^"]*)"')
        rx_added = re.compile(r'toplevel_added handle=(\d+) ')
        rx_removed = re.compile(r'toplevel_removed handle=(\d+)')
        live: set[int] = set()
        secctx: dict[int, dict] = {}
        # replay the log in order so adds/removes net out to the live set.
        for line in raw.splitlines():
            m = rx_added.search(line)
            if m:
                live.add(int(m.group(1)))
                continue
            m = rx_removed.search(line)
            if m:
                live.discard(int(m.group(1)))
                continue
            m = rx_secctx.search(line)
            if m:
                secctx[int(m.group(1))] = {
                    "engine": m.group(2), "app_id": m.group(3),
                    "instance_id": m.group(4)}
        return {h: secctx[h] for h in live if h in secctx}

    def viewer_fifo(self, vm: str, cmd: str) -> None:
        """Send one command (e.g. ``raise <handle>``, ``max <handle>``,
        ``focus <handle>``, ``close <handle>``) to the bound shell client's FIFO so
        the harness drives placement/stacking/focus — the viewer shell authority."""
        self._vmexec(self._real(vm),
                     f"printf '%s\\n' {shlex.quote(cmd)} > /tmp/qdwin-cmd.fifo",
                     check=False)

    def viewer_qdwin_log(self, vm: str) -> str:
        """The full bound-shell observation log (toplevel_added +
        toplevel_security_context + FIFO command echoes) — anti-fake evidence."""
        return self._vmexec(self._real(vm),
                            "cat /run/mm-vb/bystander.out 2>/dev/null || true",
                            check=False)

    def viewer_qdwin_geometry(self, vm: str) -> dict[int, tuple[int, int, int, int]]:
        """The LATEST ``toplevel_geometry`` (x, y, w, h) the bound shell observed per
        handle — the AUTHORITATIVE viewer-WM rectangles (qdwin reports them; the
        shell does not invent them). Used by the rung-1 gate to prove independent
        overlapping geometry + that moving A does not perturb B's rect (impl-32
        Q1). Replays the log so the last event per handle wins."""
        raw = self._vmexec(self._real(vm),
                           "cat /run/mm-vb/bystander.out 2>/dev/null || true",
                           check=False)
        rx = re.compile(
            r'toplevel_geometry handle=(\d+) x=(-?\d+) y=(-?\d+) '
            r'w=(\d+) h=(\d+)')
        geo: dict[int, tuple[int, int, int, int]] = {}
        for line in raw.splitlines():
            m = rx.search(line)
            if m:
                geo[int(m.group(1))] = (int(m.group(2)), int(m.group(3)),
                                        int(m.group(4)), int(m.group(5)))
        return geo

    def rdp_client_alive(self, vm: str, which: str) -> bool:
        """True if a specific windowed FreeRDP client unit (``a``/``b``) is live —
        process/peer truth for the per-toplevel lifecycle assertions."""
        unit = "mm-rdp-a" if which == "a" else "mm-rdp-b"
        out = self._vmexec(self._real(vm),
                           f"systemctl is-active {unit} 2>/dev/null", check=False)
        return out.strip() == "active"

    def stop_rdp_client(self, vm: str, which: str) -> None:
        """Stop one windowed FreeRDP client unit (viewer-side teardown of one
        peer's pixel backend)."""
        unit = "mm-rdp-a" if which == "a" else "mm-rdp-b"
        self._vmexec(self._real(vm),
                     f"systemctl stop {unit} 2>/dev/null || true", check=False)

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
                       unit: str = "mm-control", control_port: int | None = None,
                       control_capability: str = "") -> str:
        """Start a VM-A control unit (impl-12): the control side-channel ORIGINATES
        in VM-A, not on the host. Pushes the ``multimachine`` pkg + adds the SLIRP
        hostfwd for the control port (mirroring the RDP relay), then runs
        ``python3 -m multimachine.control_source`` as a ``systemd --user`` unit (so
        its ``systemctl --user show <marker_unit>`` source-death probe sees the
        marker's unit). Returns the in-guest-minted ``stream_id``.

        ``unit`` + ``control_port`` are parameterised so the Phase-2 rung-1 gate can
        run ONE control unit PER stream (``mm-control-a``/``mm-control-b`` on
        5571/5572, watching ``mm-marker``/``mm-marker2``) — codex impl-32 Q4. The
        per-stream status/log files are derived from the unit name so two controls
        never clobber each other's handshake. Defaults reproduce the single-stream
        impl-12 behaviour (``mm-control`` on ``self.control_port``).

        The viewer reaches it at ``10.0.2.2:control_port`` over VM-B's own NAT →
        host loopback → this VM-A hostfwd → the control unit (the host-side viewer
        broker reaches the SAME port over loopback). No host ``ControlServer``.

        The stream_id + terminal reason are read from a FILE the unit writes
        (``--status-file``), not the unit's journal — robust against the
        ``--collect`` transient unit being reaped (codex impl-13)."""
        import time
        real = self._real(vm)
        port = self.control_port if control_port is None else int(control_port)
        status_file, log_file = self._control_paths(unit)
        self._push_mm_package(real)
        self._ensure_hostfwd(real, port)                     # control port bridge
        # fresh log + status + reusable unit name across relaunches (so a stale
        # status file can never be mistaken for THIS launch).
        self._vmexec(real, self._as_admin(
            f"systemctl --user stop {unit} 2>/dev/null; "
            f"systemctl --user reset-failed {unit} 2>/dev/null; "
            f"rm -f {log_file} {status_file}; true"),
            check=False)
        auth_arg = ""
        credential_property = ""
        credential = f"/run/user/1000/{unit}-auth"
        if control_capability:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8") as secret:
                secret.write(control_capability)
                secret.flush()
                self._push(real, Path(secret.name), credential, mode=0o600)
            self._vmexec(real, f"chown admin:admin {shlex.quote(credential)}")
            credential_property = (
                f"--property=LoadCredential=mm-control-auth:{credential} ")
            auth_arg = " --auth-fd 3"
        argv = (
            "python3 -m multimachine.control_source "
            f"--port {port} --generation {generation} "
            f"--window-id {window_id} "
            f"--source-machine {shlex.quote(source_machine)} "
            f"--title {shlex.quote(title)} --app-id {shlex.quote(app_id)} "
            f"--req-w {req_w} --req-h {req_h} --marker-unit {shlex.quote(marker_unit)} "
            f"--emit-log {log_file} "
            f"--status-file {status_file}{auth_arg}")
        command = argv
        if control_capability:
            body = ("exec 3<\"$CREDENTIALS_DIRECTORY/mm-control-auth\"; "
                    f"exec {argv}")
            command = f"bash -c {shlex.quote(body)}"
        out = self._vmexec(real, self._as_admin(
            f"systemd-run --user --collect --unit={unit} "
            f"{credential_property}"
            "--setenv=PYTHONPATH=/tmp/mm "
            f"{command}"), check=False)
        if "Running as unit" not in out and f"{unit}.service" not in out:
            raise RuntimeError(f"systemd-run did not start {unit}:\n{out}")
        # wait for the unit to publish its listening status file (the bind succeeded
        # + the in-guest-minted stream_id is available).
        for _ in range(40):
            st = self._read_control_status(real, status_file)
            if st.get("state") == "listening" and st.get("stream_id"):
                if control_capability:
                    self._vmexec(real, self._as_admin(
                        f"rm -f {shlex.quote(credential)}"), check=False)
                return st["stream_id"]
            time.sleep(0.25)
        raise RuntimeError(f"{unit} never published a listening status file")

    def _control_paths(self, unit: str) -> tuple[str, str]:
        """The per-unit status + emit-log paths. The default ``mm-control`` keeps
        the historic impl-12/13 paths; any other unit gets unit-qualified files so
        per-stream controls don't clobber each other."""
        if unit == "mm-control":
            return self._MM_CONTROL_STATUS, self._MM_CONTROL_LOG
        base = f"/run/user/1000/{unit}"
        return f"{base}-status.json", f"{base}.jsonl"

    def _read_control_status(self, real: str, status_file: str | None = None) -> dict:
        import json
        path = status_file or self._MM_CONTROL_STATUS
        raw = self._vmexec(real, self._as_admin(
            f"cat {path} 2>/dev/null"), check=False).strip()
        if not raw:
            return {}
        try:
            return json.loads(raw.splitlines()[-1])
        except (ValueError, IndexError):
            return {}

    def control_log(self, vm: str, unit: str = "mm-control") -> dict:
        """What a VM-A control unit PRODUCED: the JSON-lines it sent (the
        source-derived Announce, and a source-driven Closed if the marker died) +
        the watcher's terminal reason. The honesty evidence that the control bytes
        + lifecycle originate in VM-A. Entirely FILE-based (impl-13)."""
        import json
        real = self._real(vm)
        status_file, log_file = self._control_paths(unit)
        raw = self._vmexec(real, self._as_admin(
            f"cat {log_file} 2>/dev/null"), check=False)
        sent = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                sent.append(json.loads(line))
            except ValueError:
                continue
        st = self._read_control_status(real, status_file)
        reason = st.get("reason", "") if st.get("state") == "done" else ""
        return {"sent": sent, "reason": reason}

    def stop_control(self, vm: str, unit: str = "mm-control") -> None:
        self._vmexec(self._real(vm), self._as_admin(
            f"systemctl --user stop {unit} 2>/dev/null || true"), check=False)

    def marker2_alive(self, vm: str) -> bool:
        """True if the SECOND exported marker (mm-marker2) + its relay (mm-relay2)
        are both still live — the liveness witness the 2nd-view isolation gate needs
        so a stale telemetry file can't make marker-B's zero-delta vacuous (codex
        impl-16). forward-B is a qdwin child (not a unit); mm-relay2 carrying its RDP
        port + mm-marker2 running is the reachable witness, backed by a Phase-B2
        re-injection proof in the slice."""
        out = self._vmexec(self._real(vm), self._as_admin(
            "systemctl --user is-active mm-marker2 mm-relay2 2>/dev/null"
            " | tr '\\n' ' '"), check=False)
        return out.split().count("active") >= 2

    def kill_marker(self, vm: str) -> None:
        """Source toplevel death: stop the marker's OWN systemd --user unit so
        mm-control's liveness probe sees it go inactive and emits Closed (impl-12
        source-driven teardown)."""
        self._vmexec(self._real(vm), self._as_admin(
            "systemctl --user stop mm-marker 2>/dev/null || true"), check=False)

    # ---- forward-death watch ops (item 5, codex impl-26) ----------------
    def forward_pids(self, vm: str) -> dict[int, int]:
        """Map ``rdp_port -> pid`` for every live ``qdistro-forward`` child of
        qdwin. ``pgrep -af`` prints ``<pid> <cmdline>``; each forward carries
        ``--rdp-port <N>`` so we can attribute a pid to a specific view_stream
        (item 5: kill exactly ONE forward and prove only its stream tears down)."""
        out = self._vmexec(self._real(vm), "pgrep -af qdistro-forward",
                          check=False)
        ports: dict[int, int] = {}
        for line in out.splitlines():
            toks = line.split()
            if not toks or not toks[0].isdigit():
                continue
            pid = int(toks[0])
            if "--rdp-port" in toks:
                i = toks.index("--rdp-port")
                if i + 1 < len(toks) and toks[i + 1].isdigit():
                    ports[int(toks[i + 1])] = pid
        return ports

    def kill_forward(self, vm: str, pid: int) -> None:
        """SIGKILL one ``qdistro-forward`` child (transport death). qdwin's pidfd
        death-watch must notice and tear down ONLY that view_stream."""
        self._vmexec(self._real(vm), f"kill -9 {int(pid)} 2>/dev/null || true",
                     check=False)

    def pid_reaped(self, vm: str, pid: int) -> bool:
        """True if ``pid`` is fully gone (no live process, no zombie). weston's
        signalfd handler ``waitpid(-1)``-reaps the forward, so after death the pid
        must leave NO ``Z`` zombie behind. ``ps -o stat=`` prints the state code
        (or nothing if the pid is gone); a 'Z' means an unreaped zombie = FAIL."""
        out = self._vmexec(self._real(vm),
                           f"ps -o stat= -p {int(pid)} 2>/dev/null || true",
                           check=False).strip()
        return out == "" or "Z" not in out

    def bystander_log(self, vm: str) -> str:
        """The subscriber (mm-bystander) merged stdout+stderr — where the
        ``view_stream torn_down handle=N reason="..."`` lines land (item 5's
        viewer-visible Closed at the subscribing shell client) alongside the
        per-approval ``HANDLE=``/``RDP_PORT=`` blocks (to map port -> handle)."""
        return self._vmexec(self._real(vm),
                           "cat /run/user/1000/bystander.out 2>/dev/null || true",
                           check=False)

    def qdwin_journal(self, vm: str, tail: int = 80) -> str:
        """The mm-qdwin unit journal — corroborates the COMPOSITOR-side detection
        ('qdistro-forward pid=N exited; tearing down view_stream ...')."""
        return self._vmexec(self._real(vm), self._as_admin(
            f"journalctl --user -u mm-qdwin --no-pager 2>/dev/null | tail -{int(tail)}"),
            check=False)

    def marker_unit_alive(self, vm: str, unit: str = "mm-marker") -> bool:
        """True if a specific exported marker's ``systemd --user`` unit is still
        active (process truth: transport death must NOT kill the source app)."""
        out = self._vmexec(self._real(vm), self._as_admin(
            f"systemctl --user is-active {shlex.quote(unit)} 2>/dev/null"),
            check=False)
        return out.strip() == "active"

    # ---- input-confinement ops (scenario-3, codex impl-10) --------------
    def setup_confinement_source(
        self, vm: str, *, generation: int, width: int, height: int,
        exported_telemetry: str, sentinel_telemetry: str,
        exported_label: str, sentinel_label: str,
        allow_input: int = 1, fault: str = "",
        output_id: int = 1, source_client: str = "marker",
        popup_binary: Path | None = None) -> ViewStreamApproved:
        """Bring up the confinement source on VM-A: the EXPORTED marker (fullscreen,
        subscribed, writing per-seat input telemetry) + a subscribe. The SENTINEL is
        launched separately (:meth:`launch_sentinel`) AFTER the oracle, since a
        visible sentinel overlaps the per-view capture.

        ``allow_input=1`` (default) requests an ``--allow-input`` subscription so the
        forward gets the inject channel (the positive confinement gate). ``0`` is the
        read-only **negative control** (codex impl-11/13): the SAME injection is
        attempted but the server-side permission bit must gate it, so NOTHING
        receives the presses.

        ``source_client="popup"`` runs the persistent real xdg parent+popup R4
        fixture from ``popup_binary`` while retaining the same source-owned unit
        and close lifecycle. ``fault`` (item 6, codex impl-28): if non-empty
        (``transient``/``persistent``),
        qdwin is started with ``QDISTRO_FORWARD_FAULT`` so each spawned forward
        deterministically exercises its bounded-reconnect / give-up path."""
        real = self._real(vm)
        self._sentinel_label = sentinel_label
        # Direct live-gate callers may wrap already-provisioned VMs without
        # calling spin(), whose historical side effect installed this bridge.
        # Make the source setup self-contained: its stable relay port must be
        # reachable from the viewer through the source VM's QEMU host-forward.
        self._ensure_hostfwd(real, self.relay_port)
        self._push(real, self.repo_dir / "multimachine/harness/vm/source-stack.sh",
                   "/tmp/mm-source-stack.sh")
        popup_env = ""
        if source_client == "popup":
            if popup_binary is None:
                raise ValueError("popup source requires popup_binary")
            self._push(real, Path(popup_binary), "/tmp/qdwin-popup-probe")
            self._vmexec(real, "chmod 0755 /tmp/qdwin-popup-probe")
            popup_env = " SOURCE_CLIENT=popup POPUP_BIN=/tmp/qdwin-popup-probe"
        elif source_client != "marker":
            raise ValueError(f"unknown source_client {source_client!r}")
        # qdwin now spawns qdistro-forward with `--wayland-display <its own socket>`
        # (read from WAYLAND_DISPLAY, qdwin.c), so the forward claims the input
        # channel on whatever socket our mm-qdwin listens on. No longer forced onto
        # wayland-0 — the default private SOCK=wayland-mm works (session-5 qdwin fix).
        env = (f"W={width} H={height} GEN={generation} FS=1 ANIMATE_MS=200 "
               f"OUTPUT_ID={int(output_id)} "
               f"RELAY_PORT={self.relay_port} ALLOW_INPUT={int(allow_input)} "
               f"EXPORTED_TELEMETRY={shlex.quote(exported_telemetry)} "
               f"EXPORTED_LABEL={shlex.quote(exported_label)}"
               + popup_env
               + (f" FAULT={shlex.quote(fault)}" if fault else ""))
        out = self._vmexec(real, self._as_admin(
            f"{env} bash /tmp/mm-source-stack.sh"), timeout=180)
        if "SETUP_OK" not in out:
            raise RuntimeError(f"confinement source-stack did not report SETUP_OK:\n{out}")
        self._approved = self._parse_setup(out, generation)
        return self._approved

    def setup_second_export(
        self, vm: str, *, generation: int, width: int, height: int,
        output_id: int, telemetry: str, label: str, relay_port: int,
        allow_input: int = 1) -> ViewStreamApproved:
        """Bring up a SECOND, concurrent EXPORTED marker on the ALREADY-LIVE qdwin
        (2nd-exported-view isolation gate, codex impl-15; MODE=export2). marker-B on
        a DISTINCT output + its own bystander (--allow-input → forward-B claims the
        inject channel ON SPAWN, so marker-B's per-stream seat goes live even before
        any RDP client) + its own relay on ``relay_port`` (a second host-loopback
        bridge). Returns marker-B's approval (the viewer reaches it at the relay
        port). Does NOT clobber the first export's ``self._approved``."""
        real = self._real(vm)
        self._push(real, self.repo_dir / "multimachine/harness/vm/source-stack.sh",
                   "/tmp/mm-source-stack.sh")
        self._ensure_hostfwd(real, relay_port)               # 2nd RDP relay bridge
        env = (f"MODE=export2 W={width} H={height} GEN={generation} FS=1 "
               f"ANIMATE_MS=200 OUTPUT_ID={output_id} RELAY_PORT={relay_port} "
               f"ALLOW_INPUT={int(allow_input)} "
               f"EXPORTED_TELEMETRY={shlex.quote(telemetry)} "
               f"EXPORTED_LABEL={shlex.quote(label)}")
        out = self._vmexec(real, self._as_admin(
            f"{env} bash /tmp/mm-source-stack.sh"), timeout=120)
        if "SETUP_OK" not in out:
            raise RuntimeError(f"export2 source-stack did not report SETUP_OK:\n{out}")
        info = parse_approved(out)
        if not info["password"]:
            raise RuntimeError("export2 approval has empty RDP_PASSWORD")
        return ViewStreamApproved(info["pw_node"], relay_port, "", info["password"])

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

    def setup_claimant_source(
        self, vm: str, *, generation: int, width: int, height: int,
        exported_telemetry: str, sentinel_telemetry: str,
        exported_label: str, sentinel_label: str) -> dict:
        """Bring up the COMPOSITOR-BOUNDARY direct-claimant gate on VM-A (A1,
        session 7; MODE=claimant). qdwin spawns ``qdwin-stream-claimant`` IN PLACE
        of ``qdistro-forward`` (the trusted ``QDWIN_FORWARD_BIN`` seam), so the
        per-stream access token is claimed and ``inject_*`` is driven DIRECTLY
        against ``qdwin_stream_input_v1`` — NO FreeRDP / RDP / remote viewer. The
        script keeps the sentinel up BEFORE releasing the claimant's GO-gated
        inject, then returns once the claimant reports the inject was sent.

        Returns ``{"status": <claimant status JSON>, "rdp_port": <int>}`` — the
        rdp_port lets the caller cross-check the marker's pressed-seat identity is
        ``qdwin-stream-<rdp_port>`` (the per-stream seat)."""
        real = self._real(vm)
        self._push(real, self.repo_dir / "multimachine/harness/vm/source-stack.sh",
                   "/tmp/mm-source-stack.sh")
        status_path = "/run/user/1000/claimant-status.json"
        env = (f"MODE=claimant W={width} H={height} GEN={generation} "
               f"ANIMATE_MS=200 CLAIMANT_STATUS={shlex.quote(status_path)} "
               f"EXPORTED_TELEMETRY={shlex.quote(exported_telemetry)} "
               f"EXPORTED_LABEL={shlex.quote(exported_label)} "
               f"SENTINEL_TELEMETRY={shlex.quote(sentinel_telemetry)} "
               f"SENTINEL_LABEL={shlex.quote(sentinel_label)}")
        out = self._vmexec(real, self._as_admin(
            f"{env} bash /tmp/mm-source-stack.sh"), timeout=180)
        if "SETUP_OK" not in out:
            raise RuntimeError(f"claimant source-stack did not report SETUP_OK:\n{out}")
        rdp_port = 0
        for line in out.splitlines():
            if line.startswith("SETUP_OK"):
                import re
                m = re.search(r"RDP_PORT=(\d+)", line)
                if m:
                    rdp_port = int(m.group(1))
        return {"status": self.read_claimant_status(vm, status_path),
                "rdp_port": rdp_port}

    def read_claimant_status(self, vm: str, path: str) -> dict:
        """Read + parse the direct claimant's status JSON from the guest (the
        fail-closed witness that the claim path ran + the negative protocol checks
        held). Empty dict if absent/unwritten."""
        return self.read_telemetry(vm, path)

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

    def inject_input(self, vm: str, *, x: int | None = None,
                     y: int | None = None, absolute: bool = False
                     ) -> tuple[int, int]:
        """Inject input at the VM-B viewer via ydotool — the HONEST end-to-end path
        (ydotool → kiosk weston seat → sdl-freerdp → RDP → qdistro-forward →
        per-stream seat → exported marker). Ensures ydotoold, lets weston hotplug
        its uinput device, then moves the pointer to a KNOWN viewer pixel, clicks,
        and sends a key (codex impl-10 Q2: motion→click→key). Returns ``(px, py)``.

        ``absolute=False`` (default, the confinement/isolation gates): anchor at the
        corner (a large RELATIVE move clamping to 0,0) then a relative move of
        ``(px,py)``. Fine when only PRESS deltas matter — but libinput applies
        POINTER ACCELERATION to relative motion, so the landing pixel is NOT exactly
        (px,py) (measured ~1.98× live). ``absolute=True`` (the coordinate-fidelity
        gate): ``ydotool mousemove --absolute`` maps straight onto the output and
        bypasses acceleration, so the pointer lands at the EXACT viewer pixel — the
        only honest way to assert a coordinate."""
        real = self._real(vm)
        px = self.out_w // 2 if x is None else int(x)
        py = self.out_h // 2 if y is None else int(y)
        # viewer-stack.sh already started OUR ydotoold at this socket BEFORE weston
        # (so weston enumerated the uinput device at startup). Just drive ydotool.
        sock = "/run/.ydotool_socket"
        if absolute:
            move = f"ydotool mousemove --absolute -x {px} -y {py}; sleep 0.3; "
        else:
            # anchor at the corner (relative move clamps to 0,0), then a relative
            # move of (px,py) → lands NEAR the viewer pixel (accel-skewed; ok for
            # press-delta gates that don't assert the coordinate).
            move = (f"ydotool mousemove -- -5000 -5000; sleep 0.3; "
                    f"ydotool mousemove -- {px} {py}; sleep 0.3; ")
        script = (
            f"export YDOTOOL_SOCKET={sock}; "
            f"[ -S {sock} ] || {{ echo 'NO_YDOTOOL_SOCKET'; exit 1; }}; "
            f"{move}"
            f"ydotool click 0xC0; sleep 0.3; "       # left press+release
            f"ydotool click 0xC0; sleep 0.3; "
            f"ydotool key 30:1 30:0; sleep 0.3; "    # 'a' press+release
            f"echo YDOTOOL_DONE")
        out = self._vmexec(real, script, check=False)
        if "YDOTOOL_DONE" not in out:
            raise RuntimeError(f"ydotool injection failed on {vm}:\n{out}")
        return (px, py)

    def inject_key(self, vm: str, key: str = "30:1 30:0") -> None:
        """Inject a KEYBOARD-only event at the VM-B viewer via ydotool (no pointer
        move/click). Keyboard events follow the compositor's keyboard FOCUS
        (qdwin set_keyboard_focus), so this isolates shell-owned focus routing from
        pointer hit-testing — the honest test of 'keyboard reaches only the
        viewer-focused peer's source' (rung-1 assertion 3) without the pointer-pick
        / ydotool absolute-scale confounds. Default key 30 = 'a'."""
        real = self._real(vm)
        sock = "/run/.ydotool_socket"
        script = (
            f"export YDOTOOL_SOCKET={sock}; "
            f"[ -S {sock} ] || {{ echo 'NO_YDOTOOL_SOCKET'; exit 1; }}; "
            f"ydotool key {key}; sleep 0.3; echo YDOTOOL_DONE")
        out = self._vmexec(real, script, check=False)
        if "YDOTOOL_DONE" not in out:
            raise RuntimeError(f"ydotool key injection failed on {vm}:\n{out}")

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
                "mm-relay mm-control mm-marker2 mm-bystander2 mm-relay2 "
                "2>/dev/null || true"), check=False)
        else:
            self._vmexec(real,
                         "systemctl stop mm-weston mm-viewer mm-qdwin "
                         "mm-bystander-vb mm-rdp-a mm-rdp-b 2>/dev/null || true",
                         check=False)

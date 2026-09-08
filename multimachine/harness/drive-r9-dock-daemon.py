#!/usr/bin/env python3
"""Two-VM gate for the installed R9 display-dock daemon control boundary."""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
BASE_PATH = REPO / "multimachine/harness/drive-r9-rdp-output.py"
SPEC = importlib.util.spec_from_file_location("r9_rdp_output_gate", BASE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load R9 output gate helpers")
base = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = base
SPEC.loader.exec_module(base)


GEN_READ_ONLY = base.GENERATION
GEN_INPUT = base.GENERATION + 1
GEN_REDOCK = base.GENERATION + 2
GENERATIONS = (GEN_READ_ONLY, GEN_INPUT, GEN_REDOCK)
CONTROL = "/run/qdistro-mm-display-dock/control.sock"
STATUS = "/var/lib/qdistro-mm-display-dock/status.json"


def dock_command(backend, vm: str, command: str, *, generation: int | None = None,
                 check: bool = True) -> dict:
    suffix = ""
    if command == "attach":
        if generation is None:
            raise ValueError("attach needs a generation")
        suffix = (
            f" --receipt /tmp/r9-carrier-g{generation}/grant.json"
            f" --secret /tmp/r9-carrier-g{generation}/secret"
            f" --session-id {base.SESSION_ID}")
    elif command == "detach":
        if generation is None:
            raise ValueError("detach needs a generation")
        suffix = f" --generation {generation}"
    output = backend._vmexec(
        vm, "env PYTHONPATH=/tmp/mm /usr/bin/python3 "
        f"/tmp/r9-dock-daemon-command.py {command} --path {CONTROL}{suffix}",
        timeout=70, check=check)
    lines = [line for line in output.splitlines() if line.startswith("{")]
    if not lines:
        raise RuntimeError(f"dock {command} returned no JSON: {output}")
    return json.loads(lines[-1])


def wait_phase(backend, vm: str, phase: str, *, generation: int,
               timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            response = dock_command(backend, vm, "status")
            last = response.get("status", {})
            if (response.get("ok") is True and last.get("phase") == phase
                    and last.get("generation") == generation):
                return last
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(
        f"dock did not reach {phase} generation {generation}: {last}")


def stop_generation(backend, source_vm: str, viewer_vm: str,
                    generation: int) -> None:
    backend._vmexec(
        source_vm,
        f"systemctl stop mm-r9-panel-primary-g{generation} "
        f"mm-r9-carrier-primary-g{generation} 2>/dev/null || true",
        check=False)
    backend._vmexec(
        viewer_vm,
        f"systemctl stop mm-r9-panel-peer-g{generation} "
        f"mm-r9-carrier-peer-g{generation} 2>/dev/null || true",
        check=False)


def prepare_generation(panel, carrier, generation: int) -> None:
    panel.prepare_generation(generation)
    carrier.prepare_generation(generation)


def start_installed_daemon(backend, vm: str, *, clean: bool) -> int:
    cleanup = f"rm -f {STATUS}; " if clean else ""
    backend._vmexec(
        vm,
        "systemctl stop qdistro-mm-display-dock.service 2>/dev/null || true; "
        + cleanup
        + "install -d -o root -g root -m 0755 /etc/qdistro/multimachine; "
        "printf 'QD_MM_MACHINE_ID=r9-primary\\n' "
        ">/etc/qdistro/multimachine/display-dock.conf; "
        "chmod 0644 /etc/qdistro/multimachine/display-dock.conf; "
        "systemctl reset-failed qdistro-mm-display-dock.service "
        "2>/dev/null || true; systemctl start qdistro-mm-display-dock.service")
    base.wait_guest(
        backend, vm,
        f"test -S {CONTROL} && test -s {STATUS} && echo ready",
        timeout=30, label="installed display dock daemon")
    return int(backend._vmexec(
        vm, "systemctl show -p MainPID --value "
        "qdistro-mm-display-dock.service").strip())


def telemetry(backend, vm: str) -> dict:
    return json.loads(backend._vmexec(
        vm, f"cat {base.SOURCE_RT}/marker-telemetry.json"))


def inject_key_until_delivered(backend, source_vm: str, viewer_vm: str,
                               before: dict, *, timeout: float = 5) -> dict:
    """Retry an admitted key until the asynchronous RDP seat reports it."""
    deadline = time.monotonic() + timeout
    after = before
    while time.monotonic() < deadline:
        backend._vmexec(
            viewer_vm,
            # The replacement RDP seat has no focused surface after a prior
            # output detach.  Focus the exported marker through the viewer's
            # independently calibrated 2x absolute ydotool apparatus, then
            # prove that the admitted key reaches its Wayland keyboard.
            "YDOTOOL_SOCKET=/run/.ydotool_socket "
            "ydotool mousemove --absolute -x 64 -y 150; sleep 0.2; "
            "YDOTOOL_SOCKET=/run/.ydotool_socket ydotool click 0xC0; "
            "sleep 0.2; YDOTOOL_SOCKET=/run/.ydotool_socket "
            "ydotool key 30:1 30:0; sleep 0.25")
        after = telemetry(backend, source_vm)
        if (after["totals"]["key_press"]
                > before["totals"]["key_press"]):
            break
    return after


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vm-source", required=True)
    ap.add_argument("--vm-viewer", required=True)
    ap.add_argument("--qdwin", type=Path,
                    default=Path("/home/play2/qdistro/qdwin"))
    ap.add_argument("--qdshell", type=Path,
                    default=Path("/home/play2/qdistro/qdshell"))
    ap.add_argument("--bundle", type=Path,
                    default=Path("/tmp/mm-live/r9-installed-dock-daemon"))
    args = ap.parse_args()

    args.bundle.mkdir(parents=True, exist_ok=True)
    backend = base.QciVMBackend(
        vm_a=args.vm_source, vm_b=args.vm_viewer, repo_dir=REPO,
        out_w=base.W, out_h=base.H)
    base.stage(
        backend, args.vm_source, args.vm_viewer, args.qdwin, args.qdshell)
    base.configure_panel_control_forward(backend, args.vm_source)
    base.stage_carrier_grants(
        backend, args.vm_source, args.vm_viewer,
        generations=GENERATIONS, read_only=frozenset({GEN_READ_ONLY}))

    backend._vmexec(
        args.vm_source,
        f"test ! -d {base.SOURCE_RT} || touch {base.SOURCE_RT}/stop; "
        "systemctl stop qdistro-mm-display-dock.service "
        "mm-r9-qdshell 2>/dev/null || true; "
        "for i in $(seq 1 50); do "
        "test ! -S /run/user/1000/r9-source && break; sleep 0.2; done; "
        f"test ! -S /run/user/1000/r9-source; rm -rf {base.SOURCE_RT}")
    source = subprocess.Popen(
        [str(REPO / "scripts/vm/vm-exec"), args.vm_source,
         "bash /tmp/r9-rdp-source-stack.sh"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assertions: dict[str, bool] = {}
    details: dict[str, object] = {}
    source_output = ""
    initial_pids = (0, 0)
    shell_pid = 0
    panel = None
    carrier = None
    try:
        base.wait_guest(
            backend, args.vm_source,
            f"test -e {base.SOURCE_RT}/ready && "
            f"test -s {base.SOURCE_RT}/qdshell.pid && echo ready",
            timeout=60, label="source stack", process=source)
        initial_pids = base.pids(backend, args.vm_source)
        shell_pid = int(backend._vmexec(
            args.vm_source, f"cat {base.SOURCE_RT}/qdshell.pid").strip())
        first_position = base.position_marker_through_shell(
            backend, args.vm_source)
        details["initial_shell_position"] = first_position
        assertions["fixed_user_qdshell_unit_is_authority"] = (
            int(backend._vmexec(
                args.vm_source,
                "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 "
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus "
                "systemctl --user show -p MainPID --value qdshell.service"
            ).strip()) == shell_pid)

        daemon_pid = start_installed_daemon(
            backend, args.vm_source, clean=True)
        cold = wait_phase(
            backend, args.vm_source, "disabled", generation=0)
        details["cold_start_status"] = cold
        assertions["cold_start_recovers_without_generation_agents"] = (
            daemon_pid > 1 and cold["accepting"] is True)
        denied = backend._vmexec(
            args.vm_source,
            "runuser -u admin -- env PYTHONPATH=/tmp/mm /usr/bin/python3 "
            f"/tmp/r9-dock-daemon-command.py status --path {CONTROL}",
            check=False)
        assertions["same_uid_non_root_controller_is_denied"] = (
            "unauthorized-controller" in denied)

        panel = base.LivePeerPanelEndpoint(
            backend, args.vm_source, args.vm_viewer)
        carrier = base.LiveCarrierEndpoint(
            backend, args.vm_source, args.vm_viewer)

        # Generation 90: signed read-only attach reaches ACTIVE and carries
        # pixels while qdwin's input gate remains closed.
        prepare_generation(panel, carrier, GEN_READ_ONLY)
        attached = dock_command(
            backend, args.vm_source, "attach", generation=GEN_READ_ONLY)
        assert attached["ok"] is True
        status90 = wait_phase(
            backend, args.vm_source, "active", generation=GEN_READ_ONLY)
        details["read_only_status"] = status90
        assertions["signed_read_only_generation_is_active"] = (
            status90["recovery_grant"]["allow_input"] is False)
        base.wait_rdp(backend, args.vm_viewer)
        before = telemetry(backend, args.vm_source)
        backend._vmexec(
            args.vm_viewer,
            "YDOTOOL_SOCKET=/run/.ydotool_socket ydotool key 30:1 30:0; "
            "sleep 0.5")
        after = telemetry(backend, args.vm_source)
        assertions["read_only_generation_denies_rdp_input"] = (
            after["totals"]["key_press"] == before["totals"]["key_press"])
        first = base.capture_viewer(
            backend, args.vm_viewer, args.bundle / "read-only-active.ppm")
        details["read_only_pixels"] = base.assert_remote_half(first)
        assertions["read_only_generation_still_carries_pixels"] = True

        # Loss of the peer carrier is detected by the installed daemon. Its
        # ordered teardown reaches FAILED_SAFE and only explicit reset retires
        # that terminal generation.
        backend._vmexec(
            args.vm_viewer,
            f"systemctl stop mm-r9-carrier-peer-g{GEN_READ_ONLY}")
        failed = wait_phase(
            backend, args.vm_source, "failed-safe",
            generation=GEN_READ_ONLY, timeout=15)
        details["carrier_loss_status"] = failed
        assertions["carrier_loss_reaches_durable_failed_safe"] = True
        reset = dock_command(backend, args.vm_source, "reset")
        assertions["reset_requires_all_fixed_endpoints_safe"] = (
            reset["ok"] is True
            and reset["status"]["phase"] == "disabled")
        stop_generation(
            backend, args.vm_source, args.vm_viewer, GEN_READ_ONLY)

        # Generation 91 admits input. Kill the daemon, not the agents: the
        # systemd restart must consume durable projection, drive exact recovery,
        # and retain replay state before reopening control admission.
        prepare_generation(panel, carrier, GEN_INPUT)
        # The generation-90 disable correctly rescued the crossing surface to
        # the surviving local output.  Put it back across the disabled seam as
        # an explicit shell-authority decision before testing the new RDP seat.
        input_position = base.position_marker_through_shell(
            backend, args.vm_source)
        details["input_shell_position"] = input_position
        assertions["input_surface_is_explicitly_repositioned_after_rescue"] = (
            input_position["handle"] == first_position["handle"]
            and input_position["outer"] == [base.W - base.SEAM, base.OY,
                                             base.MW, base.MH])
        attached91 = dock_command(
            backend, args.vm_source, "attach", generation=GEN_INPUT)
        assert attached91["ok"] is True
        wait_phase(backend, args.vm_source, "active", generation=GEN_INPUT)
        base.wait_rdp(backend, args.vm_viewer)
        before91 = telemetry(backend, args.vm_source)
        after91 = inject_key_until_delivered(
            backend, args.vm_source, args.vm_viewer, before91)
        assertions["input_generation_admits_rdp_input"] = (
            after91["totals"]["key_press"]
            > before91["totals"]["key_press"])
        backend._vmexec(
            args.vm_source,
            "systemctl kill --kill-who=main --signal=KILL "
            "qdistro-mm-display-dock.service")
        base.wait_guest(
            backend, args.vm_source,
            "pid=$(systemctl show -p MainPID --value "
            "qdistro-mm-display-dock.service); "
            f"test \"$pid\" -gt 1 && test \"$pid\" != \"{daemon_pid}\" "
            f"&& test -S {CONTROL} && echo restarted",
            timeout=30, label="display dock crash restart")
        recovered = wait_phase(
            backend, args.vm_source, "disabled", generation=GEN_INPUT,
            timeout=30)
        details["crash_recovery_status"] = recovered
        new_daemon_pid = int(backend._vmexec(
            args.vm_source, "systemctl show -p MainPID --value "
            "qdistro-mm-display-dock.service").strip())
        assertions["daemon_crash_restart_recovers_exact_generation"] = (
            new_daemon_pid != daemon_pid
            and recovered["recovery_grant"] is None)
        disabled = base.source_probe(
            backend, args.vm_source, "--expect-state=rdp-0:0")
        assertions["restart_recovery_disables_qdwin_slot"] = (
            "enabled=0" in disabled)
        replay = dock_command(
            backend, args.vm_source, "attach", generation=GEN_INPUT,
            check=False)
        details["replayed_generation_response"] = replay
        assertions["restart_preserves_generation_replay_denial"] = (
            replay["ok"] is False
            and replay.get("status", {}).get("phase") == "disabled"
            and replay.get("status", {}).get("generation") == GEN_INPUT)
        stop_generation(backend, args.vm_source, args.vm_viewer, GEN_INPUT)

        # Generation 92 proves a strictly newer clean redock after recovery.
        # Place a fresh straddling surface while the slot is still disabled so
        # the first frame of the new RDP connection must contain it.  This also
        # avoids accepting a stale decode record from an earlier connection.
        prepare_generation(panel, carrier, GEN_REDOCK)
        backend._vmexec(
            args.vm_source,
            "systemctl stop mm-r9-daemon-reattach 2>/dev/null || true; "
            "systemd-run --collect --unit=mm-r9-daemon-reattach --uid=admin "
            "--setenv=HOME=/home/admin --setenv=XDG_RUNTIME_DIR=/run/user/1000 "
            "--setenv=WAYLAND_DISPLAY=r9-source "
            "/tmp/r9-qdwin-marker-client --width 512 --height 400 "
            "--seam-x 256 --output-id 9 --generation 92 --frame 3 "
            "--animate-ms 200")
        second_position = base.position_marker_through_shell(
            backend, args.vm_source,
            excluded={int(first_position["handle"])})
        details["redock_shell_position"] = second_position
        attached92 = dock_command(
            backend, args.vm_source, "attach", generation=GEN_REDOCK)
        assert attached92["ok"] is True
        wait_phase(backend, args.vm_source, "active", generation=GEN_REDOCK)
        base.wait_rdp(backend, args.vm_viewer)
        final_pixels = base.capture_viewer(
            backend, args.vm_viewer, args.bundle / "redock-active.ppm")
        details["redock_pixels"] = base.assert_remote_half(final_pixels)
        detached = dock_command(
            backend, args.vm_source, "detach", generation=GEN_REDOCK)
        assertions["newer_redock_cleanly_detaches"] = (
            detached["ok"] is True
            and detached["status"]["phase"] == "disabled")
        base.assert_alive(backend, args.vm_source, initial_pids)
        assertions["daemon_gate_preserves_compositor_and_app"] = True

        weston_log = backend._vmexec(
            args.vm_source, f"cat {base.SOURCE_RT}/weston.log")
        assertions["all_disables_drain_before_carrier_retirement"] = (
            weston_log.count(
                "remote output drain output=rdp-0 result=applied") >= 3)
        details["daemon_journal"] = backend._vmexec(
            args.vm_source,
            "invocation=$(systemctl show -p InvocationID --value "
            "qdistro-mm-display-dock.service); "
            "journalctl _SYSTEMD_INVOCATION_ID=$invocation --no-pager",
            check=False)
        assertions["all_hard_assertions"] = all(assertions.values())
    finally:
        backend._vmexec(
            args.vm_source,
            "systemctl stop qdistro-mm-display-dock.service "
            "mm-r9-daemon-reattach 2>/dev/null || true",
            check=False)
        for generation in GENERATIONS:
            stop_generation(
                backend, args.vm_source, args.vm_viewer, generation)
        backend._vmexec(
            args.vm_viewer, "systemctl stop mm-r9-rdp 2>/dev/null || true",
            check=False)
        backend._vmexec(
            args.vm_source, f"touch {base.SOURCE_RT}/stop", check=False)
        if source.poll() is None:
            try:
                source_output, _ = source.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                source.kill()
                source_output, _ = source.communicate()
        elif source.stdout is not None:
            source_output = source.stdout.read()
        backend._virsh(
            "qemu-monitor-command", args.vm_source, "--hmp",
            "hostfwd_remove tcp:127.0.0.1:3388", check=False)
        (args.bundle / "source-stack.log").write_text(
            source_output, encoding="utf-8")
        for vm, path, name in (
            (args.vm_source, f"{base.SOURCE_RT}/weston.log", "source-weston.log"),
            (args.vm_viewer, f"{base.VIEWER_RT}/rdp.log", "viewer-rdp.log"),
        ):
            output = backend._vmexec(vm, f"cat {path}", check=False)
            (args.bundle / name).write_text(output, encoding="utf-8")

    result = {
        "schema": 1,
        "scenario": "r9-installed-display-dock-daemon-two-vm",
        "scope": "installed control/recovery/read-only/software gate",
        "topology": {"source": args.vm_source, "viewer": args.vm_viewer},
        "generations": list(GENERATIONS),
        "source_pids": {
            "weston": initial_pids[0], "app": initial_pids[1],
            "qdshell": shell_pid,
        },
        "assertions": assertions,
        "details": details,
    }
    (args.bundle / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    if not assertions["all_hard_assertions"]:
        raise AssertionError(f"installed daemon assertions: {assertions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

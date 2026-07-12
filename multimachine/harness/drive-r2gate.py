#!/usr/bin/env python3
"""R2 live gate: two origins through signed launcher + broker + real qdshell."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from multimachine.harness.capture import load_image  # noqa: E402
from multimachine.harness.vm_backend import QciVMBackend  # noqa: E402
from multimachine.mm_pairing_authority import (  # noqa: E402
    issue_pairing_receipt,
    public_key_bytes,
)


ap = argparse.ArgumentParser()
ap.add_argument("vm_a", help="input-capable source origin")
ap.add_argument("vm_c", help="read-only source origin")
ap.add_argument("vm_b", help="viewer running broker + qdshell")
ap.add_argument("--qdshell", type=Path,
                default=Path(os.environ.get("QDSHELL_REPO",
                                            "/home/play2/qdistro/qdshell")))
args = ap.parse_args()

W, H = 640, 400
GEN_A, GEN_C = 61, 62
RDP_A, RDP_C = 5555, 5560
CTRL_A, CTRL_C = 5571, 5572
APP_A = "qdistro.mm.vm-a.streamA"
APP_C = "qdistro.mm.vm-c.streamC"
CAP_A = secrets.token_urlsafe(24)
CAP_C = secrets.token_urlsafe(24)
TEL_A = "/run/user/1000/mm-r2-a.json"
TEL_C = "/run/user/1000/mm-r2-c.json"
BUNDLE = Path("/tmp/mm-live/r2-real")
BUNDLE.mkdir(parents=True, exist_ok=True)

be_a = QciVMBackend(args.vm_a, args.vm_b, REPO, relay_port=RDP_A,
                    out_w=W, out_h=H)
be_c = QciVMBackend(args.vm_c, args.vm_b, REPO, relay_port=RDP_C,
                    out_w=W, out_h=H)
results: dict[str, bool] = {}


def check(name: str, condition: object, detail: str = "") -> bool:
    passed = bool(condition)
    results[name] = passed
    print(f"[{'PASS' if passed else 'FAIL'}] {name}  {detail}", flush=True)
    return passed


def key_total(telemetry: dict) -> int:
    return int(telemetry.get("totals", {}).get("key_press", 0))


def viewer_exec(command: str, *, check_result: bool = True) -> str:
    return be_a._vmexec(args.vm_b, command, check=check_result)


def ipc(method: str, *values: object) -> str:
    argv = " ".join(["qs", "ipc", "call", "multimachine", method,
                     *(str(value) for value in values)])
    command = ("env XDG_RUNTIME_DIR=/run/mm-vb WAYLAND_DISPLAY=wayland-vb "
               f"{argv}")
    return viewer_exec(command, check_result=False).strip()


def remote_rows() -> list[dict]:
    output = ipc("list")
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("["):
            try:
                return json.loads(line)
            except ValueError:
                pass
        if line.startswith('"['):
            try:
                return json.loads(json.loads(line))
            except ValueError:
                pass
    raise RuntimeError(f"cannot parse multimachine IPC list: {output!r}")


def wait_rows(predicate, timeout: float = 30.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    last: list[dict] = []
    while time.monotonic() < deadline:
        try:
            last = remote_rows()
            if predicate(last):
                return last
        except RuntimeError:
            pass
        time.sleep(0.5)
    return last


def push_json(value: object, guest: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.flush()
        be_a._push(args.vm_b, Path(stream.name), guest)
    viewer_exec(f"chmod 0600 {guest}")


def prepare_viewer_runtime(receipt: dict, streams: dict,
                           public_key: bytes) -> None:
    be_a._push_mm_package(args.vm_b)
    for name in ("qdistro-mm-session-launcher", "qdistro-mm-broker",
                 "qdistro-mm-rdp-client-wrapper"):
        guest = f"/tmp/mm/multimachine/{name}"
        be_a._push(args.vm_b, REPO / "multimachine" / name, guest)
        viewer_exec(f"chmod 0755 {guest}")
    be_a._push(
        args.vm_b, REPO / "scripts/install/install-multimachine-for-vm.sh",
        "/tmp/install-multimachine-for-vm.sh")
    viewer_exec(
        "chmod 0755 /tmp/install-multimachine-for-vm.sh; "
        "bash /tmp/install-multimachine-for-vm.sh /tmp/mm/multimachine")
    be_a._push(
        args.vm_b,
        REPO / "multimachine/harness/vm/viewer-qdshell-stack.sh",
        "/tmp/mm-viewer-qdshell-stack.sh")
    viewer_exec("chmod 0755 /tmp/mm-viewer-qdshell-stack.sh")

    viewer_exec(
        "rm -rf /tmp/qdshell-r2; cp -a /usr/share/quickshell/qdshell "
        "/tmp/qdshell-r2; mkdir -p /tmp/qdshell-r2/Services/Qdwin")
    for rel in ("Services/Qdwin/Qdwin.qml",
                "Services/Qdwin/RemoteMachine.js",
                "Services/Qdwin/RemoteMachineWindows.qml"):
        be_a._push(args.vm_b, args.qdshell / rel, f"/tmp/qdshell-r2/{rel}")

    viewer_exec("mkdir -p /etc/qdistro/multimachine")
    with tempfile.NamedTemporaryFile("wb") as key_file:
        key_file.write(public_key)
        key_file.flush()
        be_a._push(args.vm_b, Path(key_file.name),
                   "/etc/qdistro/multimachine/pairing-authority.ed25519.pub")
    viewer_exec(
        "chown root:root /etc/qdistro/multimachine/pairing-authority.ed25519.pub; "
        "chmod 0644 /etc/qdistro/multimachine/pairing-authority.ed25519.pub")
    push_json(receipt, "/run/mm-r2-pairing.json")
    push_json(streams, "/run/mm-r2-streams.json")


def launch_lookalike(unit: str, app_id: str, instance: str) -> None:
    command = (
        f"systemctl stop {unit} 2>/dev/null || true; "
        f"systemd-run --collect --unit={unit} "
        "--setenv=XDG_RUNTIME_DIR=/run/mm-vb --setenv=WAYLAND_DISPLAY=wayland-vb "
        "--setenv=QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 "
        f"qdistro-secctx-exec --sandbox-engine qdistro.mm --app-id {app_id} "
        f"--instance-id {instance} -- qdwin-marker-client --width 320 --height 220 "
        "--output-id 9 --generation 99 --frame 0 --animate-ms 200")
    viewer_exec(command)


print("--- sources: two distinct machines ---", flush=True)
approval_a = be_a.setup_confinement_source(
    "vm-a", generation=GEN_A, width=W, height=H,
    exported_telemetry=TEL_A, sentinel_telemetry="",
    exported_label="origin-a", sentinel_label="", allow_input=1, output_id=1)
approval_c = be_c.setup_confinement_source(
    "vm-a", generation=GEN_C, width=W, height=H,
    exported_telemetry=TEL_C, sentinel_telemetry="",
    exported_label="origin-c", sentinel_label="", allow_input=0, output_id=2)
sid_a = be_a.launch_control(
    "vm-a", generation=GEN_A, window_id=1, source_machine="vm-a",
    title="origin A", app_id=APP_A, req_w=W, req_h=H,
    unit="mm-control-a", control_port=CTRL_A,
    control_capability=CAP_A)
sid_c = be_c.launch_control(
    "vm-a", generation=GEN_C, window_id=1, source_machine="vm-c",
    title="origin C", app_id=APP_C, req_w=W, req_h=H,
    unit="mm-control-c", control_port=CTRL_C,
    control_capability=CAP_C)

session_id = secrets.token_hex(16)
now = int(time.time())
key = Ed25519PrivateKey.generate()
origins = [
    {"machine_id": "vm-a", "trust_domain_id": "owner-machines",
     "generation": GEN_A, "capabilities": ["attach_ui", "receive_input"]},
    {"machine_id": "vm-c", "trust_domain_id": "owner-machines",
     "generation": GEN_C, "capabilities": ["attach_ui"]},
]
receipt = issue_pairing_receipt(
    origins=origins, viewer_machine_id="vm-viewer", session_id=session_id,
    issued_at=now, expires_at=now + 300, private_key=key)
streams = {
    "pairing_session_id": session_id,
    "control_host": "10.0.2.2",
    "streams": [
        {"label": "a", "spec": {
            "origin": "vm-a", "stream_id": sid_a, "generation": GEN_A,
            "app_id": APP_A, "instance_id": f"vm-a-{sid_a}",
            "rdp_host": "10.0.2.2", "rdp_port": RDP_A,
            "width": W, "height": H, "allow_input": 1},
         "control_port": CTRL_A, "rdp_unit": "mm-rdp-a",
         "marker_unit": "mm-marker", "otp": approval_a.rdp_password,
         "control_capability": CAP_A},
        {"label": "c", "spec": {
            "origin": "vm-c", "stream_id": sid_c, "generation": GEN_C,
            "app_id": APP_C, "instance_id": f"vm-c-{sid_c}",
            "rdp_host": "10.0.2.2", "rdp_port": RDP_C,
            "width": W, "height": H, "allow_input": 0},
         "control_port": CTRL_C, "rdp_unit": "mm-rdp-c",
         "marker_unit": "mm-marker", "otp": approval_c.rdp_password,
         "control_capability": CAP_C},
    ],
}
prepare_viewer_runtime(receipt, streams, public_key_bytes(key.public_key()))
out = viewer_exec(
    "PAIRING_RECEIPT=/run/mm-r2-pairing.json "
    "STREAM_SESSION=/run/mm-r2-streams.json VIEWER_MACHINE_ID=vm-viewer "
    "bash /tmp/mm-viewer-qdshell-stack.sh", check_result=False)
(BUNDLE / "viewer-setup.log").write_text(out, encoding="utf-8")
check("viewer-production-stack-ready", "VMB_QDSHELL_OK" in out, out[-1000:])
if "VMB_QDSHELL_OK" not in out:
    raise SystemExit(1)

rows = wait_rows(lambda rs: len([r for r in rs if r.get("authorized")]) == 2)
(BUNDLE / "remote-rows-initial.json").write_text(
    json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
authorized = {row["origin"]: row for row in rows if row.get("authorized")}
check("two-broker-vouched-origins", set(authorized) == {"vm-a", "vm-c"},
      repr(rows))
check("per-origin-trust-colours-distinct",
      len({row["colour"] for row in authorized.values()}) == 2,
      repr({origin: row["colour"] for origin, row in authorized.items()}))
check("per-origin-input-policy-mirrored",
      authorized.get("vm-a", {}).get("allowInput") == 1
      and authorized.get("vm-c", {}).get("allowInput") == 0)

qlog = viewer_exec("cat /run/mm-vb/qdshell.log", check_result=False)
(BUNDLE / "qdshell.log").write_text(qlog, encoding="utf-8")
ordered = True
for row in authorized.values():
    neutral = qlog.find(f"[mm] neutral chrome handle={row['handle']}")
    vouched = qlog.find(f"[mm] broker-vouched origin={row['origin']}")
    ordered = ordered and neutral >= 0 and vouched > neutral
check("neutral-before-broker-vouched", ordered)

shot = be_a.capture("vm-b", 0, BUNDLE / "two-origins.ppm")
image = load_image(shot)
check("viewer-framebuffer-painted", int(image.max()) > 20
      and len({tuple(pixel) for pixel in image[::80, ::80].reshape(-1, 3)}) > 3)

launch_lookalike("mm-unpaired", "qdistro.mm.vm-evil.fake", "evil-1")
evil_rows = wait_rows(lambda rs: any(r.get("origin") == "vm-evil" for r in rs))
evil = next((r for r in evil_rows if r.get("origin") == "vm-evil"), {})
check("unpaired-origin-stays-neutral", evil and not evil.get("authorized"),
      repr(evil))
check("unpaired-origin-has-no-shell-authority",
      "false" in ipc("focus", evil.get("handle", 0)).lower()
      and "false" in ipc("close", evil.get("handle", 0)).lower())

handle_a = authorized["vm-a"]["handle"]
handle_c = authorized["vm-c"]["handle"]
ipc("focus", handle_a); time.sleep(1)
a0 = key_total(be_a.read_telemetry("vm-a", TEL_A))
c0 = key_total(be_c.read_telemetry("vm-a", TEL_C))
be_a.inject_key("vm-b"); time.sleep(1)
a1 = key_total(be_a.read_telemetry("vm-a", TEL_A))
c1 = key_total(be_c.read_telemetry("vm-a", TEL_C))
ipc("focus", handle_c); time.sleep(1)
be_a.inject_key("vm-b"); time.sleep(1)
a2 = key_total(be_a.read_telemetry("vm-a", TEL_A))
c2 = key_total(be_c.read_telemetry("vm-a", TEL_C))
check("per-origin-input-enforced", a1 - a0 > 0 and c1 - c0 == 0
      and a2 - a1 == 0 and c2 - c1 == 0,
      f"A={a0}->{a1}->{a2} C={c0}->{c1}->{c2}")

check("qdshell-close-dispatched", "true" in ipc("close", handle_a).lower())
closed_rows = wait_rows(
    lambda rs: not any(r.get("authorized") and r.get("origin") == "vm-a"
                       for r in rs), timeout=30)
check("source-mediated-close-isolated",
      not be_a.marker_unit_alive("vm-a")
      and be_c.marker_unit_alive("vm-a")
      and any(r.get("authorized") and r.get("origin") == "vm-c"
              for r in closed_rows))

launch_lookalike("mm-stale-a", APP_A, "stale-a-after-close")
stale_rows = wait_rows(
    lambda rs: any(r.get("secctxAppId") == APP_A for r in rs), timeout=20)
stale = [r for r in stale_rows if r.get("secctxAppId") == APP_A]
check("closed-stream-secctx-reuse-denied",
      bool(stale) and all(not row.get("authorized") for row in stale), repr(stale))

for name, command in {
    "broker.log": "cat /run/mm-vb/broker.log",
    "qdshell-final.log": "cat /run/mm-vb/qdshell.log",
    "source-a-control.jsonl": "cat /run/user/1000/mm-control-a.jsonl",
}.items():
    target = be_a if "source-a" in name else None
    text = (target._vmexec(args.vm_a, target._as_admin(command), check=False)
            if target else viewer_exec(command, check_result=False))
    (BUNDLE / name).write_text(text, encoding="utf-8")
(BUNDLE / "results.json").write_text(
    json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
raise SystemExit(0 if all(results.values()) else 1)

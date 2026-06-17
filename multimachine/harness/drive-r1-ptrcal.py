#!/usr/bin/env python3
"""Pointer-apparatus calibration diagnostic for rung-1 assertion 4.

Maps the ydotool-absolute → viewer-head landing empirically by sweeping coords
and reading which source's per-stream seat received the click, for BOTH raise
orders. Reveals (a) the ydotool→head scale/offset and (b) whether `raise`
reroutes pointer hit-test. Source A=(0,0), B=(300,200); overlap = (300,200)-(640,400).

Usage: drive-r1-ptrcal.py <VM-A> <VM-B>
"""
import sys, time
from pathlib import Path
REPO = Path("/home/play2/qdistro/qdistro"); sys.path.insert(0, str(REPO))
from multimachine.harness.vm_backend import QciVMBackend          # noqa: E402

VMA, VMB = sys.argv[1], sys.argv[2]
W, H, GEN = 640, 400, 51
RELAY_A, RELAY_B = 5555, 5560
TEL_A, TEL_B = "/run/user/1000/mm-tel-a.json", "/run/user/1000/mm-tel-b.json"
be = QciVMBackend(vm_a=VMA, vm_b=VMB, repo_dir=REPO, out_w=W, out_h=H, relay_port=RELAY_A)


def btn(t):
    return int((t or {}).get("totals", {}).get("button_press", 0))


def live():
    for _ in range(12):
        tops = be.viewer_qdwin_toplevels("vm-b")
        mm = {h: d for h, d in tops.items() if d["engine"] == "qdistro.mm"}
        a = [h for h, d in mm.items() if d["app_id"].endswith(".streamA")]
        b = [h for h, d in mm.items() if d["app_id"].endswith(".streamB")]
        if len(a) == 1 and len(b) == 1:
            return a[0], b[0]
        time.sleep(2)
    return None, None


print(f"=== ptrcal VM-A={VMA} VM-B={VMB} ===", flush=True)
be.spin("vm-a"); be.spin("vm-b")
appr_a = be.setup_confinement_source("vm-a", generation=GEN, width=W, height=H,
        exported_telemetry=TEL_A, sentinel_telemetry="", exported_label="A",
        sentinel_label="s", allow_input=1)
appr_b = be.setup_second_export("vm-a", generation=GEN, width=W, height=H, output_id=2,
        telemetry=TEL_B, label="B", relay_port=RELAY_B, allow_input=1)
be.launch_viewer_qdwin("vm-b", rdp_host="10.0.2.2", port_a=RELAY_A, otp_a=appr_a.rdp_password,
        port_b=RELAY_B, otp_b=appr_b.rdp_password, stream_a="streamA", stream_b="streamB",
        origin="vm-a")
ha, hb = live()
print(f"handles A={ha} B={hb}", flush=True)
if not (ha and hb):
    sys.exit(1)
be.viewer_fifo("vm-b", f"move {ha} 0 0"); time.sleep(1.5)
be.viewer_fifo("vm-b", f"move {hb} 300 200"); time.sleep(1.5)
print(f"geometry: {be.viewer_qdwin_geometry('vm-b')}", flush=True)


def click_at(x, y, top):
    be.viewer_fifo("vm-b", f"raise {top}"); time.sleep(1.5)
    a0, b0 = btn(be.read_telemetry("vm-a", TEL_A)), btn(be.read_telemetry("vm-a", TEL_B))
    be.inject_input("vm-b", x=x, y=y, absolute=True)
    time.sleep(1.0)
    da = btn(be.read_telemetry("vm-a", TEL_A)) - a0
    db = btn(be.read_telemetry("vm-a", TEL_B)) - b0
    return da, db


# sweep a grid; for each, record A-raised and B-raised deltas. A '.'=no hit,
# 'A'=hit A, 'B'=hit B. The pair (A-raised,B-raised) reveals exclusivity/overlap.
print("x,y : Araised(dA,dB)  Braised(dA,dB)", flush=True)
for y in range(20, 360, 40):
    for x in range(20, 460, 40):
        da_a, db_a = click_at(x, y, ha)
        da_b, db_b = click_at(x, y, hb)
        cls = "."
        if (da_a > 0 or db_a > 0) and (da_b > 0 or db_b > 0):
            cls = "OVL" if (da_a > 0 and db_b > 0) else "?"
        elif da_a > 0 or db_a > 0 or da_b > 0 or db_b > 0:
            cls = "win"
        print(f"  ({x:3d},{y:3d}): A-raised=({da_a},{db_a}) B-raised=({da_b},{db_b}) {cls}",
              flush=True)
print("=== ptrcal done ===", flush=True)

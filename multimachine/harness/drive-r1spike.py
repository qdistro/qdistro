#!/usr/bin/env python3
"""Phase-2 rung-1 SPIKE driver (codex impl-30 Option B, build-order Q7).

Proves the foundational rung-1 unknown: TWO windowed secctx-tagged FreeRDP
clients become TWO DISTINCT managed toplevels inside ONE real VM-B qdwin, each
decoding its own source stream 1:1, with viewer-shell-decided z-order routing
local input to only the topmost peer (source per-stream markers prove no leak).

Usage: drive-r1spike.py <VM-A> <VM-B>
"""
import sys
from pathlib import Path

REPO = Path("/home/play2/qdistro/qdistro")
sys.path.insert(0, str(REPO))
from multimachine.harness.vm_backend import QciVMBackend          # noqa: E402
from multimachine.harness import oracle as O, marker as M         # noqa: E402
from multimachine.harness.capture import load_image               # noqa: E402

VMA, VMB = sys.argv[1], sys.argv[2]
W, H, GEN = 1280, 800, 41
RELAY_A, RELAY_B = 5555, 5560
TEL_A = "/run/user/1000/mm-tel-a.json"
TEL_B = "/run/user/1000/mm-tel-b.json"
BUNDLE = Path("/tmp/mm-live/r1spike")
BUNDLE.mkdir(parents=True, exist_ok=True)

be = QciVMBackend(vm_a=VMA, vm_b=VMB, repo_dir=REPO, out_w=W, out_h=H,
                  relay_port=RELAY_A)

results = {}


def check(name, cond, detail=""):
    results[name] = bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail}")
    return bool(cond)


def press_total(t):
    tot = (t or {}).get("totals", {})
    return int(tot.get("button_press", 0)) + int(tot.get("key_press", 0))


print(f"=== R1 spike: VM-A={VMA} VM-B={VMB} ===")
be.spin("vm-a")
be.spin("vm-b")

# --- VM-A: export TWO source streams (marker-A out#1 -> relay 5555,
#            marker-B out#2 -> relay 5560), both input-capable + telemetried. ---
print("--- VM-A: stream A (output 1) ---")
appr_a = be.setup_confinement_source(
    "vm-a", generation=GEN, width=W, height=H,
    exported_telemetry=TEL_A, sentinel_telemetry="",
    exported_label="A", sentinel_label="sentinel", allow_input=1)
print(f"  stream A approved: relay={appr_a.rdp_port} otp={appr_a.rdp_password[:6]}…")

print("--- VM-A: stream B (output 2) ---")
appr_b = be.setup_second_export(
    "vm-a", generation=GEN, width=W, height=H, output_id=2,
    telemetry=TEL_B, label="B", relay_port=RELAY_B, allow_input=1)
print(f"  stream B approved: relay={appr_b.rdp_port} otp={appr_b.rdp_password[:6]}…")

# --- VM-B: real qdwin + bystander + two windowed FreeRDP clients ---
print("--- VM-B: viewer qdwin stack ---")
vout = be.launch_viewer_qdwin(
    "vm-b", rdp_host="10.0.2.2",
    port_a=RELAY_A, otp_a=appr_a.rdp_password,
    port_b=RELAY_B, otp_b=appr_b.rdp_password,
    stream_a="streamA", stream_b="streamB", origin="vm-a")
(BUNDLE / "viewer-stack.out").write_text(vout)

import time


def live_handles(settle=2.0, tries=10):
    """Re-resolve the CURRENT live (streamA, streamB) handles — SDL3 churns
    windows, so always read fresh + wait until exactly one survivor per stream."""
    ha = hb = None
    for _ in range(tries):
        tops = be.viewer_qdwin_toplevels("vm-b")
        mm = {h: d for h, d in tops.items() if d["engine"] == "qdistro.mm"}
        a = [h for h, d in mm.items() if d["app_id"].endswith(".streamA")]
        b = [h for h, d in mm.items() if d["app_id"].endswith(".streamB")]
        if len(a) == 1 and len(b) == 1:
            return a[0], b[0], mm
        time.sleep(settle)
    # FAIL CLOSED on ambiguity (zero or >1 live handle for a stream) — never feed
    # an arbitrary/churned handle into the decode/focus checks.
    return None, None, mm


# --- Assertion 1: EXACTLY two distinct managed objects, one per stream ---
ha, hb, mm = live_handles()
print(f"  live managed mm toplevels: {mm}")
a_ids = [h for h, d in mm.items() if d["app_id"].endswith(".streamA")]
b_ids = [h for h, d in mm.items() if d["app_id"].endswith(".streamB")]
check("A1-two-distinct-managed-objects",
      len(mm) == 2 and len(a_ids) == 1 and len(b_ids) == 1
      and ha is not None and hb is not None and ha != hb,
      f"live={sorted(mm)} handle_A={ha} handle_B={hb}")

# --- Per-stream 1:1 decode: RAISE each in turn (full-size windows fully overlap;
#     raise is a compositor z-order op, does not churn the client), capture, oracle
#     keyed by the stream's source output_id (A=out#1, B=out#2). ---
layout = M.compute_layout(W, H)


def decode_check(stream, expect_out, tag):
    res = O.OracleResult(ok=False, payload=None, payload_error="no capture")
    for _ in range(12):
        ha2, hb2, _ = live_handles()
        h = ha2 if stream == "A" else hb2
        if h is None:
            time.sleep(1); continue
        be.viewer_fifo("vm-b", f"raise {h}")
        time.sleep(3)                       # let the raised window repaint on top
        cap = be.capture("vm-b", 0, BUNDLE / f"decode-{tag}.ppm")
        img = load_image(cap)
        res = O.evaluate(img, layout, 1.0, tol=O.TOL_RDP, auto_origin=True,
                         active_generation=GEN, expect_output_id=expect_out)
        if res.ok:
            break
        time.sleep(1)
    return res


if ha and hb:
    ra = decode_check("A", 1, "A")
    check("A1-streamA-decodes-1to1-out1", ra.ok, ra.summary())
    rb = decode_check("B", 2, "B")
    check("A1-streamB-decodes-1to1-out2", rb.ok, rb.summary())

# --- Assertion 3: shell-owned focus routes the keyboard to ONLY the focused peer's
#     source (keyboard follows qdwin set_keyboard_focus — isolates shell focus
#     authority from pointer hit-testing; pointer-overlap confinement = rung-1-proper). ---
def keyfocus_route(stream, tag):
    ha2, hb2, _ = live_handles()
    h = ha2 if stream == "A" else hb2
    be.viewer_fifo("vm-b", f"raise {h}")
    be.viewer_fifo("vm-b", f"focus {h}")
    time.sleep(1.5)
    a0 = press_total(be.read_telemetry("vm-a", TEL_A))
    b0 = press_total(be.read_telemetry("vm-a", TEL_B))
    be.inject_key("vm-b")                    # keyboard-only → follows qdwin focus
    time.sleep(1.5)
    da = press_total(be.read_telemetry("vm-a", TEL_A)) - a0
    db = press_total(be.read_telemetry("vm-a", TEL_B)) - b0
    print(f"  focus={tag}: dKEY A={da} B={db}")
    return da, db


if ha and hb:
    da, db = keyfocus_route("A", "A")
    check("A3-focus-A-keyboard-to-sourceA-only", da > 0 and db == 0, f"dA={da} dB={db}")
    da, db = keyfocus_route("B", "B")
    check("A3-focus-B-keyboard-to-sourceB-only", db > 0 and da == 0, f"dA={da} dB={db}")

# --- evidence + verdict ---
(BUNDLE / "bystander.out").write_text(be.viewer_qdwin_log("vm-b"))
passed = all(results.values()) and len(results) >= 5
print("=== R1 SPIKE", "PASSED" if passed else "FAILED", "===")
for k, v in results.items():
    print(f"   {k}: {'ok' if v else 'FAIL'}")
sys.exit(0 if passed else 1)

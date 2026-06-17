#!/usr/bin/env python3
"""Phase-2 rung-1 FULL 9-ASSERTION GATE driver (codex impl-32).

Builds on the session-9 spike apparatus (Option B: windowed secctx FreeRDP
clients as managed qdwin toplevels in a REAL VM-B qdwin), adding the machinery
for the remaining gate assertions:

  * 640x400 source exports + matching FreeRDP /size (scale stays 1.0) so two
    windows fit at partial OVERLAP on the viewer head;
  * the v30 qdwin_shell_v1 `request_set_position` (bystander `move`) so the
    shell drives independent geometry (assertion 2);
  * per-stream VM-A control units (mm-control-a/b on 5571/5572) + a host-side
    ViewerBroker that learns each stream_id from Announce and routes a viewer
    close through the SOURCE (CloseRequest → source Closed) — assertions 6/7/8;
  * a self-calibrating pointer-overlap confinement probe (a viewer pixel is in
    the overlap IFF clicking it hits A when A is topmost and B when B is
    topmost) — assertion 4, no ydotool-scale assumption.

Phases (same VMs reused across invocations):
  main       : assertions 1,2,3,4,6,7,8 (+9 artefact). allow_input A=1 B=1.
  allowinput : assertion 5 only (per-window SOURCE-enforced allow_input).

Usage: drive-r1gate.py <VM-A> <VM-B> [--phase main|allowinput]
                       [--allow-a 0|1] [--allow-b 0|1]
"""
import argparse
import sys
import time
from pathlib import Path

REPO = Path("/home/play2/qdistro/qdistro")
sys.path.insert(0, str(REPO))
from multimachine.harness.vm_backend import QciVMBackend          # noqa: E402
from multimachine.harness import oracle as O, marker as M         # noqa: E402
from multimachine.harness.capture import load_image               # noqa: E402
from multimachine.harness.viewer_broker import ViewerBroker       # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("vm_a"); ap.add_argument("vm_b")
ap.add_argument("--phase", default="main", choices=["main", "allowinput"])
ap.add_argument("--allow-a", type=int, default=1)
ap.add_argument("--allow-b", type=int, default=1)
args = ap.parse_args()

VMA, VMB = args.vm_a, args.vm_b
W, H, GEN = 640, 400, 51
RELAY_A, RELAY_B = 5555, 5560
CTRL_A, CTRL_B = 5571, 5572
TEL_A = "/run/user/1000/mm-tel-a.json"
TEL_B = "/run/user/1000/mm-tel-b.json"
APP_A, APP_B = "qdistro.mm.vm-a.streamA", "qdistro.mm.vm-a.streamB"
# Initial overlapping placement (codex impl-32 Q1). Outer-rect top-lefts on the
# viewer head; both windows 640x400. A's barcode (TL) stays in A's exclusive area;
# B's barcode (TL) sits in the overlap (only decodable when B is raised).
POS_A0 = (40, 40)
POS_B0 = (360, 220)         # overlap = (360,220)-(680,440) = 320x220
POS_A1 = (200, 200)         # the MOVE target for assertion 2 (still overlaps B)
BUNDLE = Path(f"/tmp/mm-live/r1gate-{args.phase}")
BUNDLE.mkdir(parents=True, exist_ok=True)

be = QciVMBackend(vm_a=VMA, vm_b=VMB, repo_dir=REPO, out_w=W, out_h=H,
                  relay_port=RELAY_A)
results: dict[str, bool] = {}


def check(name, cond, detail=""):
    results[name] = bool(cond)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}  {detail}", flush=True)
    return bool(cond)


def press_total(t):
    tot = (t or {}).get("totals", {})
    return int(tot.get("button_press", 0)) + int(tot.get("key_press", 0))


def key_total(t):
    return int((t or {}).get("totals", {}).get("key_press", 0))


def button_total(t):
    return int((t or {}).get("totals", {}).get("button_press", 0))


def live_handles(settle=2.0, tries=12):
    """Re-resolve the CURRENT live (streamA, streamB) handles via secctx app_id
    (SDL3 churns toplevels; fail closed on != exactly one survivor per stream)."""
    mm = {}
    for _ in range(tries):
        tops = be.viewer_qdwin_toplevels("vm-b")
        mm = {h: d for h, d in tops.items() if d["engine"] == "qdistro.mm"}
        a = [h for h, d in mm.items() if d["app_id"].endswith(".streamA")]
        b = [h for h, d in mm.items() if d["app_id"].endswith(".streamB")]
        if len(a) == 1 and len(b) == 1:
            return a[0], b[0], mm
        time.sleep(settle)
    return None, None, mm


def setup_source(allow_a, allow_b):
    print(f"--- VM-A: source exports (A allow_input={allow_a}, "
          f"B allow_input={allow_b}) ---", flush=True)
    appr_a = be.setup_confinement_source(
        "vm-a", generation=GEN, width=W, height=H,
        exported_telemetry=TEL_A, sentinel_telemetry="",
        exported_label="A", sentinel_label="sentinel", allow_input=allow_a)
    print(f"  stream A relay={appr_a.rdp_port} otp={appr_a.rdp_password[:6]}…",
          flush=True)
    appr_b = be.setup_second_export(
        "vm-a", generation=GEN, width=W, height=H, output_id=2,
        telemetry=TEL_B, label="B", relay_port=RELAY_B, allow_input=allow_b)
    print(f"  stream B relay={appr_b.rdp_port} otp={appr_b.rdp_password[:6]}…",
          flush=True)
    return appr_a, appr_b


def setup_control():
    print("--- VM-A: per-stream control units ---", flush=True)
    sid_a = be.launch_control(
        "vm-a", generation=GEN, window_id=1, source_machine="vm-a",
        title="A", app_id=APP_A, req_w=W, req_h=H, marker_unit="mm-marker",
        unit="mm-control-a", control_port=CTRL_A)
    sid_b = be.launch_control(
        "vm-a", generation=GEN, window_id=2, source_machine="vm-a",
        title="B", app_id=APP_B, req_w=W, req_h=H, marker_unit="mm-marker2",
        unit="mm-control-b", control_port=CTRL_B)
    print(f"  control A stream_id={sid_a} port={CTRL_A}", flush=True)
    print(f"  control B stream_id={sid_b} port={CTRL_B}", flush=True)
    return sid_a, sid_b


def setup_viewer(appr_a, appr_b):
    print("--- VM-B: viewer qdwin stack (two 640x400 windowed FreeRDP) ---",
          flush=True)
    vout = be.launch_viewer_qdwin(
        "vm-b", rdp_host="10.0.2.2",
        port_a=RELAY_A, otp_a=appr_a.rdp_password,
        port_b=RELAY_B, otp_b=appr_b.rdp_password,
        stream_a="streamA", stream_b="streamB", origin="vm-a")
    (BUNDLE / "viewer-stack.out").write_text(vout)


def make_broker(allow_a, allow_b):
    broker = ViewerBroker(control_host="127.0.0.1")
    broker.add_stream("a", origin="vm-a", app_id=APP_A, rdp_unit="mm-rdp-a",
                      relay_port=RELAY_A, control_port=CTRL_A,
                      marker_unit="mm-marker", window_id=1, allow_input=allow_a)
    broker.add_stream("b", origin="vm-a", app_id=APP_B, rdp_unit="mm-rdp-b",
                      relay_port=RELAY_B, control_port=CTRL_B,
                      marker_unit="mm-marker2", window_id=2, allow_input=allow_b)
    sid_a = broker.connect("a", timeout=30)
    sid_b = broker.connect("b", timeout=30)
    print(f"  broker connected: A.stream_id={sid_a} B.stream_id={sid_b}",
          flush=True)
    return broker


# ---------- decode helpers ----------
layout = M.compute_layout(W, H)


def crop(img, rect, pad=0):
    x, y, w, h = rect
    y0 = max(0, y - pad); x0 = max(0, x - pad)
    return img[y0:y + h + pad, x0:x + w + pad]


def decode_in_rect(tag, rect, expect_out, tries=8):
    """Raise nothing; just crop the capture to `rect` and auto-origin decode,
    asserting the expected source output_id (1=A, 2=B). Returns OracleResult."""
    res = O.OracleResult(ok=False, payload=None, payload_error="no capture")
    for _ in range(tries):
        cap = be.capture("vm-b", 0, BUNDLE / f"decode-{tag}.ppm")
        img = load_image(cap)
        sub = crop(img, rect, pad=0)
        res = O.evaluate(sub, layout, 1.0, tol=O.TOL_RDP, auto_origin=True,
                         active_generation=GEN, expect_output_id=expect_out)
        if res.ok:
            return res
        time.sleep(1.5)
    return res


def raise_handle(h):
    be.viewer_fifo("vm-b", f"raise {h}")
    time.sleep(2.0)


def move_handle(h, x, y):
    be.viewer_fifo("vm-b", f"move {h} {x} {y}")
    time.sleep(2.0)


# ============================ PHASES ============================
def phase_main():
    appr_a, appr_b = setup_source(1, 1)
    setup_control()
    setup_viewer(appr_a, appr_b)
    broker = make_broker(1, 1)

    ha, hb, mm = live_handles()
    print(f"  live mm toplevels: {mm}", flush=True)
    check("A1-two-distinct-managed-objects",
          ha is not None and hb is not None and ha != hb and len(mm) == 2,
          f"handle_A={ha} handle_B={hb}")
    if not (ha and hb):
        return finish()
    broker.bind_handle("a", ha); broker.bind_handle("b", hb)

    # ---- place the two windows at partial-overlap geometry (assertion 2 setup) ----
    move_handle(ha, *POS_A0)
    move_handle(hb, *POS_B0)
    geo = be.viewer_qdwin_geometry("vm-b")
    print(f"  geometry after place: {geo}", flush=True)
    ga, gb = geo.get(ha), geo.get(hb)
    check("A2-independent-geometry-distinct-rects",
          ga is not None and gb is not None and ga[:2] != gb[:2]
          and abs(ga[0] - POS_A0[0]) <= 4 and abs(gb[0] - POS_B0[0]) <= 4,
          f"A={ga} B={gb}")

    # ---- assertion 1 decode: each stream 1:1 at its output_id (raise then crop) ----
    raise_handle(ha)
    ra = decode_in_rect("A", (POS_A0[0], POS_A0[1], W, H), 1)
    check("A1-streamA-decodes-1to1-out1", ra.ok, ra.summary())
    raise_handle(hb)
    rb = decode_in_rect("B", (POS_B0[0], POS_B0[1], W, H), 2)
    check("A1-streamB-decodes-1to1-out2", rb.ok, rb.summary())

    # ---- assertion 2: z-order composites the overlap (topmost wins), proven by
    #      OCCLUSION ASYMMETRY. B's barcode (at B's TL, which lies UNDER A's rect)
    #      is decodable from B's rect IFF B is raised: with A on top it is occluded
    #      (no out-2 decode), with B on top it decodes out-2. The viewer compositor
    #      decides what is visible in the overlap — not source-side geometry. (The
    #      barcode is the only id-bearing region; bands are identical across streams,
    #      so a content-pixel compare can't distinguish them — occlusion of the
    #      id-bearing region is the honest test.) B's TL (360,220) is inside A's rect
    #      (40,40)+(640,400), so A genuinely covers it. ----
    b_rect = (POS_B0[0], POS_B0[1], W, H)
    raise_handle(ha)                              # A on top → B's barcode occluded
    occ = decode_in_rect("Bunder", b_rect, 2, tries=3)
    raise_handle(hb)                              # B on top → B's barcode visible
    vis = decode_in_rect("Bover", b_rect, 2, tries=6)
    check("A2-zorder-occludes-overlap",
          (not occ.ok) and vis.ok,
          f"B-occluded-decode={occ.ok}(want False) B-raised-decode={vis.ok}(want True)")

    # ---- assertion 2 core: move A; B's rect + stream + input unperturbed ----
    gb_before = be.viewer_qdwin_geometry("vm-b").get(hb)
    fb_before = (be.read_telemetry("vm-a", TEL_B) or {}).get("totals", {})
    move_handle(ha, *POS_A1)
    geo2 = be.viewer_qdwin_geometry("vm-b")
    ga2, gb_after = geo2.get(ha), geo2.get(hb)
    moved_a = ga2 is not None and abs(ga2[0] - POS_A1[0]) <= 4 and abs(ga2[1] - POS_A1[1]) <= 4
    b_unperturbed = gb_after == gb_before
    # B still decodes + remains input-capable after A moved.
    raise_handle(hb)
    rb2 = decode_in_rect("Bafter", (POS_B0[0], POS_B0[1], W, H), 2)
    be.viewer_fifo("vm-b", f"focus {hb}")
    time.sleep(1.0)
    b_k0 = key_total(be.read_telemetry("vm-a", TEL_B))
    be.inject_key("vm-b")
    time.sleep(1.5)
    b_k1 = key_total(be.read_telemetry("vm-a", TEL_B))
    check("A2-move-A-does-not-perturb-B",
          moved_a and b_unperturbed and rb2.ok and (b_k1 - b_k0) > 0,
          f"A_moved={moved_a} B_rect={gb_before}->{gb_after} "
          f"B_decode={rb2.ok} B_dkey={b_k1 - b_k0}")

    # ---- assertion 3: shell-owned keyboard focus routes to ONLY the focused source ----
    def keyfocus(stream, h):
        raise_handle(h)
        be.viewer_fifo("vm-b", f"focus {h}")
        time.sleep(1.5)
        a0 = key_total(be.read_telemetry("vm-a", TEL_A))
        b0 = key_total(be.read_telemetry("vm-a", TEL_B))
        be.inject_key("vm-b")
        time.sleep(1.5)
        da = key_total(be.read_telemetry("vm-a", TEL_A)) - a0
        db = key_total(be.read_telemetry("vm-a", TEL_B)) - b0
        print(f"  focus {stream}: dKEY A={da} B={db}", flush=True)
        return da, db
    da, db = keyfocus("A", ha)
    check("A3-focus-A-keyboard-to-sourceA-only", da > 0 and db == 0, f"dA={da} dB={db}")
    da, db = keyfocus("B", hb)
    check("A3-focus-B-keyboard-to-sourceB-only", db > 0 and da == 0, f"dA={da} dB={db}")

    # ---- assertion 4: per-stream POINTER confinement under overlap ----
    # A viewer pixel is in the overlap IFF clicking it lands on A when A is
    # topmost AND on B when B is topmost. Scan ydotool coords for such a witness
    # (self-calibrating — no ydotool→head scale assumption).
    witness = find_overlap_pointer_witness(ha, hb)
    if witness is None:
        check("A4-pointer-confinement-under-overlap", False, "no overlap witness coord found")
    else:
        (cx, cy), (wa_da, wa_db, wb_da, wb_db) = witness
        check("A4-pointer-confinement-under-overlap",
              wa_da > 0 and wa_db == 0 and wb_db > 0 and wb_da == 0,
              f"coord=({cx},{cy}) A-top:dA={wa_da},dB={wa_db} "
              f"B-top:dB={wb_db},dA={wb_da}")

    # ---- assertions 6/7/8: source-mediated close ----
    close_lifecycle(broker, ha, hb)

    # ---- assertion 9: anti-fake artefact ----
    write_artefact(broker)
    return finish()


def find_overlap_pointer_witness(ha, hb):
    """Find a ydotool absolute coord that is in the window overlap, proving
    topmost-confined pointer routing — a viewer pixel is in the overlap IFF a
    click there lands on A when A is topmost AND on B when B is topmost (so the
    witness is self-calibrating; no ydotool→head scale assumption). To make a
    witness easy to hit regardless of the unknown apparatus scale, first
    REPLACE the geometry with a LARGE overlap anchored near the head origin
    (both windows cover the head centre), then scan a modest grid and stop at
    the first witness. Returns ((cx,cy),(A-top dA,dB, B-top dA,dB)) or None."""
    # large overlap near origin: A=(0,0), B=(180,140) → overlap (180,140)+ covers
    # the central region most ydotool coords map into for any plausible scale.
    move_handle(ha, 0, 0)
    move_handle(hb, 180, 140)
    geo = be.viewer_qdwin_geometry("vm-b")
    print(f"  A4 overlap geometry: {geo.get(ha)} / {geo.get(hb)}", flush=True)
    cands = []
    for gx in range(60, 420, 60):
        for gy in range(60, 360, 60):
            cands.append((gx, gy))

    def click_delta(coord, top):
        raise_handle(top)
        a0 = button_total(be.read_telemetry("vm-a", TEL_A))
        b0 = button_total(be.read_telemetry("vm-a", TEL_B))
        be.inject_input("vm-b", x=coord[0], y=coord[1], absolute=True)
        time.sleep(1.0)
        da = button_total(be.read_telemetry("vm-a", TEL_A)) - a0
        db = button_total(be.read_telemetry("vm-a", TEL_B)) - b0
        return da, db

    seen = set()
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        wa_da, wa_db = click_delta(c, ha)        # A topmost
        if not (wa_da > 0 and wa_db == 0):
            continue                             # not on A (or leaked) — skip
        wb_da, wb_db = click_delta(c, hb)        # B topmost, same coord
        print(f"  overlap probe ({c[0]},{c[1]}): A-top dA={wa_da},dB={wa_db} "
              f"B-top dA={wb_da},dB={wb_db}", flush=True)
        if wb_db > 0 and wb_da == 0:
            return c, (wa_da, wa_db, wb_da, wb_db)
    return None


def close_lifecycle(broker, ha, hb):
    """Assertions 6/7/8: viewer close A is SOURCE-mediated. Broker sends
    CloseRequest(A) → VM-A stops mm-marker → source emits Closed(A) → we tear
    down ONLY peer A. B stays live + input-capable; B's source pid unchanged."""
    b_pid_before = source_pid("mm-marker2")
    print(f"  B source pid before close A: {b_pid_before}", flush=True)
    # peer A must be visible + its FreeRDP unit alive BEFORE close.
    a_alive_pre = be.rdp_client_alive("vm-b", "a")
    broker.request_source_close("a")
    closed = broker.wait_closed("a", timeout=25)
    check("A7-close-A-source-mediated-Closed",
          a_alive_pre and closed is not None and closed.stream_id == broker.peers["a"].stream_id,
          f"a_alive_pre={a_alive_pre} closed={closed.reason if closed else None}")
    # ONLY after source Closed do we tear down peer A's pixel backend.
    if closed is not None:
        be.stop_rdp_client("vm-b", "a")
    time.sleep(3)
    # peer A removed from the viewer; peer B still a live managed toplevel.
    _, hb2, mm2 = live_handles_b_only()
    a_gone = not any(d["app_id"].endswith(".streamA") for d in mm2.values())
    b_still = hb2 is not None
    check("A6-only-peerA-removed-B-stays", a_gone and b_still,
          f"a_gone={a_gone} b_handle={hb2}")
    # B still input-capable after A closed.
    if hb2:
        raise_handle(hb2); be.viewer_fifo("vm-b", f"focus {hb2}"); time.sleep(1.0)
        k0 = key_total(be.read_telemetry("vm-a", TEL_B))
        be.inject_key("vm-b"); time.sleep(1.5)
        k1 = key_total(be.read_telemetry("vm-a", TEL_B))
        check("A6-B-still-input-capable", (k1 - k0) > 0, f"B dkey={k1 - k0}")
    b_pid_after = source_pid("mm-marker2")
    check("A8-process-truth-B-source-pid-unchanged",
          b_pid_before and b_pid_after and b_pid_before == b_pid_after,
          f"B pid {b_pid_before} -> {b_pid_after}")


def live_handles_b_only(settle=2.0, tries=8):
    mm = {}
    for _ in range(tries):
        tops = be.viewer_qdwin_toplevels("vm-b")
        mm = {h: d for h, d in tops.items() if d["engine"] == "qdistro.mm"}
        b = [h for h, d in mm.items() if d["app_id"].endswith(".streamB")]
        if len(b) == 1:
            return None, b[0], mm
        time.sleep(settle)
    return None, None, mm


def source_pid(unit):
    # systemctl --user needs the admin uid's session bus + XDG_RUNTIME_DIR, so
    # go through the admin wrapper (be.exec runs as root → empty MainPID).
    out = be._vmexec(be._real("vm-a"), be._as_admin(
        f"systemctl --user show {unit} -p MainPID --value 2>/dev/null"),
        check=False)
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    pid = lines[-1] if lines else "0"
    return pid if pid and pid != "0" else ""


def phase_allowinput():
    """Assertion 5: per-window allow_input is SOURCE-enforced. With this run's
    (allow_a, allow_b), the viewer focuses+injects into EACH peer; only the
    allow_input=1 stream's source telemetry increments — proving a live viewer
    route cannot bypass source-side enforcement (non-vacuous by symmetry across
    the two invocations A=0/B=1 and A=1/B=0)."""
    aa, ab = args.allow_a, args.allow_b
    appr_a, appr_b = setup_source(aa, ab)
    setup_viewer(appr_a, appr_b)
    ha, hb, mm = live_handles()
    check("A5-two-managed-objects", ha is not None and hb is not None, f"A={ha} B={hb}")
    if not (ha and hb):
        return finish()
    # distinct positions so shell focus is unambiguous (co-located windows make
    # raise/focus routing ambiguous — the source-enforcement test needs the viewer
    # to deliver input to exactly the focused peer).
    move_handle(ha, *POS_A0)
    move_handle(hb, *POS_B0)

    def focus_inject(h, tel):
        raise_handle(h); be.viewer_fifo("vm-b", f"focus {h}"); time.sleep(1.5)
        k0 = key_total(be.read_telemetry("vm-a", tel))
        be.inject_key("vm-b"); time.sleep(1.5)
        return key_total(be.read_telemetry("vm-a", tel)) - k0
    da = focus_inject(ha, TEL_A)
    db = focus_inject(hb, TEL_B)
    print(f"  allow_input A={aa} B={ab}: dKEY A={da} B={db}", flush=True)
    # source-A increments IFF allow_a, source-B IFF allow_b — server-enforced.
    check(f"A5-allow_input-A{aa}-B{ab}-source-enforced",
          (da > 0) == bool(aa) and (db > 0) == bool(ab),
          f"dA={da}(expect>{0 if aa else '0=0'}) dB={db}(expect {'>' if ab else '=='}0)")
    write_artefact(None)
    return finish()


def write_artefact(broker):
    (BUNDLE / "bystander.out").write_text(be.viewer_qdwin_log("vm-b"))
    (BUNDLE / "geometry.txt").write_text(str(be.viewer_qdwin_geometry("vm-b")))
    lines = ["# rung-1 gate artefact", f"phase={args.phase}",
             f"ports: RELAY_A={RELAY_A} RELAY_B={RELAY_B} CTRL_A={CTRL_A} "
             f"CTRL_B={CTRL_B}", f"sizes: {W}x{H} gen={GEN}"]
    if broker is not None:
        for lbl in ("a", "b"):
            lines.append(f"peer {lbl}: {broker.status(lbl)}")
        lines.append(f"control A produced: {be.control_log('vm-a', 'mm-control-a')}")
        lines.append(f"control B produced: {be.control_log('vm-a', 'mm-control-b')}")
    (BUNDLE / "artefact.txt").write_text("\n".join(lines) + "\n")
    print(f"  artefact -> {BUNDLE}", flush=True)


def finish():
    print(f"=== rung-1 gate phase={args.phase} results ===", flush=True)
    for k, v in results.items():
        print(f"   {k}: {'ok' if v else 'FAIL'}", flush=True)
    passed = bool(results) and all(results.values())
    print("=== rung-1 gate", "PASSED" if passed else "FAILED",
          f"(phase={args.phase}) ===", flush=True)
    return 0 if passed else 1


print(f"=== R1 gate phase={args.phase}: VM-A={VMA} VM-B={VMB} ===", flush=True)
be.spin("vm-a"); be.spin("vm-b")
rc = phase_main() if args.phase == "main" else phase_allowinput()
sys.exit(rc)

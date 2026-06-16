# Two-VM live apparatus for the decoded-remote capture (PLAN A)

These two scripts are the **proven** (2026-06-16, session 2) live apparatus behind
the decoded-remote pixel capture and the B1/B4 survive-unmount probe. They are the
concrete, reproducible form of the flow the `QciVMBackend`
(`multimachine/harness/vm_backend.py`) drives for `scenario.run_viewer_slice`.

Topology (codex impl-4 PLAN A): VM-A renders the source + serves per-view RDP;
VM-B decodes it on its own DRM head; RDP bytes are chained over **two SLIRP NATs
meeting at host loopback** (no bridge, no root). The decoded head is captured
host-side with `virsh screenshot` (QMP) — independent of the guest agent.

## source-stack.sh (VM-A)

A **dedicated headless qdwin** (separate from the production greetd session, so it
needs no shell/locker dance) + the shipped per-view path + a fixed-port relay.

Run as the admin uid (uid 1000 — where PipeWire is live), e.g.:

```
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
  W=1280 H=800 GEN=20 FS=1 bash source-stack.sh
```

Env: `W`/`H` (qdwin output + marker size), `GEN` (generation stamped in the
marker), `FS=1` (fullscreen marker — avoids qdwin chrome/inset clipping the
barcode), `RELAY_PORT` (default 5555). Prints `SETUP_OK RDP_PORT=<dyn>
RELAY_PORT=<fixed>` and leaves `RDP_PASSWORD=<otp>` in
`/run/user/1000/bystander.out`.

Critical envs it sets for qdwin: `WESTON_MODULE_MAP` (so qdwin-shell loads
`pipewire-backend.so` from `/usr/lib64/libweston-14`) and a weston.ini with
`[pipewire] num-outputs=4` — without the PipeWire **backend** loaded the per-view
subscribe is denied "no free pipewire output".

### one-time host step (the SLIRP hostfwd)

So VM-B can reach VM-A's relay through host loopback:

```
virsh -c qemu:///session qemu-monitor-command <vm-a> --hmp \
  "hostfwd_add hostnet0 tcp:127.0.0.1:5555-:5555"
```

(`hostnet0` is the user-net id; check `info network`.) VM-B then connects to
`10.0.2.2:5555`, which its SLIRP maps to host `127.0.0.1:5555` = VM-A's hostfwd.

## decoder-stack.sh (VM-B)

`seatd` + a **kiosk-shell weston** (DRM backend, own head) + `sdl-freerdp` as a
**fullscreen Wayland client**. Run as root (vm-exec default):

```
OTP=<otp from VM-A> W=1280 H=800 bash decoder-stack.sh
```

Why these choices (hard-won, session 2):
- **kiosk-shell**, not desktop-shell: kiosk places the fullscreen surface flush at
  the output origin (0,0); desktop-shell *centres* it and clipped the marker's
  top-left quiet zone, breaking the barcode decode (bands/scale were fine).
- **weston (5b)**, not SDL-kmsdrm (5a): SDL kmsdrm connected + decoded but never
  took the CRTC / VT graphics mode when launched head-less (no logind/active VT) —
  the capture showed only its text stderr. weston via seatd owns DRM cleanly.
- **seatd**: libseat here has no `builtin` backend and weston 14 needs
  seatd/logind; `seatd` is a tiny package (zypper) — the bake should include it.

## capture + verdict

```
virsh -c qemu:///session screenshot <vm-b> --screen 0 decoded.png
```

Then `oracle.evaluate(img, compute_layout(W,H), 1.0, auto_origin=True,
tol=TOL_RDP, active_generation=GEN, expect_output_id=1)` must be `ok` with
`measured_scale==1.0` and `hidden_scaling==False`, and the evidence bundle's
`assert_remote_proof()` must pass (a passing oracle record on a `VM_B_HOST`
capture). Committed reference: `tests/unit/data/mm/live-decoded-remote-*.png`.

## honesty scope

Proves source→RDP-encode→peer-decode→peer-monitor pixels are geometrically/colour
correct at 1:1 with no hidden scaling (within RDP tolerance), on a *separate
machine's head*. The RDP byte path is host loopback (not a bridged inter-VM L2);
netem on VM-A models link impairment but the loopback relay leg bypasses it — state
that, don't imply a real network.

---

# A1-min two-output straddle apparatus (`a1-straddle-stack.sh`)

The **proven** (2026-06-16 session 3) live render-gate apparatus for A1-min: one
qdwin instance, two adjacent real outputs, one marker toplevel composited across
the seam, captured per-output. Single-VM (no RDP, no second machine) — this is a
LOCAL render proof (`VM_A_HOST`), not decoded-remote.

The hard part was getting **two capturable heads** (codex impl-7 called it "its
own discovery project"). The recipe that worked, after QXL-multihead and
two-separate-cards both failed (QXL/2nd-card heads boot `disconnected` /
weston drives only the primary card):

- **ONE `virtio-gpu-pci,id=gpu0,max_outputs=2`** so both outputs live on one DRM
  card weston actually drives (the 2-output enable is in the weston log:
  `Output 'Virtual-1' enabled` + `Output 'Virtual-2' enabled`).
- **`-display egl-headless,rendernode=/dev/dri/renderD128`** so QEMU registers a
  *console per scanout* — without it `screendump head=1` is "no such screen ID"
  and `virsh screenshot --screen 1` fails (only head 0 exists under `-display
  none`). Needs a host render node + EGL.
- **kernel `video=Virtual-2:800x600e`** (force-enable, the `e` suffix) — a
  headless QEMU boots the 2nd virtio connector `disconnected`, so weston won't
  enable it; forcing it connected at the DRM layer fixes that.
- capture each output host-side with **raw QMP** `screendump device=gpu0 head=N`
  (NOT `virsh screenshot --screen N`, whose enumeration sees only head 0).

`a1-straddle-stack.sh` (run as root in the guest) reuses the production
compositor unit `noctalia-session.service` (it already acquires seat0 via the
admin user-session + seatd `-g seat`; a bare `systemd-run` weston is rejected
"Broken pipe" — not in the active session / root not in group `seat`). It
overrides `~/weston.ini` to two 800×600 outputs (pixman, scale 1), injects the
`QDWIN_TEST_PLACE_*` env via a unit drop-in, stops the QML shell (chrome), and
launches the marker. `MODE=straddle` places it at global (544,100) so its
seam_x=256 lands on the output boundary x=800; `MODE=calib` places it wholly in
out0 at (100,100) to prove zero decoration offset + the head→output mapping.

## verdict

`oracle.evaluate_straddle(out0, out1, compute_layout(512,400,seam_x=256),
marker_x_in_out0=544, oy=100, scale=1.0, active_generation=20, expect_output_id=1)`
must be `ok` with `seam_continuous`, `measured_scale==1.0`, `hidden_scaling==False`.
Committed reference + regression: `tests/unit/data/mm/live-straddle-a1-*.png`,
`TestLiveStraddleA1`. The swapped head assignment MUST fail (proves the
screen-index→output mapping is established, not assumed).

## honesty scope (A1-min)

Proves only: one qdwin holds two adjacent outputs; libweston composites one normal
toplevel across the seam into both (correct complementary halves, no duplicate
full-surface render, no seam gap/overlap, no hidden per-output scaling, no stale
generation); the harness captures + verifies two-output evidence. Placement uses a
**test-only** hook, NOT WM policy. Does NOT prove runtime output add/remove,
hotplug, move/tiling/maximize policy, RDP-as-monitor, or input across the seam.
`egl-headless` + `video=...e` are a capturable-head test transport; they prove
qdwin/libweston multi-output composition, not a specific production GPU's
connector behaviour.

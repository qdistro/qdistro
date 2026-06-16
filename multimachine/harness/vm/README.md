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

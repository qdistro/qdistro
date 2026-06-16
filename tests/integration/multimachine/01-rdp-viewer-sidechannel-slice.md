# MM-01 — remote whole-window viewer slice (host-local, RDP-decode)

**Phase 1 (codex impl-1 = the per-window whole-window viewer; impl-2 one-host
slice).** Proves the central Phase-1 question: *can a remote whole-window viewer
work over the shipped per-view RDP transport, represented by the side-channel
state machine, with honest decoded-pixel + teardown evidence — on a single host?*

> **Scope / honesty (09 + impl-2):** this is a **host-local transport/control
> proof**, NOT two-VM remote-output proof and NOT A5 monitor fidelity. The
> capture is the **decoded RDP** path (the peer's view after decode), not qdwin's
> source framebuffer.

## Prerequisites (why this is VM-gated on the current host)

The shipped path spawns `/usr/bin/qdistro-forward` (libfreerdp-shadow3 RDP
server) and decodes with a FreeRDP client. On the dev host as of 2026-06-16:

- `sdl-freerdp` (FreeRDP 3.26 client) **is** installed;
- `qdistro-forward` is **not** built and **cannot** be (no `freerdp3-devel`/
  `winpr3`/`libpipewire-0.3` headers, no passwordless sudo);
- so this scenario **SKIPs on the bare host** and runs on the **qci VM bake**
  (FreeRDP 3.26 + `qdistro-forward` baked) or any host with those deps.

The contract this scenario exercises is already unit-tested host-side:
`multimachine.bridge`, `multimachine.sidechannel`, `multimachine.generation`,
`multimachine.harness.oracle` (`tests/unit/test_mm_*.py`).

## Setup

1. Start qdwin with a pipewire output pool (`[pipewire] num-outputs>=1`, the
   per-view forward prereq) — `tests/host/start.sh` style, or the qci VM session.
2. Launch the deterministic marker as the **source** toplevel:
   `qdwin-marker-client --width 800 --height 600 --output-id 1 --generation <G>
   --frame 0 --animate-ms 100` (animation lets the oracle read a changing frame
   counter through the decoded stream). Capture its qdwin toplevel handle `H`.

## Steps

### 1 — subscribe + bridge to the side-channel

Drive `subscribe_view_stream(H, "viewer", 800, 600, 1)` via
`qdwin-bystander --subscribe $H`; parse the `approved` line
(`PIPEWIRE_NODE_NAME`, `RDP_PORT`, `RDP_CERT_PATH`, `RDP_PASSWORD`).

Feed it through the bridge (host-side python):

```python
from multimachine.bridge import ViewStreamApproved, SourceWindowInfo, bridge_approved
from multimachine.sidechannel import RemoteViewerState
ap = ViewStreamApproved(PIPEWIRE_NODE_NAME, int(RDP_PORT), RDP_CERT_PATH, RDP_PASSWORD)
src = SourceWindowInfo(window_id=H, source_machine="self", title="marker",
                       app_id="qdwin-marker-client", req_w=800, req_h=600)
ann = bridge_approved(ap, src, generation=G)
viewer = RemoteViewerState(generation=G)
assert viewer.apply(ann)                      # proxy now tracked, correlated by stream_id
```

**Assert 1.1:** journal shows `qdwin: view_stream approved handle=$H ... rdp_port=<P>`.
**Assert 1.2:** `viewer.proxy_for_stream(ann.meta.stream_id)` is the marker window.

### 2 — decode the RDP stream + run the oracle (decoded-remote capture)

Launch the viewer's decode client with the no-scaling argv
(`multimachine.bridge.rdp_client_argv(ap, host, capture_path=...)` → `sdl-freerdp`
`/scale:100`). Capture the decoded client surface (host `virsh screenshot` of the
client window in the VM, or sdl-freerdp frame dump). Run the oracle with RDP
tolerance:

```python
from multimachine.harness import oracle as O, marker as M, capture as C
img = C.load_image(decoded_capture)
res = O.evaluate(img, M.compute_layout(800, 600), 1.0, tol=O.TOL_RDP,
                 active_generation=G, expect_output_id=1)
assert res.ok and not res.hidden_scaling
```

**Assert 2.1:** decoded markers classify (all 6 bands) within RDP tolerance.
**Assert 2.2:** `res.hidden_scaling` is False (no client-side scaling — the
monitor-extension invariant). Record the capture as `FREERDP_DECODED` /
`VM_B_HOST` class in the evidence bundle (NOT a source-intent class).

### 3 — input isolation (immediate follow-up; may be a second pass)

Inject one pointer/key via the viewer's RDP client; assert (marker/journal
evidence) it reaches **only** the exported source window (qdwin pins a per-stream
virtual seat to the source view — `qdwin.c qdwin_stream_seat_init`).

### 4 — teardown + survive-detach

Destroy the `qdwin_view_stream_v1` (or kill the RDP client). Map the resulting
`torn_down(reason)` via `bridge_torn_down` and apply to the viewer:

**Assert 4.1:** `Disconnect` → `viewer.windows == {}` and `viewer.connected`
False; a stale same-generation `Announce` is then rejected.
**Assert 4.2:** the **marker source app is still alive** (detach, not death) —
unless the test intentionally sent `CloseRequest`.

## Executable form

This scenario's flow is implemented as
`multimachine.harness.scenario.run_viewer_slice(backend, topology, ...)`. Wire a
real `VMBackend` (a thin adapter over qci `vm-exec` / `virsh screenshot --screen
N` / `tc`, + `qdwin-bystander --subscribe` for `subscribe_view_stream`) and call
it; it spins the VMs, applies netem, runs the marker + subscribe + bridge,
captures the decoded-remote framebuffer on VM-B, runs the oracle, maps teardown
to detach, asserts the source survives, and writes the evidence bundle. The
orchestration logic is already validated by `tests/unit/test_mm_scenario.py`
against a `MockBackend`; the only new code for a live run is the backend adapter.

## Pass criteria

All asserts hold; `run_viewer_slice(...).passed` is True; the evidence bundle
records the **decoded-remote** capture class and the netem profile, and
`assert_remote_proof()` holds (a passing oracle record on the decoded-remote
capture). Record as *"host-local Phase-1 RDP viewer transport/control slice
passed under netem profile X"* — never as two-VM remote-output or native-feel
proof.

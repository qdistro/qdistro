# qdistro-forward

External proxy process that qdwin spawns per forwarded view ( S3+).

Responsibilities (planned):
- Consume a per-view PipeWire stream from qdwin.
- Serve one RDP session on a dedicated port to a remote subscriber.
- Forward remote pointer/keyboard back to qdwin via `qdwin_stream_input_v1`.

Lifecycle owned by qdwin: spawned on `subscribe_view_stream` approval,
reaped on `qdwin_view_stream_v1.destroy` or peer disconnect. Runs as
its own process so crashes don't take down the compositor; meant to be
sandboxed (SELinux profile, minimal caps) once the production path is
in place.

## Current status (2026-04-22)

Scaffolding only. Launch-and-sleep prototype that validates the qdwin
spawn + SIGTERM-on-destroy lifecycle wiring. No RDP serving, no
PipeWire consumption, no input injection.

## Roadmap

- **S3a (this scaffold, commit `task(012)` S3):** argv parsing,
 logging, ready marker, SIGTERM handler.
- **S3b:** PipeWire consumer (gstreamer `pipewiresrc` or `libpipewire`
 directly) that decodes frames into a surface the RDP server can
 encode.
- **S3c:** RDP server binding — option pool:
 (1) gstreamer `rdp_sink` (doesn't exist yet)
 (2) subprocess FreeRDP server binary (does one exist?)
 (3) custom `freerdp-server-library` wrapper (C or cython) —
 ~500 LOC for a minimal encoder path.
 Decision TBD during S3b spike.
- **S4:** qbus-admin approval flow (subscribe blocks on approve).
- **S5:** `qdwin_stream_input_v1.claim` + `inject_*` handling in
 qdwin; RDP-peer-input → wayland input translation inside
 qdistro-forward.

## Invocation (future, current args stable)

 qdistro-forward \
 --pipewire-node weston.pipewire-0 \
 --access-token <random-hex> \
 --rdp-port 3401 \
 --rdp-cert-path /home/admin/qdwin-rdp/stream-3401.crt \
 --rdp-password <one-time> \
 --log-path /tmp/qdistro-forward-3401.log \
 --ready-marker /tmp/qf-3401.ready

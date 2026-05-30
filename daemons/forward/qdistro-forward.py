#!/usr/bin/env python3
"""qdistro-forward — external proxy process for one forwarded view.

Phase 6.5 S3 MVP scaffolding. Real responsibilities planned for a
follow-up:
  1. Connect to the PipeWire daemon and subscribe to
     --pipewire-node.
  2. Run an RDP server on --rdp-port, bound to 0.0.0.0 (or the
     configured bind address), TLS cert at --rdp-cert-path, require
     --rdp-password one-shot auth.
  3. Encode incoming PipeWire video frames to RDP.
  4. Connect to qdwin's wayland display, bind qdwin_stream_input_v1,
     call claim(--access-token), and route RDP-peer input to qdwin via
     inject_*.

This scaffolding does none of that yet — it logs the startup config,
touches a readiness marker, and sleeps until SIGTERM. It exists so
the parent (qdwin) can verify the spawn + lifecycle wiring end to end
while the real forwarding is being written.

Exit codes:
  0 clean SIGTERM from parent
  1 argv error
  2 pipewire daemon not reachable (once the connect is implemented)
"""
import argparse
import logging
import os
import signal
import sys
import time


def main() -> int:
    p = argparse.ArgumentParser(prog="qdistro-forward")
    # Cheap, side-effect-free health smoke: --version prints + exits 0
    # before any required-arg validation or PipeWire/RDP setup.
    p.add_argument("--version", action="version",
                   version="%(prog)s (qdistro)")
    p.add_argument("--pipewire-node", required=True,
                   help="PipeWire Node name to subscribe to "
                        "(e.g. weston.pipewire-0)")
    p.add_argument("--access-token", required=True,
                   help="one-shot token presented to "
                        "qdwin_stream_input_v1.claim")
    p.add_argument("--rdp-port", type=int, required=True,
                   help="TCP port to bind the RDP server on")
    p.add_argument("--rdp-cert-path", default="",
                   help="path to TLS cert (empty = generate ephemeral)")
    p.add_argument("--rdp-password", default="",
                   help="one-time password for RDP auth (empty = deny all)")
    p.add_argument("--log-path", default="",
                   help="append-only log file, stdout/stderr if empty")
    p.add_argument("--ready-marker", default="",
                   help="path to touch once the proxy has finished "
                        "initial setup; empty = none")
    args = p.parse_args()

    # Configure logging.
    handlers = [logging.StreamHandler(sys.stderr)]
    if args.log_path:
        handlers.append(logging.FileHandler(args.log_path))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [qdistro-forward pid=%(process)d] %(message)s",
        handlers=handlers,
    )
    log = logging.getLogger("qdistro-forward")

    log.info("starting: node=%s port=%d token=%s cert=%s",
             args.pipewire_node, args.rdp_port, args.access_token[:8] + "…",
             args.rdp_cert_path or "(none)")

    def handle_term(signum, frame):
        # Exit directly — logging from a handler can deadlock, and
        # time.sleep under PEP 475 just resumes on EINTR, so a flag
        # check in a sleep loop misses signals on Linux. Scaffolding
        # has no state worth tearing down yet.
        os._exit(0)

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)

    # === scaffolding placeholder ===
    # Real work (TBD):
    #   - connect pipewire stream
    #   - spin up FreeRDP server (python-freerdp? subprocess to external
    #     freerdp-server? ffmpeg pipeline?)
    #   - connect back to qdwin wayland for input injection
    log.info("scaffolding: no RDP server yet (see phase6.5 follow-up task)")

    if args.ready_marker:
        try:
            open(args.ready_marker, "w").close()
            log.info("ready marker touched: %s", args.ready_marker)
        except OSError as e:
            log.error("failed to touch ready marker: %s", e)

    # Use signal.pause() so SIGTERM actually wakes us; time.sleep is
    # not reliably interruptible on Linux post-PEP 475.
    while True:
        signal.pause()


if __name__ == "__main__":
    sys.exit(main())

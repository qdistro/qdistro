#!/bin/bash
# Dedicated headless-qdwin source stack on VM-A (admin uid 1000, where pipewire
# is live) for the decoded-remote capture. Private wayland socket wayland-mm —
# a separate compositor, unaffected by the production session's lock/shell.
# Run as: runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000
#         DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus bash mm-vma-setup.sh
set -uo pipefail   # pipefail surfaces in-pipe failures; cleanup lines are
                   # explicitly tolerated (|| true). The caller treats the final
                   # SETUP_OK token (not exit code alone) as the success signal.
export XDG_RUNTIME_DIR=/run/user/1000
# qdwin's wayland socket. Default wayland-mm (private). qdwin spawns qdistro-forward
# with `--wayland-display <its own socket>` (read from WAYLAND_DISPLAY, qdwin.c
# session-5 fix), so the forward claims the input-injection channel on whatever
# socket this qdwin listens on — the input-confinement gate no longer needs to force
# wayland-0 (was a HARDCODED `--wayland-display wayland-0` foot-gun, session-4).
SOCK=${SOCK:-wayland-mm}
W=${W:-1280}; H=${H:-800}; GEN=${GEN:-20}; RELAY_PORT=${RELAY_PORT:-5555}
MODE=${MODE:-full}
# Marker frame cadence. A capture (`virsh screenshot`) of an ANIMATING marker can
# catch a torn RDP frame mid-repaint (barcode CRC mismatch); ANIMATE_MS=0 = a
# single STATIC frame, which the single-frame oracle reads deterministically.
ANIMATE_MS=${ANIMATE_MS:-200}
# Step-8 input-confinement extras (codex impl-10), all optional/empty by default:
#   EXPORTED_TELEMETRY / EXPORTED_LABEL — the exported (subscribed) marker writes
#     per-seat input telemetry here; ALLOW_INPUT=1 → the bystander requests an
#     input-capable subscription so the forward gets the inject channel;
#   SENTINEL_TELEMETRY / SENTINEL_LABEL — launch a SECOND local (unexported)
#     marker as the confinement sentinel (must receive zero injected input).
EXPORTED_TELEMETRY=${EXPORTED_TELEMETRY:-}; EXPORTED_LABEL=${EXPORTED_LABEL:-exported}
SENTINEL_TELEMETRY=${SENTINEL_TELEMETRY:-}; SENTINEL_LABEL=${SENTINEL_LABEL:-sentinel}
ALLOW_INPUT=${ALLOW_INPUT:-0}
RUN() { systemd-run --user --collect "$@"; }

# discover_rdp_port FILE: wait for an approved RDP_PORT in a bystander.out file.
discover_rdp_port() {
  local f=$1 p=""
  for _ in $(seq 1 50); do
    p=$(grep -m1 '^RDP_PORT=' "$f" 2>/dev/null | cut -d= -f2 | tr -dc '0-9')
    [ -n "$p" ] && break; sleep 0.3
  done
  echo "$p"
}

# MODE=resubscribe: the qdwin + marker are ALREADY live (a prior full run). Prove
# the source can serve a FRESH export after the VM-B viewer left (codex impl-9 Q3
# stream-slot proof): re-run the bystander (a new subscription → a new dynamic RDP
# port + a fresh single-use OTP — the old OTP is single-use, B4), and repoint the
# fixed relay at it. The marker (source app) is untouched, so its survival is also
# asserted by the caller. Honesty: this proves re-exportability + source survival;
# with num-outputs>1 it is not a strict single-slot reclaim.
# MODE=sentinel: launch a SECOND, LOCAL, UNEXPORTED marker on the ALREADY-LIVE
# qdwin as the confinement detector (codex impl-10 Q3a). Launched SEPARATELY, AFTER
# the decoded oracle has captured the clean exported view — a visible sentinel
# toplevel overlaps the per-view output capture and corrupts the exported marker's
# bands, so it must not be up during the oracle (session-4 finding). It only needs
# to be live + binding seats during input injection.
if [ "$MODE" = sentinel ]; then
  if [ ! -S "$XDG_RUNTIME_DIR/$SOCK" ]; then echo "FAIL: no live qdwin socket for sentinel"; exit 9; fi
  RUN --unit=mm-sentinel --setenv=WAYLAND_DISPLAY=$SOCK \
    qdwin-marker-client --width 400 --height 300 --output-id 2 --generation $GEN --frame 0 --animate-ms $ANIMATE_MS --telemetry $SENTINEL_TELEMETRY --label $SENTINEL_LABEL
  sleep 1.5
  if systemctl --user is-active mm-sentinel >/dev/null 2>&1; then
    echo "SENTINEL_OK telemetry=$SENTINEL_TELEMETRY"
  else
    echo "FAIL: sentinel did not start"; journalctl --user -u mm-sentinel --no-pager|tail -10; exit 9
  fi
  exit 0
fi

if [ "$MODE" = resubscribe ]; then
  if [ ! -S "$XDG_RUNTIME_DIR/$SOCK" ]; then echo "FAIL: no live qdwin socket to re-export"; exit 9; fi
  if ! systemctl --user is-active mm-marker >/dev/null 2>&1; then echo "FAIL: marker not alive for re-export"; exit 9; fi
  systemctl --user stop mm-bystander mm-relay 2>/dev/null || true   # free the old slot
  sleep 0.5
  rm -f "$XDG_RUNTIME_DIR/bystander.out"
  RUN --unit=mm-bystander --setenv=WAYLAND_DISPLAY=$SOCK \
    bash -c "qdwin-bystander --subscribe last > $XDG_RUNTIME_DIR/bystander.out 2>&1"
  sleep 1.5
  RDP_PORT=$(discover_rdp_port "$XDG_RUNTIME_DIR/bystander.out")
  if [ -z "$RDP_PORT" ]; then echo "FAIL: re-subscribe not approved (slot not free?)"; echo "--- bystander.out ---"; cat "$XDG_RUNTIME_DIR/bystander.out"; exit 10; fi
  RUN --unit=mm-relay socat TCP-LISTEN:$RELAY_PORT,reuseaddr,fork TCP:127.0.0.1:$RDP_PORT
  sleep 0.5
  echo "--- bystander.out ---"; cat "$XDG_RUNTIME_DIR/bystander.out"
  echo "SETUP_OK RDP_PORT=$RDP_PORT RELAY_PORT=$RELAY_PORT"
  exit 0
fi

# MODE=export2: a SECOND, concurrent EXPORTED marker on the ALREADY-LIVE qdwin
# (2nd-exported-view isolation gate, codex impl-15). qdwin_shell_v1 is a SINGLETON
# role (only ONE bystander may bind it — a 2nd bystander gets "shell role already
# claimed"), so we DON'T start a second bystander. Instead we drive the EXISTING
# mm-bystander's command FIFO: launch marker-B (the new "last" toplevel), then send
# `subscribelast` so the one shell client subscribes marker-B too (with the same
# --allow-input it was started with → forward-B claims the inject channel on spawn,
# so marker-B's per-stream seat goes live). Then relay2 on a SECOND fixed port.
if [ "$MODE" = export2 ]; then
  if [ ! -S "$XDG_RUNTIME_DIR/$SOCK" ]; then echo "FAIL: no live qdwin socket for export2"; exit 9; fi
  if ! systemctl --user is-active mm-marker >/dev/null 2>&1; then echo "FAIL: marker-A not live for export2"; exit 9; fi
  if ! systemctl --user is-active mm-bystander >/dev/null 2>&1; then echo "FAIL: no live shell client (mm-bystander) for export2"; exit 9; fi
  OUTPUT_ID=${OUTPUT_ID:-2}
  BOUT="$XDG_RUNTIME_DIR/bystander.out"
  FIFO=${QDWIN_BYSTANDER_FIFO:-/tmp/qdwin-cmd.fifo}
  # RDP_PASSWORD is the LAST line of each approval block (HANDLE/NODE/PORT/CERT/
  # PASSWORD), so waiting for a NEW one guarantees port+node are already flushed —
  # no partial-block race (codex impl-16).
  before=$(grep -c '^RDP_PASSWORD=' "$BOUT" 2>/dev/null); before=${before:-0}
  systemctl --user stop mm-marker2 2>/dev/null || true
  systemctl --user reset-failed mm-marker2 2>/dev/null || true
  sleep 0.3
  FS_ARG=""; [ "${FS:-0}" = 1 ] && FS_ARG="--fullscreen"
  TEL_ARG=""; [ -n "$EXPORTED_TELEMETRY" ] && TEL_ARG="--telemetry $EXPORTED_TELEMETRY --label $EXPORTED_LABEL"
  RUN --unit=mm-marker2 --setenv=WAYLAND_DISPLAY=$SOCK \
    qdwin-marker-client --width $W --height $H --output-id $OUTPUT_ID --generation $GEN --frame 0 --animate-ms $ANIMATE_MS $FS_ARG $TEL_ARG
  sleep 1.5   # let the shell client see marker-B's toplevel_added (got_last)
  [ -p "$FIFO" ] || { echo "FAIL: bystander FIFO $FIFO missing"; exit 9; }
  echo subscribelast > "$FIFO"
  # wait for marker-B's COMPLETE approval block = a NEW RDP_PASSWORD line.
  RDP_PORT=""
  for _ in $(seq 1 50); do
    cnt=$(grep -c '^RDP_PASSWORD=' "$BOUT" 2>/dev/null); cnt=${cnt:-0}
    if [ "$cnt" -gt "$before" ]; then
      RDP_PORT=$(grep '^RDP_PORT=' "$BOUT" | tail -1 | cut -d= -f2 | tr -dc '0-9')
      break
    fi
    sleep 0.3
  done
  if [ -z "$RDP_PORT" ]; then echo "FAIL: export2 not approved (subscribelast)"; echo "--- bystander.out tail ---"; tail -12 "$BOUT"; exit 10; fi
  # marker-B's creds are the LAST (now-complete) approval block in the stdout.
  PW_NODE=$(grep '^PIPEWIRE_NODE_NAME=' "$BOUT" | tail -1 | cut -d= -f2)
  RDP_PASSWORD=$(grep '^RDP_PASSWORD=' "$BOUT" | tail -1 | cut -d= -f2)
  RUN --unit=mm-relay2 socat TCP-LISTEN:$RELAY_PORT,reuseaddr,fork TCP:127.0.0.1:$RDP_PORT
  sleep 0.5
  # echo ONLY marker-B's approval fields, each on its OWN line so parse_approved
  # (which anchors on `^RDP_PORT=`/`^RDP_PASSWORD=`) binds B, not A.
  echo "PIPEWIRE_NODE_NAME=$PW_NODE"
  echo "RDP_PORT=$RDP_PORT"
  echo "RDP_PASSWORD=$RDP_PASSWORD"
  echo "SETUP_OK RDP_PORT=$RDP_PORT RELAY_PORT=$RELAY_PORT"
  exit 0
fi

# Clean prior run (incl. the 2nd-export units so a crashed prior run can't poison
# this one — codex impl-16; esp. mm-relay2 holding port 5560).
systemctl --user stop mm-qdwin mm-marker mm-sentinel mm-bystander mm-relay \
  mm-marker2 mm-bystander2 mm-relay2 2>/dev/null || true
systemctl --user reset-failed mm-marker2 mm-bystander2 mm-relay2 2>/dev/null || true
sleep 1
rm -f "$XDG_RUNTIME_DIR/$SOCK" "$XDG_RUNTIME_DIR/$SOCK.lock" 2>/dev/null

# Custom weston.ini with a real pipewire output pool (the per-view capture
# pulls from it). Headless ignores the DRM [output]; pixman renderer.
cat > "$XDG_RUNTIME_DIR/mm-weston.ini" <<EOF
[core]
shell=/usr/lib64/weston/qdwin-shell.so
idle-time=0
[shell]
locking=false
[pipewire]
num-outputs=4
EOF

# 1) Headless qdwin on a private socket. WESTON_MODULE_MAP lets qdwin-shell load
#    pipewire-backend.so internally (the per-view capture pool) — same as prod.
MM="/usr/lib64/libweston-14"
WMAP="drm-backend.so=$MM/drm-backend.so;gl-renderer.so=$MM/gl-renderer.so;color-lcms.so=$MM/color-lcms.so;headless-backend.so=$MM/headless-backend.so;pipewire-backend.so=$MM/pipewire-backend.so;rdp-backend.so=$MM/rdp-backend.so;wayland-backend.so=$MM/wayland-backend.so;x11-backend.so=$MM/x11-backend.so;xwayland.so=$MM/xwayland.so"
RUN --unit=mm-qdwin --setenv=QDWIN_ALLOWED_UID=1000 --setenv=QDWIN_ALLOWED_LOCKER_ANY=1 \
  --setenv="WESTON_MODULE_MAP=$WMAP" \
  weston --backend=headless-backend.so --backend=pipewire-backend.so --renderer=pixman \
    --config="$XDG_RUNTIME_DIR/mm-weston.ini" \
    --width=$W --height=$H --socket=$SOCK >/dev/null 2>&1
for _ in $(seq 1 50); do [ -S "$XDG_RUNTIME_DIR/$SOCK" ] && break; sleep 0.2; done
if [ ! -S "$XDG_RUNTIME_DIR/$SOCK" ]; then echo "FAIL: qdwin socket never appeared"; journalctl --user -u mm-qdwin --no-pager|tail -20; exit 7; fi
echo "qdwin up on $XDG_RUNTIME_DIR/$SOCK"

# 2) Bystander first (so --subscribe last catches the EXPORTED marker's
#    toplevel_added). ALLOW_INPUT=1 requests an input-capable subscription.
rm -f "$XDG_RUNTIME_DIR/bystander.out"
AI_ARG=""; [ "$ALLOW_INPUT" = 1 ] && AI_ARG="--allow-input"
RUN --unit=mm-bystander --setenv=WAYLAND_DISPLAY=$SOCK \
  bash -c "qdwin-bystander --subscribe last $AI_ARG > $XDG_RUNTIME_DIR/bystander.out 2>&1"
sleep 1.5

# 3) EXPORTED marker (the subscribed source toplevel) WxH, animating. With
#    EXPORTED_TELEMETRY it counts per-seat injected input (step-8 gate).
FS_ARG=""; [ "${FS:-0}" = 1 ] && FS_ARG="--fullscreen"
TEL_ARG=""; [ -n "$EXPORTED_TELEMETRY" ] && TEL_ARG="--telemetry $EXPORTED_TELEMETRY --label $EXPORTED_LABEL"
RUN --unit=mm-marker --setenv=WAYLAND_DISPLAY=$SOCK \
  qdwin-marker-client --width $W --height $H --output-id 1 --generation $GEN --frame 0 --animate-ms $ANIMATE_MS $FS_ARG $TEL_ARG

# 4) Discover the approved RDP port.
RDP_PORT=""
for _ in $(seq 1 50); do
  RDP_PORT=$(grep -m1 '^RDP_PORT=' "$XDG_RUNTIME_DIR/bystander.out" 2>/dev/null | cut -d= -f2 | tr -dc '0-9')
  [ -n "$RDP_PORT" ] && break; sleep 0.3
done
if [ -z "$RDP_PORT" ]; then echo "FAIL: no RDP_PORT approved"; echo "--- bystander.out ---"; cat "$XDG_RUNTIME_DIR/bystander.out"; exit 8; fi

# 5) Fixed-port relay so the SLIRP hostfwd targets a stable port.
RUN --unit=mm-relay socat TCP-LISTEN:$RELAY_PORT,reuseaddr,fork TCP:127.0.0.1:$RDP_PORT
sleep 0.5

# NB the confinement SENTINEL is NOT launched here — it would overlap the per-view
# output capture and corrupt the exported marker's bands. The harness launches it
# separately (MODE=sentinel) AFTER the decoded oracle, before input injection.
echo "--- bystander.out ---"; cat "$XDG_RUNTIME_DIR/bystander.out"
echo "SETUP_OK RDP_PORT=$RDP_PORT RELAY_PORT=$RELAY_PORT"

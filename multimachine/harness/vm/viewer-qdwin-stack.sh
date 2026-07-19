#!/bin/bash
# VM-B viewer stack for Phase-2 rung-1 (codex impl-30 Option B): a REAL qdwin
# (weston shell=qdwin-shell.so) on the DRM head + qdwin-bystander as the bound
# qdwin_shell_v1 shell client + TWO *windowed* secctx-tagged FreeRDP clients,
# each decoding one source view_stream into its OWN managed qdwin toplevel.
#
# This is the deliberate substrate swap away from the Phase-1 viewer-stack.sh
# (kiosk-shell weston + a single FULLSCREEN sdl-freerdp). Here:
#   * the compositor is the REAL qdwin shell plugin, so the two decoded windows
#     are genuinely qdwin-managed peers (geometry/focus/stacking/decoration),
#     not a fullscreen client on a kiosk compositor (the impl-23 anti-pattern);
#   * each FreeRDP client is WINDOWED (no /f) and run under qdistro-secctx-exec
#     with engine=qdistro.mm so the bound shell sees a per-stream secctx identity
#     (qdwin_shell_v1.toplevel_security_context) — the load-bearing, non-title,
#     non-pixel attribution key (impl-30 Q6);
#   * qdwin-bystander is the bound shell client: it bind_as_shell (unholds the
#     held layer so pixels paint + focuses toplevels) AND reads a command FIFO so
#     the harness can drive maximize/focus to create a deterministic overlap and
#     prove viewer-topmost input routing.
#
# The captured head is read host-side via `virsh screenshot` (QMP), compositor-
# agnostic — identical capture path to viewer-stack.sh.
set -uo pipefail
RT=/run/mm-vb
SOCK=wayland-vb
FIFO=/tmp/qdwin-cmd.fifo
RDP_HOST=${RDP_HOST:-10.0.2.2}
RDP_PORT_A=${RDP_PORT_A:?need RDP_PORT_A}; OTP_A=${OTP_A:?need OTP_A}
RDP_PORT_B=${RDP_PORT_B:?need RDP_PORT_B}; OTP_B=${OTP_B:?need OTP_B}
STREAM_A=${STREAM_A:-streamA}; STREAM_B=${STREAM_B:-streamB}
ORIGIN=${ORIGIN:-vm-a}
W=${W:-1280}; H=${H:-800}; RDP_USER=${RDP_USER:-mm}
MM=/usr/lib64/libweston-16
WMAP="drm-backend.so=$MM/drm-backend.so;gl-renderer.so=$MM/gl-renderer.so;color-lcms.so=$MM/color-lcms.so;headless-backend.so=$MM/headless-backend.so;pipewire-backend.so=$MM/pipewire-backend.so;rdp-backend.so=$MM/rdp-backend.so;wayland-backend.so=$MM/wayland-backend.so;x11-backend.so=$MM/x11-backend.so;xwayland.so=$MM/xwayland.so"

# find a windowed-capable FreeRDP client (prefer the SDL frontend the Phase-1
# decoder proved; wlfreerdp is a native Wayland client and also windowed).
find_rdp_client() {
  for c in sdl-freerdp wlfreerdp xfreerdp3 xfreerdp; do
    command -v "$c" >/dev/null 2>&1 && { echo "$c"; return 0; }
  done
  return 1
}
RDP_CLIENT=$(find_rdp_client) || { echo "FAIL: no FreeRDP client on VM-B"; exit 5; }
command -v qdwin-bystander >/dev/null || { echo "FAIL: qdwin-bystander missing on VM-B"; exit 5; }
command -v qdistro-secctx-exec >/dev/null || { echo "FAIL: qdistro-secctx-exec missing on VM-B"; exit 5; }

# ---- teardown prior run -----------------------------------------------------
systemctl stop mm-qdwin mm-bystander-vb mm-rdp-a mm-rdp-b 2>/dev/null || true
systemctl reset-failed mm-qdwin mm-bystander-vb mm-rdp-a mm-rdp-b 2>/dev/null || true
pkill -f 'sdl-freerdp|wlfreerdp|xfreerdp' 2>/dev/null || true
pkill -x qdwin-bystander 2>/dev/null || true

# Free DRM + the seat from the production qdwin session (same foot-gun as
# viewer-stack.sh: a standard spun VM runs greetd-qdwin + the admin noctalia
# session holding DRM master + a root-rejecting `seatd -g seat`). Stop them and
# start our own unrestricted seatd.
systemctl stop greetd-qdwin greetd qdistro-session-manager 2>/dev/null || true
runuser -u admin -- systemctl --user stop noctalia-session noctalia-shell qdlocker 2>/dev/null || true
systemctl stop seatd.service seatd.socket 2>/dev/null || true
systemctl stop mm-seatd 2>/dev/null || true
pkill -x seatd 2>/dev/null || true
rm -f /run/seatd.sock 2>/dev/null || true
systemctl reset-failed mm-seatd 2>/dev/null || true
sleep 1
systemd-run --collect --unit=mm-seatd seatd
for _ in $(seq 1 30); do [ -S /run/seatd.sock ] && break; sleep 0.2; done
[ -S /run/seatd.sock ] && echo "seatd up" || { echo "FAIL: seatd socket missing"; journalctl -u mm-seatd --no-pager|tail -10; exit 6; }

# ydotoold at a fixed socket BEFORE weston (so weston enumerates the uinput
# device at startup) — the harness injects viewer input through it.
YDSOCK=/run/.ydotool_socket
systemctl stop mm-ydotoold 2>/dev/null || true
systemctl reset-failed mm-ydotoold 2>/dev/null || true
systemd-run --collect --unit=mm-ydotoold ydotoold --socket-path=$YDSOCK --socket-perm=0666
for _ in $(seq 1 30); do [ -S "$YDSOCK" ] && break; sleep 0.2; done
[ -S "$YDSOCK" ] && echo "ydotoold up ($YDSOCK)" || echo "WARN: ydotoold socket missing"
sleep 1

rm -rf "$RT"; mkdir -p "$RT"; chmod 0700 "$RT"
rm -f "$FIFO"

# ---- 1) real qdwin on the DRM head -----------------------------------------
cat > "$RT/qdwin.ini" <<EOF
[core]
shell=/usr/lib64/weston/qdwin-shell.so
idle-time=0
[shell]
locking=false
EOF
# DRM head only (virsh-screenshot-readable, proven by viewer-stack.sh), pixman
# renderer. The viewer never EXPORTS a view_stream, so it needs no pipewire
# sub-backend (pipewire-backend can't connect as root anyway). The DRM backend
# takes its resolution from the head — NOT --width/--height (those are headless-
# only; passing them is a fatal "unhandled option"). QDWIN_ALLOWED_UID=0: the
# whole viewer stack runs as root, so the root bystander may bind_as_shell.
# QDWIN_SECCTX_OPEN=1 is qdwin's dev/test secctx mode: it lets a helper launched
# from a plain shell (not the production trusted root launcher) bind
# wp_security_context_manager_v1. Test-only — rung-1-proper would spawn the
# windowed FreeRDP clients via the trusted launcher path so this isn't needed.
systemd-run --collect --unit=mm-qdwin \
  --setenv=XDG_RUNTIME_DIR=$RT --setenv=HOME=/root \
  --setenv=LIBSEAT_BACKEND=seatd \
  --setenv="WESTON_MODULE_MAP=$WMAP" \
  --setenv=QDWIN_ALLOWED_UID=0 --setenv=QDWIN_ALLOWED_LOCKER_ANY=1 \
  --setenv=QDWIN_SECCTX_OPEN=1 \
  weston --backend=drm-backend.so --renderer=pixman \
    --config="$RT/qdwin.ini" --socket=$SOCK
for _ in $(seq 1 80); do [ -S "$RT/$SOCK" ] && break; sleep 0.2; done
if [ ! -S "$RT/$SOCK" ]; then echo "FAIL: qdwin socket never appeared"; journalctl -u mm-qdwin --no-pager|tail -30; exit 7; fi
echo "qdwin up on $RT/$SOCK (client=$RDP_CLIENT)"

# ---- 2) qdwin-bystander as the bound shell client --------------------------
# No --subscribe: on the VIEWER we do not export view_streams; the bystander only
# binds_as_shell (unholds + focuses + decorates) and serves the command FIFO. Its
# stdout/stderr carry toplevel_added + toplevel_security_context lines we parse
# for handle<->stream attribution.
systemd-run --collect --unit=mm-bystander-vb \
  --setenv=XDG_RUNTIME_DIR=$RT --setenv=HOME=/root \
  --setenv=WAYLAND_DISPLAY=$SOCK \
  --setenv=QDWIN_BYSTANDER_FIFO=$FIFO \
  bash -c "qdwin-bystander > $RT/bystander.out 2>&1"
for _ in $(seq 1 40); do [ -p "$FIFO" ] && break; sleep 0.2; done
[ -p "$FIFO" ] || { echo "FAIL: bystander FIFO $FIFO never appeared"; journalctl -u mm-bystander-vb --no-pager|tail -20; exit 8; }
echo "bystander bound (fifo=$FIFO)"

# Set placement=center BEFORE the clients map (set_wm_policy affects NEW windows):
# the default 'smart' policy cascades the 2nd toplevel by (40,40), which offsets +
# clips a full-output-size window past the head so the decoded-marker oracle can't
# read it. center(0) lands both full-size windows at (0,0) → clean full overlap,
# z-order (raise) alone decides which is captured. wmpolicy args:
# <focus=click 0> <ffm_ms 0> <raise_on_click 1> <raise_on_hover 0>
# <placement=center 0> <snap_enabled 0> <snap_distance 8>.
echo "wmpolicy 0 0 1 0 0 0 8" > "$FIFO"
sleep 0.3

# ---- 3) two windowed secctx-tagged FreeRDP clients -------------------------
# argv mirrors bridge.rdp_client_argv(fullscreen=False, from_stdin=False,
# gfx_avc=False): /p:<otp> on argv (single-use OTP; the /from-stdin path
# mis-negotiates dim AVC on this build) + /gfx RFX override + /size (windowed),
# NO /f. Each client is wrapped by qdistro-secctx-exec so the bound shell sees
# engine=qdistro.mm + a per-stream app_id.
#
# /log-level:DEBUG is LOAD-BEARING, not just diagnostics (session-9 finding):
# this FreeRDP/SDL3 build has a timing-sensitive race that STALLS the connection
# at the TLS→MCS transition (frozen at "tls_verify_certificate", never loads the
# gfx channel) at /log-level:INFO or default; the extra DEBUG logging perturbs
# scheduling enough that the handshake completes reliably. Confirmed by bisection:
# identical argv + /log-level:DEBUG decodes (rdpgfx frames flow), + INFO stalls
# (2/2). DEBUG also keeps the readiness "Loading Dynamic Virtual Channel rdpgfx"
# (INFO) line present.
launch_rdp() {
  local unit=$1 port=$2 otp=$3 stream=$4
  local appid="qdistro.mm.${ORIGIN}.${stream}"
  local inst="${ORIGIN}-${stream}-$$"
  systemd-run --collect --unit="$unit" \
    --setenv=XDG_RUNTIME_DIR=$RT --setenv=HOME=/root \
    --setenv=WAYLAND_DISPLAY=$SOCK --setenv=SDL_VIDEODRIVER=wayland \
    --setenv=SDL_RENDER_DRIVER=software \
    --setenv=LIBGL_ALWAYS_SOFTWARE=1 --setenv=GALLIUM_DRIVER=llvmpipe \
    --setenv=QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 \
    bash -c "qdistro-secctx-exec --sandbox-engine qdistro.mm --app-id '$appid' --instance-id '$inst' -- \
      $RDP_CLIENT /v:$RDP_HOST:$port /u:$RDP_USER /p:$otp /scale:100 -grab-keyboard \
      /cert:ignore /gfx:AVC444:off,AVC420:off /size:${W}x${H} /log-level:DEBUG > $RT/${unit}.log 2>&1"
  echo "launched $unit -> $RDP_HOST:$port app_id=$appid"
}
launch_rdp mm-rdp-a "$RDP_PORT_A" "$OTP_A" "$STREAM_A"
launch_rdp mm-rdp-b "$RDP_PORT_B" "$OTP_B" "$STREAM_B"

# ---- 4) wait for BOTH clients to decode + map as managed toplevels ----------
# Readiness = each freerdp log shows the rdpgfx channel (decoded), AND the
# bystander observed two distinct secctx app_ids. FAIL CLOSED otherwise. NB
# `grep -c` exits 1 on zero matches AFTER printing "0", so never chain `|| echo 0`
# (it would yield "0\n0"); take the last numeric line instead.
# Readiness signals (churn-robust): the gfx dynamic channel LOADS on BOTH RDP
# connections AND both per-stream secctx app_ids are observed. NB the
# per-frame "rdpgfx" lines are DEBUG-level (suppressed by default → false
# negative); the channel-LOAD line is INFO, emitted because the clients run
# with /log-level:INFO. `grep -c` exits 1 on zero matches AFTER printing "0",
# so take the last numeric line; never chain `|| echo 0`.
GFXLOAD='Loading Dynamic Virtual Channel rdpgfx'
countmatch() { grep -c "$1" "$2" 2>/dev/null | tail -1 | tr -dc '0-9'; }
have() { grep -q "$1" "$2" 2>/dev/null; }
GOT=0
for _ in $(seq 1 80); do
  na=$(countmatch "$GFXLOAD" "$RT/mm-rdp-a.log"); na=${na:-0}
  nb=$(countmatch "$GFXLOAD" "$RT/mm-rdp-b.log"); nb=${nb:-0}
  if [ "$na" -ge 1 ] && [ "$nb" -ge 1 ] \
     && have "app_id=\"qdistro.mm.${ORIGIN}.${STREAM_A}\"" "$RT/bystander.out" \
     && have "app_id=\"qdistro.mm.${ORIGIN}.${STREAM_B}\"" "$RT/bystander.out"; then
    GOT=1; break
  fi
  sleep 0.5
done
sleep 2   # settle one repaint, as the proven decoder readiness does
echo "--- bystander toplevels ---"
grep -E 'toplevel_added|toplevel_security_context' "$RT/bystander.out" 2>/dev/null || true
echo "--- freerdp A tail ---"; tail -4 "$RT/mm-rdp-a.log" 2>/dev/null || true
echo "--- freerdp B tail ---"; tail -4 "$RT/mm-rdp-b.log" 2>/dev/null || true
if [ "$GOT" != 1 ]; then echo "FAIL: two mm managed toplevels did not both decode/map"; journalctl -u mm-qdwin --no-pager|tail -20; exit 9; fi
echo "VMB_QDWIN_OK socket=$RT/$SOCK fifo=$FIFO bystander=$RT/bystander.out"

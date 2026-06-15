#!/bin/bash
# spin-test-vm.sh — one entry point from scratch to a qdistro test VM
# with qdwin + qdshell + broker installed.
#
# Pipeline:
#   1. build-baseweed-from-scratch.sh   (if baseweed-admin.qcow2 absent)
#   2. build-baked-baseweed.sh          (if baseweed-baked.qcow2 absent)
#   3. clone-baseweed.sh --from-baked   (fresh disposable VM)
#   4. tarball + HTTP-stage the three sibling repos
#   5. fresh-vm-bootstrap.sh in VM      (build qdwin, build daemons,
#                                        install broker + qdshell)
#   6. systemctl start greetd-qdwin.service
#   7. ping ctrl-socket; print PASS/FAIL + VM name
#
# Sibling-checkout layout required:
#   <parent>/qdistro/   (this repo)
#   <parent>/qdwin/
#   <parent>/qdshell/
#   <parent>/qnotebook/
#
# Usage:
#   QDWIN_VM_TEMPLATE=<template-domain> scripts/vm/spin-test-vm.sh [<prefix>]
#
# Default prefix is "qd-test"; VM name (with timestamp suffix) is
# printed on the last stdout line.

set -euo pipefail

# QDWIN_VM_TEMPLATE: optional. clone-baseweed.sh dumps this domain's
# XML and substitutes name + disk path + MAC for each new test VM.
# If unset, auto-create a minimal qdistro-template domain.
QDWIN_VM_TEMPLATE="${QDWIN_VM_TEMPLATE:-qdistro-template}"
if ! virsh -c qemu:///session dominfo "$QDWIN_VM_TEMPLATE" >/dev/null 2>&1; then
    echo "[spin-test-vm] template domain '$QDWIN_VM_TEMPLATE' not defined; creating..." >&2
    "$(dirname "$0")/create-template-domain.sh" "$QDWIN_VM_TEMPLATE" >&2
fi
export QDWIN_VM_TEMPLATE

PREFIX="${1:-qd-test}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"           # qdistro/
PARENT="$(cd "$REPO/.." && pwd)"                  # qdistro-org/
IMG="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"

log() { echo "[spin-test-vm] $*" >&2; }

# Sanity-check sibling checkout.
for sib in qdwin qdshell qnotebook; do
    if [ ! -d "$PARENT/$sib" ]; then
        log "ERROR: sibling repo '$sib' not found at $PARENT/$sib"
        log "       Clone codeberg.org/qdistro/$sib next to qdistro/"
        exit 2
    fi
done

# Stages 1/2 build the SHARED baseweed images. Under parallel spins, multiple
# workers must not enter the build scripts at once (they share partial-file
# names and would clobber each other). Serialize behind a host flock and
# re-check inside the lock so only the first worker builds; the rest wait, then
# see the images present. The fast path (images already exist) still pays only a
# lock acquire.
mkdir -p "$IMG"
exec 9>"$IMG/.baseweed-build.lock"
if flock -w 2400 9; then
    # Stage 1.
    if [ ! -f "$IMG/baseweed-admin.qcow2" ]; then
        log "stage 1: building baseweed-admin.qcow2 from scratch (~5-10 min)..."
        bash "$REPO/scripts/vm/build-baseweed-from-scratch.sh" >&2
    else
        log "stage 1: baseweed-admin.qcow2 already present"
    fi

    # Stage 2.
    if [ ! -f "$IMG/baseweed-baked.qcow2" ]; then
        log "stage 2: baking dependencies onto overlay (~15-25 min)..."
        bash "$REPO/scripts/vm/build-baked-baseweed.sh" >&2
    else
        log "stage 2: baseweed-baked.qcow2 already present"
    fi
    flock -u 9
else
    log "WARN: could not acquire baseweed build lock within 40 min; proceeding (images assumed present)"
fi
exec 9>&-

# Stage 3.
if [ -n "${QCI_RUN_GOLDEN_BACKING:-}" ]; then
    log "stage 3: cloning a fresh VM from run-golden ($QCI_RUN_GOLDEN_BACKING)..."
    VM=$(bash "$REPO/scripts/vm/clone-baseweed.sh" "$PREFIX" \
            --from-run-golden="$QCI_RUN_GOLDEN_BACKING" | tail -1)
else
    log "stage 3: cloning a fresh VM from baked..."
    VM=$(bash "$REPO/scripts/vm/clone-baseweed.sh" "$PREFIX" --from-baked | tail -1)
fi
log "    VM = $VM"

# From here on the VM exists but qci does NOT yet know about it (the caller only
# registers it after this script prints the name on success). So if we fail or
# get interrupted before that handoff, WE must tear the VM down or it leaks as a
# running domain + overlay. SPUN_OK gates that teardown; it is set to 1 just
# before the final success output. The trap also tidies $STAGE / the HTTP server
# (both may be unset this early — guarded).
SPUN_OK=0
_SPIN_CLEANED=0
cleanup_spin() {
    [ "$_SPIN_CLEANED" = 1 ] && return 0   # idempotent: signal trap + EXIT trap
    _SPIN_CLEANED=1
    [ -n "${STAGE:-}" ] && rm -rf "$STAGE" 2>/dev/null || true
    [ -n "${HTTP_PID:-}" ] && kill "$HTTP_PID" 2>/dev/null || true
    if [ "$SPUN_OK" != 1 ] && [ -n "${VM:-}" ]; then
        log "spin failed/interrupted before handoff — tearing down $VM"
        virsh -c qemu:///session destroy "$VM" >/dev/null 2>&1 || true
        virsh -c qemu:///session undefine "$VM" --nvram >/dev/null 2>&1 \
            || virsh -c qemu:///session undefine "$VM" >/dev/null 2>&1 || true
        rm -f "$IMG/$VM.qcow2" 2>/dev/null || true
    fi
}
trap cleanup_spin EXIT
# On a signal, clean up and EXIT IMMEDIATELY (nonzero) — do NOT fall through and
# keep running with a torn-down VM, which would let the script reach SPUN_OK=1 /
# exit 0 and make qci register a VM that no longer exists. The exit re-fires the
# EXIT trap, but the _SPIN_CLEANED guard makes that second call a no-op.
trap 'cleanup_spin; exit 130' INT TERM

log "    starting + waiting for guest agent..."
virsh -c qemu:///session start "$VM" >/dev/null 2>&1 || true
"$SCRIPT_DIR/vm-start-and-wait" "$VM" >&2

# Stages 4-5 (source staging + in-guest fresh-vm-bootstrap build) are SKIPPED
# when cloning from a run-golden: the golden disk already contains the built
# compositor. We only boot + verify in that case. The body below is unindented
# but enclosed by this guard (bash ignores indentation).
if [ -z "${QCI_RUN_GOLDEN_BACKING:-}" ]; then
# Stage 4: tarball the three sibling repos and serve over SLIRP.
STAGE="$(mktemp -d -t qdistro-stage.XXXXXX)"

log "stage 4a: tarballing qdistro, qdwin, qdshell..."
# `build-*` excludes match by basename anywhere in the tree; the
# old `--exclude='build-*'` ate `print-vm/build-print-image.sh`
# (spec/20 priority #5 source-of-truth probed by s64). Restrict the
# exclude to the known meson-host build dirs in sibling repos so
# regular scripts named `build-*` survive into the staged tarballs.
# Exclude ci/runs (the live CI run dirs): the guest never needs host CI
# artifacts, and tarring the run dir that concurrent qci workers are actively
# writing raced as `tar: ./ci/runs/...: file changed as we read it`, failing the
# spin (observed on a parallel gui worker). Excluding it removes the race and
# shrinks the tarball.
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.git' --exclude='build' --exclude='build-host*' \
    --exclude='./ci/runs' \
    -czf "$STAGE/qdistro.tar.gz" -C "$PARENT/qdistro" .
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.git' --exclude='build' --exclude='build-host*' \
    --exclude='libweston-vendored/src/build' \
    -czf "$STAGE/qdwin.tar.gz" -C "$PARENT/qdwin" .
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.git' --exclude='build' --exclude='build-host*' \
    -czf "$STAGE/qdshell.tar.gz" -C "$PARENT/qdshell" .
if [ -d "$PARENT/qdlocker" ]; then
    tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
        --exclude='.git' --exclude='build' --exclude='build-host*' \
        -czf "$STAGE/qdlocker.tar.gz" -C "$PARENT/qdlocker" .
fi
# qdbrowser ships the outer ``qdbrowser/qdbrowser/`` python package
# that install-browser-bridge-for-vm.sh stages to
# /usr/local/lib/qdistro/qdbrowser/ (so probes can
# ``from qdbrowser.pwd_autofill import ...``). Without staging the
# tarball here the bridge installer auto-search misses the package
# and the bake-time python313-jeepney is wasted.
if [ -d "$PARENT/qdbrowser" ]; then
    tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
        --exclude='.git' --exclude='build' --exclude='build-host*' \
        -czf "$STAGE/qdbrowser.tar.gz" -C "$PARENT/qdbrowser" .
fi
if [ -d "$PARENT/qdgreeter" ]; then
    tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
        --exclude='.git' --exclude='build' --exclude='build-host*' \
        -czf "$STAGE/qdgreeter.tar.gz" -C "$PARENT/qdgreeter" .
fi
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='.git' --exclude='build' --exclude='build-host*' \
    -czf "$STAGE/qnotebook.tar.gz" -C "$PARENT/qnotebook" .

# Also stage the bootstrap script next to the tarballs so the VM
# can fetch it before unpacking anything.
cp "$REPO/scripts/vm/fresh-vm-bootstrap.sh" "$STAGE/fresh-vm-bootstrap.sh"

# Use a per-run EPHEMERAL port + a log inside the per-user $STAGE dir
# rather than a fixed host-wide port (8765) and a fixed /tmp path. On a
# shared host, multiple users running this script collide on both: a
# /tmp/spin-http.log owned by another uid is unwritable (sticky /tmp),
# and port 8765 may already be held by another user's server we cannot
# kill — and the guest's 10.0.2.2:8765 would then hit THAT server.
# Picking a free port per run and serving the log from $STAGE makes
# concurrent multi-user runs independent.
# Start the staging HTTP server on a kernel-assigned free port with NO
# probe/race window: Python binds port 0 (kernel picks a free port) and serves
# from that exact live socket, printing the actual bound port on stdout. There
# is no bind-probe-then-close gap, so concurrent parallel spins can never pick
# the same port or fetch each other's tarballs. Bind 0.0.0.0 so the guest
# reaches us via 10.0.2.2 over SLIRP NAT (127.0.0.1 is unreachable from guest).
PORT_FILE="$STAGE/http-port"
: > "$PORT_FILE"
(
    cd "$STAGE" || exit 1
    exec python3 -c '
import http.server, socketserver, sys
socketserver.TCPServer.allow_reuse_address = False
httpd = socketserver.TCPServer(("0.0.0.0", 0), http.server.SimpleHTTPRequestHandler)
sys.stdout.write(str(httpd.server_address[1]) + "\n"); sys.stdout.flush()
httpd.serve_forever()
' > "$PORT_FILE" 2>"$STAGE/spin-http.log"
) &
HTTP_PID=$!
SPIN_HTTP_PORT=""
for _ in $(seq 1 50); do
    SPIN_HTTP_PORT=$(head -1 "$PORT_FILE" 2>/dev/null | tr -dc '0-9')
    [ -n "$SPIN_HTTP_PORT" ] && break
    kill -0 "$HTTP_PID" 2>/dev/null || break   # server died before binding
    sleep 0.2
done
if [ -z "$SPIN_HTTP_PORT" ]; then
    log "ERROR: staging HTTP server failed to bind a port"
    exit 3
fi
log "stage 4b: host HTTP server on 0.0.0.0:$SPIN_HTTP_PORT (pid $HTTP_PID)"

STAGE_URL="http://10.0.2.2:$SPIN_HTTP_PORT"
log "stage 5: running fresh-vm-bootstrap.sh in VM..."
"$SCRIPT_DIR/vm-exec" "$VM" "wget -q -O /root/fresh-vm-bootstrap.sh $STAGE_URL/fresh-vm-bootstrap.sh" >&2 \
    || { log "ERROR: failed to fetch bootstrap script (port $SPIN_HTTP_PORT reachable?)"; exit 3; }

# Bootstrap fetches the three tarballs and runs the build. Pass the
# per-run staging URL so the in-VM bootstrap fetches from THIS run's
# server (its default is the old fixed http://10.0.2.2:8765).
# Normalize the tier-2 image-prebuild flag to a bare 0/1 before embedding it in
# the guest command string (defensive for manual invocations passing true/yes).
case "${QDISTRO_BUILD_TIER2_IMAGES:-0}" in 1|true|yes|on) _T2_IMAGES=1 ;; *) _T2_IMAGES=0 ;; esac
"$SCRIPT_DIR/vm-exec" "$VM" "QDISTRO_HTTP_HOST='$STAGE_URL' QDISTRO_BUILD_TIER2_IMAGES='$_T2_IMAGES' bash /root/fresh-vm-bootstrap.sh" >&2

fi  # end stages 4-5 (skipped in run-golden mode)

# Stage 6: verify the qdwin session came up. SKIPPED when
# QCI_SPIN_VERIFY_SESSION=none — the gui spinner (spin-test-vm-gui.sh) sets that
# and does its OWN profile-aware verification in its POSTBOOT. A labwc GUI
# golden clone boots straight into labwc (wayland-0, no wayland-1), so an
# unconditional wayland-1 check here would fail every labwc clone before the gui
# POSTBOOT ever runs. Default (qdwin) preserves the historic check for bats +
# standalone spin-test-vm.sh.
if [ "${QCI_SPIN_VERIFY_SESSION:-qdwin}" != none ]; then
# wayland-1 is the core "compositor came up" signal — fatal on miss.
# qdshell ctrl-socket + qdlocker.sock are warn-only. qdshell may not have
# bound the ctrl-socket yet at this exact moment. qdlocker.sock is created by
# a successfully running qdlocker by default (QDLOCKER_CTRL_SOCKET=1); it is
# expected to be ABSENT on this lxqt/labwc admin-test harness because qdlocker
# only runs inside the qdwin compositor session — without qdwin's
# qdwin_locker_v1 global it retry-crashes (by design) before creating its ctrl
# socket. The /etc/qdistro/locker-ctrl-introspection marker gates only the
# diagnostic commands (status/unlock-result/prompt-text) on that socket, not
# the socket itself or the production `lock` command. So distinguish: socket
# absent + qdlocker not active = expected here; qdlocker active + socket
# missing = a real ctrl-socket regression.
sleep 3
WAYLAND_OK=$("$SCRIPT_DIR/vm-exec" "$VM" "[ -S /run/user/1000/wayland-1 ] && echo yes || echo no" 2>/dev/null | tail -1)
if [ "${WAYLAND_OK:-no}" != "yes" ]; then
    log "FAIL: /run/user/1000/wayland-1 missing — compositor did not come up"
    exit 4
fi
log "PASS: /run/user/1000/wayland-1 present (compositor up)"

REPLY=$("$SCRIPT_DIR/vm-exec" "$VM" "echo list | socat - UNIX-CONNECT:/run/user/1000/qdshell.sock 2>&1 | head -1" 2>/dev/null | tail -1)
if [ "${REPLY:-}" = "ok list" ]; then
    log "PASS: qdshell ctrl-socket responsive"
else
    log "WARN: qdshell ctrl-socket not ready (reply was: ${REPLY:-empty})"
fi

LOCKER_OK=$("$SCRIPT_DIR/vm-exec" "$VM" "[ -S /run/user/1000/qdlocker.sock ] && echo yes || echo no" 2>/dev/null | tail -1)
if [ "${LOCKER_OK:-no}" = "yes" ]; then
    log "PASS: qdlocker.sock present"
else
    # Don't flat-normalize the absence: an absent socket is expected only when
    # qdlocker isn't running (no qdwin session on this harness). If qdlocker is
    # active but the socket is missing, that's a real ctrl-socket regression.
    # is-active returns nonzero for an inactive/failed unit; neutralize so the
    # substitution doesn't trip set -e/pipefail before we classify the state.
    LOCKER_STATE=$("$SCRIPT_DIR/vm-exec" "$VM" 'runuser -l admin -c "systemctl --user is-active qdlocker.service"' 2>/dev/null | tr -d '\r' | tail -1 || true)
    if [ "${LOCKER_STATE:-}" = "active" ]; then
        log "WARN: qdlocker.sock missing though qdlocker.service is active — possible ctrl-socket regression (QDLOCKER_CTRL_SOCKET leak / bad XDG_RUNTIME_DIR / bind failure)"
    else
        log "WARN: qdlocker.sock absent (expected on this lxqt harness: qdlocker.service is '${LOCKER_STATE:-unknown}', no qdwin compositor session to bind qdwin_locker_v1; introspection marker gates only status/unlock-result/prompt-text)"
    fi
fi
fi  # end stage 6 (skipped when QCI_SPIN_VERIFY_SESSION=none)

# Success: emit the VM name FIRST, then mark SPUN_OK so the EXIT trap won't tear
# it down. Ordering matters — qci only trusts a clean (rc=0) exit, so if we are
# interrupted before SPUN_OK=1 (the last statement) it stays 0 and the trap
# reclaims the VM, which is exactly what we want since the caller will treat the
# interrupted spin as a provisioning failure.
log "ready. VM:"
echo "$VM"
SPUN_OK=1

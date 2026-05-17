#!/bin/bash
# qdistro-tier5b-publisher — runs inside the tier-5b per-app guest VM.
#
# This file is the SOURCE-OF-TRUTH copy that lives in-tree under
# qdistro/tier5b-vm/. The same script (with $APP_BIN substituted at
# build time) is embedded into the per-app guest image by
# build-guest-image.sh. Shipping a free-standing copy here lets unit
# tests cover argv assembly and the "single-app discipline" guard
# without booting a VM.
#
# Invoked by the host via qemu-guest-agent guest-exec:
#
#   qdistro-tier5b-publisher.sh <vsock_port> [extra_args...]
#
# Unlike tier-5's session-grain publisher, tier-5b is pinned to a
# single binary. The first guest-exec arg is the vsock port; the rest
# are appended to the baked binary's argv. A compromised host can ask
# the guest to launch the one app with weird args, but it cannot ask
# the guest to run a different binary.
#
# When this script is used directly on the host for unit tests, set
# QDISTRO_TIER5B_APP_BIN to override the default (firefox).
set -uo pipefail

# In the baked-into-image variant, build-guest-image.sh substitutes
# this constant. In the source-of-truth variant (the file you're
# reading), we default to firefox and allow override for tests.
APP_BIN="${QDISTRO_TIER5B_APP_BIN:-firefox}"

usage() {
    echo "usage: $0 <vsock_port> [extra args for $APP_BIN...]" >&2
}

if [ $# -lt 1 ]; then
    usage
    exit 2
fi

PORT="$1"
shift

# Port sanity. 1..65535. Reject everything else loudly.
case "$PORT" in
    ''|*[!0-9]*)
        echo "[tier5b-publisher] FAIL: port '$PORT' is not an integer" >&2
        exit 2
        ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "[tier5b-publisher] FAIL: port '$PORT' out of range 1..65535" >&2
    exit 2
fi

# App-bin sanity. Refuse path separators and shell metacharacters; the
# host should send "extra args", not a different binary path. (Defense
# in depth: APP_BIN is baked from build-guest-image.sh, but the env
# override path used by tests would otherwise widen the attack surface.)
case "$APP_BIN" in
    */*|*\ *|*\;*|*\|*|*\&*|*'$'*|*'`'*)
        echo "[tier5b-publisher] FAIL: APP_BIN '$APP_BIN' contains forbidden chars" >&2
        exit 2
        ;;
esac

LOG="${QDISTRO_TIER5B_LOG:-/var/log/qdistro-tier5b-publisher.log}"
# Guarded redirect: don't blow up unit tests that can't write
# /var/log; if the log dir isn't writable, fall through to a tmpfile.
if ! touch "$LOG" 2>/dev/null; then
    LOG="$(mktemp -t qdistro-tier5b-publisher.XXXXXX.log)"
fi
exec >>"$LOG" 2>&1
echo "=== $(date -Iseconds): tier5b-publisher port=$PORT bin=$APP_BIN extra='$*' ==="

# Wait briefly for vsock module to be live. modprobe is async on first
# boot of cloud images; the wait is idempotent and bounded.
modprobe vhost_vsock 2>/dev/null || true
modprobe vsock 2>/dev/null || true
for _ in 1 2 3 4 5; do
    [ -e /dev/vsock ] && break
    sleep 0.5
done

# Firefox needs MOZ_ENABLE_WAYLAND=1 for native Wayland. Probe verdict
# §"When direct is not sufficient": X11-needing apps are out of MVP.
if [ "$APP_BIN" = "firefox" ]; then
    export MOZ_ENABLE_WAYLAND=1
fi

# Honour QDISTRO_TIER5B_DRY_RUN=1 for unit tests: print the resolved
# argv to a separate file and exit 0 instead of exec'ing waypipe (which
# isn't installed in the unit-test sandbox).
if [ "${QDISTRO_TIER5B_DRY_RUN:-0}" = "1" ]; then
    DRY_OUT="${QDISTRO_TIER5B_DRY_OUT:-/tmp/qdistro-tier5b-dry.log}"
    {
        echo "port=$PORT"
        echo "app_bin=$APP_BIN"
        echo "argv=waypipe --vsock -s 2:$PORT server -- $APP_BIN $*"
        echo "moz_enable_wayland=${MOZ_ENABLE_WAYLAND:-}"
    } >"$DRY_OUT"
    exit 0
fi

# guest-side waypipe-server: connects out to host CID=2 on $PORT and
# execs the baked binary. Single-app discipline.
exec waypipe --vsock -s "2:$PORT" server -- "$APP_BIN" "$@"

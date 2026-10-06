# tier3s/spike/c2-lib.sh — shared helpers for the Phase C2 silo-uid spike.
# THROWAWAY (spike only). Sourced by scripts that run as root INSIDE the
# dev test VM; nothing here runs on the host. Unlike lib.sh's as_admin,
# these helpers run podman/runsc as a NON-admin silo uid to answer the
# `03` Phase C step-3 prerequisite questions.
set -u
ADMIN=admin
ADMIN_UID=1000
ADMIN_RT=/run/user/$ADMIN_UID
WRAPPER=/usr/libexec/qdistro/tier3s-runsc
RUNSC=/usr/libexec/qdistro/runsc/runsc
SPIKE_SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK=/var/tmp/tier3s-spike
mkdir -p "$WORK"
chmod 0755 "$WORK"
SMOKE=$WORK/smoke.json
install -m 0644 "$SPIKE_SRC/smoke.json" "$SMOKE"

# C2 test identities — deleted+recreated each run so useradd/subuid
# allocation is observed fresh. C2A joins the bridge group; C2B stays out
# of every group so it is the non-member control for traversal checks.
SILO_A=${T3S_SILO_A:-t3s-c2a}
SILO_B=${T3S_SILO_B:-t3s-c2b}
BRIDGE_GROUP=qdistro-tier3   # reuse tier-3's group if present; else a test group

say() { printf '\n## %s\n' "$*"; }
obs() { printf 'OBSERVE: %s\n' "$*"; }
pass() { printf 'PASS: %s\n' "$*"; }
FAILS=0
fail() { FAILS=$((FAILS+1)); printf 'FAIL: %s\n' "$*"; }
# Capture a probe's full output+rc, print output minus podman session noise.
probe() { local out rc; out=$("$@" 2>&1); rc=$?; printf '%s\n' "$out" | grep -v 'level=warning'; printf 'rc=%d\n' "$rc"; return "$rc"; }

# Run a command as a silo user with a scrubbed environment. $2 variant:
# as_silo <user> <cmd...> uses a root-pre-created runtime dir at
# /run/qdistro-tier3s-rt/<uid> (the no-logind-session substitute).
as_silo() {
    local u=$1; shift
    local uid rt
    uid=$(id -u "$u")
    rt=/run/qdistro-tier3s-rt/$uid
    runuser -u "$u" -- env -i PATH=/usr/bin:/bin HOME=/home/$u \
        USER=$u LOGNAME=$u XDG_RUNTIME_DIR=$rt \
        DBUS_SESSION_BUS_ADDRESS=unix:path=$rt/bus "$@"
}

as_admin() {
    runuser -u "$ADMIN" -- env -i PATH=/usr/bin:/bin HOME=/home/$ADMIN \
        USER=$ADMIN LOGNAME=$ADMIN XDG_RUNTIME_DIR=$ADMIN_RT \
        DBUS_SESSION_BUS_ADDRESS=unix:path=$ADMIN_RT/bus "$@"
}

# Root pre-creates the per-uid runsc state root + runtime dir for a silo
# (D-A1 shape: /run/qdistro-tier3s-runsc/<host uid> mode 0700 owner=uid).
prep_silo_dirs() {
    local u=$1 uid
    uid=$(id -u "$u")
    install -d -m 0755 -o root -g root /run/qdistro-tier3s-runsc /run/qdistro-tier3s-rt
    install -d -m 0700 -o "$u" -g "$u" "/run/qdistro-tier3s-runsc/$uid" "/run/qdistro-tier3s-rt/$uid"
}

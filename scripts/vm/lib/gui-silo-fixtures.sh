#!/bin/bash
# gui-silo-fixtures.sh — GUEST-side, as root: provision the permissions-gui
# fixtures work (uid 2000) and work2 (uid 3000) as ACTIVE silos with their
# user-relay grants, then verify the grants and states.
#
# Usage (inside the VM):
#   bash /root/qdistro-src/scripts/vm/lib/gui-silo-fixtures.sh [--prove-restart]
#
# Two callers:
#   - spin-test-vm-gui.sh bakes them into the labwc (gui-admin) golden, with
#     --prove-restart: the reconcile that used to purge the grants runs at
#     daemon start, so the bake restarts the session manager once and
#     re-verifies (cheap there, expensive when discovered per scenario).
#   - qdwin-lane permissions-gui scenarios run it at Setup on their own
#     disposable clone of the gui-qdwin golden (see
#     tests/integration/permissions-gui/AGENTS.md, "qdwin lane"). The qdwin
#     golden deliberately has no silos: Active silos add a network-egress
#     indicator to the qdwin lock screen, which every qdlocker GUI scenario
#     on that golden would then show.
#
# Idempotent: an existing work/work2 that the session manager confirms is the
# right silo is kept; anything else is a hard failure (see below).
set -eu
case "${1:-}" in
    "") PROVE_RESTART=0 ;;
    --prove-restart) PROVE_RESTART=1 ;;
    *) echo "usage: gui-silo-fixtures.sh [--prove-restart]" >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo "[gui-silo-fixtures] ERROR: run as root" >&2; exit 2; }
# The VM test password (bake-baseweed sets the same one for admin).
VM_PASSWORD=${VM_PASSWORD:-Pa_ssw0rd45}

# 1. work + work2 as REAL SILOS (uids 2000 / 3000), linger so their
#    session buses come up.
#
# These are created through SessionManager1.CreateSilo, NOT a plain
# `useradd`, and that distinction is load-bearing. A silo's user-relay
# bus-name grant lives in a per-silo fragment
# (/etc/dbus-1/system.d/org.qdistro.UserRelay.silo-<name>.conf) that the
# session manager issues on create and RECONCILES against silos.yaml at
# every daemon start. The reconcile's first pass purges every fragment
# whose name has no silo row (_purge_unsafe_relay_fragments, "no such
# silo") and then reloads dbus.
#
# So a plain useradd plus a hand-written fragment — what this script did
# between 2026-07-26 and this change — is self-defeating: the bake writes
# the two fragments, and then the FIRST boot of every cloned worker VM
# starts the session manager, which deletes them again because work and
# work2 have no rows. That is what broke permissions-gui 12 and 15 in the
# full run of 2026-07-26 (both relays exit 78, AccessDenied on
# org.qdistro.UserRelay.uid<N>), and what made 11 and 16 "pass" only
# because their agents hand-repaired the VM mid-scenario.
#
# Creating real silos fixes it at the source: the row is durable, so every
# reconcile REISSUES the grant instead of revoking it. It is also the
# production path, so the scenarios now exercise what a real install does.
#
# They must also end up ACTIVE, not merely created. CreateSilo persists
# State.CREATED, and the broker's cross-uid relay gate refuses any
# REGISTERED target whose state is not Active — while it treats an ABSENT
# row as legacy-compatible and lets it through. So registering these two
# and stopping there would be worse than the bug it replaces: scenarios
# 11-17 would fail earlier, with SiloNotActive, even with perfect grants.
#
# CreateSilo is called as admin, not root: _require_admin rejects any
# caller whose uid != ADMIN_UID (1000), and root is not exempt. `--system`
# reaches the system bus, so no XDG_RUNTIME_DIR is needed. The daemon
# itself runs as root and does the useradd, so nothing is lost. Same
# pattern as tests/integration/vm/s101-session-lifecycle.sh.
#
# Ordering matters: create() refuses a name or uid that already exists, so
# it has to run BEFORE anything makes these accounts. Password and linger
# are applied afterwards, to the users the daemon created.

sm_call() {  # sm_call <Method> <signature> <args...>
    runuser -u admin -- busctl --system call \
        org.qdistro.SessionManager1 \
        /org/qdistro/SessionManager1 \
        org.qdistro.SessionManager1 "$@"
}
# ListSilos is also the RECONCILE BARRIER. The unit is Type=dbus and the
# daemon claims its bus name BEFORE constructing SessionManager, and it is
# that construction which runs autostart_pass() and the relay reconcile. So
# `systemctl is-active` can be true while the reconcile has not run —
# checking fragments then is flaky-green. A synchronous ListSilos reply
# cannot be served until the GLib main loop is running, which is after
# construction, so a successful reply is proof the reconcile has completed.
#
# There is no GetSilo method; ListSilos returns a JSON array of rows, so the
# state query goes through python-dbus rather than parsing busctl's output.
# An explicit short D-Bus timeout is what makes the outer deadline real.
# dbus-python's default is ~25s, so a wedged constructor would turn a
# "60 iteration" loop into ~26 minutes of bake — and autostart_pass() has
# podman sweeps after the reconcile that can genuinely block. The loop is
# bounded on the monotonic clock rather than by counting iterations, since
# each iteration does not cost a fixed amount of time.
silo_row() {  # silo_row <name> <field> — prints one field, or nothing
    runuser -u admin -- /usr/bin/python3 -c '
import json, sys, dbus
bus = dbus.SystemBus()
obj = bus.get_object("org.qdistro.SessionManager1",
                     "/org/qdistro/SessionManager1")
rows = json.loads(dbus.Interface(
    obj, "org.qdistro.SessionManager1").ListSilos(timeout=2.0))
for row in rows:
    if row.get("name") == sys.argv[1]:
        print(row.get(sys.argv[2], ""))
        break
' "$1" "$2" 2>/dev/null
}
silo_state() { silo_row "$1" state; }
# /proc/uptime, not `date +%s`: the guest clock can step backwards (chrony
# correcting a drifted VM clock is routine right after boot, which is exactly
# when this runs), and a backward step silently extends a wall-clock deadline.
_mono_s() { awk '{print int($1)}' /proc/uptime; }
sm_ready() {  # bounded wait for a real method reply, not just is-active
    local _deadline
    _deadline=$(( $(_mono_s) + 120 ))
    while [ "$(_mono_s)" -lt "$_deadline" ]; do
        silo_row __probe__ state >/dev/null 2>&1 && return 0
        sleep 2
    done
    return 1
}
# Exact identity, not merely "some row exists": a row of the wrong kind or
# uid must not be mistaken for the fixture we asked for.
silo_matches() {  # silo_matches <name> <uid>
    [ "$(silo_row "$1" uid)" = "$2" ] && \
    [ "$(silo_row "$1" kind)" = "tier3-user" ]
}


[ "$(id -u admin 2>/dev/null)" = 1000 ] \
    || { echo "[gui-spin] ERROR: admin is not uid 1000; SessionManager1 rejects" \
              "every caller whose uid != ADMIN_UID, so CreateSilo cannot work"; exit 1; }
systemctl is-active --quiet qdistro-session-manager.service \
    || systemctl start qdistro-session-manager.service \
    || { echo "[gui-spin] ERROR: qdistro-session-manager.service will not start;" \
              "work/work2 cannot be provisioned as silos"; exit 1; }
sm_ready || { echo "[gui-spin] ERROR: SessionManager1 never answered ListSilos"; exit 1; }

for _pair in "work:2000" "work2:3000"; do
    _u=${_pair%%:*}; _uid=${_pair##*:}
    if id "$_u" >/dev/null 2>&1; then
        # An existing account is only acceptable if the DAEMON agrees it is
        # this silo. A VM baked by the pre-2026-07-27 harness has a plain
        # useradd user — possibly still carrying a forged relay fragment,
        # which would sail past a fragment-presence check and then be purged
        # at the next reconcile. Hard-fail with a migration message rather
        # than repair: an automatic userdel would destroy a reused VM's
        # fixture home, which is too destructive to do implicitly.
        if silo_matches "$_u" "$_uid"; then
            echo "[gui-harness] $_u is already a registered silo — skipping CreateSilo"
        else
            echo "[gui-spin] ERROR: user $_u exists but is NOT a registered" \
                 "tier3-user silo at uid $_uid (daemon says uid='$(silo_row "$_u" uid)'" \
                 "kind='$(silo_row "$_u" kind)'). This VM was probably baked by the" \
                 "pre-2026-07-27 harness (plain useradd). Any relay grant it has will" \
                 "be purged at the next reconcile. Rebake the golden, or remove $_u by" \
                 "hand and re-run."; exit 1
        fi
    else
        sm_call CreateSilo si "$_u" "$_uid" \
            || { echo "[gui-spin] ERROR: CreateSilo failed for $_u (uid $_uid)"; exit 1; }
        echo "[gui-harness] created silo $_u (uid $_uid)"
    fi
    id "$_u" >/dev/null 2>&1 \
        || { echo "[gui-spin] ERROR: CreateSilo left no Linux user $_u"; exit 1; }
done

loginctl enable-linger work
loginctl enable-linger work2

# Set the test password so scenarios that need work to authenticate
# (e.g. via polkit) can. Match what bake-baseweed sets for admin.
echo "work:${VM_PASSWORD}" | chpasswd
echo "work2:${VM_PASSWORD}" | chpasswd

# 1b. Bring both silos to ACTIVE — the state the broker's relay gate wants.
for _pair in "work:2000" "work2:3000"; do
    _u=${_pair%%:*}
    if [ "$(silo_state "$_u")" != Active ]; then
        sm_call StartSilo s "$_u" \
            || { echo "[gui-spin] ERROR: StartSilo failed for $_u"; exit 1; }
    fi
done

# 1c. Verify the grants and states the scenarios rely on. With
# --prove-restart, first prove the whole thing SURVIVES a session-manager
# restart.
#
# The bug this replaces was invisible at bake time and only appeared on the
# clone's next boot, because the reconcile runs at daemon startup. Restarting
# here reproduces that boot in the one place a failure is cheap — during
# golden construction, not spread across per-scenario results.
if [ "$PROVE_RESTART" = 1 ]; then
    systemctl restart qdistro-session-manager.service \
        || { echo "[gui-spin] ERROR: session manager failed to restart"; exit 1; }
    sm_ready || { echo "[gui-spin] ERROR: SessionManager1 did not come back after restart"; exit 1; }
fi
for _pair in "work:2000" "work2:3000"; do
    _u=${_pair%%:*}; _uid=${_pair##*:}
    [ -f "/etc/dbus-1/system.d/org.qdistro.UserRelay.silo-$_u.conf" ] \
        || { echo "[gui-spin] ERROR: the relay policy fragment for $_u is missing" \
                  "(no silos.yaml row, or it did not survive a session-manager restart)"; exit 1; }
    grep -q "uid$_uid" "/etc/dbus-1/system.d/org.qdistro.UserRelay.silo-$_u.conf" \
        || { echo "[gui-spin] ERROR: the fragment for $_u does not grant" \
                  "org.qdistro.UserRelay.uid$_uid"; exit 1; }
    [ -e "/etc/systemd/system/qdshell-session-$_u@.service" ] \
        || { echo "[gui-spin] ERROR: silo $_u has no launcher unit link, so it" \
                  "can never reach Active"; exit 1; }
    _state=$(silo_state "$_u")
    [ "$_state" = Active ] \
        || { echo "[gui-spin] ERROR: silo $_u is $_state, not Active" \
                  "— the broker refuses cross-uid relays to a registered" \
                  "non-Active target (SiloNotActive)"; exit 1; }
done
if [ "$PROVE_RESTART" = 1 ]; then
    echo "[gui-harness] work/work2 are Active silos with surviving relay grants"
else
    echo "[gui-harness] work/work2 are Active silos with relay grants"
fi

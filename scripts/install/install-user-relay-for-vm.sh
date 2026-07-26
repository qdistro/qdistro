#!/bin/bash
# Idempotent install for qdistro-user-relay (per-uid session-bus relay).
#
# Drops qdistro_user_relay.py under /usr/libexec/qdistro/, installs
# the dbus system-bus policy (org.qdistro.UserRelay.conf), and the
# systemd template `qdistro-user-relay@<uid>.service`. The template
# is started on-demand by qdshell-session-launcher when a silo comes
# up, not enabled here — there's no point in starting a uid's relay
# before that uid's session bus exists.
#
# DEST_LIB=/usr/libexec/qdistro (was /usr/local/lib/qdistro until
# 2026-07-26). Two reasons for the move:
#   * it is the flat directory the broker and the shared daemon
#     modules (browser-bridge, portal backend, session-manager,
#     polkit agent, templates, phone, print) have used since
#     2026-04-29 — see install-broker-for-qdwin.sh's header for the
#     Tumbleweed lib_t/restorecon rationale. (Some components —
#     media, qsu, multimachine — still install under
#     /usr/local/lib/qdistro; the relay must follow the BROKER, not
#     those, because it imports broker modules.) And
#   * the relay imports the broker's qdistro_admin_rules for the
#     Firefox-containers cross-uid opt-in. qdistro python modules are
#     installed FLATTENED into one directory, so the relay resolves
#     that import via its own script dir. Installed to a directory the
#     broker modules are not in, the import failed on every real
#     install and the opt-in could never be turned on.
# DESTDIR (unset in production) prefixes every path so the installed
# layout can be reproduced in a test tmpdir; see
# tests/unit/test_user_relay_installed_layout.py.
#
# Usage: $0 [SRC]     # SRC defaults to /root/qdistro-src/qdistro/user_relay
set -eu

SRC=${1:-/root/qdistro-src/qdistro/user_relay}
DESTDIR=${DESTDIR:-}
DEST_LIB=$DESTDIR/usr/libexec/qdistro
LEGACY_LIB=$DESTDIR/usr/local/lib/qdistro
SYSTEMD_DIR=$DESTDIR/etc/systemd/system
POLICY=$DESTDIR/etc/dbus-1/system.d/org.qdistro.UserRelay.conf
UNIT_TEMPLATE=$SYSTEMD_DIR/qdistro-user-relay@.service

# Production installs are root-owned (the browser bridge's trusted-caller
# gate stats the relay script and refuses anything not root-owned and
# non-writable). Under DESTDIR the test harness is not root, so the
# ownership flags are dropped there and there only.
if [ -z "$DESTDIR" ]; then
    OWN=(-o root -g root)
else
    OWN=()
fi

if [ ! -d "$SRC" ]; then
    echo "ERROR: user-relay source not found at $SRC" >&2
    exit 2
fi

install -d "${OWN[@]}" -m 0755 "$DEST_LIB"

# The relay's Firefox-containers opt-in gate imports qdistro_admin_rules
# from its own (flattened) install dir. install-broker-for-qdwin.sh puts
# it there and runs BEFORE this script in every install chain. Refuse to
# lay down a relay whose security toggle could only ever fail closed —
# an install-time error is recoverable, a silently dead opt-in is not.
if [ ! -f "$DEST_LIB/qdistro_admin_rules.py" ]; then
    echo "ERROR: $DEST_LIB/qdistro_admin_rules.py is missing." >&2
    echo "       Run scripts/install/install-broker-for-qdwin.sh first;" >&2
    echo "       without it the Firefox-containers cross-uid opt-in" >&2
    echo "       (F4) can never be enabled." >&2
    exit 2
fi

install "${OWN[@]}" -m 0644 "$SRC/qdistro_user_relay.py" \
    "$DEST_LIB/qdistro_user_relay.py"

# Remove the pre-2026-07-26 copy so an upgraded host cannot keep running
# a stale relay out of the old prefix (the unit's ExecStart moved with it,
# but leaving the file behind invites exactly this class of confusion).
rm -f "$LEGACY_LIB/qdistro_user_relay.py"

install -d "${OWN[@]}" -m 0755 "$(dirname "$POLICY")" "$SYSTEMD_DIR"
install "${OWN[@]}" -m 0644 "$SRC/org.qdistro.UserRelay.conf" "$POLICY"
install "${OWN[@]}" -m 0644 "$SRC/qdistro-user-relay@.service" "$UNIT_TEMPLATE"

if [ -z "$DESTDIR" ]; then
    systemctl reload dbus-broker.service 2>/dev/null \
        || systemctl reload dbus.service 2>/dev/null \
        || true

    systemctl daemon-reload

    # Upgrades: a relay that is ALREADY running holds the old code in
    # memory, so replacing the file on disk changes nothing until the
    # process restarts and a later `systemctl start` is a no-op while the
    # unit is active. try-restart touches only currently-active instances
    # — it never starts a dormant uid's relay, which is
    # qdshell-session-launcher's job. Best-effort: on a first install
    # there is nothing to restart.
    systemctl try-restart 'qdistro-user-relay@*.service' 2>/dev/null || true
fi

echo "qdistro-user-relay template installed; start per-uid with: " \
     "systemctl start qdistro-user-relay@<uid>.service"

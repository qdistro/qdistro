#!/bin/bash
# Idempotent install for qdistro-session-manager (P02).
# Mirrors install-broker-for-qdwin.sh: drops the daemon under
# /usr/libexec/qdistro/, installs the dbus policy + systemd unit,
# reloads dbus, and enables the service.
#
# Usage: $0 [SRC]      # SRC defaults to /root/qdistro-src/qdistro/session_manager
set -eu

SRC=${1:-/root/qdistro-src/qdistro/session_manager}
DEST=/usr/libexec/qdistro
UNIT=/etc/systemd/system/qdistro-session-manager.service
POLICY=/etc/dbus-1/system.d/org.qdistro.SessionManager1.conf

if [ ! -d "$SRC" ]; then
    echo "ERROR: session-manager source not found at $SRC" >&2
    exit 2
fi

# Per-silo state lives under /var/lib/qdistro/silos/<name>/; the
# parent dir is root:root 0755 so the daemon can chown sub-dirs to
# silo uids without granting traversal to a non-silo user.
install -d -o root -g root -m 0755 /var/lib/qdistro/silos
# silos.yaml lives under /etc/qdistro/ alongside rules.d.
install -d -o root -g root -m 0755 /etc/qdistro
# Cgroup root is created on first StartSilo, but pre-create here so
# `systemctl restart` doesn't race the kernel's cgroup-controller
# delegation. Best-effort: the cgroup hierarchy may not be writable
# from script context (e.g. nested test VMs); the daemon handles it.
install -d -o root -g root -m 0755 /sys/fs/cgroup/qdistro-silos 2>/dev/null || true

install -d -o root -g root -m 0755 "$DEST"
install -o root -g root -m 0755 "$SRC/qdistro_session_manager.py" \
    "$DEST/qdistro_session_manager.py"

install -m 0644 "$SRC/org.qdistro.SessionManager1.conf" "$POLICY"
install -m 0644 "$SRC/qdistro-session-manager.service" "$UNIT"

systemctl reload dbus-broker.service 2>/dev/null \
    || systemctl reload dbus.service 2>/dev/null \
    || true

systemctl daemon-reload
systemctl enable --now qdistro-session-manager.service

for _ in 1 2 3 4 5; do
    busctl list --no-pager 2>/dev/null \
        | grep -q org.qdistro.SessionManager1 && break
    sleep 0.5
done

if ! busctl list --no-pager 2>/dev/null \
        | grep -q org.qdistro.SessionManager1; then
    echo "ERROR: qdistro-session-manager failed to claim bus name" >&2
    journalctl -u qdistro-session-manager.service --no-pager -n 30 >&2
    exit 3
fi

echo "session manager ready on org.qdistro.SessionManager1"

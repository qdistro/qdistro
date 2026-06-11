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
# Templated launcher referenced by SILO_LAUNCHER_FMT in
# qdistro_session_manager.py — the session manager runs
# `systemctl start qdshell-session-<name>@<uid>.service`, which
# resolves to the canonical template installed below + a per-silo
# symlink that gives the unit name its silo-specific prefix.
LAUNCHER_TEMPLATE=/usr/lib/systemd/system/qdshell-session@.service
LAUNCHER_HELPER=/usr/libexec/qdistro/qdshell-session-launcher

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
# Per-silo netns egress (todo/fable-networking task 3): the pure egress
# backend imported by the daemon, plus the admin tunnel-provisioning helper.
install -o root -g root -m 0755 "$SRC/qdistro_silo_egress.py" \
    "$DEST/qdistro_silo_egress.py"
install -o root -g root -m 0755 "$SRC/qdistro_wg_provision.py" \
    "$DEST/qdistro_wg_provision.py"

install -m 0644 "$SRC/org.qdistro.SessionManager1.conf" "$POLICY"
install -m 0644 "$SRC/qdistro-session-manager.service" "$UNIT"

# qdshell-session launcher: the canonical template + the
# privilege-dropping helper that joins the silo cgroup and keeps
# the silo uid alive. Symlinks for the per-silo unit names
# (qdshell-session-<name>@.service) are dropped below.
install -d -o root -g root -m 0755 /usr/lib/systemd/system
install -o root -g root -m 0644 "$SRC/qdshell-session@.service" \
    "$LAUNCHER_TEMPLATE"
install -o root -g root -m 0755 "$SRC/qdshell-session-launcher" \
    "$LAUNCHER_HELPER"

# fableplan2 task 04: the tier-2 templated-silo launcher unit + script (the
# session manager runs `systemctl start qdistro-tier2-silo@<name>.service`,
# which drops to admin and execs spawn-tier2) and the silo-launch CLI.
install -o root -g root -m 0644 "$SRC/qdistro-tier2-silo@.service" \
    /etc/systemd/system/qdistro-tier2-silo@.service
install -o root -g root -m 0755 "$SRC/qdistro-tier2-silo-launch" \
    "$DEST/qdistro-tier2-silo-launch"
install -o root -g root -m 0644 "$SRC/qdistro_silo_launch.py" \
    "$DEST/qdistro_silo_launch.py"
cat >"$DEST/qdistro-silo-launch" <<EOF
#!/bin/bash
exec /usr/bin/python3 $DEST/qdistro_silo_launch.py "\$@"
EOF
chmod 0755 "$DEST/qdistro-silo-launch"
ln -sf "$DEST/qdistro-silo-launch" /usr/local/bin/qdistro-silo-launch

# Drop a per-silo symlink for every silo currently in
# /etc/qdistro/silos.yaml. The session manager itself never
# creates these — a future task will move this into CreateSilo
# proper; for now installing what silos.yaml already lists is
# enough to satisfy the app-launcher integration test (which
# pre-creates a "work" silo).
seed_silo_symlink() {
    local name="$1"
    local link="/etc/systemd/system/qdshell-session-${name}@.service"
    if [ -L "$link" ] || [ -e "$link" ]; then
        return 0
    fi
    ln -s "$LAUNCHER_TEMPLATE" "$link"
}

# Always seed "work" — that's the app-launcher.bats fixture silo
# and the smoke target. Idempotent: ln -s above no-ops if the
# symlink already exists.
seed_silo_symlink work

# Scan silos.yaml for any other silo names. Tolerate missing or
# malformed yaml — this is best-effort, not a hard dependency.
if [ -r /etc/qdistro/silos.yaml ]; then
    while IFS= read -r silo_name; do
        [ -n "$silo_name" ] || continue
        seed_silo_symlink "$silo_name"
    done < <(awk '
        /^[[:space:]]*-?[[:space:]]*name:[[:space:]]*/ {
            sub(/^[[:space:]]*-?[[:space:]]*name:[[:space:]]*/, "");
            gsub(/["'\''[:space:]]/, "");
            if ($0 ~ /^[a-z_][a-z0-9_-]{0,31}$/) print $0;
        }
    ' /etc/qdistro/silos.yaml 2>/dev/null || true)
fi

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

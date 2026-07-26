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
# Disposables backend (M3): the pure tier-2 --disposable helper imported by
# the daemon at startup (qdistro_session_manager.py: `import qdistro_disposables`).
# Without this the daemon ModuleNotFoundErrors and crash-loops on boot.
install -o root -g root -m 0755 "$SRC/qdistro_disposables.py" \
    "$DEST/qdistro_disposables.py"
# Open-in-disposable class registry parser (P2): the pure resolver the trusted
# spawn path (qdistro-tier2-spawn) shells out to for the qdistro.dispose.open
# gate + workload/network pinning, and the SDK helper uses to map a class to its
# workload. Shipped alongside qdistro_disposables (it imports it).
install -o root -g root -m 0755 "$SRC/qdistro_disposable_classes.py" \
    "$DEST/qdistro_disposable_classes.py"
# The class registry itself (admin-editable local policy). Only installed if
# absent so an admin's edits survive re-install; the floor invariant in the
# parser keeps hostile classes off regardless of edits.
if [ ! -f /etc/qdistro/disposable-classes.toml ]; then
    install -o root -g root -m 0644 "$SRC/disposable-classes.toml" \
        /etc/qdistro/disposable-classes.toml
fi
# Export-back promoter (P2 / D7 copy-exception): the defensive host-side importer
# the daemon imports (qdistro_session_manager.py: `import qdistro_disposable_export`).
# Without it the daemon ModuleNotFoundErrors and crash-loops on boot.
install -o root -g root -m 0755 "$SRC/qdistro_disposable_export.py" \
    "$DEST/qdistro_disposable_export.py"
# Data-lineage receipt library + store (live under broker/, a sibling of the
# session-manager source). The daemon imports them to seal a chain-anchored
# receipt for each artifact it lands via export-back; the flat libexec layout
# makes them importable. Idempotent if the broker install already dropped them.
_qd_broker_src="$(dirname "$SRC")/broker"
for _qd_lin in qdistro_lineage_store.py qdistro_lineage_receipts.py; do
    if [ -f "$_qd_broker_src/$_qd_lin" ]; then
        install -o root -g root -m 0644 "$_qd_broker_src/$_qd_lin" "$DEST/$_qd_lin"
    else
        echo "install-session-manager: WARN: lineage module $_qd_lin not found at" \
             "$_qd_broker_src; export-back receipts will be skipped at runtime" >&2
    fi
done
# Root-owned data-lineage store dir for export-back receipts. root:root 0700 so
# only the privileged daemon reads/writes it (created explicitly here, not via the
# store's makedirs which would run before any restrictive umask).
install -d -o root -g root -m 0700 /var/lib/qdistro/lineage
# Root-controlled base for export-back staging. The PARENT (/var/lib/qdistro) is
# root:root 0755, so admin cannot replace this entry with a symlink (no write on
# the parent); the dir itself is admin-owned 0700 so the admin tier-2 launcher can
# create per-token <token>/{meta.json,payload/} subtrees the keep-id disposable
# writes. The importer (root) verifies it is a real dir before use; the boot sweep
# reaps orphans.
_qd_admin_user="admin"
if id "$_qd_admin_user" >/dev/null 2>&1; then
    install -d -o "$_qd_admin_user" -g "$_qd_admin_user" -m 0700 \
        /var/lib/qdistro/disposable-export
else
    echo "install-session-manager: WARN: admin user '$_qd_admin_user' absent;" \
         "creating /var/lib/qdistro/disposable-export root-owned (export-back" \
         "will fail until it is chowned to the admin uid)" >&2
    install -d -o root -g root -m 0700 /var/lib/qdistro/disposable-export
fi
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
# which runs spawn-tier2 in root-launcher mode — root parent for the secctx
# wire tag, podman/resolver/broker dropped to admin) and the silo-launch CLI.
install -o root -g root -m 0644 "$SRC/qdistro-tier2-silo@.service" \
    /etc/systemd/system/qdistro-tier2-silo@.service
install -o root -g root -m 0755 "$SRC/qdistro-tier2-silo-launch" \
    "$DEST/qdistro-tier2-silo-launch"
# ExecStop helper: the unit runs as root, so stopping the admin-rootless
# container must drop to the fixed admin user and fail closed on a missing
# user / uid 0.
install -o root -g root -m 0755 "$SRC/qdistro-tier2-silo-stop" \
    "$DEST/qdistro-tier2-silo-stop"
# Tracker J12 Fix A: the pod-app launcher unit + helpers. A launcher click
# used to fork spawn-tier2 straight from the unprivileged qdshell session,
# which has no root launcher parent — so the app's window arrived UN-TAGGED in
# dev and the launch was refused outright on a hardened profile. The click now
# goes through SessionManager1.LaunchPodApp, which starts
# qdistro-podapp@<launch-token>.service: same root-parent/admin-podman split as
# the tier-2 silo unit above.
install -o root -g root -m 0644 "$SRC/qdistro-podapp@.service" \
    /etc/systemd/system/qdistro-podapp@.service
install -o root -g root -m 0755 "$SRC/qdistro-podapp-launch" \
    "$DEST/qdistro-podapp-launch"
install -o root -g root -m 0755 "$SRC/qdistro-podapp-stop" \
    "$DEST/qdistro-podapp-stop"

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
# `enable --now` is a no-op against an ALREADY-RUNNING daemon, so on an
# upgrade the new file lands on disk and the old code keeps serving from
# memory — and the verify below passes, because the bus name is claimed by
# the stale process. Observed live: an upgraded host silently ran the
# previous session manager, which (among other things) issued no per-silo
# relay policy. try-restart touches only an active unit, so this is a no-op
# on a first install, where `enable --now` has just started it.
# Mirrors install-user-relay-for-vm.sh, which already did this.
systemctl try-restart qdistro-session-manager.service 2>/dev/null || true

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

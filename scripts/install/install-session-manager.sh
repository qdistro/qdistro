#!/bin/bash
# Idempotent install for qdistro-session-manager (P02).
# Mirrors install-broker-for-qdwin.sh: drops the daemon under
# /usr/libexec/qdistro/, installs the dbus policy + systemd unit,
# reloads dbus, and enables the service.
#
# Usage: $0 [SRC]      # SRC defaults to /root/qdistro-src/session_manager
set -eu

# Offline-install contract (todo/iso/14 Phase B): file drops always run;
# operations that need a running system manager / bus are skipped and
# logged when QDISTRO_OFFLINE_INSTALL=1 names a corroborated chroot.
_QDO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=lib/qdistro-offline.sh
. "$_QDO_DIR/lib/qdistro-offline.sh"
resolve_offline_install

SRC=${1:-/root/qdistro-src/session_manager}
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
install -o root -g root -m 0755 "$SRC/qdistro_silo_skill.py" \
    "$DEST/qdistro_silo_skill.py"
_qd_skill_src="$(dirname "$SRC")/agents/skills/silo/SKILL.md"
_qd_skill_dest=/usr/share/qdistro/agents/skills/silo
install -d -o root -g root -m 0755 "$_qd_skill_dest"
install -o root -g root -m 0644 "$_qd_skill_src" "$_qd_skill_dest/SKILL.md"
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
    # The admin user's PRIMARY group, not a group literally named "admin":
    # the image creates admin with primary group `users`, and `install -g
    # admin` then fails -- which, under set -e, aborted this installer
    # before its unit and bus policy were dropped, and the image build's
    # old fail-open loop hid that as "verify failed" (found by iso/14
    # Phase B; run 19's image shipped without qdistro-session-manager.service).
    _qd_admin_group="$(id -gn "$_qd_admin_user")"
    install -d -o "$_qd_admin_user" -g "$_qd_admin_group" -m 0700 \
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

# Tier 3s (gVisor runsc; Experimental, dev profile only): the launch path of
# tier3s/CONTRACT.md §1, all root-owned. OPT-IN (paravirt O10): installed only
# when QDISTRO_TIER3S=1; without it nothing tier-3s-specific is installed
# (scripts, units, seccomp profiles, tmpfiles), so an image built without the
# flag carries none of it. Any value but unset, empty, 0 or 1 is an error, so
# a typo never silently skips the install. runsc itself is NOT installed here
# even with the flag (paravirt D1: provisioned on demand by
# tier3s/provision-runsc.sh, which also installs the runtime wrapper
# /usr/libexec/qdistro/tier3s-runsc); without it the probe refuses every
# tier3s launch. Re-running without the flag does not remove an earlier
# tier3s install.
case "${QDISTRO_TIER3S:-0}" in
    1)    _qd_t3s=1 ;;
    0|'') _qd_t3s=0 ;;
    *)    echo "ERROR: QDISTRO_TIER3S must be 0 or 1 (got '${QDISTRO_TIER3S}')" >&2
          exit 2 ;;
esac
if [ "$_qd_t3s" = 1 ]; then
    _qd_t3s_src="$(dirname "$SRC")/tier3s"
    _qd_t3s_lib=/usr/lib/qdistro/tier3s
    if [ ! -d "$_qd_t3s_src" ]; then
        echo "ERROR: tier3s source not found at $_qd_t3s_src" >&2
        exit 2
    fi
    install -d -o root -g root -m 0755 /usr/lib/qdistro "$_qd_t3s_lib" "$_qd_t3s_lib/seccomp" \
        "$_qd_t3s_lib/workloads"
    # Phase C2 model A: every tier3s silo runs its podman/runsc as a dedicated
    # qt3s-<silo> account; group qdistro-tier3s is the membership marker the
    # spawn requires (accounts are created at first launch, never here).
    groupadd --force qdistro-tier3s \
        || { echo "ERROR: cannot create group qdistro-tier3s" >&2; exit 2; }
    # The root supervisor and the prerequisite screen. probe.sh compares the
    # provisioned wrapper and pin against the copies beside it, and refuses to run
    # as root unless this directory chain is root-owned.
    install -o root -g root -m 0755 "$_qd_t3s_src/spawn-tier3s.sh" "$_qd_t3s_lib/spawn-tier3s.sh"
    install -o root -g root -m 0755 "$_qd_t3s_src/probe.sh" "$_qd_t3s_lib/probe.sh"
    install -o root -g root -m 0755 "$_qd_t3s_src/tier3s-runsc" "$_qd_t3s_lib/tier3s-runsc"
    install -o root -g root -m 0644 "$_qd_t3s_src/RUNSC_RELEASE" "$_qd_t3s_lib/RUNSC_RELEASE"
    # Per-workload seccomp profiles (rendered by seccomp/make-profiles.py; the
    # spawn refuses a workload without one, no podman-default fallback).
    for _qd_f in "$_qd_t3s_src"/seccomp/*.json; do
        install -o root -g root -m 0644 "$_qd_f" "$_qd_t3s_lib/seccomp/$(basename "$_qd_f")"
    done
    # Per-workload declarations (parsed, never sourced): GUI= selects the
    # waypipe bridge half of the launch (CONTRACT §5 step 12). An absent
    # declaration means GUI=0 (headless); a present-but-invalid one refuses.
    for _qd_f in "$_qd_t3s_src"/workloads/*.env; do
        install -o root -g root -m 0644 "$_qd_f" "$_qd_t3s_lib/workloads/$(basename "$_qd_f")"
    done
    # The image-side bridge entrypoint and the image-build context
    # (CONTRACT §1 installed-paths table: Containerfile.<workload>,
    # headless-smoke.sh, configure-snapshot-repos.sh, make-tier3s-image.sh)
    # so an installed tree can rebuild every workload image as admin.
    install -o root -g root -m 0755 "$_qd_t3s_src/qdistro-tier3s-entrypoint" \
        "$_qd_t3s_lib/qdistro-tier3s-entrypoint"
    install -o root -g root -m 0755 "$_qd_t3s_src/make-tier3s-image.sh" \
        "$_qd_t3s_lib/make-tier3s-image.sh"
    install -o root -g root -m 0755 "$_qd_t3s_src/headless-smoke.sh" \
        "$_qd_t3s_lib/headless-smoke.sh"
    install -o root -g root -m 0644 "$_qd_t3s_src/configure-snapshot-repos.sh" \
        "$_qd_t3s_lib/configure-snapshot-repos.sh"
    for _qd_f in "$_qd_t3s_src"/Containerfile.*; do
        install -o root -g root -m 0644 "$_qd_f" "$_qd_t3s_lib/$(basename "$_qd_f")"
    done
    # The root scope helper (first process of the owning scope) and the only
    # teardown path (spawn EXIT trap, unit ExecStop/ExecStopPost, reconciliation).
    install -o root -g root -m 0755 "$_qd_t3s_src/qdistro-tier3s-scope" "$DEST/qdistro-tier3s-scope"
    install -o root -g root -m 0755 "$_qd_t3s_src/qdistro-tier3s-cleanup" "$DEST/qdistro-tier3s-cleanup"
    # The runsc state root and the control/per-launch parents: tmpfiles is their
    # ONLY creator (CONTRACT §2); create them now, and systemd recreates them at
    # every boot.
    install -d -o root -g root -m 0755 /usr/lib/tmpfiles.d
    install -o root -g root -m 0644 "$_qd_t3s_src/tmpfiles/qdistro-tier3s.conf" \
        /usr/lib/tmpfiles.d/qdistro-tier3s.conf
    live_only "systemd-tmpfiles --create qdistro-tier3s.conf" \
        systemd-tmpfiles --create /usr/lib/tmpfiles.d/qdistro-tier3s.conf
    # The silo launch unit (SessionManager1.CreateTier3sSilo + StartSilo) and its
    # root launch helper (parses the stanza, execs spawn-tier3s.sh).
    install -o root -g root -m 0644 "$SRC/qdistro-tier3s-silo@.service" \
        /etc/systemd/system/qdistro-tier3s-silo@.service
    install -o root -g root -m 0755 "$SRC/qdistro-tier3s-silo-launch" \
        "$DEST/qdistro-tier3s-silo-launch"
else
    echo "install-session-manager: tier 3s not installed (QDISTRO_TIER3S is not 1; paravirt O10)"
fi
# --- end tier 3s ---

install -o root -g root -m 0644 "$SRC/qdistro_silo_launch.py" \
    "$DEST/qdistro_silo_launch.py"
cat >"$DEST/qdistro-silo-launch" <<EOF
#!/bin/bash
exec /usr/bin/python3 $DEST/qdistro_silo_launch.py "\$@"
EOF
chmod 0755 "$DEST/qdistro-silo-launch"
ln -sf "$DEST/qdistro-silo-launch" /usr/local/bin/qdistro-silo-launch

# Drop a per-silo symlink for every silo currently in
# /etc/qdistro/silos.yaml.
#
# As of 2026-07-28 the session manager DOES create these itself:
# CreateSilo links the launcher and DeleteSilo unlinks it, and an
# additive reconcile at daemon startup links any tier-3 silo that
# lacks one. (Before that, a silo created after install had no
# link, so StartSilo could not resolve its unit and the silo could
# never reach Active — which the broker's cross-uid relay gate
# then refuses outright.)
#
# This seeding stays because it runs BEFORE the daemon starts, so
# a fresh install has links in place for the first autostart pass
# rather than depending on the reconcile to repair them, and
# because the app-launcher integration test pre-creates a "work"
# silo out of band.
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

sd_reload_dbus

sd_daemon_reload
sd_enable_now qdistro-session-manager.service
# `enable --now` is a no-op against an ALREADY-RUNNING daemon, so on an
# upgrade the new file lands on disk and the old code keeps serving from
# memory — and the verify below passes, because the bus name is claimed by
# the stale process. Observed live: an upgraded host silently ran the
# previous session manager, which (among other things) issued no per-silo
# relay policy. try-restart acts only on an ACTIVE unit — it never starts a
# stopped one — so it restarts whatever `enable --now` just left running,
# which on a first install is a cheap second start and on an upgrade is the
# whole point. Mirrors install-user-relay-for-vm.sh, which already did this.
sd_try_restart qdistro-session-manager.service || true

if is_offline; then
    echo "[offline] skipped (needs a running system bus): probe org.qdistro.SessionManager1"
else
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
fi

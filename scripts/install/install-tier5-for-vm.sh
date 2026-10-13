#!/bin/bash
# Install the tier-5 (per-app VM, waypipe-over-AF_VSOCK) launch
# infrastructure into a qdistro VM. Idempotent.
#
# Lands:
#   - /usr/local/bin/qdistro-tier5-spawn  → symlink to the source
#     tree's tier5-vm/spawn-tier5.sh (or /usr/share/qdistro/tier5/
#     spawn-tier5.sh when installed from an RPM).
#   - /usr/local/bin/qdistro-tier5-cleanup → tier5-vm/qdistro-tier5-cleanup.sh
#   - /usr/local/bin/qdistro-tier5-build-guest-image →
#     tier5-vm/build-guest-image.sh
#   - /usr/local/bin/qdistro-tier5-vmapps-scan →
#     tier5-vm/vmapps-scan.py (guest-base .desktop scanner)
#   - /usr/local/bin/qdistro-tier5-vmapps-reconcile →
#     tier5-vm/vmapps-reconcile.sh (service run for the catalogue)
#   - /etc/systemd/system/qdistro-tier5-vmapps-scan.{service,path,timer}
#     — boot + path-triggered + periodic reconcile of the dynamic
#     VM-app catalogue (vmapps-dynamic)
#   - /usr/share/polkit-1/actions/org.qdistro.tier5.policy — polkit
#     rule that lets the *active* admin session pkexec the spawn
#     helper without re-authenticating. qdshell's VMApps.launch
#     invokes via pkexec so the launcher (admin uid) can drive the
#     spawn helper (which needs root for libvirt/virsh).
#
# Usage:
#   bash install-tier5-for-vm.sh <qdistro-src-root>
#
# Where <qdistro-src-root> is the directory containing tier5-vm/.
# Typically /root/qdistro-src in the bats VM (per
# fresh-vm-bootstrap.sh's $SRC).
set -euo pipefail

# Offline-install contract (todo/iso/14 Phase B): live operations are
# skipped and logged inside a corroborated chroot (the kiwi image build).
_QDO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=lib/qdistro-offline.sh
. "$_QDO_DIR/lib/qdistro-offline.sh"
resolve_offline_install

if [ "$(id -u)" -ne 0 ]; then
    echo "[install-tier5] must run as root" >&2
    exit 2
fi

SRC_ROOT="${1:?usage: $0 <qdistro-src-root>}"
TIER5_DIR="$SRC_ROOT/tier5-vm"

if [ ! -d "$TIER5_DIR" ]; then
    echo "[install-tier5] FAIL: $TIER5_DIR not found" >&2
    exit 3
fi

# --- wrappers for the spawn helpers ---
#
# Do not symlink directly into the source tree: spawn-tier5.sh resolves
# sibling files via dirname("$0")/../lib and dirname("$0")/domain-template.xml.
# A /usr/local/bin symlink makes those relative lookups land under
# /usr/local/lib and /usr/local/bin, which breaks the launcher path.
install -d /usr/local/bin
for tool in spawn-tier5.sh:qdistro-tier5-spawn \
            qdistro-tier5-cleanup.sh:qdistro-tier5-cleanup \
            build-guest-image.sh:qdistro-tier5-build-guest-image \
            vmapps-scan.py:qdistro-tier5-vmapps-scan \
            vmapps-reconcile.sh:qdistro-tier5-vmapps-reconcile; do
    src_basename="${tool%%:*}"
    dst_name="${tool##*:}"
    src="$TIER5_DIR/$src_basename"
    dst="/usr/local/bin/$dst_name"
    # A helper the repo ships but the tree lacks (or lost its exec bit in
    # the sync) is a broken tree: fail the step, so the chain records a gap
    # instead of a half-installed tier (todo/iso/14 Phase D review).
    [ -x "$src" ] || { echo "[install-tier5] FATAL: $src missing or not executable" >&2; exit 1; }
    cat > "$dst" <<EOF
#!/bin/bash
exec "$src" "\$@"
EOF
    chmod 0755 "$dst"
    echo "[install-tier5] installed wrapper $dst → $src"
done

install -d /usr/share/qdistro/tier5
install -m 0644 "$TIER5_DIR/domain-template.xml" \
    /usr/share/qdistro/tier5/domain-template.xml

# --- polkit policy ---
# Allow the active admin session to pkexec qdistro-tier5-spawn without
# password. allow_active=yes is the qdistro single-tenant convention —
# the admin user is trusted on their own login session. allow_any
# requires admin-auth in case some other uid (cron, ssh-user) tries.
POLKIT_DIR=/usr/share/polkit-1/actions
install -d "$POLKIT_DIR"
cat > "$POLKIT_DIR/org.qdistro.tier5.policy" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC
 "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1.0/policyconfig.dtd">
<policyconfig>
  <vendor>qdistro</vendor>
  <vendor_url>https://qdistro.org/</vendor_url>

  <action id="org.qdistro.tier5.spawn">
    <description>Spawn a tier-5 per-app VM</description>
    <message>Authentication required to spawn an isolated VM</message>
    <icon_name>application-x-executable</icon_name>
    <defaults>
      <allow_any>auth_admin_keep</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>yes</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/local/bin/qdistro-tier5-spawn</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>

  <action id="org.qdistro.tier5.cleanup">
    <description>Tear down a tier-5 per-app VM</description>
    <message>Authentication required to tear down an isolated VM</message>
    <icon_name>application-x-executable</icon_name>
    <defaults>
      <allow_any>auth_admin_keep</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>yes</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/local/bin/qdistro-tier5-cleanup</annotate>
  </action>
</policyconfig>
EOF
chmod 0644 "$POLKIT_DIR/org.qdistro.tier5.policy"
echo "[install-tier5] installed polkit policy at $POLKIT_DIR/org.qdistro.tier5.policy"

# --- dynamic VM-app catalogue reconcile units (vmapps-dynamic) -------
#
# The service is a oneshot reconcile run (vmapps-reconcile.sh): absent
# base -> manifest removal; unreadable-by-admin base -> stale publish;
# fresh manifest -> fast path; otherwise rescan with bounded retries.
# It exits 0 even on exhaustion (the launcher just sees the stale
# manifest and shows Foot only).
#
#   * boot:     WantedBy=multi-user.target does the initial reconcile
#   * path:     base qcow2 replaced -> rescan
#   * timer:    10min drift-safety reconcile
SYSTEMD_DIR=/etc/systemd/system
install -d "$SYSTEMD_DIR"
cat > "$SYSTEMD_DIR/qdistro-tier5-vmapps-scan.service" <<'EOF'
[Unit]
Description=qdistro tier-5 VM-app catalogue reconcile
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/qdistro-tier5-vmapps-reconcile
# guestfish opens + inspects the multi-GB base; keep it out of the way.
Nice=19
IOSchedulingClass=idle

[Install]
WantedBy=multi-user.target
EOF
cat > "$SYSTEMD_DIR/qdistro-tier5-vmapps-scan.path" <<'EOF'
[Unit]
Description=Watch the tier-5 guest base for VM-app catalogue rescans

[Path]
PathChanged=/var/lib/libvirt/images/qdistro-tier5-base.qcow2
Unit=qdistro-tier5-vmapps-scan.service

[Install]
WantedBy=paths.target
EOF
cat > "$SYSTEMD_DIR/qdistro-tier5-vmapps-scan.timer" <<'EOF'
[Unit]
Description=Periodic tier-5 VM-app catalogue reconcile

[Timer]
OnUnitInactiveSec=10min
Unit=qdistro-tier5-vmapps-scan.service

[Install]
WantedBy=timers.target
EOF
chmod 0644 "$SYSTEMD_DIR"/qdistro-tier5-vmapps-scan.{service,path,timer}
sd_daemon_reload
sd_enable_now qdistro-tier5-vmapps-scan.service \
              qdistro-tier5-vmapps-scan.path \
              qdistro-tier5-vmapps-scan.timer
echo "[install-tier5] installed + enabled qdistro-tier5-vmapps-scan.{service,path,timer}"

# Reload polkit so the new policy takes effect immediately.
sd_reload_polkit

echo "[install-tier5] done."

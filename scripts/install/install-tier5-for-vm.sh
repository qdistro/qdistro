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
# Typically /root/qdistro-src/qdistro in the bats VM (per
# fresh-vm-bootstrap.sh's $SRC).
set -euo pipefail

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

# --- symlinks for the spawn helpers ---
install -d /usr/local/bin
for tool in spawn-tier5.sh:qdistro-tier5-spawn \
            qdistro-tier5-cleanup.sh:qdistro-tier5-cleanup \
            build-guest-image.sh:qdistro-tier5-build-guest-image; do
    src_basename="${tool%%:*}"
    dst_name="${tool##*:}"
    src="$TIER5_DIR/$src_basename"
    dst="/usr/local/bin/$dst_name"
    [ -x "$src" ] || { echo "[install-tier5] WARN: $src missing or not executable"; continue; }
    ln -sf "$src" "$dst"
    echo "[install-tier5] linked $dst → $src"
done

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

# Reload polkit so the new policy takes effect immediately.
systemctl reload polkit.service 2>/dev/null || \
    pkill -HUP polkitd 2>/dev/null || true

echo "[install-tier5] done."

#!/bin/bash
# Install the tier-3 (different-uid silo, waypipe-over-UNIX) launch
# infrastructure into a qdistro VM. Idempotent.
#
# Polkit note: the policy at /usr/share/polkit-1/actions/
# org.qdistro.tier3.policy is forward-compatible. No production launcher
# wires up `pkexec qdistro-tier3-spawn` yet — the bats drivers call
# spawn-tier3 directly as root via vm-exec. When a GUI launcher (e.g. a
# tier-3 sibling of VMAppsProvider.qml) lands, the user will see one
# admin-password prompt per session (auth_admin_keep cache). Hardened
# from the round-1 default of allow_active=yes per security review.
#
# Lands:
#   - group qdistro-tier3
#   - silo users user1 + user2, both members of qdistro-tier3
#     (override the list via TIER3_SILO_USERS="silo-a silo-b ...")
#   - admin user added to qdistro-tier3 (so admin can chgrp the bridge
#     socket after waypipe creates it)
#   - /usr/local/bin/qdistro-tier3-spawn   → symlink to tier3/spawn-tier3.sh
#   - /usr/local/bin/qdistro-tier3-cleanup → symlink to tier3/qdistro-tier3-cleanup.sh
#   - /usr/share/polkit-1/actions/org.qdistro.tier3.policy — polkit
#     rule that lets the active admin session pkexec the spawn helper
#     without re-authenticating (mirrors tier-5's policy shape).
#
# Usage:
#   bash install-tier3-for-vm.sh <qdistro-src-root>
#
# Where <qdistro-src-root> is the directory containing tier3/.
# Typically /root/qdistro-src/qdistro in the bats VM (per
# fresh-vm-bootstrap.sh's $SRC).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[install-tier3] must run as root" >&2
    exit 2
fi

SRC_ROOT="${1:?usage: $0 <qdistro-src-root>}"
TIER3_DIR="$SRC_ROOT/tier3"

if [ ! -d "$TIER3_DIR" ]; then
    echo "[install-tier3] FAIL: $TIER3_DIR not found" >&2
    exit 3
fi

ADMIN_USER="${TIER3_ADMIN_USER:-admin}"
TIER3_GROUP="${TIER3_GROUP:-qdistro-tier3}"
SILO_USERS="${TIER3_SILO_USERS:-user1 user2}"

# --- 1. group ---------------------------------------------------------
if ! getent group "$TIER3_GROUP" >/dev/null; then
    groupadd -r "$TIER3_GROUP"
    echo "[install-tier3] created group $TIER3_GROUP"
else
    echo "[install-tier3] group $TIER3_GROUP already present"
fi

# --- 2. silo users ----------------------------------------------------
# When a silo name already exists, refuse to co-opt the account unless
# it looks like one of ours: regular-range uid (>= 1000), locked
# password (passwd -S shows L or LK), interactive shell. Adding an
# arbitrary pre-existing account to qdistro-tier3 would grant it a
# trust-group membership it never opted into, and a future spawn-
# tier3 invocation could end up running attacker-controlled code as
# that uid — defense in depth against package-collision installs.
validate_existing_silo() {
    local user="$1"
    local uid shell pwstatus
    uid=$(id -u "$user" 2>/dev/null || echo "")
    if ! [[ "$uid" =~ ^[0-9]+$ ]] || [ "$uid" -lt 1000 ]; then
        echo "[install-tier3] FAIL: '$user' exists with uid=$uid (need a regular uid >= 1000)" >&2
        return 1
    fi
    shell=$(getent passwd "$user" | cut -d: -f7)
    case "$shell" in
        /bin/bash|/usr/bin/bash|/bin/sh|/usr/bin/sh) ;;
        *)
            echo "[install-tier3] FAIL: '$user' exists with shell='$shell'" >&2
            echo "                     Refusing to co-opt non-standard account into qdistro-tier3" >&2
            return 1
            ;;
    esac
    # passwd -S statuses: L/LK = locked, NP = no password set, P = set.
    # Round-2 security review (2026-05-16) tightened this to reject NP:
    # an `NP` account is reachable via `su -` without auth from a prior
    # local-root attacker, which (combined with name collision in
    # SILO_USERS) is a weaponizable path. Require strictly locked.
    pwstatus=$(passwd -S "$user" 2>/dev/null | awk '{print $2}')
    case "$pwstatus" in
        L|LK) ;;
        *)
            echo "[install-tier3] FAIL: '$user' has password status '$pwstatus' (need L or LK; explicitly locked)" >&2
            echo "                     Lock with: passwd -l $user" >&2
            return 1
            ;;
    esac
    return 0
}

for u in $SILO_USERS; do
    if id -u "$u" >/dev/null 2>&1; then
        if ! validate_existing_silo "$u"; then
            echo "[install-tier3] aborting — pre-existing '$u' fails silo-safety checks" >&2
            exit 3
        fi
        echo "[install-tier3] silo user $u already present (validated)"
    else
        # -m creates home; -s /bin/bash gives an interactive shell so
        # `runuser -u $u -- env ...` works without weirdness.
        useradd -m -s /bin/bash -G "$TIER3_GROUP" "$u"
        # Lock the password — silos are not interactive login accounts.
        passwd -l "$u" >/dev/null 2>&1 || true
        echo "[install-tier3] created silo user $u (uid=$(id -u "$u"), locked password)"
    fi
    # Ensure membership even if the user already existed without it.
    usermod -a -G "$TIER3_GROUP" "$u" 2>/dev/null || true
done

# --- 2b. /run/qdistro-tier3 socket dir -------------------------------
# Bridge sockets land here. Mode 0710 group=qdistro-tier3 lets group
# members traverse (silos can reach a socket by full path) but not
# list (so silo A can't enumerate silo B's tokens). Admin owns it +
# can write (creates sockets at spawn time).
#
# admin's own /run/user/$UID is mode 0700 and excludes the silo uid
# entirely, so bridge sockets there hit EACCES on dir-traverse from
# the silo side. /run/qdistro-tier3 is the dedicated dir.
#
# tmpfiles.d entry persists the dir across reboots (systemd-tmpfiles
# recreates it on boot before any user services start).
install -d -o "$ADMIN_USER" -g "$TIER3_GROUP" -m 0710 /run/qdistro-tier3 2>/dev/null \
    || install -d -o root -g "$TIER3_GROUP" -m 0710 /run/qdistro-tier3
echo "[install-tier3] socket dir /run/qdistro-tier3 ($(stat -c '%U:%G %a' /run/qdistro-tier3))"

install -d /etc/tmpfiles.d
cat > /etc/tmpfiles.d/qdistro-tier3.conf <<EOF
# Persistent /run/qdistro-tier3 socket dir for tier-3 bridge sockets.
# See qdistro/tier3/spawn-tier3.sh + scripts/install/install-tier3-for-vm.sh.
d  /run/qdistro-tier3  0710  $ADMIN_USER  $TIER3_GROUP  -  -
EOF
echo "[install-tier3] tmpfiles entry at /etc/tmpfiles.d/qdistro-tier3.conf"

# --- 3. admin in qdistro-tier3 ---------------------------------------
# Admin needs to be in the group so it can chgrp the bridge socket
# after waypipe creates it. (chgrp requires either CAP_CHOWN or
# group membership for the target gid.)
if id -u "$ADMIN_USER" >/dev/null 2>&1; then
    if ! id -Gn "$ADMIN_USER" | tr ' ' '\n' | grep -qx "$TIER3_GROUP"; then
        usermod -a -G "$TIER3_GROUP" "$ADMIN_USER"
        echo "[install-tier3] added $ADMIN_USER to $TIER3_GROUP"
    fi
else
    echo "[install-tier3] WARN: admin user '$ADMIN_USER' not present yet — skipping group-add" >&2
fi

# --- 4. symlinks ------------------------------------------------------
install -d /usr/local/bin
for pair in spawn-tier3.sh:qdistro-tier3-spawn \
            qdistro-tier3-cleanup.sh:qdistro-tier3-cleanup; do
    src_basename="${pair%%:*}"
    dst_name="${pair##*:}"
    src="$TIER3_DIR/$src_basename"
    dst="/usr/local/bin/$dst_name"
    [ -x "$src" ] || { echo "[install-tier3] WARN: $src missing or not executable"; continue; }
    ln -sf "$src" "$dst"
    echo "[install-tier3] linked $dst → $src"
done

# --- 5. polkit policy ------------------------------------------------
# allow_active=auth_admin_keep (was: yes, until 2026-05-16 security
# review). Rationale: spawn-tier3 takes attacker-controllable argv
# (<silo> + arbitrary <cmd>) and runs as root. With allow_active=yes
# any process in the admin session can pkexec to root + silo uids
# without prompting, which collapses the boundary the moment any
# admin-session process is compromised. auth_admin_keep prompts for
# admin's password ONCE per session (5-minute cache), so a captive
# attacker can't silently spawn tier-3 helpers in the background.
# The spawn-tier3.sh wrapper also rejects security-sensitive env
# overrides (TIER3_SOCKET_DIR / TIER3_USE_SECCTX / TIER3_GROUP / ...)
# when invoked via pkexec — defense in depth.
POLKIT_DIR=/usr/share/polkit-1/actions
install -d "$POLKIT_DIR"
cat > "$POLKIT_DIR/org.qdistro.tier3.policy" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC
 "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1.0/policyconfig.dtd">
<policyconfig>
  <vendor>qdistro</vendor>
  <vendor_url>https://qdistro.org/</vendor_url>

  <action id="org.qdistro.tier3.spawn">
    <description>Spawn a tier-3 silo (different-uid waypipe bridge)</description>
    <message>Authentication required to spawn an isolated silo</message>
    <icon_name>application-x-executable</icon_name>
    <defaults>
      <allow_any>auth_admin_keep</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/local/bin/qdistro-tier3-spawn</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>

  <action id="org.qdistro.tier3.cleanup">
    <description>Reap orphan tier-3 bridge sockets</description>
    <message>Authentication required to reap tier-3 bridge sockets</message>
    <icon_name>application-x-executable</icon_name>
    <defaults>
      <allow_any>auth_admin_keep</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/local/bin/qdistro-tier3-cleanup</annotate>
  </action>
</policyconfig>
EOF
chmod 0644 "$POLKIT_DIR/org.qdistro.tier3.policy"
echo "[install-tier3] installed polkit policy at $POLKIT_DIR/org.qdistro.tier3.policy"

# Reload polkit so the new policy takes effect immediately.
systemctl reload polkit.service 2>/dev/null || \
    pkill -HUP polkitd 2>/dev/null || true

echo "[install-tier3] done."

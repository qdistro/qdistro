#!/bin/bash
# install-polkit-agent-for-vm.sh — idempotent install of the qdistro
# polkit AuthenticationAgent (spec/13 §"admin polkit
# AuthenticationAgent") onto a fresh-clone VM.
#
# Layout:
#   /usr/libexec/qdistro/qdistro_polkit_agent.py      # ExecStart target
#   /usr/local/bin/qdistro-polkit-prompt              # password-prompt subprocess
#   /etc/systemd/system/qdistro-polkit-agent.service  # system unit, User=admin
#   /etc/qdistro/polkit-agent.conf                    # per-action method config
#
# The agent is a SYSTEM service (User=admin), not a per-user unit: a
# user unit's drop-ins and manager environment are writable by every
# same-uid process — including an empty drop-in UnsetEnvironment= that
# resets the unit's injection denylist — so the agent's launch
# environment would be attacker-controllable (sol r169). As a system
# unit its unit file, drop-in dirs, environment and system.slice cgroup
# are all root-owned. It still registers with polkitd scoped to the
# admin's login session, and reaches the session bus via an explicit
# DBUS_SESSION_BUS_ADDRESS (linger keeps user@1000 up from early boot).
set -euo pipefail

# Offline-install contract (todo/iso/14 Phase B): file drops always run;
# operations that need a running system manager / bus are skipped and
# logged when QDISTRO_OFFLINE_INSTALL=1 names a corroborated chroot.
_QDO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=lib/qdistro-offline.sh
. "$_QDO_DIR/lib/qdistro-offline.sh"
resolve_offline_install

SRC=${1:-/root/polkit-src}
if [ ! -d "$SRC" ]; then
    echo "[install-polkit-agent] missing source dir $SRC" >&2
    exit 2
fi

DEST_LIB=/usr/libexec/qdistro
DEST_BIN=/usr/local/bin
DEST_SYSTEM_SYSD=/etc/systemd/system
DEST_ETC=/etc/qdistro

install -d -m 0755 "$DEST_LIB" "$DEST_BIN" "$DEST_SYSTEM_SYSD" "$DEST_ETC"

# Defensive: ensure python-pam is present. The agent works without it
# (PAM auth fails closed with a clear message) but pretty much every
# real flow needs PAM.
if ! python3 -c "import pam" 2>/dev/null; then
    echo "[install-polkit-agent] zypper installing python314-python-pam..."
    zypper -n install python314-python-pam >/dev/null 2>&1 \
        || echo "[install-polkit-agent] WARN: python-pam install failed (PAM auth degrades)" >&2
fi

install -m 0755 "$SRC/qdistro_polkit_agent.py" "$DEST_LIB/qdistro_polkit_agent.py"
install -m 0755 "$SRC/qdistro-polkit-prompt.py" "$DEST_BIN/qdistro-polkit-prompt"
install -m 0644 "$SRC/qdistro-polkit-agent.service" \
    "$DEST_SYSTEM_SYSD/qdistro-polkit-agent.service"

# Per-action method config. Don't clobber an admin's edits — only
# install if absent.
if [ ! -f "$DEST_ETC/polkit-agent.conf" ]; then
    install -m 0644 "$SRC/polkit-agent.conf" "$DEST_ETC/polkit-agent.conf"
else
    echo "[install-polkit-agent] keeping existing $DEST_ETC/polkit-agent.conf"
fi

# Enable via the SYSTEM manager. `systemctl enable` writes the
# multi-user.target.wants symlink — a pure filesystem operation, so it
# works at this chain position (step 5) and in offline installs, unlike
# the per-user forms this script used historically: `runuser -u admin --
# systemctl --user enable --now` died on every install ("Failed to
# connect to user scope bus") before the admin user manager existed, and
# the failure was swallowed — the agent had never run (VM-verified
# 2026-07-26). The system-unit move (sol r169) removes the need for any
# user manager at enable time, and failure is fatal rather than
# swallowed.
sd_daemon_reload || true
if ! systemctl enable qdistro-polkit-agent.service >/dev/null 2>&1; then
    echo "[install-polkit-agent] ERROR: could not enable qdistro-polkit-agent.service" >&2
    echo "       the polkit agent would be installed and never started" >&2
    exit 4
fi

# Opportunistic start on a running system (a re-install); on an offline
# chroot or a build-time image the first boot's multi-user.target brings
# it up via the wants link above. Failure here is genuinely fine — the
# enable is what the install depends on.
if is_offline; then
    echo "[offline] skipped (needs a running system manager): start qdistro-polkit-agent.service"
else
    systemctl start qdistro-polkit-agent.service >/dev/null 2>&1 \
        || echo "[install-polkit-agent] note: live start failed;" \
                "the agent starts on the next boot" >&2
fi

echo "[install-polkit-agent] OK — qdistro-polkit-agent installed at $DEST_LIB"

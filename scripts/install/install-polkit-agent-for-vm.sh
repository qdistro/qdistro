#!/bin/bash
# install-polkit-agent-for-vm.sh — idempotent install of the qdistro
# polkit AuthenticationAgent (spec/13 §"admin polkit
# AuthenticationAgent") onto a fresh-clone VM.
#
# Layout:
#   /usr/libexec/qdistro/qdistro_polkit_agent.py     # ExecStart target
#   /usr/local/bin/qdistro-polkit-prompt             # password-prompt subprocess
#   /etc/systemd/user/qdistro-polkit-agent.service   # per-user session unit
#   /etc/qdistro/polkit-agent.conf                   # per-action method config
#
# The agent is a per-user (not system) daemon — it needs the admin's
# session bus to expose the AuthenticationAgent interface, and
# polkitd registers it scoped to the session subject. Enabled via
# `systemctl --user enable --now` for the admin uid (admin).
set -euo pipefail

SRC=${1:-/root/polkit-src}
if [ ! -d "$SRC" ]; then
    echo "[install-polkit-agent] missing source dir $SRC" >&2
    exit 2
fi

DEST_LIB=/usr/libexec/qdistro
DEST_BIN=/usr/local/bin
DEST_USER_SYSD=/etc/systemd/user
DEST_ETC=/etc/qdistro

install -d -m 0755 "$DEST_LIB" "$DEST_BIN" "$DEST_USER_SYSD" "$DEST_ETC"

# Defensive: ensure python-pam is present. The agent works without it
# (PAM auth fails closed with a clear message) but pretty much every
# real flow needs PAM.
if ! python3 -c "import pam" 2>/dev/null; then
    echo "[install-polkit-agent] zypper installing python313-python-pam..."
    zypper -n install python313-python-pam >/dev/null 2>&1 \
        || echo "[install-polkit-agent] WARN: python-pam install failed (PAM auth degrades)" >&2
fi

install -m 0755 "$SRC/qdistro_polkit_agent.py" "$DEST_LIB/qdistro_polkit_agent.py"
install -m 0755 "$SRC/qdistro-polkit-prompt.py" "$DEST_BIN/qdistro-polkit-prompt"
install -m 0644 "$SRC/qdistro-polkit-agent.service" \
    "$DEST_USER_SYSD/qdistro-polkit-agent.service"

# Per-action method config. Don't clobber an admin's edits — only
# install if absent.
if [ ! -f "$DEST_ETC/polkit-agent.conf" ]; then
    install -m 0644 "$SRC/polkit-agent.conf" "$DEST_ETC/polkit-agent.conf"
else
    echo "[install-polkit-agent] keeping existing $DEST_ETC/polkit-agent.conf"
fi

# Reload + enable (per-user, scoped to admin uid 1000 = admin).
systemctl --user daemon-reload 2>/dev/null || true

ADMIN_UID=1000
ADMIN_USER=admin
if id "$ADMIN_USER" >/dev/null 2>&1; then
    # systemctl --user against a not-yet-fully-up session needs
    # XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS — easiest is to
    # invoke as the admin user under their existing graphical-session
    # target via systemd's machinectl-shell, but for our purposes
    # `runuser -u admin -- systemctl --user ...` works once linger is
    # enabled (which fresh-vm-bootstrap.sh does).
    runuser -u "$ADMIN_USER" -- env \
        XDG_RUNTIME_DIR="/run/user/$ADMIN_UID" \
        systemctl --user daemon-reload 2>/dev/null || true
    runuser -u "$ADMIN_USER" -- env \
        XDG_RUNTIME_DIR="/run/user/$ADMIN_UID" \
        systemctl --user enable --now qdistro-polkit-agent.service 2>&1 \
        | tail -5 || true
fi

echo "[install-polkit-agent] OK — qdistro-polkit-agent installed at $DEST_LIB"

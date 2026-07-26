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

# Enable.
#
# `systemctl --global enable`, NOT the per-user `runuser -u admin --
# systemctl --user enable --now` this used to do. That form failed on every
# install and said OK anyway. This installer is chain step 5
# (qdistro-bootstrap.sh) and the admin user manager does not exist until the
# session is installed much later (fresh-vm-bootstrap.sh:462), so the enable
# died with
#
#     Failed to connect to user scope bus via local transport: No such file
#
# — deterministically, on the release path, swallowed by the `| tail -5 ||
# true` and followed by the script's own "OK" line. Nothing ever wrote the
# .wants symlink, so the agent was disabled and had never run. VM-verified
# 2026-07-26.
#
# --global writes /etc/systemd/user/qdwin-session.target.wants/ and needs no
# running user manager, so it cannot fail for this reason. It applies to every
# uid, which is correct and costs nothing: the unit is WantedBy the desktop
# session target, and a silo uid never reaches it.
systemctl daemon-reload 2>/dev/null || true
if ! systemctl --global enable qdistro-polkit-agent.service >/dev/null 2>&1; then
    echo "[install-polkit-agent] ERROR: could not enable qdistro-polkit-agent.service" >&2
    echo "       the polkit agent would be installed and never started" >&2
    exit 4
fi

# Opportunistic start, ONLY if the admin session is already up (a re-install
# on a running system). Failure here is genuinely fine — the --global enable
# above is what makes it come up on the next session — so unlike the old code
# this is allowed to fail quietly, and it is not the thing the install depends
# on.
ADMIN_UID=1000
ADMIN_USER=admin
if id "$ADMIN_USER" >/dev/null 2>&1 && [ -d "/run/user/$ADMIN_UID" ]; then
    runuser -u "$ADMIN_USER" -- env \
        XDG_RUNTIME_DIR="/run/user/$ADMIN_UID" \
        systemctl --user start qdistro-polkit-agent.service >/dev/null 2>&1 \
        || echo "[install-polkit-agent] note: no live admin session to start into;" \
                "the agent starts with the next desktop session" >&2
fi

echo "[install-polkit-agent] OK — qdistro-polkit-agent installed at $DEST_LIB"

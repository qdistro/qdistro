#!/bin/bash
# install-pwd-for-vm.sh — idempotent install of the qdistro password-
# manager daemon (spec/13 Phase 8) onto a fresh-clone VM.
#
# Mirrors install-broker-for-qdwin.sh but for the pwd daemon. Sources
# come from /root/pwd-src/ (staged by fresh-vm-bootstrap.sh from
# host:8765/pwd/).
#
# Layout (matches the broker's split for the same lib_t-vs-bin_t reason):
#   /usr/libexec/qdistro/qdistro_pwd_daemon.py     # ExecStart target
#   /usr/libexec/qdistro/qdistro_pwd_vault.py
#   /usr/libexec/qdistro/qdistro_pwd_identity.py
#   /usr/libexec/qdistro/qdistro_pwd_audit.py
#   /usr/local/bin/qdistro-pwd-admin               # admin CLI
#   /usr/local/bin/qdistro-pwd-get                 # app CLI
#   /etc/dbus-1/system.d/com.qdistro.Pwd1.conf
#   /etc/systemd/system/qdistro-pwd.service
#   /var/lib/qdistro/vaults/                       # 0700 root:root
#   /var/lib/qdistro/audit/pwd_audit.sqlite        # created on first record
#
# Daemon runs as root in MVP. Once an SELinux module + qdistro-pwd
# system uid land in a follow-up task, this script grows
# `useradd --system qdistro-pwd` + chown of the dirs.
set -euo pipefail

SRC=${1:-/root/pwd-src}
if [ ! -d "$SRC" ]; then
    echo "[install-pwd] missing source dir $SRC" >&2
    exit 2
fi

DEST_LIB=/usr/libexec/qdistro
DEST_BIN=/usr/local/bin
DEST_DBUS=/etc/dbus-1/system.d
DEST_SYSD=/etc/systemd/system
DEST_USER_SYSD=/etc/systemd/user
DEST_VAR=/var/lib/qdistro
DEST_POLKIT_ACTION=/usr/share/polkit-1/actions
DEST_POLKIT_RULES=/usr/share/polkit-1/rules.d
DEST_PORTAL_DIR=/usr/share/xdg-desktop-portal/portals
DEST_PORTAL_CFG=/usr/share/xdg-desktop-portal

install -d -m 0755 "$DEST_LIB" "$DEST_BIN" "$DEST_DBUS" "$DEST_SYSD" "$DEST_USER_SYSD"
install -d -m 0755 "$DEST_POLKIT_ACTION" "$DEST_POLKIT_RULES"
install -d -m 0755 "$DEST_PORTAL_DIR" "$DEST_PORTAL_CFG"

# Phase-8.4: dedicated qdistro-pwd system uid + group. Idempotent —
# `useradd --system` re-runs are no-ops if the user exists.
if ! getent group qdistro-pwd >/dev/null 2>&1; then
    groupadd --system qdistro-pwd
fi
if ! id qdistro-pwd >/dev/null 2>&1; then
    useradd --system --gid qdistro-pwd \
        --home-dir /var/lib/qdistro \
        --shell /sbin/nologin qdistro-pwd
fi
# `tss` group lets the daemon talk to /dev/tpmrm0 on hosts where
# tpm2-abrmd / the kernel resource manager grant by GID. Idempotent
# usermod when group is missing on hosts without a TPM.
if getent group tss >/dev/null 2>&1; then
    usermod -a -G tss qdistro-pwd >/dev/null 2>&1 || true
fi

install -d -m 0700 -o qdistro-pwd -g qdistro-pwd "$DEST_VAR/vaults"
install -d -m 0700 -o qdistro-pwd -g qdistro-pwd "$DEST_VAR/audit"
# Existing files (from a pre-Phase-8.4 install) need a chown so the
# new uid can read them. SCOPE the chown to PWD-OWNED files only:
# vault files always belong to the pwd daemon, but the broker's own
# `audit.sqlite` lives in the same dir (spec/30 §"Broker module
# split-out" docstrings — distinct filenames inside one dir). A
# blanket `chown -R` on /var/lib/qdistro/audit/ steals the broker's
# database too, which then fails to open on the next broker restart
# (the broker runs as root in qdistro_broker_t but DAC kicks in
# before SELinux).
chown -R qdistro-pwd:qdistro-pwd "$DEST_VAR/vaults" 2>/dev/null || true
# Pwd-specific files only; explicitly skip broker-owned `audit.sqlite`
# and its WAL/SHM siblings.
for f in "$DEST_VAR/audit/pwd_audit.sqlite" \
         "$DEST_VAR/audit/pwd_audit.sqlite-wal" \
         "$DEST_VAR/audit/pwd_audit.sqlite-shm"; do
    [ -e "$f" ] && chown qdistro-pwd:qdistro-pwd "$f" 2>/dev/null || true
done

# Defensive: ensure cryptography is present. Tumbleweed's baseweed
# may pre-date the install-deps.sh entry; idempotent no-op when
# already installed.
if ! python3 -c "from cryptography.hazmat.primitives.ciphers.aead import AESGCM" 2>/dev/null; then
    echo "[install-pwd] zypper installing python313-cryptography..."
    zypper -n install python313-cryptography >/dev/null 2>&1 \
        || echo "[install-pwd] WARN: cryptography install failed; daemon will fail at boot" >&2
fi

# Daemon + sibling modules (kept together so Python's
# script-dir-on-sys.path rule resolves the imports).
install -m 0755 "$SRC/qdistro_pwd_daemon.py"   "$DEST_LIB/"
install -m 0644 "$SRC/qdistro_pwd_vault.py"    "$DEST_LIB/"
install -m 0644 "$SRC/qdistro_pwd_identity.py" "$DEST_LIB/"
install -m 0644 "$SRC/qdistro_pwd_audit.py"    "$DEST_LIB/"
install -m 0644 "$SRC/qdistro_pwd_tpm.py"      "$DEST_LIB/"
install -m 0644 "$SRC/qdistro_pwd_polkit.py"   "$DEST_LIB/"
install -m 0755 "$SRC/qdistro_pwd_portal.py"   "$DEST_LIB/"
install -m 0644 "$SRC/qdistro_pwd_pinstash.py" "$DEST_LIB/"
install -m 0644 "$SRC/qdistro_pwd_fprint.py"   "$DEST_LIB/"

# Phase-8.3 portal Secret backend (per-user session daemon).
install -m 0644 "$SRC/qdistro-pwd-portal.service" \
    "$DEST_USER_SYSD/qdistro-pwd-portal.service"
# spec/13 §"portal-keys auto-unlock" (P4): per-user oneshot at login
# that runs `qdistro-pwd-admin auto-unlock-portal-keys`. SKIPs cleanly
# (ConditionPathExists) if the admin hasn't sealed a portal-keys PIN.
install -m 0644 "$SRC/qdistro-portal-keys-unlock.service" \
    "$DEST_USER_SYSD/qdistro-portal-keys-unlock.service"
install -m 0644 "$SRC/org.qdistro.PortalSecret.portal" \
    "$DEST_PORTAL_DIR/org.qdistro.PortalSecret.portal"
install -m 0644 "$SRC/qdistro-portals.conf" \
    "$DEST_PORTAL_CFG/qdistro-portals.conf"

# Polkit action + rule (Phase-8.2). Action defines com.qdistro.pwd.unlock;
# rule routes non-admin callers through admin auth.
install -m 0644 "$SRC/com.qdistro.pwd.policy" \
    "$DEST_POLKIT_ACTION/com.qdistro.pwd.policy"
install -m 0644 "$SRC/qdistro-pwd.rules" \
    "$DEST_POLKIT_RULES/50-qdistro-pwd.rules"
# spec/13 fprintd-bound auto-unlock: lets the qdistro-pwd uid invoke
# the net.reactivated.fprint.device.verify action without an admin
# password prompt. Loaded only if the source ships it (older bundles
# pre-date the file).
if [ -f "$SRC/qdistro-pwd-fprint.rules" ]; then
    install -m 0644 "$SRC/qdistro-pwd-fprint.rules" \
        "$DEST_POLKIT_RULES/50-qdistro-pwd-fprint.rules"
fi

# polkitd reads its actions+rules at startup AND watches for changes;
# but a brand-new install doesn't always see the inotify event before
# the daemon's first CheckAuthorization call, so kick it.
systemctl reload polkit.service 2>/dev/null || \
    systemctl restart polkit.service 2>/dev/null || true

# tpm2-tools is a runtime dep for v2 / TPM-sealed vaults. The daemon
# auto-falls-back to v1 / scrypt if absent — install is best-effort.
if ! command -v tpm2_unseal >/dev/null 2>&1; then
    echo "[install-pwd] zypper installing tpm2.0-tools..."
    zypper -n install tpm2.0-tools >/dev/null 2>&1 \
        || echo "[install-pwd] WARN: tpm2.0-tools install failed; v2 vaults unavailable" >&2
fi

# CLIs without .py suffix so `qdistro-pwd-get gmail` reads naturally.
install -m 0755 "$SRC/qdistro-pwd-admin.py" "$DEST_BIN/qdistro-pwd-admin"
install -m 0755 "$SRC/qdistro-pwd-get.py"   "$DEST_BIN/qdistro-pwd-get"

# D-Bus policy + systemd unit.
install -m 0644 "$SRC/com.qdistro.Pwd1.conf" "$DEST_DBUS/com.qdistro.Pwd1.conf"
install -m 0644 "$SRC/qdistro-pwd.service"   "$DEST_SYSD/qdistro-pwd.service"

# Phase-8.4: load the qdistro_pwd SELinux module if a pwd-policy
# directory was staged alongside the source tree. install-policy.sh
# is idempotent and SKIPs cleanly when selinux-policy-devel is absent.
if [ -d "$SRC/../pwd-policy" ] && [ -f "$SRC/../pwd-policy/install-policy.sh" ]; then
    bash "$SRC/../pwd-policy/install-policy.sh" || \
        echo "[install-pwd] WARN: pwd-policy install failed (non-fatal)" >&2
elif [ -d /root/pwd-policy ] && [ -f /root/pwd-policy/install-policy.sh ]; then
    bash /root/pwd-policy/install-policy.sh || \
        echo "[install-pwd] WARN: pwd-policy install failed (non-fatal)" >&2
fi

# Reload + enable. Reuse qdistro-dbus-reload.service if present so the
# Pwd1.conf policy lands on first activation (same install-time
# dbus-broker behaviour the admin broker hits — see spec/30 §"dbus-
# broker policy-reload mystery").
systemctl daemon-reload
if [ -f "$DEST_SYSD/qdistro-dbus-reload.service" ]; then
    systemctl enable --now qdistro-dbus-reload.service >/dev/null 2>&1 || true
    systemctl start qdistro-dbus-reload.service >/dev/null 2>&1 || true
else
    systemctl reload dbus-broker.service 2>/dev/null \
        || systemctl reload dbus.service 2>/dev/null || true
fi
# `--now` may fail on a fresh VM if the dbus policy hasn't fully
# propagated yet, or if a TPM/keyring dependency is missing. The
# sanity-probe block below handles a delayed start with a warning;
# allow this line to fail without taking the whole bootstrap down
# (set -e at the top of the script would otherwise abort here, which
# breaks `spin-test-vm.sh` chains that don't need pwd to be live).
systemctl enable --now qdistro-pwd.service || \
    echo "[install-pwd] WARN: enable --now returned non-zero; sanity probe will retry" >&2

# Sanity probe — broker will be active+listening within ~1s normally.
for _ in 1 2 3 4 5; do
    if systemctl is-active --quiet qdistro-pwd.service; then
        break
    fi
    sleep 1
done
if ! systemctl is-active --quiet qdistro-pwd.service; then
    echo "[install-pwd] WARN: qdistro-pwd.service not active after install" >&2
    journalctl -u qdistro-pwd.service --no-pager -n 30 >&2 || true
fi

echo "[install-pwd] OK — qdistro-pwd installed at $DEST_LIB"

#!/bin/bash
# Install the qdistro audispd plugin and wire it into auditd.
#
# spec/30 step 7. Idempotent: re-running replaces the plugin script
# in place, re-installs the descriptor, and SIGHUPs auditd so it
# re-reads /etc/audit/plugins.d/. Skips if auditd isn't installed
# (host without audit-userspace, fine for non-SELinux test runs).
#
# Source layout: assumes this script's directory contains
#   qdistro-audisp-plugin   (python script, 0755)
#   qdistro-audisp.conf     (audispd descriptor, 0640)
#
# Pre-req: broker/qdistro_audisp_parser.py already deployed
# to /usr/libexec/qdistro/ (the broker install path since 2026-04-29).
# install-broker-for-qdwin.sh handles that.
set -eu

SRC=$(cd "$(dirname "$0")" && pwd)
PLUGIN_DEST=/usr/local/sbin/qdistro-audisp-plugin
DESC_DEST=/etc/audit/plugins.d/qdistro-audisp.conf

if ! command -v auditctl >/dev/null 2>&1; then
    echo "audisp install: auditctl missing; SKIP (audit-userspace not installed)"
    exit 0
fi

if [ ! -d /etc/audit/plugins.d ]; then
    # Older audit packages used /etc/audisp/plugins.d. Try that as a
    # fallback before bailing.
    if [ -d /etc/audisp/plugins.d ]; then
        DESC_DEST=/etc/audisp/plugins.d/qdistro-audisp.conf
    else
        echo "audisp install: neither /etc/audit/plugins.d nor "\
             "/etc/audisp/plugins.d exists; SKIP"
        exit 0
    fi
fi

if [ ! -f "$SRC/qdistro-audisp-plugin" ]; then
    echo "audisp install: plugin script missing at $SRC/qdistro-audisp-plugin" >&2
    exit 2
fi

install -m 0755 -o root -g root \
    "$SRC/qdistro-audisp-plugin" "$PLUGIN_DEST"
install -m 0640 -o root -g root \
    "$SRC/qdistro-audisp.conf"   "$DESC_DEST"

# Tell auditd to re-read its plugin set. SIGHUP is the documented
# protocol; restart works too but loses in-flight records.
#
# `systemctl show -p MainPID --value auditd` returns:
#   - the actual PID when systemd manages auditd as a service,
#   - "0" when the unit is loaded but inactive,
#   - "1" or empty on installs where auditd is sysvinit-managed
#     and systemd inherited the kernel's pid 1 as a stand-in.
# kill -HUP 1 is a soft-reboot signal — never let that happen.
# Validate the PID looks live (>1 + corresponds to an auditd
# process) before HUPing; otherwise drop straight to
# reload-or-restart.
if systemctl is-active --quiet auditd; then
    /usr/sbin/auditctl -s 2>/dev/null | grep -q "enabled 1" || \
        /usr/sbin/auditctl -e 1 || true
    AUDITD_PID="$(systemctl show -p MainPID --value auditd 2>/dev/null)"
    SAFE_HUP=0
    if [ -n "$AUDITD_PID" ] && [ "$AUDITD_PID" -gt 1 ] 2>/dev/null \
       && [ -r "/proc/$AUDITD_PID/comm" ] \
       && [ "$(cat /proc/"$AUDITD_PID"/comm 2>/dev/null)" = "auditd" ]; then
        SAFE_HUP=1
    fi
    if [ "$SAFE_HUP" = "1" ]; then
        /bin/kill -HUP "$AUDITD_PID" 2>/dev/null \
            || systemctl reload-or-restart auditd 2>/dev/null \
            || true
    else
        systemctl reload-or-restart auditd 2>/dev/null || true
    fi
else
    systemctl enable --now auditd 2>/dev/null || true
fi

echo "audisp install: plugin at $PLUGIN_DEST"
echo "audisp install: descriptor at $DESC_DEST"

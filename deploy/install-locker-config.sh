#!/usr/bin/env bash
# Install qdlocker system configuration:
#   1. /etc/qdistro/locker.conf      (locker behavior knobs)
#   2. /etc/systemd/logind.conf.d/90-qdistro-lid-lock.conf
#                                    (lid-close -> Session.Lock signal)
#
# Must be run as root. We deliberately do NOT call `sudo` ourselves —
# that mis-interacts with packaging post-install scripts (which run
# with no controlling tty) and with `make install` (which may already
# be elevated). The caller (Makefile, RPM/dpkg spec, or human) is
# responsible for privilege.
#
# Logind drop-in semantics: `HandleLidSwitch=lock` makes logind emit
# `org.freedesktop.login1.Session.Lock` on the user's session bus
# object — it does NOT exec any external script. qdlocker subscribes
# to that signal directly (see qdlocker/qdlocker/logind.py).

set -euo pipefail

# Resolve our own directory so relative paths work regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SYSTEM_CONFIG_DIR="/etc/qdistro"
SYSTEM_CONFIG_FILE="$SYSTEM_CONFIG_DIR/locker.conf"
LOCKER_CONFIG_SRC="$SCRIPT_DIR/etc/qdistro/locker.conf"

LOGIND_DROPIN_DIR="/etc/systemd/logind.conf.d"
LOGIND_DROPIN_SRC="$SCRIPT_DIR/systemd/logind/90-qdistro-lid-lock.conf"
LOGIND_DROPIN_DST="$LOGIND_DROPIN_DIR/90-qdistro-lid-lock.conf"

DRY_RUN=0
if [[ "$(id -u)" -ne 0 ]]; then
    echo "warning: not running as root; entering dry-run mode" >&2
    echo "  (re-run with sudo / as root to actually install)" >&2
    DRY_RUN=1
fi

run_or_echo() {
    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "DRY-RUN: $*"
    else
        "$@"
    fi
}

[[ -f "$LOCKER_CONFIG_SRC" ]] || {
    echo "error: locker config source not found: $LOCKER_CONFIG_SRC" >&2
    exit 1
}
[[ -f "$LOGIND_DROPIN_SRC" ]] || {
    echo "error: logind drop-in source not found: $LOGIND_DROPIN_SRC" >&2
    exit 1
}

# ---- /etc/qdistro/locker.conf ----
run_or_echo install -d -m 0755 -o 0 -g 0 "$SYSTEM_CONFIG_DIR"
if [[ -e "$SYSTEM_CONFIG_FILE" ]] && [[ "$DRY_RUN" -eq 0 ]]; then
    echo "preserving existing $SYSTEM_CONFIG_FILE (delete it manually to re-install defaults)"
else
    run_or_echo install -m 0644 -o 0 -g 0 "$LOCKER_CONFIG_SRC" "$SYSTEM_CONFIG_FILE"
    echo "installed $SYSTEM_CONFIG_FILE"
fi

# ---- /etc/systemd/logind.conf.d/90-qdistro-lid-lock.conf ----
run_or_echo install -d -m 0755 -o 0 -g 0 "$LOGIND_DROPIN_DIR"
run_or_echo install -m 0644 -o 0 -g 0 "$LOGIND_DROPIN_SRC" "$LOGIND_DROPIN_DST"
echo "installed $LOGIND_DROPIN_DST"

# ---- reload logind so HandleLidSwitch=lock takes effect ----
if [[ "$DRY_RUN" -eq 0 ]]; then
    if command -v systemctl >/dev/null 2>&1; then
        # `reload` is non-destructive; restart would drop sessions.
        # logind responds to SIGHUP via reload; if reload isn't
        # supported on this version, fall back to restart with a loud
        # warning (which kills active sessions — operator's choice).
        if systemctl reload systemd-logind 2>/dev/null; then
            echo "reloaded systemd-logind"
        else
            echo "warning: systemctl reload systemd-logind failed; restart pending"
            echo "         run: systemctl restart systemd-logind   (will end sessions!)"
        fi
    else
        echo "warning: systemctl not found; reload systemd-logind manually"
    fi
fi

echo "locker configuration install complete"

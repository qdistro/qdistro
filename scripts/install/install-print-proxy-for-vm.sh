#!/bin/bash
# install-print-proxy-for-vm.sh — idempotent install of qdistro-print-proxy
# (spec/20 Phase-9 §step 0 skeleton) onto a fresh-clone VM.
#
# The proxy is a pure transport: AF_UNIX listener on
# /run/qdistro-print/ipp.sock, forwards each accepted connection to a
# configurable backend. With no print VM yet, the service starts but
# the backend connect fails fast and the proxy logs the failure on
# every accepted connection. That's expected for skeleton state.
#
# Layout:
#   /usr/local/bin/qdistro-print-proxy           # CLI entry
#   /etc/systemd/system/qdistro-print-proxy.service
#   /run/qdistro-print/                          # tmpfs at runtime
set -euo pipefail

SRC=${1:-/root/print-src}
if [ ! -d "$SRC" ]; then
    echo "[install-print] missing source dir $SRC" >&2
    exit 2
fi

DEST_BIN=/usr/local/bin
DEST_SYSD=/etc/systemd/system
DEST_LIB=/usr/libexec/qdistro
DEST_POLKIT_ACTION=/usr/share/polkit-1/actions
DEST_AUDIT=/var/lib/qdistro/audit

install -d -m 0755 "$DEST_BIN" "$DEST_SYSD" "$DEST_LIB" "$DEST_POLKIT_ACTION"
install -d -m 0700 "$DEST_AUDIT"

install -m 0755 "$SRC/qdistro_print_proxy.py" "$DEST_BIN/qdistro-print-proxy"
install -m 0644 "$SRC/qdistro-print-proxy.service" \
        "$DEST_SYSD/qdistro-print-proxy.service"
# Phase-9 §step 2: per-job audit module. Lives next to the proxy so
# the script can `from qdistro_print_audit import PrintAuditLog`.
if [ -f "$SRC/qdistro_print_audit.py" ]; then
    install -m 0644 "$SRC/qdistro_print_audit.py" \
        "$DEST_LIB/qdistro_print_audit.py"
fi
# Priority #5: cups-browsed allowlist renderer. Pure-python module
# imported by the host-side qdistro-print-allowlist CLI; the install
# script is run inside the VM so the module also lands in the VM's
# /usr/lib/qdistro for the rare case where the VM itself shells out.
if [ -f "$SRC/qdistro_print_browse.py" ]; then
    install -m 0644 "$SRC/qdistro_print_browse.py" \
        "$DEST_LIB/qdistro_print_browse.py"
fi
# Host CLI (qdistro-print-allowlist). On the test VM it just sits
# unused; on the admin host it's the entry point for `apply --vm`.
if [ -f "$SRC/qdistro-print-allowlist" ]; then
    install -m 0755 "$SRC/qdistro-print-allowlist" \
        "$DEST_BIN/qdistro-print-allowlist"
fi
# Host CLI (qdistro-print-jobs) — task(109): wrapper that drives the
# in-VM qdistro-print-job-control via qemu-guest-agent. Used by the
# admin-app's Printing > Jobs sub-pane and direct admin invocation.
if [ -f "$SRC/qdistro-print-jobs" ]; then
    install -m 0755 "$SRC/qdistro-print-jobs" \
        "$DEST_BIN/qdistro-print-jobs"
fi
# Phase-9 §step 1: spawn helper. Lands as a sibling so the proxy's
# QDISTRO_PRINT_VM_SPAWN env can point at /usr/local/bin/spawn-print-vm.sh
# without further config. Idempotent.
if [ -f "$SRC/spawn-print-vm.sh" ]; then
    install -m 0755 "$SRC/spawn-print-vm.sh" "$DEST_BIN/spawn-print-vm.sh"
fi
# Phase-9 §step 2: USB hot-plug helpers. Polkit-gated wrappers around
# `virsh attach-device` so admin-app + manual workflows have a single
# source-of-truth for printer attach/detach.
for helper in qdistro-print-attach-usb.sh qdistro-print-detach-usb.sh \
              install-print-vm.sh; do
    if [ -f "$SRC/$helper" ]; then
        # Strip .sh suffix on the user-facing CLI entries; keep
        # install-print-vm.sh as-is (it's an admin sysadmin tool).
        case "$helper" in
            install-print-vm.sh)
                install -m 0755 "$SRC/$helper" "$DEST_BIN/$helper" ;;
            *)
                install -m 0755 "$SRC/$helper" \
                    "$DEST_BIN/${helper%.sh}" ;;
        esac
    fi
done
# domain-template.xml lives under /usr/share/qdistro/print-vm so the
# install-print-vm CLI finds it at the standard system path.
DEST_TEMPLATE_DIR=/usr/share/qdistro/print-vm
if [ -f "$SRC/domain-template.xml" ]; then
    install -d -m 0755 "$DEST_TEMPLATE_DIR"
    install -m 0644 "$SRC/domain-template.xml" \
        "$DEST_TEMPLATE_DIR/domain-template.xml"
fi
# polkit actions for org.qdistro.print.* — safe to install even when
# no admin agent is registered yet (polkitd just enumerates actions).
if [ -f "$SRC/org.qdistro.print.policy" ]; then
    install -m 0644 "$SRC/org.qdistro.print.policy" \
        "$DEST_POLKIT_ACTION/org.qdistro.print.policy"
fi

systemctl daemon-reload
systemctl enable qdistro-print-proxy.service >/dev/null 2>&1 || true
# Don't `--now` start: with no print VM the proxy logs ECONNREFUSED on
# every accept, which would clutter journals. Tests start it explicitly.

echo "[install-print] OK — qdistro-print-proxy installed (not started)"

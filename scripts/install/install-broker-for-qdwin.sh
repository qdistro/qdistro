#!/bin/bash
# Idempotent minimal broker install for qdwin VMs that want §6.5 S4
# end-to-end coverage. Installs broker/ under
# /usr/libexec/qdistro/, drops the dbus policy + systemd unit,
# reloads dbus, and enables qdistro-admin-broker.service.
#
# DEST=/usr/libexec/qdistro/ (since 2026-04-29). Earlier versions
# installed under /usr/local/lib/qdistro/ but Tumbleweed targeted
# policy carries a `/usr/(.*/)?lib(/.*)?` → `lib_t` rule that beats
# our more-specific .fc entry under restorecon's selabel lookup —
# the runtime chcon was a workaround. /usr/libexec/ is outside the
# lib glob so .fc wins natively.
#
# Source: takes the broker tree path as $1. fresh-vm-bootstrap.sh
# untars the umbrella repo to /root/qdistro-src/qdistro/ and invokes
# this script with that path's broker/ subdir.
#
# Pre-reqs already baked into baseweed: python313-dbus-python,
# python313-gobject (Gdk/GLib), user `admin` (uid 1000).
set -eu

BROKER_SRC=${1:-/root/qdistro-src/qdistro/broker}
DEST=/usr/libexec/qdistro
UNIT=/etc/systemd/system/qdistro-admin-broker.service
POLICY=/etc/dbus-1/system.d/org.qdistro.AdminBroker1.conf

if [ ! -d "$BROKER_SRC" ]; then
    echo "ERROR: broker source not found at $BROKER_SRC" >&2
    echo "       pass the broker/ dir as \$1 or untar qdistro to /root/qdistro-src/qdistro/" >&2
    exit 2
fi

# 1. Filesystem layout.
install -d -m 0700 /var/lib/qdistro/approvals
install -d -m 0700 /var/lib/qdistro/audit
install -d -o root -g root -m 0755 "$DEST"

# 2. Broker source code (root-owned so non-root can't replace it).
# qdistro_admin_broker.py is mode 0755 — it's the systemd ExecStart
# target so the kernel's execve hook reads its SELinux label and
# triggers the init_daemon_domain transition into qdistro_broker_t
# (broker-policy/ Phase 2 / module 0.2.0). The other modules are
# import-only so they stay 0644.
install -o root -g root -m 0755 "$BROKER_SRC/qdistro_admin_broker.py" "$DEST/qdistro_admin_broker.py"
for f in qdistro_admin_cache.py qdistro_admin_audit.py \
         qdistro_admin_ratelimit.py qdistro_admin_rules.py \
         qdistro_audisp_parser.py qdistro_hook_client.py; do
    install -o root -g root -m 0644 "$BROKER_SRC/$f" "$DEST/$f"
done

# 2b. Workflow orchestration package. The broker loads it in-process
# and resolves a "workflow/" subdir beside itself (see
# _workflow_dir_candidates). In the source tree it's a sibling of
# broker/; here it's flattened into $DEST/workflow/. Best-effort: a
# missing source dir just leaves the broker without workflows.
WORKFLOW_SRC="$(dirname "$BROKER_SRC")/workflow"
if [ -d "$WORKFLOW_SRC" ]; then
    install -d -o root -g root -m 0755 "$DEST/workflow"
    for f in __init__.py workflow_schema.py workflow_loader.py \
             trigger_registry.py cron_parser.py audit_logger.py \
             secret_delivery.py workflow_engine.py pwd_secret_source.py; do
        [ -f "$WORKFLOW_SRC/$f" ] && \
            install -o root -g root -m 0644 "$WORKFLOW_SRC/$f" \
                "$DEST/workflow/$f"
    done
fi

# Apply qdistro_broker_exec_t label if the broker-policy module is
# loaded. Best-effort: fresh-vm-bootstrap.sh runs broker-policy
# install-policy.sh before this script in the canonical bootstrap
# order, but the §6.5 standalone qdwin install path skips broker-
# policy. With DEST=/usr/libexec/qdistro/ since 2026-04-29 the .fc
# now wins natively under restorecon (no Tumbleweed lib_t glob in
# the way) so restorecon is the primary path. Both fail-soft when
# the type doesn't exist (broker-policy module not yet installed).
if command -v restorecon >/dev/null 2>&1; then
    restorecon "$DEST/qdistro_admin_broker.py" 2>/dev/null || true
fi

# 3. dbus policy + systemd unit.
install -m 0644 "$BROKER_SRC/org.qdistro.AdminBroker1.conf" "$POLICY"
install -m 0644 "$BROKER_SRC/qdistro-admin-broker.service" "$UNIT"
# Defensive dbus-broker reload oneshot, ordered Before=qdistro-admin-
# broker.service. Closes a flaky-on-first-enforcing-baked-boot path
# where the broker's RequestName fails with a generic policy
# AccessDenied even though the .conf is on disk. See spec/30 §"dbus-
# broker policy-reload mystery". Idempotent.
RELOAD_UNIT=/etc/systemd/system/qdistro-dbus-reload.service
install -m 0644 "$BROKER_SRC/qdistro-dbus-reload.service" "$RELOAD_UNIT"

# 4. Reload dbus so the new policy takes effect. System bus uses
#    dbus-broker.service on modern Tumbleweed, fall back to dbus.service
#    on older variants.
systemctl reload dbus-broker.service 2>/dev/null \
    || systemctl reload dbus.service 2>/dev/null \
    || true

# 5. Start the broker.
systemctl daemon-reload
systemctl enable --now qdistro-dbus-reload.service 2>/dev/null || true
systemctl enable --now qdistro-admin-broker.service

# 6. Wait for the bus name (Type=dbus activates automatically, but
#    give it a moment to claim).
for _ in 1 2 3 4 5; do
    busctl list --no-pager 2>/dev/null | grep -q org.qdistro.AdminBroker1 && break
    sleep 0.5
done

if ! busctl list --no-pager 2>/dev/null | grep -q org.qdistro.AdminBroker1; then
    echo "ERROR: broker service failed to claim bus name" >&2
    journalctl -u qdistro-admin-broker.service --no-pager -n 30 >&2
    exit 3
fi

echo "broker ready on org.qdistro.AdminBroker1"

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
         qdistro_audisp_parser.py qdistro_hook_client.py \
         qdistro_proc_identity.py qdistro_launch_record.py \
         qdistro_resolver.py qdistro_lineage_store.py \
         qdistro_lineage_receipts.py qdistro_export_lineage.py \
         qdistro_commit_lineage.py qdistro_lineage.py \
         qdistro_guard_registry.py qdistro_metadata_schema.py \
         qdistro_silo_security.py \
         qdistro_upload_lineage.py qdistro_upload_lineage_entry.py; do
    install -o root -g root -m 0644 "$BROKER_SRC/$f" "$DEST/$f"
done

# Silo -> security-snapshot store: the v1 bootstrap backing for the central
# snapshot authority (qdistro_silo_security.TomlSnapshotAuthority). Installed mode
# 0644 (root-owned, only-owner-writable) because the loader refuses a
# group/world-writable or non-root-owned store (it mints resolved FlowEndpoints).
# Conditional install preserves admin edits, mirroring disposable-classes.toml;
# a future control-plane daemon owns/populates the store behind the same seam.
install -d -o root -g root -m 0755 /etc/qdistro
if [ ! -f /etc/qdistro/silo-security.toml ]; then
    install -o root -g root -m 0644 \
        "$BROKER_SRC/silo-security.toml" \
        /etc/qdistro/silo-security.toml
fi

# Broker-central export lineage re-validates disposable class policy itself.
# These pure modules live under session_manager/ in the source tree but the
# broker is installed before the session-manager in the canonical bootstrap, so
# copy them here too.
SESSION_MANAGER_SRC="$(dirname "$BROKER_SRC")/session_manager"
for f in qdistro_disposables.py qdistro_disposable_classes.py; do
    install -o root -g root -m 0644 "$SESSION_MANAGER_SRC/$f" "$DEST/$f"
done

install -d -o root -g root -m 0700 /var/lib/qdistro/lineage

# 2b. Workflow orchestration package. The broker loads it in-process
# and resolves a "workflow/" subdir beside itself (see
# _workflow_dir_candidates). In the source tree it's a sibling of
# broker/; here it's flattened into $DEST/workflow/.
#
# The list below MUST be closed under the package's top-level imports.
# It was not: condition_eval.py and agent_relay.py were omitted while
# workflow_engine.py does a top-level `import condition_eval`, so on
# every installed system `from workflow_engine import WorkflowEngine`
# raised ModuleNotFoundError, the broker's best-effort handler left
# self.workflow_engine = None, and ListWorkflows returned [] forever.
# No test caught it because pyproject.toml puts workflow/ on pytest's
# pythonpath, so the repo layout always resolves. The guard against a
# repeat is tests/unit/test_broker_workflow_installed_layout.py, which parses
# this loop and asserts closure at the INSTALLED destination.
#
# condition_eval.py additionally imports qdistro_proc_identity, which
# is not in this package — it resolves because the broker installs it
# into $DEST and $DEST is on sys.path (the broker's own sys.path[0]).
WORKFLOW_SRC="$(dirname "$BROKER_SRC")/workflow"
# Hard error on an absent source dir too. This used to be a best-effort
# `if [ -d ... ]`, which meant a moved or renamed workflow/ reproduced
# exactly the bug above — a broker that starts fine and silently has no
# workflows — with no install-time signal. BROKER_SRC is always a full
# checkout, so the directory being missing is a packaging fault, not a
# supported configuration.
if [ ! -d "$WORKFLOW_SRC" ]; then
    echo "ERROR: workflow package not found at $WORKFLOW_SRC" >&2
    echo "       expected it beside the broker source ($BROKER_SRC)" >&2
    exit 2
fi
install -d -o root -g root -m 0755 "$DEST/workflow"
for f in __init__.py workflow_schema.py workflow_loader.py \
         trigger_registry.py cron_parser.py audit_logger.py \
         secret_delivery.py workflow_engine.py pwd_secret_source.py \
         condition_eval.py agent_relay.py; do
    # Hard error, not a skip: a missing module here is invisible at
    # install time and fatal at run time (the failure this comment
    # documents). The old `[ -f ... ] && install ...` form also made
    # the whole `set -e` script exit if the LAST entry was absent.
    if [ ! -f "$WORKFLOW_SRC/$f" ]; then
        echo "ERROR: workflow module $f missing from $WORKFLOW_SRC" >&2
        echo "       the broker cannot load the workflow engine without it" >&2
        exit 2
    fi
    install -o root -g root -m 0644 "$WORKFLOW_SRC/$f" \
        "$DEST/workflow/$f"
done

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

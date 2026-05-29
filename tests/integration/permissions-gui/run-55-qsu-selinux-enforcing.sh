#!/bin/bash
# Runner for scenario 55 — qsu end-to-end under SELinux Enforcing with
# zero new AVCs against the qdistro domains.
#
# This is the executable form of
# tests/integration/permissions-gui/55-qsu-selinux-enforcing.md. It runs
# the 43/50 shape (qsu prompt -> admin approve over D-Bus -> command
# streams back) but with SELinux flipped to ENFORCING, and asserts:
#
#   (A) the command actually ran (stdout matches the permissive run:
#       `id` reports uid=2000(work)), AND
#   (B) NO new AVC / audit2why denial touching the qdistro source
#       contexts (qdistro_root_exec_t / qdistro_tier1_t / qdistro_broker_t
#       / qdistro_pwd_t) appears in /var/log/audit/audit.log for the qsu
#       flow since a baseline cursor.
#
# TRANSPORT — SSH, NOT qga.
#   qemu-guest-agent runs in a confined SELinux domain
#   (virt_qemu_ga_t-equivalent) that is NOT granted selinux_setenforce, so
#   `setenforce 1` over vm-exec returns "Permission denied" (empirically
#   validated 2026-05-16 against tier1-test-260516-1252 and
#   qd-sudo-260516-1657 — systemd-run --scope and /etc/systemd/system
#   writes are denied too). This runner therefore REQUIRES SSH transport.
#   It reuses the VM_SSH_* convention from tests/integration/vm/helpers.bash:
#
#     export VM_SSH_PORT=<port>     # e.g. 5722 — REQUIRED, no default
#     export VM_SSH_HOST=127.0.0.1                 # default
#     export VM_SSH_USER=root                      # default
#     export VM_SSH_KEY=$HOME/.ssh/qdistro_enforcing_id_ed25519  # default
#     export VMNAME=tier1-test-260516-1252         # informational only
#
# Exit codes:
#   0  PASS  — command ran AND zero qdistro-domain AVCs since baseline
#   1  FAIL  — command did not run, or AVCs appeared (printed + audit2allow)
#   2  ERROR — preconditions not met (VM_SSH_PORT unset, ssh unreachable)
#   3  SKIP  — VM is config-pinned permissive (cannot reach Enforcing)
#
# This runner is intentionally headless: it drives admin's approve via
# the broker D-Bus API (runuser -u admin -- python3) rather than the GUI,
# because scenario 55 is about the SELinux transitions, not the Qt UI.
#
# LIVE-RUN STATUS: this runner is COMPLETE and `bash -n`-clean, but the
# end-to-end PASS requires a live enforcing VM with SSH transport
# configured and CANNOT run headless in the dev sandbox (no such VM, and
# qga cannot setenforce). Provision the VM + export VM_SSH_PORT to run it.
set -uo pipefail

# ---------------------------------------------------------------------------
# Preconditions + SSH transport
# ---------------------------------------------------------------------------

: "${VM_SSH_HOST:=127.0.0.1}"
: "${VM_SSH_USER:=root}"
: "${VM_SSH_KEY:=$HOME/.ssh/qdistro_enforcing_id_ed25519}"

if [[ -z "${VM_SSH_PORT:-}" ]]; then
    echo "ERROR: qga transport insufficient for setenforce; this scenario" \
         "requires VM_SSH_PORT" >&2
    echo "       export VM_SSH_PORT=<port> VM_SSH_KEY=<ed25519 key> and" \
         "re-run (see 55-qsu-selinux-enforcing.md Transport)." >&2
    exit 2
fi

# vm_ssh <cmd> — run a single command inside the VM over SSH. Mirrors the
# ssh invocation in tests/integration/vm/helpers.bash::vm_run.
vm_ssh() {
    ssh \
        -p "$VM_SSH_PORT" \
        -i "$VM_SSH_KEY" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        -o ConnectTimeout=10 \
        -o BatchMode=yes \
        "$VM_SSH_USER@$VM_SSH_HOST" \
        "$@"
}

# Fail fast if the VM is unreachable over SSH.
if ! vm_ssh 'true' 2>/dev/null; then
    echo "ERROR: cannot reach VM over ssh ($VM_SSH_USER@$VM_SSH_HOST:" \
         "$VM_SSH_PORT, key $VM_SSH_KEY)" >&2
    exit 2
fi

PASS=0
fail()  { echo "--- FAIL: $* ---" >&2; PASS=1; }
check() { echo "--- CHECK: $* ---" >&2; }

# ---------------------------------------------------------------------------
# Setup — qsu install + work user, drain broker state, confirm Enforcing
#         is actually reachable.
# ---------------------------------------------------------------------------

echo "=== Setup: ensure work user + drain broker state ==="
vm_ssh 'id work >/dev/null 2>&1 || useradd -m -u 2000 -U work; loginctl enable-linger work >/dev/null 2>&1 || true'

# Pre-flight: confirm the kernel can actually flip to Enforcing. A
# config-pinned-permissive VM (SELINUX=permissive in /etc/selinux/config
# or a boot enforcing=0) lets `setenforce 1` succeed silently but
# `getenforce` stays Permissive — that is a SKIP, not a FAIL.
PINCHECK=$(vm_ssh '/usr/sbin/setenforce 1 2>/dev/null; m=$(/usr/sbin/getenforce); /usr/sbin/setenforce 0 2>/dev/null; echo "$m"')
if [[ "$PINCHECK" != "Enforcing" ]]; then
    echo "--- SKIP: VM cannot reach Enforcing (getenforce=$PINCHECK after" \
         "setenforce 1); config-pinned permissive. ---" >&2
    exit 3
fi
check "VM can flip to Enforcing (pre-flight getenforce=$PINCHECK)"

vm_ssh '
  set -e
  pkill -u work -f qsu 2>/dev/null || true
  rm -f /etc/qdistro/rules.d/[0-9][0-9]*.yaml
  systemctl restart qdistro-admin-broker.service
  systemctl restart qdistro-root-exec.socket
  sleep 2
  sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE '"'"'qsu.exec:%'"'"';" 2>/dev/null || true
  sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE '"'"'qsu.exec:%'"'"';" 2>/dev/null || true
'

# ---------------------------------------------------------------------------
# S1 — baseline audit cursor + flip to Enforcing
# ---------------------------------------------------------------------------

echo "=== S1: baseline cursor + setenforce 1 ==="
MODE=$(vm_ssh '
  BASELINE_TS=$(($(date +%s) - 1))
  echo $BASELINE_TS > /tmp/55-baseline-ts
  /usr/sbin/setenforce 1
  /usr/sbin/getenforce
')
if [[ "$MODE" != "Enforcing" ]]; then
    echo "--- SKIP: getenforce=$MODE after setenforce 1 (config-pinned). ---" >&2
    vm_ssh '/usr/sbin/setenforce 0 2>/dev/null || true'
    exit 3
fi
check "SELinux mode is Enforcing"

# ---------------------------------------------------------------------------
# S2 — qsu invocation under enforcing (strict identity profile ON)
# ---------------------------------------------------------------------------

echo "=== S2: qsu /usr/bin/id as work, under Enforcing ==="
vm_ssh '
  sudo -u work bash -c "/usr/local/bin/qsu /usr/bin/id \
    > /tmp/55-qsu.log 2>&1 & echo \$! > /tmp/55-qsu.pid"
  sleep 2
'

# ---------------------------------------------------------------------------
# S3 — admin approves via D-Bus (no GUI), then collect qsu output
# ---------------------------------------------------------------------------

echo "=== S3: admin approve over D-Bus ==="
DECIDE=$(vm_ssh '
  runuser -u admin -- python3 - <<PYEOF
import dbus
bus = dbus.SystemBus()
obj = bus.get_object("org.qdistro.AdminBroker1",
                     "/org/qdistro/AdminBroker1")
iface = dbus.Interface(obj, "org.qdistro.AdminBroker1")
rows = [r for r in iface.GetPending()
        if str(r.get("action", "")).startswith("qsu.exec:")]
assert rows, "no qsu pending row found"
rid = int(rows[0]["id"])
iface.DecideRequest(rid, "allow", "forever_argv")
print("decided rid=", rid)
PYEOF
')
echo "$DECIDE"
if ! grep -q "decided rid=" <<<"$DECIDE"; then
    fail "admin D-Bus approve did not report a decision (broker reachability" \
         "/ qdistro_broker AVC?): $DECIDE"
fi

sleep 2
QSU_OUT=$(vm_ssh 'wait $(cat /tmp/55-qsu.pid) 2>/dev/null; cat /tmp/55-qsu.log')
echo "--- qsu output ---"
echo "$QSU_OUT"

# Assert (A): the command RAN and produced the work-uid identity. This is
# the load-bearing "the SELinux transitions did not break the exec" check;
# it must match the permissive run (id reports uid=2000(work)).
if grep -q "uid=2000(work)" <<<"$QSU_OUT"; then
    check "qsu exec ran under Enforcing — id reports uid=2000(work)"
else
    fail "qsu /usr/bin/id did NOT report uid=2000(work) under Enforcing" \
         "(transition broke the exec, or approval never delivered)." \
         "Output: $QSU_OUT"
fi

# ---------------------------------------------------------------------------
# S4 — collect new AVCs against the qdistro domains since baseline
# ---------------------------------------------------------------------------

echo "=== S4: AVC scan since baseline ==="
AVC_REPORT=$(vm_ssh '
  BASELINE_TS=$(cat /tmp/55-baseline-ts)
  sleep 1   # let auditd flush
  ausearch -m AVC,USER_AVC \
    --start "$(date -d @"$BASELINE_TS" "+%x %T")" 2>/dev/null \
    | grep -E "scontext=[^ ]*:(qdistro_root_exec_t|qdistro_tier1_t|qdistro_broker_t|qdistro_pwd_t|qsu_child_t)" \
    > /tmp/55-avcs.txt || true
  echo "AVC_COUNT=$(wc -l < /tmp/55-avcs.txt)"
  echo "--- avcs ---"
  cat /tmp/55-avcs.txt
  echo "--- audit2allow ---"
  audit2allow -i /tmp/55-avcs.txt 2>/dev/null || true
')
echo "$AVC_REPORT"
AVC_COUNT=$(grep -oE 'AVC_COUNT=[0-9]+' <<<"$AVC_REPORT" | head -1 | cut -d= -f2)
AVC_COUNT=${AVC_COUNT:-unknown}

# Assert (B): zero new AVCs touching qdistro source contexts.
if [[ "$AVC_COUNT" == "0" ]]; then
    check "zero new qdistro-domain AVCs since baseline"
else
    fail "found $AVC_COUNT new qdistro-domain AVC(s) under Enforcing for the" \
         "qsu flow. The audit2allow output above is the candidate fix —" \
         "add the rules to selinux/qsu/qdistro_qsu.te (or qdistro_tier1.te" \
         "/ qdistro_broker.te) and re-run."
fi

# ---------------------------------------------------------------------------
# S5 / Teardown — restore permissive, clean up
# ---------------------------------------------------------------------------

echo "=== Teardown: restore permissive + clean state ==="
vm_ssh '
  /usr/sbin/setenforce 0 2>/dev/null || true
  pkill -u work -f qsu 2>/dev/null || true
  rm -f /tmp/55-*.log /tmp/55-*.pid /tmp/55-baseline-ts /tmp/55-avcs.txt
  sqlite3 /var/lib/qdistro/approvals/approvals.sqlite "DELETE FROM approvals WHERE action LIKE '"'"'qsu.exec:%'"'"';" 2>/dev/null || true
  sqlite3 /var/lib/qdistro/audit/audit.sqlite "DELETE FROM audit WHERE action LIKE '"'"'qsu.exec:%'"'"';" 2>/dev/null || true
'
FINAL_MODE=$(vm_ssh '/usr/sbin/getenforce')
check "SELinux restored to $FINAL_MODE"

if [[ "$PASS" -eq 0 ]]; then
    echo "=== PASS: qsu ran under Enforcing with zero qdistro-domain AVCs ==="
else
    echo "=== FAIL: see CHECK/FAIL lines above ===" >&2
fi
exit "$PASS"

#!/bin/bash
# In-VM driver for tiered-isolation.bats:phase7-qsu-enforcing.
#
# Headless port of tests/integration/permissions-gui/55-qsu-selinux-enforcing.md
# (formerly driven from the host by permissions-gui/run-55-qsu-selinux-enforcing.sh).
# The scenario never needed the GUI: it flips SELinux to Enforcing, runs the
# real `qsu /usr/bin/id` as the non-admin user, approves it over D-Bus as the
# admin uid, and asserts
#   (A) the command ran as that user (id reports uid=<work>(work)), and
#   (B) zero new AVC / USER_AVC denials with a qdistro source context
#       (qdistro_root_exec_t / qdistro_tier1_t / qdistro_broker_t /
#       qdistro_pwd_t / qsu_child_t) since a baseline cursor.
#
# TRANSPORT: qemu-guest-agent's domain cannot setenforce, so the bats case
# runs this only over SSH (VM_SSH_PORT, run-bats-enforcing.sh) and SKIPs
# otherwise. SKIP semantics match s55/s56: Disabled, config-pinned
# permissive, or the runtime flip refused -> "SKIP:". The EXIT trap restores
# Permissive on natural exit or signal.

set -u

PASSCOUNT=0
FAILCOUNT=0
pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
skip() { echo "SKIP: $*"; exit 0; }

SE_MODE_INITIAL=$(/usr/sbin/getenforce 2>/dev/null || echo Disabled)
[ "$SE_MODE_INITIAL" = Disabled ] && skip "SELinux is Disabled"
if grep -Eq '^SELINUX=permissive' /etc/selinux/config 2>/dev/null; then
    skip "/etc/selinux/config pins SELINUX=permissive — runtime flip refused"
fi
[ -x /usr/local/bin/qsu ] || { fail "/usr/local/bin/qsu absent"; exit 1; }

WORK_USER=work
if ! getent passwd "$WORK_USER" >/dev/null 2>&1; then
    useradd -m -U -s /bin/bash "$WORK_USER" \
        || { fail "could not create $WORK_USER"; exit 1; }
fi
WORK_UID=$(id -u "$WORK_USER")

QSU_PID=""
restore() {
    [ -n "$QSU_PID" ] && kill "$QSU_PID" 2>/dev/null
    /usr/sbin/setenforce 0 2>/dev/null || true
}
trap restore EXIT INT TERM

# Drain qsu state so the request below is the only one.
python3 - <<'PYEOF' 2>/dev/null || true
import sqlite3
con = sqlite3.connect("/var/lib/qdistro/approvals/approvals.sqlite", timeout=10)
con.execute("DELETE FROM approvals WHERE action LIKE 'qsu.exec:%'")
con.commit()
PYEOF

BASELINE_TS=$(( $(date +%s) - 1 ))
/usr/sbin/setenforce 1 2>/dev/null
SE_MODE=$(/usr/sbin/getenforce 2>/dev/null || echo Unknown)
if [ "$SE_MODE" = Enforcing ]; then
    pass "SELinux mode now Enforcing"
else
    skip "setenforce 1 left mode at $SE_MODE (config-pinned or transport cannot setenforce)"
fi

systemctl restart qdistro-admin-broker.service 2>/dev/null || true
systemctl restart qdistro-root-exec.socket 2>/dev/null || true
for _ in $(seq 1 40); do
    dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
        /org/qdistro/AdminBroker1 org.freedesktop.DBus.Peer.Ping >/dev/null 2>&1 && break
    sleep 0.25
done

rm -f /tmp/s54.out /tmp/s54.rc
( runuser -u "$WORK_USER" -- /usr/local/bin/qsu /usr/bin/id >/tmp/s54.out 2>&1 </dev/null
  echo $? >/tmp/s54.rc ) &
QSU_PID=$!

DECIDE=$(runuser -u admin -- python3 -c '
import dbus, sys, time
bus = dbus.SystemBus()
iface = dbus.Interface(bus.get_object("org.qdistro.AdminBroker1",
        "/org/qdistro/AdminBroker1"), "org.qdistro.AdminBroker1")
uid = int(sys.argv[1])
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    for r in iface.GetPending():
        if int(r["uid"]) == uid and str(r.get("action", "")) == "qsu.exec:root":
            iface.DecideRequest(int(r["id"]), "allow", "once")
            print("decided rid=%d" % int(r["id"]))
            sys.exit(0)
    time.sleep(0.2)
print("no qsu pending row")
' "$WORK_UID" 2>&1)
case "$DECIDE" in
    "decided rid="*) pass "admin approved the qsu request over D-Bus under Enforcing ($DECIDE)" ;;
    *) fail "admin D-Bus approve failed under Enforcing: $DECIDE" ;;
esac

for _ in $(seq 1 100); do [ -s /tmp/s54.rc ] && break; sleep 0.2; done
wait "$QSU_PID" 2>/dev/null; QSU_PID=""
OUT=$(cat /tmp/s54.out 2>/dev/null)
if grep -q "uid=0(root)" <<<"$OUT" && [ "$(cat /tmp/s54.rc 2>/dev/null)" = 0 ]; then
    pass "qsu /usr/bin/id ran under Enforcing (uid=0(root))"
else
    fail "qsu /usr/bin/id did not run under Enforcing: rc=$(cat /tmp/s54.rc 2>/dev/null) out=$OUT"
fi

sleep 1   # let auditd flush
AVCS=/tmp/s54-avcs.txt
: >"$AVCS"
ausearch -m AVC,USER_AVC --start "$(date -d @"$BASELINE_TS" '+%x %T')" 2>/dev/null \
    | grep -E 'scontext=[^ ]*:(qdistro_root_exec_t|qdistro_tier1_t|qdistro_broker_t|qdistro_pwd_t|qsu_child_t)' \
    >"$AVCS" || true
N=$(wc -l <"$AVCS")
if [ "$N" -eq 0 ]; then
    pass "0 new denials — qsu policy covers the enforcing qsu flow"
else
    cat "$AVCS" >&2
    command -v audit2allow >/dev/null 2>&1 && audit2allow -i "$AVCS" >&2
    fail "$N new qdistro-domain AVCs for the qsu flow under Enforcing"
fi

if [ "$FAILCOUNT" -eq 0 ]; then
    echo "[s54] $PASSCOUNT passes, 0 failures"; exit 0
fi
echo "[s54] $PASSCOUNT passes, $FAILCOUNT failures"; exit 1

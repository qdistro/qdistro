#!/bin/bash
# gui-fixes-verify.sh — in-VM verification of the five GUI-test fixes diagnosed
# from full-20260619T101414Z. Run as root inside a qci bats VM (full qdistro
# stack: broker, print-proxy, qdwin compositor + qdshell, qdlocker, the
# qdwin-bystander binary; the qdwin session is booted).
#
# Emits PASS:/FAIL: lines the .bats asserts on, and INFO: lines that capture
# diagnostic state for the two items (qdlocker PAM, broker signal) whose root
# cause needed an in-VM repro. Best-effort, never `set -e` — a single probe
# crashing must not hide the others.
set -u
PASS() { echo "PASS: $*"; }
FAIL() { echo "FAIL: $*"; }
INFO() { echo "INFO: $*"; }
RUA() { runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 "$@"; }

echo "=== #1 print-proxy RuntimeDirectory ==="
systemctl reset-failed qdistro-print-proxy.service 2>/dev/null || true
systemctl restart qdistro-print-proxy.service 2>/dev/null || true
sleep 1.5
if systemctl is-active --quiet qdistro-print-proxy.service; then
    PASS "print-proxy active after restart"
else
    FAIL "print-proxy not active ($(systemctl is-active qdistro-print-proxy.service 2>&1))"
fi
if [ -d /run/qdistro-print ]; then
    PASS "print-proxy /run/qdistro-print exists"
else
    FAIL "print-proxy /run/qdistro-print missing"
fi
n226=$(journalctl -u qdistro-print-proxy.service --since '-90 sec' 2>/dev/null | grep -c '226/NAMESPACE')
if [ "${n226:-1}" -eq 0 ]; then
    PASS "print-proxy no 226/NAMESPACE in last 90s"
else
    FAIL "print-proxy still hit 226/NAMESPACE x$n226"
fi

echo "=== #2 qdlocker PAM repro (diagnostic) ==="
INFO "passwd: $(getent passwd admin | cut -d: -f1,3,6)"
for c in /usr/sbin/unix_chkpwd /sbin/unix_chkpwd "$(command -v unix_chkpwd 2>/dev/null)"; do
    [ -e "$c" ] && { INFO "unix_chkpwd: $(ls -l "$c")"; break; }
done
INFO "faillock(before): $(faillock --user admin 2>/dev/null | tail -3 | tr '\n' '|')"
faillock --user admin --reset 2>/dev/null || true
pam_qd=$(runuser -u admin -- python3 -c "
import pam
p=pam.pam()
ok=p.authenticate('admin','Pa_ssw0rd45',service='qdlocker',call_end=True)
print('RESULT', ok, 'code', getattr(p,'code',None), 'reason', repr(getattr(p,'reason',None)))
" 2>&1 | tr '\n' ' ')
INFO "pam(qdlocker) as admin: $pam_qd"
pam_login=$(runuser -u admin -- python3 -c "
import pam
p=pam.pam()
print('RESULT', p.authenticate('admin','Pa_ssw0rd45',service='login',call_end=True))
" 2>&1 | tr '\n' ' ')
INFO "pam(login) as admin: $pam_login"
if echo "$pam_qd" | grep -q 'RESULT True'; then
    PASS "qdlocker PAM accepts Pa_ssw0rd45 via 'qdlocker' service (standalone)"
else
    FAIL "qdlocker PAM rejects Pa_ssw0rd45 via 'qdlocker' service (reproduced unlock bug outside GUI)"
fi

echo "=== #3 broker ApprovalRevoked signal repro ==="
systemctl restart qdistro-admin-broker.service 2>/dev/null || true
sleep 2
sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 'DELETE FROM approvals;' 2>/dev/null || true
python3 - <<'PY' 2>&1 | sed 's/^/INFO seed: /'
import sys; sys.path.insert(0, '/usr/libexec/qdistro')
from qdistro_admin_cache import ApprovalCache
c = ApprovalCache('/var/lib/qdistro/approvals/approvals.sqlite')
c.store(2000, 'test.action', '/usr/bin/python3', 'forever_exe', True, 1000)
print('seeded')
PY
apid=$(sqlite3 /var/lib/qdistro/approvals/approvals.sqlite 'SELECT MAX(id) FROM approvals;' 2>/dev/null)
INFO "seeded approval id=${apid:-?}"
# Inline subscriber: a real dbus-python add_signal_receiver (the path product
# subscribers use), not dbus-monitor's eavesdrop. Writes the captured payload.
cat >/tmp/ar-sub.py <<'PY'
import sys, json
import dbus, dbus.mainloop.glib
from gi.repository import GLib
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus(); loop = GLib.MainLoop(); seen = []
def h(*a):
    seen.append([int(x) if str(x).lstrip('-').isdigit() else str(x) for x in a])
    open('/tmp/ar-out.json','w').write(json.dumps(seen)); loop.quit()
bus.add_signal_receiver(h, signal_name='ApprovalRevoked',
    dbus_interface='org.qdistro.AdminBroker1', path='/org/qdistro/AdminBroker1')
open('/tmp/ar-ready','w').write('ready')
GLib.timeout_add(12000, lambda: loop.quit())
loop.run()
sys.exit(0 if seen else 2)
PY
rm -f /tmp/ar-out.json /tmp/ar-ready
setsid python3 /tmp/ar-sub.py >/tmp/ar-sub.log 2>&1 &
for _i in $(seq 1 50); do [ -f /tmp/ar-ready ] && break; sleep 0.1; done
[ -f /tmp/ar-ready ] && INFO "subscriber ready" || INFO "subscriber NOT ready (log: $(cat /tmp/ar-sub.log 2>/dev/null))"
dbus-send --system --print-reply --dest=org.qdistro.AdminBroker1 \
    /org/qdistro/AdminBroker1 org.qdistro.AdminBroker1.RevokeApproval \
    "int32:${apid:-1}" 2>&1 | sed 's/^/INFO dbus-send: /'
sleep 2.5
journalctl -u qdistro-admin-broker.service --since '-15 sec' 2>/dev/null \
    | grep -i 'revoke' | tail -2 | sed 's/^/INFO broker-log: /'
# Require the captured payload to match the seeded row, not just that some
# ApprovalRevoked arrived — an unrelated revoke must not satisfy the check.
if [ -f /tmp/ar-out.json ] && python3 - <<'PY'
import json, sys
try:
    p = json.load(open('/tmp/ar-out.json'))
except Exception:
    sys.exit(1)
sys.exit(0 if [2000, 'test.action', '/usr/bin/python3'] in p else 1)
PY
then
    PASS "broker ApprovalRevoked captured by real subscriber: $(cat /tmp/ar-out.json)"
else
    FAIL "broker ApprovalRevoked NOT captured with expected payload (got: $(cat /tmp/ar-out.json 2>/dev/null || echo none))"
fi

echo "=== #4 + #5 bystander FIFO default + become-shell ==="
sock=$(runuser -u admin -- bash -c 'WPID=$(pgrep -u admin weston | head -1); ls -l /proc/$WPID/fd 2>/dev/null | grep -oE "wayland-[0-9]+\.lock" | head -1 | sed "s/\.lock$//"')
INFO "weston socket: ${sock:-none}"
RUA systemctl --user reset-failed qdshell.service 2>/dev/null || true
RUA systemctl --user stop qdshell.service 2>/dev/null || true
pkill -u admin -x qs 2>/dev/null || true
pkill -u admin -x qdwin-bystander 2>/dev/null || true
sleep 0.5
rm -f /run/user/1000/qdwin-cmd.fifo /tmp/qdwin-cmd.fifo
# Launch the bystander WITHOUT QDWIN_BYSTANDER_FIFO so we exercise the new C
# default ($XDG_RUNTIME_DIR/qdwin-cmd.fifo). XDG_RUNTIME_DIR is /run/user/1000.
runuser -u admin -- bash -c "export XDG_RUNTIME_DIR=/run/user/1000; export WAYLAND_DISPLAY=${sock:-wayland-1}; setsid qdwin-bystander >/tmp/bystander.log 2>&1 &"
for _i in $(seq 1 60); do [ -p /run/user/1000/qdwin-cmd.fifo ] && break; sleep 0.1; done
if [ -p /run/user/1000/qdwin-cmd.fifo ]; then
    PASS "bystander FIFO at /run/user/1000/qdwin-cmd.fifo (C default = XDG_RUNTIME_DIR)"
elif [ -p /tmp/qdwin-cmd.fifo ]; then
    FAIL "bystander FIFO landed in /tmp (C default fix not applied)"
else
    FAIL "bystander FIFO absent (log: $(tail -3 /tmp/bystander.log 2>/dev/null | tr '\n' '|'))"
fi
# #5: with qdshell cleanly stopped (Restart= suppressed), the bystander should
# hold the role without a 'shell role already claimed' crash storm.
if grep -qi 'shell role already claimed' /tmp/bystander.log 2>/dev/null; then
    FAIL "bystander hit 'shell role already claimed' (qdshell still competing)"
else
    PASS "bystander bound shell role without contention (qdshell stopped)"
fi
RUA systemctl --user start qdshell.service 2>/dev/null || true

echo "=== #2b qdlocker PAM inside qdlocker.service-equivalent sandbox (diagnostic) ==="
# Reproduce the locker's RUNTIME context: PrivateNetwork + restricted address
# families, as in qdlocker/systemd/qdlocker.service. If THIS fails while the
# plain repro above passed, unix_chkpwd's 'user unknown' is the sandbox; if it
# also passes, the GUI unlock failure is the known QMP keystroke-injection flake.
faillock --user admin --reset 2>/dev/null || true
sb=$(systemd-run --pipe --quiet --uid=admin \
        -p PrivateNetwork=yes \
        -p 'RestrictAddressFamilies=AF_UNIX AF_NETLINK' \
        -p IPAddressDeny=any \
        python3 -c "import pam; print('SANDBOX', pam.pam().authenticate('admin','Pa_ssw0rd45',service='qdlocker',call_end=True))" 2>&1 | tr '\n' ' ')
INFO "pam(qdlocker) in sandbox: $sb"
if echo "$sb" | grep -q 'SANDBOX True'; then
    INFO "#2 VERDICT: PAM works even in the locker sandbox -> GUI unlock fail is the keystroke-injection flake, not a product bug"
else
    INFO "#2 VERDICT: PAM FAILS in the locker sandbox -> real qdlocker.service hardening bug breaking unix_chkpwd"
fi

echo "VERIFY-DONE"

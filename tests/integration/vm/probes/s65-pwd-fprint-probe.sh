#!/bin/bash
# §spec/13 — fprintd wrapper module + Pwd1.UnlockVaultFprint probe.
#
# Pins surfaces from task(102) + task(104) without requiring an actual
# fingerprint reader on the bake VM:
#   - qdistro_pwd_fprint module importable from /usr/libexec/qdistro/.
#   - polkit rule 50-qdistro-pwd-fprint.rules installed at the
#     standard location.
#   - daemon's Pwd1 interface advertises UnlockVaultFprint.
#
# Skips cleanly when fprintd isn't even installed on the host —
# Pwd1.UnlockVaultFprint will raise PolicyError("fprintd unreachable")
# at call time, which is the documented failure mode.
set -uo pipefail

PWD_LIB=/usr/libexec/qdistro/qdistro_pwd_fprint.py
RULE=/usr/share/polkit-1/rules.d/50-qdistro-pwd-fprint.rules

if [ ! -f "$PWD_LIB" ]; then
    echo "SKIP: qdistro_pwd_fprint.py not installed (rerun bootstrap after task 102)"
    exit 0
fi
echo "PASS: qdistro_pwd_fprint module installed"

if [ ! -f "$RULE" ]; then
    echo "SKIP: 50-qdistro-pwd-fprint.rules not installed"
    exit 0
fi
grep -q 'qdistro-pwd' "$RULE" || {
    echo "FAIL: rule does not reference qdistro-pwd uid"
    exit 1
}
grep -q 'net.reactivated.fprint.device.verify' "$RULE" || {
    echo "FAIL: rule does not allow fprint verify"
    exit 1
}
echo "PASS: polkit rule shape"

# Module importable and exposes the documented surface.
python3 -c "
import sys
sys.path.insert(0, '/usr/libexec/qdistro')
from qdistro_pwd_fprint import (
    admin_username, is_fprintd_available, verify, DEFAULT_TIMEOUT_S,
)
assert callable(verify)
assert callable(is_fprintd_available)
# admin_username falls back to admin when the uid lookup fails.
assert admin_username(0) == 'root'
assert admin_username(999_999) == 'admin'
print('module-OK')
" 2>&1 | tee /tmp/qdistro-pwd-fprint-probe.out
if ! grep -q "module-OK" /tmp/qdistro-pwd-fprint-probe.out; then
    echo "FAIL: qdistro_pwd_fprint module probe"
    exit 2
fi
echo "PASS: qdistro_pwd_fprint module shape"

# Pwd1.UnlockVaultFprint method registration. Introspect the system-
# bus interface — works without making the call (no fprintd needed).
if ! systemctl is-active --quiet qdistro-pwd.service; then
    systemctl start qdistro-pwd.service 2>/dev/null || true
    sleep 1
fi
if ! systemctl is-active --quiet qdistro-pwd.service; then
    echo "SKIP: qdistro-pwd.service not running"
    exit 0
fi
# Daemon registers as `org.qdistro.Pwd1` on the system bus (see
# qdistro/pwd/qdistro_pwd_daemon.py BUS_NAME). Earlier copies of this
# probe used the `com.qdistro.*` prefix from the spec draft, which
# never matched the live name and made every run print "method not
# advertised" even when registration was correct.
out=$(busctl --system introspect org.qdistro.Pwd1 /org/qdistro/Pwd1 2>/dev/null \
      || dbus-send --system --print-reply \
            --dest=org.qdistro.Pwd1 /org/qdistro/Pwd1 \
            org.freedesktop.DBus.Introspectable.Introspect 2>/dev/null)
if ! echo "$out" | grep -q 'UnlockVaultFprint'; then
    echo "FAIL: Pwd1.UnlockVaultFprint not advertised in introspection"
    echo "----- introspection output -----"
    echo "$out" | head -50
    exit 3
fi
echo "PASS: Pwd1.UnlockVaultFprint advertised"

echo "PASS: §spec/13 fprint wrapper + UnlockVaultFprint probe"

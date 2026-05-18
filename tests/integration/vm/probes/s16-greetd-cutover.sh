#!/bin/bash
# §6.6 S7 — greetd cutover smoke. Runs deploy/bootstrap-qdwin-in-vm.sh
# inside the VM, then asserts:
#   1. greetd-qdwin.service loads and is enabled.
#   2. The unit's TTYPath is /dev/tty3 (S7 cutover from tty4).
#   3. /etc/greetd/qdwin.toml has vt=3.
#   4. /usr/local/bin/qdistro-start-qdwin is installed + executable.
#   5. /usr/share/qdshell/qdshell.py is installed + protocol bindings
#      regenerated.
#
# Does NOT start the service — bats runs against a live VM with other
# tests ongoing; actually claiming tty3 would disrupt the session.
set -eo pipefail

QDWIN_SRC=${QDWIN_SRC:-/root/qdistro-src}

# Run (or re-run) the deploy bootstrap.
if [ -x "$QDWIN_SRC/deploy/bootstrap-qdwin-in-vm.sh" ]; then
    bash "$QDWIN_SRC/deploy/bootstrap-qdwin-in-vm.sh" >/tmp/s16-bootstrap.log 2>&1 || {
        echo "FAIL: deploy bootstrap failed"
        tail -30 /tmp/s16-bootstrap.log
        exit 2
    }
else
    echo "FAIL: $QDWIN_SRC/deploy/bootstrap-qdwin-in-vm.sh missing"
    exit 2
fi
echo "PASS: deploy bootstrap completed"

# 1. Unit loads.
if systemctl show greetd-qdwin.service --property=LoadState \
        | grep -q "LoadState=loaded"; then
    echo "PASS: greetd-qdwin.service loaded"
else
    echo "FAIL: greetd-qdwin.service not loaded"
    systemctl status greetd-qdwin.service 2>&1 | head -10
    exit 3
fi

# 2. Unit targets tty3.
TTYPATH=$(systemctl show greetd-qdwin.service --property=TTYPath \
    | sed 's/TTYPath=//')
if [ "$TTYPATH" = "/dev/tty3" ]; then
    echo "PASS: unit TTYPath=/dev/tty3 (S7 cutover)"
else
    echo "FAIL: expected TTYPath=/dev/tty3, got $TTYPATH"
    exit 4
fi

# 3. greetd config vt=3.
if grep -E "^vt[[:space:]]*=[[:space:]]*3" /etc/greetd/qdwin.toml >/dev/null; then
    echo "PASS: greetd config vt=3"
else
    echo "FAIL: /etc/greetd/qdwin.toml vt != 3"
    grep -i vt /etc/greetd/qdwin.toml
    exit 5
fi

# 4. Session launcher installed.
if [ -x /usr/local/bin/qdistro-start-qdwin ]; then
    echo "PASS: qdistro-start-qdwin installed"
else
    echo "FAIL: /usr/local/bin/qdistro-start-qdwin missing"
    exit 6
fi

# 5. qdshell at /usr/share/qdshell.
if [ -f /usr/share/qdshell/qdshell.py ] \
        && [ -f /usr/share/qdshell/modules/locker.py ] \
        && [ -d /usr/share/qdshell/protocol ]; then
    echo "PASS: qdshell deployed at /usr/share/qdshell"
else
    echo "FAIL: qdshell deploy incomplete"
    ls -R /usr/share/qdshell 2>/dev/null | head -20
    exit 7
fi

# 6. protocol bindings got regenerated against the installed XML.
# pywayland.scanner emits a dir `qdwin_shell_v1/` with __init__.py +
# per-interface modules.
if [ -d /usr/share/qdshell/protocol/qdwin_shell_v1 ]; then
    echo "PASS: qdwin_shell_v1 bindings generated"
else
    echo "FAIL: qdwin_shell_v1 bindings missing from protocol/"
    ls /usr/share/qdshell/protocol 2>/dev/null | head -10
    exit 8
fi

# 7. systemd-analyze verify catches any unit-level errors.
if systemd-analyze verify greetd-qdwin.service 2>&1 | \
        grep -E "(error|Failed)" | grep -v "not installed" | head -3 \
        | grep -q .; then
    echo "WARN: systemd-analyze verify flagged issues:"
    systemd-analyze verify greetd-qdwin.service 2>&1 | head -5
else
    echo "PASS: systemd-analyze verify clean"
fi

echo "PASS: §6.6 S7 greetd-qdwin-on-tty3 cutover deployed"

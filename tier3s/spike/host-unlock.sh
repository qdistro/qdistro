#!/bin/bash
# tier3s/spike/host-unlock.sh — HOST side. Unlock the dev VM's qdlocker by
# typing the standard test password (qdlocker/tests/gui/AGENTS.md) through
# QMP key events (the locker only takes keys via qdwin_locker_v1 overlay_key,
# so guest-side ydotool typing does not reach it). Usage: host-unlock.sh <vm>
set -eo pipefail   # not -u: qdwin-helpers.sh reads optional unset vars
export VMNAME=$1
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../../qdwin/tests/gui/qdwin-helpers.sh"
k() { qdwin_qmp_key "$1" down; sleep 0.04; qdwin_qmp_key "$1" up; sleep 0.04; }
sk() { qdwin_qmp_key shift down; sleep 0.04; k "$1"; qdwin_qmp_key shift up; sleep 0.04; }
since=$(date -u '+%Y-%m-%d %H:%M:%S')
k shift; sleep 1        # wake the output first; a key during DPMS wake can be lost
for _ in $(seq 1 20); do k backspace; done
sk p; k a; sk minus; k s; k s; k w; k 0; k r; k d; k 4; k 5; k ret
qdwin_release_modifiers
sleep 3
# Verify from qdwin's own journal line, not from a screenshot.
if timeout 60 "$here/../../scripts/vm/vm-exec" "$VMNAME" \
     "journalctl _UID=1000 --since '$since UTC' --no-pager -o cat | grep -c 'locked_changed=0'" 2>/dev/null \
     | grep -qx '[1-9][0-9]*'; then
    echo "host-unlock: unlocked (qdwin locked_changed=0 since $since UTC)"
else
    echo "host-unlock: NO unlock event since $since UTC (already unlocked, or the password did not land)"
fi

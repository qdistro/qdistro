#!/bin/bash
# tier3s/spike/host-unlock.sh — HOST side. Unlock the dev VM's qdlocker by
# typing the standard test password (qdlocker/tests/gui/AGENTS.md) through
# QMP key events (the locker only takes keys via qdwin_locker_v1 overlay_key,
# so guest-side ydotool typing does not reach it). Usage: host-unlock.sh <vm>
set -euo pipefail
export VMNAME=$1
here=$(cd "$(dirname "$0")" && pwd)
. "$here/../../qdwin/tests/gui/qdwin-helpers.sh"
k() { qdwin_qmp_key "$1" down; sleep 0.04; qdwin_qmp_key "$1" up; sleep 0.04; }
sk() { qdwin_qmp_key shift down; sleep 0.04; k "$1"; qdwin_qmp_key shift up; sleep 0.04; }
for _ in $(seq 1 20); do k backspace; done
sk p; k a; sk minus; k s; k s; k w; k 0; k r; k d; k 4; k 5; k ret
qdwin_release_modifiers

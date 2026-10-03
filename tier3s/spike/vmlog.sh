#!/bin/bash
# tier3s/spike/vmlog.sh — HOST side. Run one command inside the VM through
# scripts/vm/vm-exec and keep the transcript as evidence:
#   vmlog.sh <logfile> <vm> '<command>'
# The log starts with the exact command, then vm-exec's capture (its stderr
# rides inside the capture), then `### exit=<rc>`. Appends if the log exists.
# Exits with the VM command's status (sol r1: callers must see failures).
set -u
log=$1 vm=$2 cmd=$3
here=$(cd "$(dirname "$0")" && pwd)
vmexec=$here/../../scripts/vm/vm-exec
mkdir -p "$(dirname "$log")"
{
    printf '### host %s: vm-exec %s <<CMD\n%s\nCMD\n' "$(date -u +%FT%TZ)" "$vm" "$cmd"
    timeout "${VMLOG_TIMEOUT:-900}" "$vmexec" "$vm" "$cmd" 2>&1
    rc=$?
    printf '### exit=%s\n' "$rc"
} >> "$log"
tail -n "${VMLOG_TAIL:-40}" "$log"
exit "$rc"

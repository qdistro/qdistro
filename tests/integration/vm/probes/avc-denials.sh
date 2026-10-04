#!/bin/bash
# avc-denials.sh <type-regex> — list AVC denials since boot whose record
# matches <type-regex>, failing closed when the audit trail cannot be read.
#
#   exit 0  query worked, no matching denial   (prints "AVC-CLEAN")
#   exit 1  query worked, matching denials      (prints them)
#   exit 2  usage
#   exit 3  audit query unusable: ausearch missing, auditd not running,
#           or ausearch failed for a reason other than "no matches"
#
# ausearch exits 1 with "<no matches>" when nothing matched; any other
# nonzero exit is an error, never a clean result.
set -u
pattern=${1:-}
[ -n "$pattern" ] || { echo "usage: $0 <type-regex>" >&2; exit 2; }
command -v ausearch >/dev/null 2>&1 || { echo "AVC-UNUSABLE: ausearch not installed"; exit 3; }
if command -v systemctl >/dev/null 2>&1 && ! systemctl is-active --quiet auditd.service; then
    echo "AVC-UNUSABLE: auditd.service not active"
    exit 3
fi
out=$(ausearch -m AVC,USER_AVC -ts boot 2>&1)
rc=$?
if [ "$rc" -ne 0 ]; then
    if [ "$rc" -eq 1 ] && printf '%s\n' "$out" | grep -qx '<no matches>'; then
        echo "AVC-CLEAN"
        exit 0
    fi
    echo "AVC-UNUSABLE: ausearch rc=$rc: $(printf '%s' "$out" | head -n 3 | tr '\n' ' ')"
    exit 3
fi
hits=$(printf '%s\n' "$out" | grep -E 'denied' | grep -E -- "$pattern" || true)
if [ -n "$hits" ]; then
    printf '%s\n' "$hits"
    exit 1
fi
echo "AVC-CLEAN"
exit 0

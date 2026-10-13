#!/bin/bash
# qdistro-tier5-vmapps-reconcile — the service run for the dynamic
# tier-5 VM-app catalogue (vmapps-dynamic plan, todo/vmapps-dynamic/01-plan.md).
#
# One invocation is a self-checking reconcile loop over the tier-5 guest
# base and its published manifest at /usr/share/qdistro/vmapps/base.json:
#
#   1. Sample base identity I0 (dev/ino/mtime/size).
#   2. Base absent                -> remove the manifest; exit 0.
#   3. Base present but admin
#      cannot read it            -> publish a stale (empty) manifest so
#                                  the launcher drops to Foot-only;
#                                  exit 0. Never publish a catalogue
#                                  for a base the spawner cannot read.
#   4. FAST PATH: manifest exists, parses at schema 1, is non-stale, has
#      usable rows, its recorded identity still matches I0, and admin
#      can read the base          -> exit 0 (no rescan).
#   5. Otherwise run the scanner; re-sample I1. Base replaced mid-scan
#      or scan failure           -> publish a stale marker and retry,
#                                  bounded attempts + backoff.
#   6. Exit 0 even on exhaustion — the launcher sees the stale manifest
#      and shows Foot only. The failing side is the manifest, never a
#      systemd failure storm.
#
# Started by qdistro-tier5-vmapps-scan.service (oneshot, WantedBy
# multi-user.target) and re-triggered by the .path unit on base changes
# and the .timer unit as drift safety.
#
# Env overrides (tests):
#   VMAPPS_BASE        base path (default /var/lib/libvirt/images/qdistro-tier5-base.qcow2)
#   VMAPPS_MANIFEST    manifest path (default /usr/share/qdistro/vmapps/base.json)
#   VMAPPS_ADMIN_USER  readability-check user (default admin)
#   VMAPPS_SCANNER     scanner command (default vmapps-scan.py next to this script)
#   VMAPPS_MAX_ATTEMPTS  scan attempts before giving up (default 3)
#   VMAPPS_BACKOFF_SECS  sleep between attempts (default 5)
set -euo pipefail

BASE="${VMAPPS_BASE:-/var/lib/libvirt/images/qdistro-tier5-base.qcow2}"
MANIFEST="${VMAPPS_MANIFEST:-/usr/share/qdistro/vmapps/base.json}"
ADMIN_USER="${VMAPPS_ADMIN_USER:-admin}"
SCANNER="${VMAPPS_SCANNER:-$(cd "$(dirname "$0")" && pwd)/vmapps-scan.py}"
MAX_ATTEMPTS="${VMAPPS_MAX_ATTEMPTS:-3}"
BACKOFF="${VMAPPS_BACKOFF_SECS:-5}"

say() { echo "[vmapps-reconcile] $*"; }

# Single-line identity tuple; must agree with the fields the scanner
# records (baseDev baseIno baseMtime baseMtimeNs baseSize — compared
# inside `vmapps-scan.py --check`). %y carries nanoseconds — a
# same-second same-size rewrite must still read as a replacement.
identity() { stat -c '%d %i %y %s' "$1" 2>/dev/null || true; }

admin_can_read() {
    runuser -u "$ADMIN_USER" -- test -r "$BASE" 2>/dev/null
}

publish_stale() {
    "$SCANNER" --emit-stale "$MANIFEST" --reason "$1" \
        || say "WARN: stale publish failed: $1"
}

# 1. identity sample
I0="$(identity "$BASE")"

# 2. base absent -> no catalogue exists; remove the manifest and exit.
if [ -z "$I0" ]; then
    if [ -e "$MANIFEST" ]; then
        rm -f "$MANIFEST"
        say "base $BASE absent — removed manifest"
    else
        say "base $BASE absent — nothing to do"
    fi
    exit 0
fi

# 3. unreadable by the spawner -> refuse to publish a catalogue for it.
if ! admin_can_read; then
    publish_stale "base exists but is not readable by $ADMIN_USER"
    say "base not readable by $ADMIN_USER — stale manifest published"
    exit 0
fi

# 4. fast path: current valid manifest matching this base identity.
if [ -f "$MANIFEST" ] \
   && "$SCANNER" --check "$MANIFEST" --base "$BASE" >/dev/null 2>&1; then
    say "manifest fresh — fast path"
    exit 0
fi

# 5. scan loop: bounded attempts; replacement-during-scan and scan
#    failures publish a stale marker and retry.
attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
    say "scan attempt $attempt/$MAX_ATTEMPTS"
    rc=0
    "$SCANNER" --base "$BASE" --out "$MANIFEST" || rc=$?

    I1="$(identity "$BASE")"
    if [ -z "$I1" ]; then
        rm -f "$MANIFEST"
        say "base removed mid-scan — removed manifest"
        exit 0
    fi
    if [ "$I1" != "$I0" ]; then
        say "base replaced during scan — stale marker published, retrying"
        publish_stale "base replaced during scan"
        I0="$I1"
    elif [ "$rc" -ne 0 ]; then
        say "scan failed rc=$rc — stale marker published, retrying"
        publish_stale "scan failed rc=$rc"
    else
        say "manifest published"
        exit 0
    fi

    attempt=$((attempt + 1))
    [ "$attempt" -le "$MAX_ATTEMPTS" ] && sleep "$BACKOFF"
done

# 6. exhausted — the stale marker above is already published; launcher
#    shows Foot only. Exit 0.
say "reconcile exhausted after $MAX_ATTEMPTS attempts — stale manifest published"
exit 0

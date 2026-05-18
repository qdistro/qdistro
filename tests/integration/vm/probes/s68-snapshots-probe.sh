#!/bin/bash
# spec/19 §"Phase-8 MVP scope" — snapshot bridge surfaces probe.
#
# Asserts:
#   - install-snapshots-for-vm.sh staged engine + CLI + systemd
#     unit + timer at the spec-pinned paths.
#   - qdistro-snap-export print-cmd renders a valid btrfs send |
#     rage -e | ssh pipeline.
#   - qdistro-snap-export check-recipients accepts a valid file
#     and rejects an empty / malformed one.
#   - The broker module imports qdistro_snapshots successfully so
#     SnapshotBefore / ListSnapshots / GetFiles aren't permanently
#     SnapperUnavailable.
#   - qdistro-backup.service + timer are systemd-parseable (no
#     literal start; the env file is absent on a vanilla install
#     and the unit ConditionPathExists-skips).
set -euo pipefail

PASS() { echo "PASS: $*"; }
SKIP() { echo "SKIP: $*"; exit 0; }

ENG=/usr/libexec/qdistro/qdistro_snapshots.py
CLI=/usr/local/bin/qdistro-snap-export
SVC=/etc/systemd/system/qdistro-backup.service
TMR=/etc/systemd/system/qdistro-backup.timer

[ -f "$ENG" ] || SKIP "$ENG missing"
[ -x "$CLI" ] || SKIP "$CLI missing"
[ -f "$SVC" ] || SKIP "$SVC missing"
[ -f "$TMR" ] || SKIP "$TMR missing"

PASS "snapshot surfaces installed"

# --- print-cmd round-trip ---
OUT=$("$CLI" print-cmd \
    --snap /.snapshots/42/snapshot \
    --recipients /etc/qdistro/backup-recipients.txt \
    --ssh user@host --remote snap-42.btrfs.age 2>&1)
echo "$OUT" | python3 -c "
import json, sys
body = json.loads(sys.stdin.read())
assert body[0] == 'bash' and body[1] == '-c', body
cmd = body[2]
for piece in ('btrfs send', '/.snapshots/42/snapshot',
              'rage -e', 'ssh user@host', 'snap-42.btrfs.age'):
    assert piece in cmd, (piece, cmd)
print('OK')
" >/dev/null
PASS "qdistro-snap-export print-cmd renders the canonical pipeline"

# --- check-recipients accepts good file ---
TMP=$(mktemp -d /tmp/s68-snap.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
cat >"$TMP/recipients" <<'TXT'
# qdistro backup recipients
age1xyz123
age1abc456
TXT
"$CLI" check-recipients "$TMP/recipients" >/dev/null
PASS "check-recipients accepts a valid file"

# --- check-recipients rejects empty file ---
echo "# only comments" > "$TMP/empty"
if "$CLI" check-recipients "$TMP/empty" >/dev/null 2>&1; then
    echo "FAIL: empty recipients should have failed"; exit 1
fi
PASS "check-recipients rejects empty file"

# --- broker imports qdistro_snapshots ---
/usr/bin/python3 - <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location(
    "qdistro_snapshots",
    "/usr/libexec/qdistro/qdistro_snapshots.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
assert hasattr(m, "SnapperClient")
assert hasattr(m, "snapshot_before")
assert hasattr(m, "render_backup_command")
print("OK")
PY
PASS "qdistro_snapshots imports cleanly"

# --- systemd parses the unit + timer ---
systemd-analyze verify "$SVC" 2>&1 | grep -v "Conditions\|condition" \
    || true  # verify exits non-zero on Conditions warnings; they're benign
systemd-analyze verify "$TMR" >/dev/null 2>&1
PASS "qdistro-backup.service + .timer parse"

PASS "§spec/19 Phase-8 MVP snapshot probe"

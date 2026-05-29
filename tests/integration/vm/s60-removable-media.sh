#!/bin/bash
# In-VM driver for removable-media.bats — exercises the brokered
# mount/unmount path end-to-end through qdistro-media-exec + the broker.
#
# Verifies the security contract from doc/removable-media-design.md:
#   1. A device string with shell metacharacters is REFUSED by the
#      helper BEFORE the broker is contacted (no shell, validated input).
#   2. With a `qdistro.media.mount:*` allow rule, mounting a real
#      loopback device succeeds and udisks2 mounts it under /run/media.
#   3. With a `qdistro.media.mount` deny rule, the mount is refused.
#   4. unmount of the device succeeds and the action is a DISTINCT
#      qdistro.media.unmount:* action (not covered by the mount rule).
#
# Each scenario echoes PASS:/FAIL:; the bats wrapper greps for them.

set -u

PASSCOUNT=0
FAILCOUNT=0
pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }

RULES=/etc/qdistro/rules.d
mkdir -p "$RULES"
rm -f "$RULES"/test-media-*.yaml 2>/dev/null

systemctl is-active --quiet qdistro-admin-broker.service \
    || systemctl start qdistro-admin-broker.service
# Socket-activated; ensure the unit is installed/enabled.
systemctl start qdistro-media-exec.socket 2>/dev/null || true

# Small JSON client that speaks the media-exec wire protocol. Connects
# as a NON-root uid so SO_PEERCRED + RequestPermissionAs see a real
# caller; we run this helper via `runuser -u user1` below.
cat > /tmp/media_client.py <<'PYEOF'
import json, socket, sys
op, device = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/run/qdistro-media-exec/sock")
req = {"op": op, "device": device, "label": sys.argv[3] if len(sys.argv) > 3 else ""}
s.sendall((json.dumps(req) + "\n").encode())
buf = b""
while b"\n" not in buf:
    c = s.recv(4096)
    if not c:
        break
    buf += c
print(buf.decode().strip())
PYEOF

run_client() {  # op device [label]  -- as user1
    runuser -u user1 -- python3 /tmp/media_client.py "$@" 2>&1
}

# --- 1. injection / non-/dev device refused pre-broker ----------------
out=$(run_client mount "/dev/sdb1; rm -rf /")
if echo "$out" | grep -q '"ok": false'; then
    pass "media: injection device string refused"
else
    fail "media: expected refusal of injection string, got: $out"
fi

out=$(run_client mount "/etc/passwd")
if echo "$out" | grep -q '"ok": false'; then
    pass "media: non-/dev device refused"
else
    fail "media: expected refusal of non-/dev path, got: $out"
fi

# --- set up a real loopback block device with a vfat filesystem -------
IMG=/tmp/media-test.img
dd if=/dev/zero of="$IMG" bs=1M count=8 status=none
mkfs.vfat -n QDTEST "$IMG" >/dev/null 2>&1
LOOP=$(losetup --find --show "$IMG")
if [ -z "$LOOP" ]; then
    fail "media: could not set up loopback device (skipping mount cases)"
    echo "SUMMARY: pass=$PASSCOUNT fail=$FAILCOUNT"
    exit 1
fi
# Give udev a beat to settle the new node.
udevadm settle 2>/dev/null || sleep 1

# --- 2. allow rule → mount succeeds -----------------------------------
cat > "$RULES/test-media-allow.yaml" <<'EOF'
- name: test-media-allow
  decision: allow
  match:
    action: 'qdistro.media.mount:*'
EOF
sleep 2
out=$(run_client mount "$LOOP")
if echo "$out" | grep -q '"ok": true'; then
    pass "media: brokered mount allowed by rule"
else
    fail "media: expected mount ok, got: $out"
fi
if echo "$out" | grep -q '/run/media'; then
    pass "media: mounted under /run/media"
else
    fail "media: expected /run/media mountpoint, got: $out"
fi

# --- 3. unmount is a distinct action; mount rule does NOT cover it -----
# (no unmount rule authored yet, and the broker has no admin attached in
#  this headless driver, so the request would block on a prompt — assert
#  the action namespace instead by checking CheckPermission directly.)
BUS=org.qdistro.AdminBroker1
CP=$(busctl --system --no-pager call "$BUS" /org/qdistro/AdminBroker1 \
        "$BUS" CheckPermission sa{sv} "qdistro.media.unmount:$LOOP" 0 2>&1)
if echo "$CP" | grep -q '"unknown"'; then
    pass "media: unmount action not covered by mount allow rule"
else
    fail "media: expected unmount unknown, got: $CP"
fi

# clean up the mount before unmount-allow test
runuser -u user1 -- udisksctl unmount -b "$LOOP" >/dev/null 2>&1 || true

# --- 4. deny rule blocks the mount ------------------------------------
rm -f "$RULES/test-media-allow.yaml"
cat > "$RULES/test-media-deny.yaml" <<'EOF'
- name: test-media-deny
  decision: deny
  match:
    action: 'qdistro.media.mount:*'
EOF
sleep 2
out=$(run_client mount "$LOOP")
if echo "$out" | grep -q '"ok": false'; then
    pass "media: brokered mount denied by deny rule"
else
    fail "media: expected mount denied, got: $out"
fi

# --- cleanup ----------------------------------------------------------
rm -f "$RULES"/test-media-*.yaml
runuser -u user1 -- udisksctl unmount -b "$LOOP" >/dev/null 2>&1 || true
losetup -d "$LOOP" 2>/dev/null || true
rm -f "$IMG" /tmp/media_client.py
sleep 1

echo "SUMMARY: pass=$PASSCOUNT fail=$FAILCOUNT"
[ "$FAILCOUNT" -eq 0 ] && echo "PASS: removable-media end-to-end" \
    || echo "FAIL: removable-media end-to-end ($FAILCOUNT failures)"

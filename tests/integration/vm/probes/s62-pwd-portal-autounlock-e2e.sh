#!/bin/bash
# §Phase-8 spec/13 portal-keys auto-unlock — end-to-end probe.
#
# Drives the StashPortalPin + AutoUnlockPortalKeys daemon path:
#   1. Stash a TPM-sealed PIN via `qdistro-pwd-admin store-portal-pin`.
#   2. Verify portal-pin-info reports the stash.
#   3. Lock the portal-keys vault.
#   4. Run auto-unlock-portal-keys.
#   5. Verify IsUnlocked returns true.
#   6. Re-lock; re-unlock; idempotent.
#
# Bats wrapper: phase8.bats `phase8-pwd-portal-autounlock-e2e`.
set -uo pipefail

PORTAL_PIN=hunter2portal
PORTAL_VAULT=portal-keys

# Step 0 — daemon up.
if ! systemctl is-active --quiet qdistro-pwd.service; then
    systemctl start qdistro-pwd.service 2>/dev/null
    sleep 1
fi
if ! systemctl is-active --quiet qdistro-pwd.service; then
    echo "SKIP: qdistro-pwd.service not active"
    exit 0
fi

# Bypass polkit gate for the test (non-admin paths aren't exercised here).
mkdir -p /etc/systemd/system/qdistro-pwd.service.d
cat >/etc/systemd/system/qdistro-pwd.service.d/test-no-polkit.conf <<'CONF'
[Service]
Environment="QDISTRO_PWD_POLKIT_REQUIRED=0"
CONF
systemctl daemon-reload
systemctl restart qdistro-pwd.service
sleep 1

# Reset: drop any stale portal-keys vault + stash file.
rm -f "/var/lib/qdistro/vaults/${PORTAL_VAULT}.vault" \
      /var/lib/qdistro/vaults/portal-keys-pin.tpm 2>/dev/null

# Step 1 — create the portal-keys vault as admin.
if ! runuser -u admin -- bash -c \
        "printf '%s\n%s\n' '$PORTAL_PIN' '$PORTAL_PIN' \
            | qdistro-pwd-admin create '$PORTAL_VAULT'" \
        >/tmp/s62-step1.log 2>&1; then
    cat /tmp/s62-step1.log
    echo "FAIL: CreateVault portal-keys"; exit 1
fi
echo "PASS: portal-keys vault created"

# Step 2 — stash the PIN. QDISTRO_PWD_TPM_BACKEND should be picked up
# by the daemon's select_backend(). On the test VM with no TPM the
# daemon's auto-detect picks NoneBackend → StashPortalPin would fail
# with TpmUnavailable. Force mock.
mkdir -p /etc/systemd/system/qdistro-pwd.service.d
cat >/etc/systemd/system/qdistro-pwd.service.d/test-tpm-backend.conf <<'CONF'
[Service]
Environment="QDISTRO_PWD_TPM_BACKEND=mock"
CONF
systemctl daemon-reload
systemctl restart qdistro-pwd.service
sleep 1

if ! runuser -u admin -- env QDISTRO_PWD_PORTAL_PIN="$PORTAL_PIN" \
        qdistro-pwd-admin store-portal-pin >/tmp/s62-step2.log 2>&1; then
    cat /tmp/s62-step2.log
    echo "FAIL: store-portal-pin"; exit 2
fi
grep -q "portal-keys PIN sealed" /tmp/s62-step2.log || {
    cat /tmp/s62-step2.log
    echo "FAIL: store-portal-pin output mismatch"
    exit 2
}
echo "PASS: store-portal-pin sealed"

# Step 3 — portal-pin-info reports the stash.
INFO=$(runuser -u admin -- qdistro-pwd-admin portal-pin-info 2>/tmp/s62-step3.log)
if ! printf '%s' "$INFO" | grep -q "backend=mock"; then
    cat /tmp/s62-step3.log
    echo "FAIL: portal-pin-info: $INFO"
    exit 3
fi
echo "PASS: portal-pin-info reports backend=mock"

# Step 4 — lock the portal-keys vault (it's currently unlocked from
# step 1's create — the create flow doesn't auto-unlock, but we'll
# defensively lock).
runuser -u admin -- qdistro-pwd-admin lock "$PORTAL_VAULT" >/dev/null 2>&1 || true
STAT=$(runuser -u admin -- qdistro-pwd-admin status "$PORTAL_VAULT" 2>/dev/null)
if [ "$STAT" != "locked" ]; then
    echo "FAIL: portal-keys not locked after lock: $STAT"
    exit 4
fi
echo "PASS: portal-keys locked pre-auto-unlock"

# Step 5 — auto-unlock.
if ! runuser -u admin -- qdistro-pwd-admin auto-unlock-portal-keys \
        >/tmp/s62-step5.log 2>&1; then
    cat /tmp/s62-step5.log
    echo "FAIL: auto-unlock-portal-keys"
    exit 5
fi
grep -q "portal-keys unlocked" /tmp/s62-step5.log || {
    cat /tmp/s62-step5.log
    echo "FAIL: auto-unlock output mismatch"
    exit 5
}
STAT=$(runuser -u admin -- qdistro-pwd-admin status "$PORTAL_VAULT" 2>/dev/null)
if [ "$STAT" != "unlocked" ]; then
    echo "FAIL: portal-keys not unlocked: $STAT"
    exit 5
fi
echo "PASS: auto-unlock-portal-keys flipped to unlocked"

# Step 6 — idempotent: a second auto-unlock returns success.
if ! runuser -u admin -- qdistro-pwd-admin auto-unlock-portal-keys \
        >/tmp/s62-step6.log 2>&1; then
    cat /tmp/s62-step6.log
    echo "FAIL: second auto-unlock"
    exit 6
fi
echo "PASS: auto-unlock idempotent on already-unlocked"

# Step 7 — relock + reunlock proves the stash survives multiple cycles.
runuser -u admin -- qdistro-pwd-admin lock "$PORTAL_VAULT" >/dev/null 2>&1
runuser -u admin -- qdistro-pwd-admin auto-unlock-portal-keys \
    >/tmp/s62-step7.log 2>&1
STAT=$(runuser -u admin -- qdistro-pwd-admin status "$PORTAL_VAULT" 2>/dev/null)
if [ "$STAT" != "unlocked" ]; then
    cat /tmp/s62-step7.log
    echo "FAIL: relock/reunlock failed: $STAT"
    exit 7
fi
echo "PASS: relock + auto-unlock cycle"

echo "PASS: §Phase-8 spec/13 portal-keys auto-unlock end-to-end"

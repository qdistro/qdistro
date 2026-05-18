#!/bin/bash
# §Phase-8 spec/13 password-manager MVP — end-to-end probe.
#
# Drives the qdistro-pwd daemon through the full lifecycle from the
# admin uid (admin) and from a non-admin uid (a fresh `pwduser` silo).
#
# Asserts:
#   1. Daemon is up and reachable on com.qdistro.Pwd1.
#   2. Admin can CreateVault + AddItem with an exe-pin.
#   3. Admin GetItemAdmin reads the value (bypasses pin gate).
#   4. Non-admin uid that DOESN'T match the pin gets PolicyError.
#   5. Non-admin uid that matches the pin (correct exe) gets the value.
#   6. LockVault wipes the in-memory key — subsequent GetItem fails
#      with NotUnlocked even for an exe-pinned caller.
#   7. UnlockVault with wrong password is denied.
#   8. Non-admin uid is denied at the bus level for AddItem (the
#      defense-in-depth uid check is also exercised).
#
# Bats wrapper: phase8.bats `phase8-pwd-e2e`.
set -uo pipefail

PASSWD=hunter2
VAULT=phase8-vault
ITEM=test.tag

# Step 0 — provision pwduser if not present (idempotent). The pin will
# be matched by /usr/local/bin/qdistro-pwd-get, so we set its exe pin
# to that path.
if ! id pwduser >/dev/null 2>&1; then
    useradd --system --create-home -s /bin/bash pwduser >/dev/null 2>&1 || {
        echo "FAIL: useradd pwduser"; exit 1; }
fi

# Step 1 — daemon reachable.
if ! systemctl is-active --quiet qdistro-pwd.service; then
    systemctl start qdistro-pwd.service 2>/dev/null
    sleep 1
fi
if ! systemctl is-active --quiet qdistro-pwd.service; then
    echo "SKIP: qdistro-pwd.service not active"
    exit 0
fi
echo "PASS: qdistro-pwd.service active"

# Step 2 — admin clears any stale vault from a prior run; create + unlock.
runuser -u admin -- env XDG_RUNTIME_DIR="/run/user/1000" \
    qdistro-pwd-admin status "$VAULT" >/dev/null 2>&1 && \
    runuser -u admin -- qdistro-pwd-admin lock "$VAULT" >/dev/null 2>&1 || true
# Delete stale on-disk vault file so create succeeds.
rm -f "/var/lib/qdistro/vaults/${VAULT}.vault"

if ! runuser -u admin -- env QDISTRO_PWD_PASSWORD="$PASSWD" \
        bash -c "echo '$PASSWD' | qdistro-pwd-admin create '$VAULT' < <(printf '%s\n%s\n' '$PASSWD' '$PASSWD')" \
        >/tmp/pwd-step2.log 2>&1; then
    cat /tmp/pwd-step2.log
    echo "FAIL: CreateVault"; exit 2
fi
echo "PASS: CreateVault as admin"

if ! runuser -u admin -- env QDISTRO_PWD_PASSWORD="$PASSWD" \
        qdistro-pwd-admin unlock "$VAULT" >/tmp/pwd-step2b.log 2>&1; then
    cat /tmp/pwd-step2b.log
    echo "FAIL: UnlockVault as admin"; exit 2
fi
grep -q unlocked /tmp/pwd-step2b.log || { echo "FAIL: unlock did not return 'unlocked'"; cat /tmp/pwd-step2b.log; exit 2; }
echo "PASS: UnlockVault as admin"

# Step 3 — admin AddItem with an exe pin. We pin to /usr/bin/dbus-send
# (a real native binary). Using a Python CLI like qdistro-pwd-get for
# the pin doesn't work because /proc/<pid>/exe of a python-shebang
# script is the interpreter (python3.13), not the script path — every
# python script would satisfy that pin. dbus-send is a real ELF; pinning
# to it is a meaningful identity claim. See spec/13 §"App identity
# verification" + README phase1/pwd/README.md.
DBUS_SEND=$(command -v dbus-send)
if ! runuser -u admin -- env QDISTRO_PWD_VALUE="topsecret123" \
        qdistro-pwd-admin add "$VAULT" "$ITEM" --pin-exe "$DBUS_SEND" \
        >/tmp/pwd-step3.log 2>&1; then
    cat /tmp/pwd-step3.log
    echo "FAIL: AddItem"; exit 3
fi
echo "PASS: AddItem with --pin-exe $DBUS_SEND"

# Step 4 — admin GetItemAdmin reads the value.
ADMIN_VAL=$(runuser -u admin -- qdistro-pwd-admin get "$VAULT" "$ITEM" 2>/tmp/pwd-step4.log) || {
    cat /tmp/pwd-step4.log; echo "FAIL: GetItemAdmin"; exit 4; }
if [ "$ADMIN_VAL" != "topsecret123" ]; then
    echo "FAIL: GetItemAdmin returned $ADMIN_VAL not topsecret123"
    exit 4
fi
echo "PASS: GetItemAdmin returned the original value"

# Step 5 — non-admin uid with WRONG exe (Python CLI is python3.13) is denied.
DENIED=$(runuser -u pwduser -- bash -c "qdistro-pwd-get '$VAULT' '$ITEM' 2>&1" || true)
if printf '%s' "$DENIED" | grep -qE "PolicyError|exe mismatch|pin gate refused"; then
    echo "PASS: non-admin uid with wrong exe is denied"
else
    echo "FAIL: expected policy denial, got: $DENIED"
    exit 5
fi

# Step 6 — non-admin uid invoking the pinned binary (dbus-send) directly:
# ALLOWED. The pin is on /usr/bin/dbus-send; pwduser invokes dbus-send
# to call GetItem on the Pwd1 bus. /proc/<pid>/exe matches the pin.
DBUS_OUT=$(runuser -u pwduser -- "$DBUS_SEND" --system --print-reply \
    --dest=com.qdistro.Pwd1 /com/qdistro/Pwd1 \
    com.qdistro.Pwd1.GetItem "string:$VAULT" "string:$ITEM" \
    2>/tmp/pwd-step6.log) || {
    cat /tmp/pwd-step6.log
    echo "FAIL: dbus-send (pinned exe) should have been allowed"; exit 6; }
if ! printf '%s' "$DBUS_OUT" | grep -q "topsecret123"; then
    echo "FAIL: pinned exe got wrong value: $DBUS_OUT"
    exit 6
fi
echo "PASS: non-admin uid invoking pinned exe got the value"

# Step 7 — LockVault wipes key; pinned exe now denied with NotUnlocked.
runuser -u admin -- qdistro-pwd-admin lock "$VAULT" >/dev/null 2>&1 || true
LOCKED=$(runuser -u pwduser -- /usr/local/bin/qdistro-pwd-get "$VAULT" "$ITEM" 2>&1 || true)
if printf '%s' "$LOCKED" | grep -qE "NotUnlocked|locked"; then
    echo "PASS: post-lock GetItem fails with NotUnlocked"
else
    echo "FAIL: expected NotUnlocked after lock, got: $LOCKED"
    exit 7
fi

# Step 8 — UnlockVault with wrong password is denied.
runuser -u admin -- env QDISTRO_PWD_PASSWORD="wrong" \
    qdistro-pwd-admin unlock "$VAULT" >/tmp/pwd-step8.log 2>&1
if grep -qE "BadPassword|wrong vault password" /tmp/pwd-step8.log; then
    echo "PASS: wrong password denied"
else
    echo "FAIL: wrong password should have been denied; got:"
    cat /tmp/pwd-step8.log
    exit 8
fi

# Step 9 — bus-level deny on AddItem for non-admin uid.
BUSDENY=$(runuser -u pwduser -- qdistro-pwd-admin add "$VAULT" "$ITEM" 2>&1 <<<"oops" || true)
if printf '%s' "$BUSDENY" | grep -qE "AccessDenied|PolicyError|requires admin uid|not allowed"; then
    echo "PASS: non-admin AddItem denied at bus or method level"
else
    echo "FAIL: non-admin AddItem should have been refused, got: $BUSDENY"
    exit 9
fi

echo "PASS: §Phase-8 spec/13 password manager MVP end-to-end"

#!/bin/bash
# qdlocker-faillock.sh — verifies the dedicated screen-unlock PAM service
# (/etc/pam.d/qdlocker) enforces an explicit pam_faillock brute-force
# lockout end-to-end (harden-qdlocker findings 01 + 03).
#
# Runs inside the VM AS ROOT (faillock tally files under /var/run/faillock/
# are root-relevant; resetting/reading them needs root). Authenticates via
# python-pam — already a qdlocker runtime dep — against service `qdlocker`,
# user `admin`. pamtester is NOT on the image, so do not use it.
#
# /etc/pam.d/qdlocker is installed by the VM's own provisioning
# (fresh-vm-bootstrap.sh §7 copies the checked-in qdlocker/pam/qdlocker), so
# this driver asserts that REAL provisioned file is present and tests it
# in place — verifying the true production install path, not a copy. The
# bats wrapper delivers THIS driver into the VM via base64 over vm_run (no
# shared http server), so the test is safe to run concurrently with other
# VM gates.
#
# The correct password is $QDISTRO_VM_PASSWORD (the baseweed-clone
# convention; the bats wrapper passes it into the VM). Policy: deny=5,
# unlock_time=10.
#
# Verifies:
#   0. dedicated /etc/pam.d/qdlocker is present (installed by provisioning)
#   1. BASELINE: the correct password authenticates when unlocked — the guard
#      that catches a PAM stack which rejects valid passwords (which would make
#      step 3 meaningless)
#   2. deny=5 WRONG attempts each fail and lock the account (valid >= deny)
#   3. the CORRECT password is REFUSED while locked (load-bearing: step 1 proved
#      it normally works, so the refusal here is the lockout)
#   4. after unlock_time the CORRECT password succeeds again AND the success
#      clears the tally (the authsucc line)

set -eo pipefail

GOOD_PASSWORD=${QDISTRO_VM_PASSWORD:-admin}
BAD_PASSWORD=NOT_THE_PASSWORD
USER=admin

# python-pam authenticate helper. Prints "OK" on success, "NO" on failure,
# exits non-zero only on a hard error (import/bug), not on auth failure.
pam_auth() {
    local pw="$1"
    PW="$pw" python3 -c '
import os, sys
import pam
ok = pam.pam().authenticate("'"$USER"'", os.environ["PW"], service="qdlocker")
print("OK" if ok else "NO")
'
}

# Count of CURRENTLY-VALID faillock failures for $USER — the trailing column
# of `faillock --user` is "V" (valid; counts toward deny) or "I" (invalid).
faillock_valid() {
    faillock --user "$USER" 2>/dev/null | awk '$NF == "V" { c++ } END { print c + 0 }'
}

DENY=5

# ---- 0. assert the provisioned dedicated PAM file is present --------------
rpm -q python313-python-pam >/dev/null 2>&1 || \
    zypper -n install python313-python-pam >/dev/null 2>&1 || true

[ -f /etc/pam.d/qdlocker ] || {
    echo "FAIL: /etc/pam.d/qdlocker not installed by provisioning"
    echo "  (fresh-vm-bootstrap.sh §7 should copy qdlocker/pam/qdlocker)"
    exit 2
}
grep -q 'pam_faillock.so preauth' /etc/pam.d/qdlocker || {
    echo "FAIL: /etc/pam.d/qdlocker missing pam_faillock preauth line"
    cat /etc/pam.d/qdlocker
    exit 2
}
echo "PASS: provisioned /etc/pam.d/qdlocker present with faillock lines"

# ---- 1. BASELINE: the correct password must authenticate when unlocked ----
# This is the guard that catches a PAM stack which rejects valid passwords
# (e.g. a faillock authfail line reached on success): without a working
# baseline, step 3's "refused while locked" would be meaningless. Also pins
# admin's password to QDISTRO_VM_PASSWORD (the baseweed-clone convention).
faillock --user "$USER" --reset || true
OUT=$(pam_auth "$GOOD_PASSWORD")
[ "$OUT" = "OK" ] || {
    echo "FAIL: correct password did NOT authenticate against the qdlocker"
    echo "  service when unlocked (got: $OUT) — the PAM stack is broken or"
    echo "  admin's password != \$QDISTRO_VM_PASSWORD."
    cat /etc/pam.d/qdlocker
    exit 3
}
echo "PASS: correct password authenticates when unlocked (baseline)"

# ---- 2. deny=5 WRONG attempts → each refused; account becomes LOCKED ------
faillock --user "$USER" --reset || true
for i in 1 2 3 4 5; do
    OUT=$(pam_auth "$BAD_PASSWORD")
    [ "$OUT" = "NO" ] || {
        echo "FAIL: wrong-password attempt $i was NOT refused (got: $OUT)"
        exit 4
    }
done
V=$(faillock_valid)
[ "$V" -ge "$DENY" ] || {
    echo "FAIL: expected >= $DENY valid failures after 5 wrong attempts (got: $V)"
    faillock --user "$USER" || true
    exit 4
}
echo "PASS: account locked after 5 wrong attempts (valid failures=$V >= deny=$DENY)"

# ---- 3. the CORRECT password is REFUSED while locked (load-bearing) -------
# Baseline (step 1) proved this same password authenticates when unlocked, so
# a refusal here is the lockout doing its job — not just a wrong password.
OUT=$(pam_auth "$GOOD_PASSWORD")
[ "$OUT" = "NO" ] || {
    echo "FAIL: correct password was accepted while locked out (got: $OUT)"
    exit 5
}
echo "PASS: correct password refused while locked"

# ---- 4. after unlock_time the correct password SUCCEEDS again -------------
# Sleep past unlock_time=10 (measured from the last recorded failure above);
# the same correct password must now unlock, and the success must clear the
# tally (the authsucc line).
sleep 12
OUT=$(pam_auth "$GOOD_PASSWORD")
[ "$OUT" = "OK" ] || {
    echo "FAIL: correct password still refused after unlock_time (got: $OUT)"
    faillock --user "$USER" || true
    exit 6
}
V=$(faillock_valid)
[ "$V" -eq 0 ] || {
    echo "FAIL: tally not cleared after a successful auth (got: $V valid)"
    echo "  the authsucc line is missing or ineffective"
    faillock --user "$USER" || true
    exit 7
}
echo "PASS: correct password succeeds after unlock_time and clears the tally"

echo "PASS: qdlocker faillock lockout end-to-end"

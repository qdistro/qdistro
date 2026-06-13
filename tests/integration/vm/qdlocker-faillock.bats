#!/usr/bin/env bats
# qdlocker faillock lockout — VM end-to-end (harden-qdlocker 01 + 03).
#
# Verifies the dedicated screen-unlock PAM service (/etc/pam.d/qdlocker)
# enforces an explicit pam_faillock brute-force lockout: 5 wrong attempts
# lock the account, the CORRECT password is REFUSED while locked, and after
# unlock_time=10 the correct password succeeds again and clears the tally.
#
# /etc/pam.d/qdlocker is installed by the VM's own provisioning
# (fresh-vm-bootstrap.sh §7 copies the checked-in qdlocker/pam/qdlocker), so
# this test exercises the REAL production install path.
#
# The driver is delivered into the VM via base64 over vm_run — NOT the shared
# :8768 http-staging server — so the test is safe to run concurrently with
# other VM gates (which also bind :8768).

load helpers

@test "qdlocker-faillock: dedicated PAM service enforces pam_faillock lockout end-to-end" {
    local driver b64
    driver="$(dirname "$BATS_TEST_FILENAME")/qdlocker-faillock.sh"
    [ -f "$driver" ] || fail_loud "driver script not found at $driver"

    # Ship the driver into the VM without any shared host server: base64 it on
    # the host, decode it in the guest. A ~4 KB script is well under the guest
    # command-line limit. The driver runs as root (faillock tally files are
    # root-relevant) and sleeps 12s past unlock_time=10, so keep the timeout
    # generous.
    b64="$(base64 -w0 "$driver")"
    vm_run "echo '$b64' | base64 -d > /tmp/qdlocker-faillock.sh && chmod +x /tmp/qdlocker-faillock.sh && timeout 120 bash /tmp/qdlocker-faillock.sh 2>&1"
    assert_success

    # Load-bearing assertions: the correct password authenticates when
    # unlocked (baseline) but is REFUSED while locked. A stack that rejected
    # valid passwords, or a faillock that did not gate auth, fails one of these.
    assert_output_contains "PASS: correct password authenticates when unlocked (baseline)"
    assert_output_contains "PASS: correct password refused while locked"
    assert_output_contains "PASS: qdlocker faillock lockout end-to-end"
}

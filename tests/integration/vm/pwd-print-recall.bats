#!/usr/bin/env bats
# §Phase-8 spec/13 password manager — MVP end-to-end.
#
# Bats wrapper around s60-pwd-e2e.sh which drives qdistro-pwd through:
#   - daemon up
#   - admin CreateVault + UnlockVault
#   - admin AddItem with --pin-exe pointing at qdistro-pwd-get
#   - admin GetItemAdmin reads value (bypasses pin gate)
#   - non-admin uid + wrong exe → PolicyError
#   - non-admin uid invoking pinned exe → ALLOWED
#   - LockVault wipes key → subsequent GetItem fails NotUnlocked
#   - wrong password on UnlockVault → BadPassword
#   - non-admin uid invoking AddItem → bus-level or method-level denial
#
# Skips when qdistro-pwd.service isn't installed (legacy bake).

load helpers

setup() {
    vm_run "systemctl is-active --quiet qdistro-pwd.service || \
            systemctl start qdistro-pwd.service 2>/dev/null"
}

@test "phase8-pwd-e2e: spec/13 password-manager MVP end-to-end" {
    vm_run "bash /root/s60-pwd-e2e.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "qdistro-pwd.service absent (rerun fresh-vm-bootstrap.sh after task 088)"
    fi
    assert_output_contains "PASS: qdistro-pwd.service active"
    assert_output_contains "PASS: CreateVault as admin"
    assert_output_contains "PASS: UnlockVault as admin"
    assert_output_contains "PASS: AddItem with --pin-exe"
    assert_output_contains "PASS: GetItemAdmin returned the original value"
    assert_output_contains "PASS: non-admin uid with wrong exe is denied"
    assert_output_contains "PASS: non-admin uid invoking pinned exe got the value"
    assert_output_contains "PASS: post-lock GetItem fails with NotUnlocked"
    assert_output_contains "PASS: wrong password denied"
    assert_output_contains "PASS: non-admin AddItem denied at bus or method level"
    assert_output_contains "PASS: §Phase-8 spec/13 password manager MVP end-to-end"
}

@test "phase8-pwd-tpm-e2e: spec/13 Phase-8.1 TPM-sealed (v2) end-to-end" {
    vm_run "bash /root/s61-pwd-tpm-e2e.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "qdistro-pwd.service absent or TPM backend not selectable"
    fi
    assert_output_contains "PASS: CreateVaultTPM"
    assert_output_contains "PASS: v2 on-disk format (tpm_seal present, no kdf)"
    assert_output_contains "PASS: info reports version=2 backend="
    assert_output_contains "PASS: UnlockVault auto-routes through TPM path"
    assert_output_contains "PASS: AddItem + GetItemAdmin roundtrip on v2 vault"
    assert_output_contains "PASS: wrong PIN denied"
    assert_output_contains "PASS: v1 + v2 vaults coexist with correct version routing"
    assert_output_contains "PASS: §Phase-8.1 spec/13 TPM-sealed vault end-to-end"
}

@test "phase9-print-vm-helpers-probe: spec/20 Phase-9 §step 2 helpers shape probe" {
    if ! vm_run "test -f /root/s63-print-vm-helpers-probe.sh && echo HAVE_SCRIPT"; then
        fail_loud "s63 script absent (rerun fresh-vm-bootstrap.sh after task 099)"
    fi
    vm_run "bash /root/s63-print-vm-helpers-probe.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "print-VM helpers not installed"
    fi
    assert_output_contains "PASS: print-VM helpers installed"
    assert_output_contains "PASS: install-print-vm --remove on absent domain"
    assert_output_contains "PASS: domain-template.xml structure"
    assert_output_contains "PASS: attach/detach --help"
    assert_output_contains "PASS: org.qdistro.print.policy ships all 5 actions"
    assert_output_contains "PASS: §spec/20 Phase-9 §step 2 print-VM helpers probe"
}

@test "phase8-pwd-portal-autounlock-e2e: spec/13 portal-keys auto-unlock end-to-end" {
    if ! vm_run "test -f /root/s62-pwd-portal-autounlock-e2e.sh && echo HAVE_SCRIPT"; then
        fail_loud "s62 script absent (rerun fresh-vm-bootstrap.sh after task 097)"
    fi
    vm_run "bash /root/s62-pwd-portal-autounlock-e2e.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "qdistro-pwd.service absent"
    fi
    assert_output_contains "PASS: portal-keys vault created"
    assert_output_contains "PASS: store-portal-pin sealed"
    assert_output_contains "PASS: portal-pin-info reports backend=mock"
    assert_output_contains "PASS: portal-keys locked pre-auto-unlock"
    assert_output_contains "PASS: auto-unlock-portal-keys flipped to unlocked"
    assert_output_contains "PASS: auto-unlock idempotent on already-unlocked"
    assert_output_contains "PASS: relock + auto-unlock cycle"
    assert_output_contains "PASS: §Phase-8 spec/13 portal-keys auto-unlock end-to-end"
}

@test "phase9-print-allowlist-caps-probe: spec/20 priority #5/#6 — allowlist + caps shape" {
    if ! vm_run "test -f /root/s64-print-allowlist-caps-probe.sh && echo HAVE_SCRIPT"; then
        fail_loud "s64 script absent (rerun fresh-vm-bootstrap.sh after task 105)"
    fi
    vm_run "bash /root/s64-print-allowlist-caps-probe.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "print-allowlist surfaces not installed (legacy bake)"
    fi
    assert_output_contains "PASS: print-allowlist CLI + module installed"
    assert_output_contains "PASS: qdistro_print_browse module shape"
    assert_output_contains "PASS: build-print-image.sh ships caps + page-limit helper + default-deny browsed.conf"
    assert_output_contains "PASS: §spec/20 print-VM allowlist + caps probe"
}

@test "phase8-pwd-fprint-probe: spec/13 — fprintd helper module + Pwd1.UnlockVaultFprint" {
    if ! vm_run "test -f /root/s65-pwd-fprint-probe.sh && echo HAVE_SCRIPT"; then
        fail_loud "s65 script absent (rerun fresh-vm-bootstrap.sh after task 102)"
    fi
    vm_run "bash /root/s65-pwd-fprint-probe.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "fprint surfaces not installed (legacy bake)"
    fi
    assert_output_contains "PASS: qdistro_pwd_fprint module installed"
    assert_output_contains "PASS: polkit rule shape"
    assert_output_contains "PASS: qdistro_pwd_fprint module shape"
    assert_output_contains "PASS: Pwd1.UnlockVaultFprint advertised"
    assert_output_contains "PASS: §spec/13 fprint wrapper + UnlockVaultFprint probe"
}

@test "phase8-browser-bridge-probe: spec/14 — native-messaging host + install tool + manifest shape" {
    if ! vm_run "test -f /root/s66-browser-bridge-probe.sh && echo HAVE_SCRIPT"; then
        fail_loud "s66 script absent (rerun fresh-vm-bootstrap.sh after browser-bridge task)"
    fi
    vm_run "bash /root/s66-browser-bridge-probe.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "browser-bridge surfaces not installed (legacy bake)"
    fi
    assert_output_contains "PASS: browser-bridge surfaces installed"
    assert_output_contains "PASS: Firefox manifest shape via --print"
    assert_output_contains "PASS: Chromium manifest shape via --print"
    assert_output_contains "PASS: qdistro-browser-install writes all six per-browser manifests"
    assert_output_contains "PASS: bridge stdin/stdout round-trip with stubbed parent allowlist"
    assert_output_contains "PASS: bridge denies non-allowlisted parent with clean error"
    assert_output_contains "PASS: §spec/14 Phase-8 MVP browser-bridge in-VM probe"
}

@test "v1-recall-cut-probe: Recall capture/viewer surfaces are disabled" {
    if ! vm_run "test -f /root/s67-recall-probe.sh && echo HAVE_SCRIPT"; then
        fail_loud "s67 script absent (rerun fresh-vm-bootstrap.sh after Recall cut task)"
    fi
    vm_run "bash /root/s67-recall-probe.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "recall cut probe skipped (legacy bake)"
    fi
    assert_output_contains "PASS: Recall timer/service not installed in v1 profile"
    assert_output_contains "PASS: bridge recall.push is not registered"
    assert_output_contains "PASS: SDK push_text_snapshot fails closed when present"
    assert_output_contains "PASS: v1 Recall cut probe"
}

@test "phase8-snapshots-probe: spec/19 — Snapper bridge engine + qdistro-snap-export + qdistro-backup unit" {
    if ! vm_run "test -f /root/s68-snapshots-probe.sh && echo HAVE_SCRIPT"; then
        fail_loud "s68 script absent (rerun fresh-vm-bootstrap.sh after snapshots task)"
    fi
    vm_run "bash /root/s68-snapshots-probe.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "snapshot surfaces not installed (legacy bake)"
    fi
    assert_output_contains "PASS: snapshot surfaces installed"
    assert_output_contains "PASS: qdistro-snap-export print-cmd renders the canonical pipeline"
    assert_output_contains "PASS: check-recipients accepts a valid file"
    assert_output_contains "PASS: check-recipients rejects empty file"
    assert_output_contains "PASS: qdistro_snapshots imports cleanly"
    assert_output_contains "PASS: qdistro-backup.service + .timer parse"
    assert_output_contains "PASS: §spec/19 Phase-8 MVP snapshot probe"
}

@test "phase8-phone-probe: spec/18 — qdistro-phone daemon + CLI + ntfy push body + signed-callback HTTP listener" {
    if ! vm_run "test -f /root/s69-phone-probe.sh && echo HAVE_SCRIPT"; then
        fail_loud "s69 script absent (rerun fresh-vm-bootstrap.sh after phone task)"
    fi
    vm_run "bash /root/s69-phone-probe.sh 2>/dev/null"
    assert_success
    if [[ "$output" == *"SKIP:"* ]]; then
        fail_loud "phone surfaces not installed (legacy bake)"
    fi
    assert_output_contains "PASS: phone surfaces installed"
    assert_output_contains "PASS: qdistro-phone pair/list round-trip"
    assert_output_contains "PASS: qdistro-phone unpair removes the entry"
    assert_output_contains "PASS: qdistro-phone push renders ntfy body"
    assert_output_contains "PASS: daemon refuses to start without QDISTRO_PHONE_NTFY_URL"
    assert_output_contains "PASS: daemon HTTP listener records valid signed decision"
    assert_output_contains "PASS: §spec/18 Phase-8 MVP qdistro-phone probe"
}

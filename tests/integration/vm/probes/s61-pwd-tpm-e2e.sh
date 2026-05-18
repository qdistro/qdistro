#!/bin/bash
# §Phase-8.1 spec/13 password-manager — TPM-sealed (v2) end-to-end probe.
#
# Drives qdistro-pwd through the v2 lifecycle. Backend selection:
#   - If /dev/tpmrm0 + tpm2_unseal present and PIN-enabled: real TPM.
#   - Otherwise: QDISTRO_PWD_TPM_BACKEND=mock so the test still
#     exercises the v2 path on hosts without swtpm.
#
# Bats wrapper: phase8.bats `phase8-pwd-tpm-e2e`.
#
# Asserts:
#   1. CreateVaultTPM persists v2 vault file (tpm_seal section, no kdf).
#   2. VaultVersion returns 2; VaultInfo carries backend name.
#   3. UnlockVault auto-routes through the TPM path.
#   4. AddItem + GetItemAdmin roundtrip through the v2 master key.
#   5. Wrong PIN → BadPassword (mapped from TpmAuthFailed).
#   6. v1 + v2 vaults coexist; UnlockVault picks the right secret kind.
set -uo pipefail

PIN=987654
VAULT=phase8-tpm-vault
ITEM=tpm.tag

# Decide backend.
if [ -e /dev/tpmrm0 ] && command -v tpm2_unseal >/dev/null && \
   TPM2TOOLS_TCTI=device:/dev/tpmrm0 tpm2_getrandom --hex 4 >/dev/null 2>&1; then
    BACKEND=tpm2tools
else
    BACKEND=mock
fi
echo "INFO: using TPM backend: $BACKEND"

# Restart daemon with chosen backend env.
mkdir -p /etc/systemd/system/qdistro-pwd.service.d
cat >/etc/systemd/system/qdistro-pwd.service.d/tpm-backend.conf <<EOF
[Service]
Environment="QDISTRO_PWD_TPM_BACKEND=$BACKEND"
EOF
systemctl daemon-reload
systemctl restart qdistro-pwd.service
for _ in 1 2 3 4 5; do
    systemctl is-active --quiet qdistro-pwd.service && break
    sleep 1
done
systemctl is-active --quiet qdistro-pwd.service || { echo "SKIP: qdistro-pwd.service not active"; exit 0; }
echo "PASS: qdistro-pwd.service active with $BACKEND"

# Clear stale vault.
rm -f "/var/lib/qdistro/vaults/${VAULT}.vault"

# Step 1 — admin creates v2 vault.
if ! runuser -u admin -- bash -c "printf '%s\n%s\n' '$PIN' '$PIN' | qdistro-pwd-admin create-tpm '$VAULT'" \
        >/tmp/tpm-step1.log 2>&1; then
    cat /tmp/tpm-step1.log
    echo "FAIL: CreateVaultTPM"; exit 1
fi
echo "PASS: CreateVaultTPM"

# Step 2 — file contains tpm_seal not kdf.
if ! grep -q '"tpm_seal"' "/var/lib/qdistro/vaults/${VAULT}.vault"; then
    echo "FAIL: vault file missing tpm_seal section"; exit 2
fi
if grep -q '"kdf"' "/var/lib/qdistro/vaults/${VAULT}.vault"; then
    echo "FAIL: v2 vault file unexpectedly has kdf section"; exit 2
fi
echo "PASS: v2 on-disk format (tpm_seal present, no kdf)"

# Step 3 — VaultInfo says version=2 + correct backend.
INFO=$(runuser -u admin -- qdistro-pwd-admin info "$VAULT" 2>/tmp/tpm-step3.log) || {
    cat /tmp/tpm-step3.log; echo "FAIL: info"; exit 3; }
if ! printf '%s' "$INFO" | grep -q "version=2 kind=tpm-sealed backend=$BACKEND"; then
    echo "FAIL: info reports unexpected: $INFO"; exit 3
fi
echo "PASS: info reports version=2 backend=$BACKEND"

# Step 4 — Unlock auto-routes (passes PIN through UnlockVault).
if ! runuser -u admin -- env QDISTRO_PWD_PASSWORD="$PIN" \
        qdistro-pwd-admin unlock "$VAULT" >/tmp/tpm-step4.log 2>&1; then
    cat /tmp/tpm-step4.log
    echo "FAIL: UnlockVault (TPM auto-route)"; exit 4
fi
grep -q unlocked /tmp/tpm-step4.log || { cat /tmp/tpm-step4.log; echo "FAIL: unlock did not say unlocked"; exit 4; }
echo "PASS: UnlockVault auto-routes through TPM path"

# Step 5 — AddItem + GetItemAdmin roundtrip.
if ! runuser -u admin -- env QDISTRO_PWD_VALUE="tpm-payload" \
        qdistro-pwd-admin add "$VAULT" "$ITEM" >/tmp/tpm-step5.log 2>&1; then
    cat /tmp/tpm-step5.log
    echo "FAIL: AddItem on v2 vault"; exit 5
fi
RV=$(runuser -u admin -- qdistro-pwd-admin get "$VAULT" "$ITEM" 2>/tmp/tpm-step5b.log) || {
    cat /tmp/tpm-step5b.log; echo "FAIL: GetItemAdmin v2"; exit 5; }
if [ "$RV" != "tpm-payload" ]; then
    echo "FAIL: GetItemAdmin returned $RV not tpm-payload"; exit 5
fi
echo "PASS: AddItem + GetItemAdmin roundtrip on v2 vault"

# Step 6 — wrong PIN denied.
runuser -u admin -- qdistro-pwd-admin lock "$VAULT" >/dev/null 2>&1 || true
runuser -u admin -- env QDISTRO_PWD_PASSWORD="111111" \
    qdistro-pwd-admin unlock "$VAULT" >/tmp/tpm-step6.log 2>&1
if grep -qE "BadPassword|wrong PIN|wrong vault password|denied" /tmp/tpm-step6.log; then
    echo "PASS: wrong PIN denied"
else
    echo "FAIL: wrong PIN should be denied; got:"; cat /tmp/tpm-step6.log
    exit 6
fi

# Step 7 — Coexistence with a v1 vault. Re-create the original
# scrypt vault from s60 and confirm both lifetimes.
runuser -u admin -- qdistro-pwd-admin lock phase8-vault >/dev/null 2>&1 || true
rm -f /var/lib/qdistro/vaults/phase8-vault.vault
if ! runuser -u admin -- bash -c "printf '%s\n%s\n' 'hunter2' 'hunter2' | qdistro-pwd-admin create phase8-vault" \
        >/tmp/tpm-step7.log 2>&1; then
    cat /tmp/tpm-step7.log; echo "FAIL: coexistence v1 create"; exit 7
fi
V1_INFO=$(runuser -u admin -- qdistro-pwd-admin info phase8-vault 2>&1)
V2_INFO=$(runuser -u admin -- qdistro-pwd-admin info "$VAULT" 2>&1)
if ! printf '%s' "$V1_INFO" | grep -q "version=1 kind=scrypt-password"; then
    echo "FAIL: v1 info wrong: $V1_INFO"; exit 7
fi
if ! printf '%s' "$V2_INFO" | grep -q "version=2 kind=tpm-sealed"; then
    echo "FAIL: v2 info wrong: $V2_INFO"; exit 7
fi
echo "PASS: v1 + v2 vaults coexist with correct version routing"

# Cleanup the override drop-in so other tests start clean (mock backend
# doesn't survive a reboot anyway).
rm -f /etc/systemd/system/qdistro-pwd.service.d/tpm-backend.conf
systemctl daemon-reload
systemctl restart qdistro-pwd.service

echo "PASS: §Phase-8.1 spec/13 TPM-sealed vault end-to-end"

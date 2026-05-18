#!/bin/bash
# §spec/20 Phase-9 §step 2 — print-VM helpers probe.
#
# Verifies the host-side print-VM scaffolding is present + the
# helpers behave correctly even when the qdistro-print domain isn't
# defined yet:
#   1. install-print-vm.sh --remove on absent domain succeeds
#      (idempotent SKIP path).
#   2. domain-template.xml has all placeholders.
#   3. attach-usb / detach-usb --help work.
#   4. polkit policy file is installed at the standard location.
#   5. com.qdistro.print.policy contains all five actions.
#
# SKIP cleanly if the helpers haven't landed (legacy bake).
set -uo pipefail

INSTALL=/usr/local/bin/install-print-vm.sh
ATTACH=/usr/local/bin/qdistro-print-attach-usb
DETACH=/usr/local/bin/qdistro-print-detach-usb
TEMPLATE=/usr/share/qdistro/print-vm/domain-template.xml
POLICY=/usr/share/polkit-1/actions/com.qdistro.print.policy

if ! [ -x "$INSTALL" ] || ! [ -x "$ATTACH" ] || ! [ -x "$DETACH" ]; then
    echo "SKIP: print-VM helpers not installed (rerun bootstrap after task 099)"
    exit 0
fi
echo "PASS: print-VM helpers installed"

# Step 1 — install-print-vm.sh --remove on absent domain.
unique="qdistro-print-probe-$(date +%s)"
out=$(QDISTRO_PRINT_VM_NAME="$unique" \
      QDISTRO_PRINT_LIBVIRT_URI="qemu:///session" \
      "$INSTALL" --remove 2>&1)
echo "$out" | grep -qE "not defined|undefined" || {
    echo "FAIL: --remove on absent domain unexpected output: $out"
    exit 1
}
echo "PASS: install-print-vm --remove on absent domain"

# Step 2 — domain template placeholders.
if [ ! -f "$TEMPLATE" ]; then
    echo "FAIL: $TEMPLATE missing"
    exit 2
fi
for ph in __VM_NAME__ __MAC__ __MEM_KIB__ __CID__ __DISK_PATH__; do
    if ! grep -q "$ph" "$TEMPLATE"; then
        echo "FAIL: $TEMPLATE missing placeholder $ph"
        exit 2
    fi
done
grep -q "qemu-xhci" "$TEMPLATE" || {
    echo "FAIL: domain template lacks qemu-xhci controller"; exit 2; }
grep -q "<vsock" "$TEMPLATE" || {
    echo "FAIL: domain template lacks vsock device"; exit 2; }
echo "PASS: domain-template.xml structure"

# Step 3 — attach/detach --help.
if ! "$ATTACH" --help 2>&1 | grep -q "vendor-product"; then
    echo "FAIL: attach --help"
    exit 3
fi
if ! "$DETACH" --help 2>&1 | grep -q "vendor-product"; then
    echo "FAIL: detach --help"
    exit 3
fi
echo "PASS: attach/detach --help"

# Step 4 — polkit policy installed.
if [ ! -f "$POLICY" ]; then
    echo "FAIL: $POLICY not installed"
    exit 4
fi
for act in attach-usb detach-usb cancel-job purge-jobs access; do
    grep -q "id=\"com.qdistro.print.$act\"" "$POLICY" || {
        echo "FAIL: policy missing action $act"
        exit 4
    }
done
echo "PASS: com.qdistro.print.policy ships all 5 actions"

# Step 5 — pkaction can read the policy (proves polkit parsed it).
if command -v pkaction >/dev/null 2>&1; then
    if pkaction --action-id com.qdistro.print.attach-usb >/dev/null 2>&1; then
        echo "PASS: pkaction sees com.qdistro.print.attach-usb"
    else
        echo "FAIL: pkaction can't read attach-usb action"
        exit 5
    fi
fi

echo "PASS: §spec/20 Phase-9 §step 2 print-VM helpers probe"

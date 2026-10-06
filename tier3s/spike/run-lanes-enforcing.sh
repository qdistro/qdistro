#!/bin/bash
# tier3s/spike/run-lanes-enforcing.sh — HOST side. Phase D enforcing
# qualification for the tier3s GUI lanes (s123–s129):
#
#   run-lanes-enforcing.sh <logdir> [phase7-tier3s-file ...]
#
# Default lane set is the seven GUI bats files (waypipe, app, lifecycle,
# chrome-secctx, clipboard-gate, lineage, hostile-stream). Each file gets
# a FRESH enforcing clone — guest setup asserts --expect-fresh — cloned
# from baseweed-enforcing-baked.qcow2 (build-enforcing-baseweed.sh), and
# vm_run routes over SSH (qga's virt_qemu_ga_t is denied under
# enforcing). After each file the guest audit log is harvested for
# scontext=...qdistro_tier3s_t denials: the Phase D acceptance gate is
# all lanes green AND zero residual domain AVCs under Enforcing.
#
# Failed VMs are preserved for triage; passing VMs are destroyed.
set -uo pipefail

L=$1; shift || { echo "usage: $0 <logdir> [phase7-file ...]"; exit 2; }
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
VM_DIR="$repo/tests/integration/vm"
IMG="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
ENFORCING="$IMG/baseweed-enforcing-baked.qcow2"
KEYFILE="$HOME/.ssh/qdistro_enforcing_id_ed25519"

[ -f "$ENFORCING" ] || { echo "ERROR: $ENFORCING missing — run scripts/vm/build-enforcing-baseweed.sh" >&2; exit 2; }
[ -f "$KEYFILE" ] || { echo "ERROR: $KEYFILE missing — the enforcing bake creates it" >&2; exit 2; }
[ -e "$L/INDEX.md" ] && { echo "refusing: $L already has a run"; exit 2; }
mkdir -p "$L"

FILES=("$@")
if [ ${#FILES[@]} -eq 0 ]; then
    FILES=(phase7-tier3s-waypipe.bats phase7-tier3s-app.bats
           phase7-tier3s-lifecycle.bats phase7-tier3s-chrome-secctx.bats
           phase7-tier3s-clipboard-gate.bats phase7-tier3s-lineage.bats
           phase7-tier3s-hostile-stream.bats)
fi

ssh_vm() {
    ssh -p "$SSH_PORT" -i "$KEYFILE" \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes \
        root@127.0.0.1 "$@"
}

FAILS=0
declare -A VERDICT
for f in "${FILES[@]}"; do
    base="${f%.bats}"
    echo "== $f: cloning fresh enforcing worker"
    out=$("$repo/scripts/vm/clone-baseweed.sh" "t3s-ef-$base" --from-enforcing-baked 2>&1)
    VM=$(printf '%s\n' "$out" | sed -n '1p')
    SSH_PORT=$(printf '%s\n' "$out" | sed -n '2p' | sed 's/^ssh_port=//')
    if [ -z "$VM" ] || [ -z "$SSH_PORT" ]; then
        printf '%s\n' "$out" >"$L/$base.clone-fail.log"
        echo "   CLONE FAILED (see $L/$base.clone-fail.log)"; FAILS=$((FAILS+1))
        VERDICT[$f]="clone-fail"; continue
    fi
    # sshd needs a few seconds after clone returns.
    for i in $(seq 1 30); do ssh_vm 'true' 2>/dev/null && break; sleep 5; done
    {
        printf '### VM=%s ssh_port=%s\n' "$VM" "$SSH_PORT"
        ssh_vm 'getenforce; grep ^SELINUX= /etc/selinux/config'
    } | tee "$L/$base.environ.log"

    echo "== $f: bats on $VM (ssh :$SSH_PORT)"
    ( cd "$VM_DIR" && VM_NAME="$VM" VM_SSH_PORT="$SSH_PORT" \
        bats --timing "$f" ) >"$L/$base.bats.log" 2>&1
    rc=$?

    # Harvest tier3s-domain AVCs for the whole window this VM lived.
    ssh_vm 'ausearch -m avc 2>/dev/null | grep scontext=.*qdistro_tier3s_t | sort -u; echo "--"; getenforce' \
        >"$L/$base.avc.log" 2>&1
    avc_n=$(grep -c 'denied' "$L/$base.avc.log" || true)

    if [ "$rc" -eq 0 ] && [ "$avc_n" -eq 0 ]; then
        VERDICT[$f]="pass"
        virsh -c qemu:///session destroy "$VM" >/dev/null 2>&1 || true
        virsh -c qemu:///session undefine "$VM" --nvram >/dev/null 2>&1 \
            || virsh -c qemu:///session undefine "$VM" >/dev/null 2>&1 || true
        rm -f "$IMG/${VM}.qcow2"
        echo "   PASS (bats rc=0, tier3s AVCs=0) — VM removed"
    else
        VERDICT[$f]="fail rc=$rc avc=$avc_n"
        FAILS=$((FAILS+1))
        echo "   FAIL rc=$rc tier3s-AVCs=$avc_n — VM $VM preserved"
    fi
done

{
    echo "# tier3s enforcing lane run ($(basename "$L"))"
    echo
    echo "commit=$(git -C "$repo" rev-parse --short HEAD)  FAILS=$FAILS"
    echo
    for f in "${FILES[@]}"; do
        echo "- $f — ${VERDICT[$f]:-skipped}"
    done
} | tee "$L/INDEX.md"

echo "== done; FAILS=$FAILS; logs in $L"
exit "$FAILS"

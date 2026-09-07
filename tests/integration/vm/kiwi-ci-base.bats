#!/usr/bin/env bats
# Phase G of todo/iso/14: kiwi tester image as the qci *base*, with the
# per-run source overlay still built on top by fresh-vm-bootstrap.sh.
# VM-free except the optional live clone (skipped unless the imported
# qcow2 exists AND QDISTRO_KIWI_LIVE=1).

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    VM="$REPO/scripts/vm"
    IMAGE="$REPO/image"
    T="$BATS_TEST_TMPDIR"
}

@test "clone-baseweed: --from-kiwi is mutually exclusive with baked/enforcing/golden" {
    run bash "$VM/clone-baseweed.sh" x --from-kiwi --from-baked
    [ "$status" -eq 2 ]
    [[ "$output" == *"mutually exclusive"* ]]
    run bash "$VM/clone-baseweed.sh" x --from-kiwi --from-enforcing-baked
    [ "$status" -eq 2 ]
    run bash "$VM/clone-baseweed.sh" x --from-kiwi --from-run-golden=/tmp/nope.qcow2
    [ "$status" -eq 2 ]
}

@test "clone-baseweed: --from-kiwi without an imported base names import-kiwi-base.sh" {
    run env QDWIN_IMG_DIR="$T/empty" bash "$VM/clone-baseweed.sh" x --from-kiwi
    [ "$status" -eq 1 ]
    [[ "$output" == *"import-kiwi-base.sh"* ]]
    [[ "$output" == *"qdistro-kiwi-base.qcow2"* ]]
}

@test "vm-base: auto prefers a stamped qcow2, not a leftover empty file" {
    mkdir -p "$T/img"
    run env QDWIN_IMG_DIR="$T/img" QDISTRO_VM_BASE=auto bash -c '
        source "$1"
        qdistro_vm_base_kind
    ' _ "$VM/lib/vm-base.sh"
    [ "$status" -eq 0 ]
    [ "$output" = baked ]
    # leftover empty dest without stamp must NOT flip auto off baked
    : > "$T/img/qdistro-kiwi-base.qcow2"
    run env QDWIN_IMG_DIR="$T/img" QDISTRO_VM_BASE=auto bash -c '
        source "$1"
        qdistro_vm_base_kind
    ' _ "$VM/lib/vm-base.sh"
    [ "$status" -eq 0 ]
    [ "$output" = baked ]
    qemu-img create -f qcow2 "$T/img/qdistro-kiwi-base.qcow2" 4M >/dev/null
    echo 'DIGEST=dead' > "$T/img/qdistro-kiwi-base.qcow2.stamp"
    run env QDWIN_IMG_DIR="$T/img" QDISTRO_VM_BASE=auto bash -c '
        source "$1"
        qdistro_vm_base_kind
    ' _ "$VM/lib/vm-base.sh"
    [ "$status" -eq 0 ]
    [ "$output" = kiwi ]
    run env QDWIN_IMG_DIR="$T/img" QDISTRO_VM_BASE=baked bash -c '
        source "$1"
        qdistro_vm_base_kind
    ' _ "$VM/lib/vm-base.sh"
    [ "$status" -eq 0 ]
    [ "$output" = baked ]
    run env QDWIN_IMG_DIR="$T/missing" QDISTRO_VM_BASE=kiwi bash -c '
        source "$1"
        qdistro_vm_base_kind
    ' _ "$VM/lib/vm-base.sh"
    [ "$status" -eq 2 ]
    [[ "$output" == *"import-kiwi-base.sh"* ]]
    run env QDISTRO_VM_BASE=nope bash -c '
        source "$1"
        qdistro_vm_base_kind
    ' _ "$VM/lib/vm-base.sh"
    [ "$status" -eq 2 ]
}

@test "ovmf inject: keeps machine type, lands loader and nvram, refuses XML metacharacters" {
    source "$VM/lib/ovmf.sh"
    xml='<domain><os>
    <type arch="x86_64" machine="pc-i440fx-11.0">hvm</type>
    <boot dev="hd"/>
  </os></domain>'
    export QDISTRO_OVMF=/usr/share/qemu/ovmf-x86_64-4m.bin
    export QDISTRO_OVMF_VARS=/usr/share/qemu/ovmf-x86_64-4m-vars.bin
    export QDISTRO_NVRAM=/tmp/fake.nvram.fd
    run bash -c 'source "$1"; printf %s "$2" | qdistro_inject_ovmf_os' _ "$VM/lib/ovmf.sh" "$xml"
    [ "$status" -eq 0 ]
    [[ "$output" == *'machine="pc-i440fx-11.0"'* ]] || [[ "$output" == *"machine='pc-i440fx-11.0'"* ]] || [[ "$output" == *pc-i440fx-11.0* ]]
    [[ "$output" == *"$QDISTRO_OVMF"* ]]
    [[ "$output" == *"$QDISTRO_NVRAM"* ]]
    [[ "$output" == *"<loader"* ]]
    [[ "$output" == *"<nvram"* ]]
    export QDISTRO_OVMF='/tmp/bad<"path.bin'
    run bash -c 'source "$1"; printf %s "$2" | qdistro_inject_ovmf_os' _ "$VM/lib/ovmf.sh" "$xml"
    [ "$status" -eq 2 ]
    [[ "$output" == *"XML metacharacters"* ]]
    export QDISTRO_OVMF=ovmf.bin QDISTRO_OVMF_VARS=/usr/share/qemu/ovmf-x86_64-4m-vars.bin QDISTRO_NVRAM=/tmp/fake.nvram.fd
    run bash -c 'source "$1"; printf %s "$2" | qdistro_inject_ovmf_os' _ "$VM/lib/ovmf.sh" "$xml"
    [ "$status" -eq 2 ]
    [[ "$output" == *"not absolute"* ]]
}

@test "import-kiwi-base: converts a tiny raw to a stamped qcow2 and is idempotent on the same digest" {
    command -v qemu-img >/dev/null
    mkdir -p "$T/img"
    # 4 MiB raw, second half zeros so -S 64k actually sparsifies.
    { head -c $((2*1024*1024)) /dev/urandom; head -c $((2*1024*1024)) /dev/zero; } > "$T/tiny.raw"
    run env QDWIN_IMG_DIR="$T/img" bash "$VM/import-kiwi-base.sh" "$T/tiny.raw"
    [ "$status" -eq 0 ]
    [ -f "$T/img/qdistro-kiwi-base.qcow2" ]
    [ -f "$T/img/qdistro-kiwi-base.qcow2.stamp" ]
    grep -q '^KIND=raw$' "$T/img/qdistro-kiwi-base.qcow2.stamp"
    grep -q '^DIGEST=' "$T/img/qdistro-kiwi-base.qcow2.stamp"
    qemu-img info "$T/img/qdistro-kiwi-base.qcow2" | grep -q 'file format: qcow2'
    run env QDWIN_IMG_DIR="$T/img" bash "$VM/import-kiwi-base.sh" "$T/tiny.raw"
    [ "$status" -eq 0 ]
    [[ "$output" == *"already imported"* ]]
}

@test "spin-test-vm: QDISTRO_VM_BASE dispatches --from-kiwi or --from-baked; builder stays baked" {
    grep -q 'qdistro_vm_base_kind' "$VM/spin-test-vm.sh"
    grep -q -- '--from-kiwi' "$VM/spin-test-vm.sh"
    grep -q -- '--from-baked' "$VM/spin-test-vm.sh"
    grep -q 'QCI_RUN_GOLDEN_BACKING' "$VM/spin-test-vm.sh"
    grep -q 'nvram.fd' "$VM/spin-test-vm.sh"
    # golden clone still wins over the base kind
    awk '/QCI_RUN_GOLDEN_BACKING/{f=1} f{print; if(/from-run-golden/){found=1; exit}} END{exit found?0:1}' "$VM/spin-test-vm.sh"
    # workers still get OVMF when that golden is kiwi-backed
    grep -q 'qdistro_backing_needs_ovmf' "$VM/clone-baseweed.sh"
    grep -q 'NEED_BAKED' "$VM/spin-test-vm.sh"
    grep -q 'skipped (kiwi base or run-golden' "$VM/spin-test-vm.sh"
    grep -q 'imported kiwi base' "$REPO/ci/lib/gates/preflight.sh"
    grep -q 'qdistro_kiwi_base_ok' "$REPO/ci/lib/gates/preflight.sh"
}

@test "backing-needs-ovmf: a golden overlay on the kiwi base is UEFI; an unrelated qcow2 is not" {
    command -v qemu-img >/dev/null
    mkdir -p "$T/img"
    qemu-img create -f qcow2 "$T/img/qdistro-kiwi-base.qcow2" 8M >/dev/null
    echo 'DIGEST=dead' > "$T/img/qdistro-kiwi-base.qcow2.stamp"
    qemu-img create -f qcow2 -F qcow2 -b "$T/img/qdistro-kiwi-base.qcow2" "$T/img/golden.qcow2" >/dev/null
    qemu-img create -f qcow2 "$T/img/other.qcow2" 8M >/dev/null
    run env QDWIN_IMG_DIR="$T/img" bash -c '
        source "$1"
        qdistro_backing_needs_ovmf "$2"
    ' _ "$VM/lib/vm-base.sh" "$T/img/golden.qcow2"
    [ "$status" -eq 0 ]
    run env QDWIN_IMG_DIR="$T/img" bash -c '
        source "$1"
        qdistro_backing_needs_ovmf "$2"
    ' _ "$VM/lib/vm-base.sh" "$T/img/other.qcow2"
    [ "$status" -ne 0 ]
    run env QDWIN_IMG_DIR="$T/img" bash -c '
        source "$1"
        qdistro_backing_needs_ovmf "$2"
    ' _ "$VM/lib/vm-base.sh" "$T/img/qdistro-kiwi-base.qcow2"
    [ "$status" -eq 0 ]
    # symlink dest: qemu-img stores the -b string, probe must still match
    mkdir -p "$T/realimg" "$T/linkimg"
    qemu-img create -f qcow2 "$T/realimg/qdistro-kiwi-base.qcow2" 8M >/dev/null
    echo 'DIGEST=dead' > "$T/realimg/qdistro-kiwi-base.qcow2.stamp"
    ln -s "$T/realimg/qdistro-kiwi-base.qcow2" "$T/linkimg/qdistro-kiwi-base.qcow2"
    ln -s "$T/realimg/qdistro-kiwi-base.qcow2.stamp" "$T/linkimg/qdistro-kiwi-base.qcow2.stamp"
    qemu-img create -f qcow2 -F qcow2 -b "$T/linkimg/qdistro-kiwi-base.qcow2" "$T/linkimg/golden.qcow2" >/dev/null
    run env QDWIN_IMG_DIR="$T/linkimg" bash -c '
        source "$1"
        qdistro_backing_needs_ovmf "$2"
    ' _ "$VM/lib/vm-base.sh" "$T/linkimg/golden.qcow2"
    [ "$status" -eq 0 ]
    # relative -b
    ( cd "$T/realimg" && qemu-img create -f qcow2 -F qcow2 -b ./qdistro-kiwi-base.qcow2 rel-golden.qcow2 ) >/dev/null
    run env QDWIN_IMG_DIR="$T/realimg" bash -c '
        source "$1"
        qdistro_backing_needs_ovmf "$2"
    ' _ "$VM/lib/vm-base.sh" "$T/realimg/rel-golden.qcow2"
    [ "$status" -eq 0 ]
}

@test "backing-needs-ovmf: hunt-list (symlink images dir, two-level, baked skip, hardlink dest)" {
    command -v qemu-img >/dev/null
    # symlink images dir (libvirt images live on a bigger disk)
    mkdir -p "$T/realdisk/images"
    qemu-img create -f qcow2 "$T/realdisk/images/qdistro-kiwi-base.qcow2" 8M >/dev/null
    echo 'DIGEST=dead' > "$T/realdisk/images/qdistro-kiwi-base.qcow2.stamp"
    ln -s "$T/realdisk/images" "$T/linkdisk"
    qemu-img create -f qcow2 -F qcow2 -b "$T/linkdisk/qdistro-kiwi-base.qcow2" \
        "$T/linkdisk/golden.qcow2" >/dev/null
    run env QDWIN_IMG_DIR="$T/linkdisk" bash -c '
        source "$1"
        qdistro_backing_needs_ovmf "$2"
    ' _ "$VM/lib/vm-base.sh" "$T/linkdisk/golden.qcow2"
    [ "$status" -eq 0 ]
    # two-level worker, absolute, same dir (qci worker shape)
    qemu-img create -f qcow2 -F qcow2 -b "$T/linkdisk/golden.qcow2" \
        "$T/linkdisk/worker.qcow2" >/dev/null
    run env QDWIN_IMG_DIR="$T/linkdisk" bash -c '
        source "$1"
        qdistro_backing_needs_ovmf "$2"
    ' _ "$VM/lib/vm-base.sh" "$T/linkdisk/worker.qcow2"
    [ "$status" -eq 0 ]
    # nested-dir relative two-level: inner -b is ./kiwi named by golden, not worker
    mkdir -p "$T/nest/a" "$T/nest/b"
    qemu-img create -f qcow2 "$T/nest/a/qdistro-kiwi-base.qcow2" 8M >/dev/null
    echo 'DIGEST=dead' > "$T/nest/a/qdistro-kiwi-base.qcow2.stamp"
    ( cd "$T/nest/a" && qemu-img create -f qcow2 -F qcow2 -b ./qdistro-kiwi-base.qcow2 golden.qcow2 ) >/dev/null
    ( cd "$T/nest/b" && qemu-img create -f qcow2 -F qcow2 -b ../a/golden.qcow2 worker.qcow2 ) >/dev/null
    run env QDWIN_IMG_DIR="$T/nest/a" bash -c '
        source "$1"
        qdistro_backing_needs_ovmf "$2"
    ' _ "$VM/lib/vm-base.sh" "$T/nest/b/worker.qcow2"
    [ "$status" -eq 0 ]
    # baked golden with kiwi sitting in the same dir must stay BIOS
    mkdir -p "$T/mix"
    qemu-img create -f qcow2 "$T/mix/qdistro-kiwi-base.qcow2" 8M >/dev/null
    echo 'DIGEST=dead' > "$T/mix/qdistro-kiwi-base.qcow2.stamp"
    qemu-img create -f qcow2 "$T/mix/baked.qcow2" 8M >/dev/null
    qemu-img create -f qcow2 -F qcow2 -b "$T/mix/baked.qcow2" "$T/mix/baked-golden.qcow2" >/dev/null
    run env QDWIN_IMG_DIR="$T/mix" bash -c '
        source "$1"
        qdistro_backing_needs_ovmf "$2"
    ' _ "$VM/lib/vm-base.sh" "$T/mix/baked-golden.qcow2"
    [ "$status" -ne 0 ]
    # hardlink dest: stored -b is a different path, same inode
    mkdir -p "$T/hl"
    qemu-img create -f qcow2 "$T/hl/qdistro-kiwi-base.qcow2" 8M >/dev/null
    echo 'DIGEST=dead' > "$T/hl/qdistro-kiwi-base.qcow2.stamp"
    ln "$T/hl/qdistro-kiwi-base.qcow2" "$T/hl/hl-kiwi.qcow2"
    qemu-img create -f qcow2 -F qcow2 -b "$T/hl/hl-kiwi.qcow2" "$T/hl/hl-golden.qcow2" >/dev/null
    run env QDWIN_IMG_DIR="$T/hl" bash -c '
        source "$1"
        qdistro_backing_needs_ovmf "$2"
    ' _ "$VM/lib/vm-base.sh" "$T/hl/hl-golden.qcow2"
    [ "$status" -eq 0 ]
}

@test "clone-baseweed: --from-kiwi refuses a raw QDISTRO_KIWI_BASE" {
    mkdir -p "$T/img"
    { head -c 1048576 /dev/zero; } > "$T/img/not-qcow2.raw"
    run env QDWIN_IMG_DIR="$T/img" QDISTRO_KIWI_BASE="$T/img/not-qcow2.raw" \
        bash "$VM/clone-baseweed.sh" x --from-kiwi
    [ "$status" -eq 1 ]
    [[ "$output" == *"not qcow2"* ]]
    [[ "$output" == *"import-kiwi-base.sh"* ]]
}

@test "bootstrap: ensures CI extras after masking greetd, before fetching tarballs" {
    local b="$VM/fresh-vm-bootstrap.sh"
    grep -q 'ensuring CI extras' "$b"
    grep -q 'command -v bats' "$b"
    grep -q 'bats ydotool tesseract-ocr rage-encryption rsync' "$b"
    grep -q 'QCI_OFFLINE=1 forbids zypper' "$b"
    grep -q "QCI_OFFLINE=" "$VM/spin-test-vm.sh"
    local extras_line fetch_line mask_line offline_line
    extras_line="$(grep -n 'ensuring CI extras' "$b" | head -1 | cut -d: -f1)"
    fetch_line="$(grep -n 'fetching tarballs' "$b" | head -1 | cut -d: -f1)"
    mask_line="$(grep -n 'masking jeos-firstboot + greetd' "$b" | head -1 | cut -d: -f1)"
    offline_line="$(grep -n 'QCI_OFFLINE=1 forbids zypper' "$b" | head -1 | cut -d: -f1)"
    [ "$mask_line" -lt "$offline_line" ]
    [ "$offline_line" -lt "$extras_line" ]
    [ "$extras_line" -lt "$fetch_line" ]
}

@test "live clone from imported kiwi base answers qemu-guest-agent" {
    if [ "${QDISTRO_KIWI_LIVE:-0}" != 1 ]; then
        skip "set QDISTRO_KIWI_LIVE=1 to boot a real --from-kiwi clone"
    fi
    local imgdir="${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}"
    local img="$imgdir/qdistro-kiwi-base.qcow2"
    if [ ! -f "$img" ]; then
        skip "no imported kiwi base at $img"
    fi
    local vm worker
    _live_cleanup() {
        local n
        for n in "$worker" "$vm"; do
            [ -n "$n" ] || continue
            virsh -c qemu:///session destroy "$n" >/dev/null 2>&1 || true
            virsh -c qemu:///session undefine "$n" --nvram >/dev/null 2>&1 \
                || virsh -c qemu:///session undefine "$n" >/dev/null 2>&1 || true
            rm -f "$imgdir/${n}.qcow2" "$imgdir/${n}.nvram.fd"
        done
    }
    trap _live_cleanup EXIT
    vm="$(bash "$VM/clone-baseweed.sh" qdistro-kg-live --from-kiwi)"
    [ -n "$vm" ]
    virsh -c qemu:///session dumpxml "$vm" | grep -q '<loader'
    virsh -c qemu:///session dumpxml "$vm" | grep -q 'ovmf'
    run bash "$VM/vm-exec" "$vm" 'test -f /etc/qdistro/release && cat /etc/qdistro/release'
    local rc=$status
    # Worker path qci actually uses: overlay of the golden, not --from-kiwi.
    worker="$(bash "$VM/clone-baseweed.sh" qdistro-kg-w --from-run-golden="$imgdir/${vm}.qcow2")"
    [ -n "$worker" ]
    virsh -c qemu:///session dumpxml "$worker" | grep -q '<loader'
    virsh -c qemu:///session dumpxml "$worker" | grep -q 'ovmf'
    run bash "$VM/vm-exec" "$worker" 'test -f /etc/qdistro/release && cat /etc/qdistro/release'
    local wrc=$status
    _live_cleanup
    [ "$rc" -eq 0 ]
    [ "$wrc" -eq 0 ]
    [[ "$output" == *"PROFILE="* ]] || [[ "$output" == *"profile="* ]] || [[ "$output" == *"SNAPSHOT="* ]] || [[ "$output" == *"qdistro"* ]]
}

#!/usr/bin/env bats
# Host-only cleanup contract for UEFI NVRAM, managed-save and BIOS domains.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    REAL_QEMU_IMG="$(command -v qemu-img)"
    TMP="$(mktemp -d)"
    RDIR="$TMP/run"; mkdir -p "$RDIR/host"
    QDWIN_IMG_DIR="$TMP/images"; mkdir -p "$QDWIN_IMG_DIR"
    DISK="$QDWIN_IMG_DIR/qci-cleanup-test.qcow2"
    printf disk > "$DISK"
    touch -d '2 hours ago' "$DISK"
    DOMAIN="$TMP/domain"
    printf 'qci-cleanup-test\n' > "$DOMAIN"
    STATE="$TMP/state"; printf 'shut off\n' > "$STATE"
    LIST_CALLS="$TMP/list.calls"; printf '0\n' > "$LIST_CALLS"
    OTHER_DISK_FILE="$TMP/other.disk"
    CALLS="$TMP/virsh.calls"; : > "$CALLS"
    BIN="$TMP/bin"; mkdir -p "$BIN"
    cat > "$BIN/virsh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$CALLS"
case "$1" in
    list)
        count=$(cat "$LIST_CALLS"); count=$((count + 1)); printf '%s\n' "$count" > "$LIST_CALLS"
        if [ "$count" = "${RACE_ATTACH_ON_LIST:-never}" ]; then
            grep -Fxq "${RACE_ATTACH_OWNER:-other-domain}" "$DOMAIN" 2>/dev/null \
                || printf '%s\n' "${RACE_ATTACH_OWNER:-other-domain}" >> "$DOMAIN"
            printf '%s\n' "$RACE_ATTACH_DISK" > "$OTHER_DISK_FILE"
        fi
        if [ "$count" = "${RACE_CHILD_ON_LIST:-never}" ]; then
            "$REAL_QEMU_IMG" create -q -f qcow2 -F qcow2 \
                -b "$RACE_CHILD_BACKING" "$RACE_CHILD_ON_LIST_PATH"
        fi
        if [ -s "$DOMAIN" ]; then cat "$DOMAIN"; fi
        ;;
    domblklist)
        if [ "$2" = "other-domain" ]; then
            if [ -s "$OTHER_DISK_FILE" ]; then
                printf 'file disk vda %s\n' "$(cat "$OTHER_DISK_FILE")"
            else
                printf 'file disk vda %s\n' "$OTHER_DISK"
            fi
        else
            printf 'file disk vda %s\n' "$DISK"
            case " $* " in
                *" --inactive "*) printf '%b' "${INACTIVE_STORAGE_ROWS:-${EXTRA_STORAGE_ROWS:-}}" ;;
                *) printf '%b' "${LIVE_STORAGE_ROWS:-${EXTRA_STORAGE_ROWS:-}}" ;;
            esac
        fi
        ;;
    domstate)
        if [ "$2" = "other-domain" ]; then printf 'shut off\n'; else cat "$STATE"; fi
        ;;
    destroy)
        [ "${DESTROY_FAIL:-0}" = 0 ] || exit 26
        printf 'shut off\n' > "$STATE"
        ;;
    undefine)
        args=" $* "
        [[ "$args" != *" --remove-all-storage "* ]] || exit 21
        [[ "$args" == *" --managed-save "* ]] || exit 22
        [ "${UNDEFINE_FAIL:-0}" = 0 ] || exit 25
        if [ "$FIRMWARE" = uefi ]; then
            [[ "$args" == *" --nvram "* ]] || exit 23
        elif [[ "$args" == *" --nvram "* ]]; then
            exit 24
        fi
        : > "$DOMAIN"
        if [ -n "${CREATE_CHILD_ON_UNDEFINE:-}" ]; then
            printf child > "$CREATE_CHILD_ON_UNDEFINE"
            printf '%s\n' "$DISK" > "$CREATE_CHILD_ON_UNDEFINE.backing"
        fi
        ;;
esac
SH
    chmod +x "$BIN/virsh"
    cat > "$BIN/qemu-img" <<'SH'
#!/bin/sh
[ -f "$2.backing" ] && printf 'backing file: %s\n' "$(cat "$2.backing")"
exit 0
SH
    chmod +x "$BIN/qemu-img"
    PATH="$BIN:$PATH"
    export CALLS DOMAIN DISK STATE LIST_CALLS OTHER_DISK_FILE
    export FIRMWARE UNDEFINE_FAIL REAL_QEMU_IMG EXTRA_STORAGE_ROWS
    export LIVE_STORAGE_ROWS INACTIVE_STORAGE_ROWS OTHER_DISK DESTROY_FAIL
    export CREATE_CHILD_ON_UNDEFINE RACE_ATTACH_ON_LIST RACE_ATTACH_OWNER RACE_ATTACH_DISK
    export RACE_CHILD_ON_LIST RACE_CHILD_BACKING RACE_CHILD_ON_LIST_PATH
    VIRSH=("$BIN/virsh")
    EXIT_OK=0; EXIT_RUNNER=90
    # These stubs are invoked by functions in the sourced gate modules.
    # shellcheck disable=SC2329
    qci_assert_run_dir() { :; }
    # shellcheck disable=SC2329
    is_protected_vm() { return 1; }
    # shellcheck disable=SC2329
    record_result() { printf '%s\n' "$*"; }
    # shellcheck disable=SC2329
    exit_class_name() { [ "$1" -eq 0 ] && printf pass || printf runner; }
    # shellcheck disable=SC2329
    log() { :; }
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/vm.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/cleanup.sh"
}

teardown() { rm -rf "$TMP"; }

@test "cleanup removes UEFI domain NVRAM managed-save and storage" {
    FIRMWARE=uefi; export FIRMWARE
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ ! -s "$DOMAIN" ]
    [ ! -e "$DISK" ]
    grep -q '^undefine qci-cleanup-test --managed-save --nvram$' "$CALLS"
    [ "$(grep -c '^undefine ' "$CALLS")" -eq 1 ]
}

@test "cleanup retries non-UEFI domain without nvram and keeps teardown flags" {
    FIRMWARE=bios; export FIRMWARE
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ ! -s "$DOMAIN" ]
    [ ! -e "$DISK" ]
    grep -q '^undefine qci-cleanup-test --managed-save --nvram$' "$CALLS"
    grep -q '^undefine qci-cleanup-test --managed-save$' "$CALLS"
    [ "$(grep -c '^undefine ' "$CALLS")" -eq 2 ]
}

@test "cleanup failure preserves defined domain and disk for diagnosis" {
    FIRMWARE=uefi; UNDEFINE_FAIL=1; export FIRMWARE UNDEFINE_FAIL
    run gate_cleanup --age-hours 1
    [ "$status" -eq 90 ]
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    grep -q '^FAIL undefine qci-cleanup-test$' "$RDIR/host/cleanup.log"
}

@test "cleanup reaudits before unlink when a child appears during undefine" {
    FIRMWARE=uefi
    CREATE_CHILD_ON_UNDEFINE="$QDWIN_IMG_DIR/qci-racing-child.qcow2"
    export FIRMWARE CREATE_CHILD_ON_UNDEFINE
    run gate_cleanup --age-hours 1
    [ "$status" -eq 90 ]
    [ ! -s "$DOMAIN" ]
    [ -e "$DISK" ]
    [ -e "$CREATE_CHILD_ON_UNDEFINE" ]
    grep -Fq "keep undefined-domain overlay $DISK (final backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
}

@test "cleanup preserves a defined qcow2 parent referenced by another overlay" {
    FIRMWARE=uefi; export FIRMWARE
    local child="$QDWIN_IMG_DIR/qci-worker-child.qcow2"
    printf child > "$child"
    printf '%s\n' "$DISK" > "$child.backing"
    touch -d '2 hours ago' "$child"

    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    [ "$(grep -c '^destroy qci-cleanup-test$' "$CALLS" || true)" -eq 0 ]
    [ "$(grep -c '^undefine qci-cleanup-test ' "$CALLS" || true)" -eq 0 ]
    grep -Fq "keep qci-cleanup-test disk=$DISK (backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
}

@test "cleanup preserves a real qcow2 parent referenced by a recent child" {
    FIRMWARE=uefi; export FIRMWARE
    local child="$QDWIN_IMG_DIR/qci-recent-child.qcow2"
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 -F qcow2 -b "$DISK" "$child"
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    touch -d '2 hours ago' "$DISK"
    touch "$child"

    [ "$(backing_referrer_state "$DISK")" = referred ]
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    [ -e "$child" ]
    [ "$(grep -c '^destroy qci-cleanup-test$' "$CALLS" || true)" -eq 0 ]
    [ "$(grep -c '^undefine qci-cleanup-test ' "$CALLS" || true)" -eq 0 ]
    grep -Fq "keep qci-cleanup-test disk=$DISK (backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
}

@test "cleanup protects a real secondary parent disk through the orphan sweep" {
    FIRMWARE=uefi; export FIRMWARE
    local secondary="$QDWIN_IMG_DIR/qci-secondary-parent.qcow2"
    local child="$QDWIN_IMG_DIR/qci-recent-child.qcow2"
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 "$secondary" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 -F qcow2 -b "$secondary" "$child"
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    EXTRA_STORAGE_ROWS="file disk vdb $secondary\n"; export EXTRA_STORAGE_ROWS
    touch -d '2 hours ago' "$DISK" "$secondary"
    touch "$child"

    [ "$(backing_referrer_state "$secondary")" = referred ]
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    [ -e "$secondary" ]
    [ -e "$child" ]
    [ "$(grep -c '^destroy qci-cleanup-test$' "$CALLS" || true)" -eq 0 ]
    [ "$(grep -c '^undefine qci-cleanup-test ' "$CALLS" || true)" -eq 0 ]
    grep -Fq "keep qci-cleanup-test disk=$secondary (backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
    grep -Fq "keep qci-secondary-parent.qcow2 (attached to defined domain qci-cleanup-test)" \
        "$RDIR/host/cleanup.log"
}

@test "cleanup preserves domains with pooled cdrom or block-device storage" {
    FIRMWARE=uefi; export FIRMWARE
    local row
    for row in \
        'volume disk vdb pool/volume' \
        'file cdrom sda /var/lib/libvirt/images/installer.iso' \
        'block disk vdc /dev/mapper/qci-test'; do
        EXTRA_STORAGE_ROWS="$row\n"; export EXTRA_STORAGE_ROWS
        : > "$CALLS"
        run gate_cleanup --age-hours 1
        [ "$status" -eq 0 ]
        [ -s "$DOMAIN" ]
        [ -e "$DISK" ]
        [ "$(grep -c '^destroy qci-cleanup-test$' "$CALLS" || true)" -eq 0 ]
        [ "$(grep -c '^undefine qci-cleanup-test ' "$CALLS" || true)" -eq 0 ]
        grep -q 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log"
    done
}

@test "cleanup reports runner failure when destroy leaves domain running" {
    FIRMWARE=uefi; DESTROY_FAIL=1
    printf 'running\n' > "$STATE"
    export FIRMWARE DESTROY_FAIL
    run gate_cleanup --age-hours 1
    [ "$status" -eq 90 ]
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    grep -q 'FAIL destroy qci-cleanup-test rc=26 state=running' \
        "$RDIR/host/cleanup.log"
    [ "$(grep -c '^undefine qci-cleanup-test ' "$CALLS" || true)" -eq 0 ]
}

@test "cleanup preserves a disk directly owned by another defined domain" {
    FIRMWARE=uefi
    printf 'qci-cleanup-test\nother-domain\n' > "$DOMAIN"
    OTHER_DISK="$DISK"
    export FIRMWARE OTHER_DISK
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -e "$DISK" ]
    grep -Fq "$DISK (directly owned by other domain: other-domain)" \
        "$RDIR/host/cleanup.log"
    [ "$(grep -c '^destroy qci-cleanup-test$' "$CALLS" || true)" -eq 0 ]
}

@test "live-only attachment makes cleanup globally fail closed" {
    FIRMWARE=uefi
    printf 'running\n' > "$STATE"
    local live_only="$QDWIN_IMG_DIR/qci-live-only.qcow2"
    printf live > "$live_only"
    touch -d '2 hours ago' "$live_only"
    LIVE_STORAGE_ROWS="file disk vdb $live_only\n"
    INACTIVE_STORAGE_ROWS=""
    export FIRMWARE LIVE_STORAGE_ROWS INACTIVE_STORAGE_ROWS
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    [ -e "$live_only" ]
    grep -q 'live-only attachment' "$RDIR/host/cleanup.log"
}

@test "canonical inventory protects orphan reached through another domain alias" {
    local target="$QDWIN_IMG_DIR/qci-alias-target.qcow2"
    local alias="$TMP/owned-through-alias.qcow2"
    printf target > "$target"
    touch -d '2 hours ago' "$target"
    ln -s "$target" "$alias"
    printf 'other-domain\n' > "$DOMAIN"
    OTHER_DISK="$alias"
    export OTHER_DISK
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -e "$target" ]
    grep -Fq 'keep qci-alias-target.qcow2 (attached to defined domain other-domain)' \
        "$RDIR/host/cleanup.log"
}

@test "post-undefine refresh preserves a real qcow2 attached by a new owner" {
    FIRMWARE=uefi
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    touch -d '2 hours ago' "$DISK"
    RACE_ATTACH_ON_LIST=2
    RACE_ATTACH_OWNER="other-domain"
    RACE_ATTACH_DISK="$DISK"
    export FIRMWARE RACE_ATTACH_ON_LIST RACE_ATTACH_OWNER RACE_ATTACH_DISK

    run gate_cleanup --age-hours 1
    [ "$status" -eq 90 ]
    [ -e "$DISK" ]
    grep -Fq "keep $DISK (post-undefine ownership changed; attached to other-domain)" \
        "$RDIR/host/cleanup.log"
}

@test "orphan refresh preserves a real qcow2 after an existing owner changes attachment" {
    local prior="$TMP/prior-owner-disk.qcow2"
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 "$prior" 1M
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    touch -d '2 hours ago' "$DISK"
    printf 'other-domain\n' > "$DOMAIN"
    printf '%s\n' "$prior" > "$OTHER_DISK_FILE"
    RACE_ATTACH_ON_LIST=2
    RACE_ATTACH_OWNER="other-domain"
    RACE_ATTACH_DISK="$DISK"
    export RACE_ATTACH_ON_LIST RACE_ATTACH_OWNER RACE_ATTACH_DISK

    run gate_cleanup --age-hours 1
    [ "$status" -eq 90 ]
    [ -e "$DISK" ]
    grep -Fq "keep $DISK (orphan ownership changed; attached to other-domain)" \
        "$RDIR/host/cleanup.log"
}

@test "post-undefine final backing audit catches a child created during ownership refresh" {
    local child="$QDWIN_IMG_DIR/qci-refresh-child.qcow2"
    FIRMWARE=uefi
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    touch -d '2 hours ago' "$DISK"
    RACE_CHILD_ON_LIST=2
    RACE_CHILD_BACKING="$DISK"
    RACE_CHILD_ON_LIST_PATH="$child"
    export FIRMWARE RACE_CHILD_ON_LIST RACE_CHILD_BACKING RACE_CHILD_ON_LIST_PATH

    run gate_cleanup --age-hours 1
    [ "$status" -eq 90 ]
    [ -e "$DISK" ]
    [ -e "$child" ]
    grep -Fq "keep undefined-domain overlay $DISK (final backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
}

@test "orphan final backing audit catches a child created during ownership refresh" {
    local child="$QDWIN_IMG_DIR/qci-refresh-child.qcow2"
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    touch -d '2 hours ago' "$DISK"
    : > "$DOMAIN"
    RACE_CHILD_ON_LIST=2
    RACE_CHILD_BACKING="$DISK"
    RACE_CHILD_ON_LIST_PATH="$child"
    export RACE_CHILD_ON_LIST RACE_CHILD_BACKING RACE_CHILD_ON_LIST_PATH

    run gate_cleanup --age-hours 1
    [ "$status" -eq 90 ]
    [ -e "$DISK" ]
    [ -e "$child" ]
    grep -Fq "keep orphan qci-cleanup-test.qcow2 (final backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
}

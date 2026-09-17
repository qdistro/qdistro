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
# ROUND 5: the foreign-URI ownership audit is now MANDATORY and fails closed
# when a configured URI cannot be interrogated. The default stub therefore
# behaves like a reachable foreign connection with no domains defined, which is
# the honest "nothing foreign owns our images" state. Tests that need an
# unreachable or a claiming foreign URI shadow this script.
if [ "$1" = "-c" ]; then
    case "$3" in
        list) exit 0 ;;
        *) exit 0 ;;
    esac
fi
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
    # backing_referrer_state now inspects EVERY file in the images directory and
    # walks whole chains, so the stub must behave like `qemu-img info -- PATH`:
    # the path is the LAST argument, a file it cannot open is a nonzero exit,
    # and a `<path>.backing` sidecar declares that path's immediate parent.
    cat > "$BIN/qemu-img" <<'SH'
#!/bin/sh
for a in "$@"; do last=$a; done
[ -e "$last" ] || exit 1
[ -f "$last.backing" ] && printf 'backing file: %s\n' "$(cat "$last.backing")"
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
    EXIT_USAGE=2
    # shellcheck disable=SC2329
    record_blocked() { printf 'BLOCKED %s\n' "$*"; }
    # Never probe a REAL foreign libvirt connection from a unit test: the stub
    # above answers `virsh -c <uri> ...`. An EMPTY QCI_CLEANUP_FOREIGN_URIS is
    # no longer a usable test default -- it now fails the audit closed, which is
    # exactly the point (see cleanup_foreign_uri_conflict).
    unset QCI_CLEANUP_FOREIGN_URIS
    unset QCI_CLEANUP_ALLOW_UNREACHABLE_URI
    # shellcheck disable=SC2329
    log() { :; }
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/vm.sh"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/cleanup.sh"
}

teardown() {
    # A test that fails an assertion never reaches _release_storage_lock.
    if [ -n "${LOCK_HOLDER_PID:-}" ]; then
        kill "$LOCK_HOLDER_PID" 2>/dev/null || true
        wait "$LOCK_HOLDER_PID" 2>/dev/null || true
    fi
    # A test may leave a deliberately unsearchable directory behind; restore
    # mode first so teardown itself can never fail.
    chmod -R u+rwX "$TMP" 2>/dev/null || true
    rm -rf "$TMP"
}

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

# SPLIT from one loop into three cases. The loop shared a single body and, when
# the cdrom fixture changed to an EXISTING iso, the shared
# `global storage ownership audit incomplete` assertion was dropped for all
# three rows at once -- silently weakening the pooled and block cases, which
# still depend on it. tests/AGENTS.md forbids dropping an assertion like that,
# so each row now carries the assertion its own classification warrants.

@test "cleanup preserves a domain with a pooled volume and reports the audit incomplete" {
    FIRMWARE=uefi; export FIRMWARE
    # A `volume` row names a pool volume this audit cannot resolve to a path,
    # so the PRIMARY audit must fail closed rather than treat it as no storage.
    EXTRA_STORAGE_ROWS='volume disk vdb pool/volume\n'; export EXTRA_STORAGE_ROWS
    : > "$CALLS"
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    [ "$(grep -c '^destroy qci-cleanup-test$' "$CALLS" || true)" -eq 0 ]
    [ "$(grep -c '^undefine qci-cleanup-test ' "$CALLS" || true)" -eq 0 ]
    grep -q 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log"
}

@test "cleanup preserves a domain with block-device storage and reports the audit incomplete" {
    FIRMWARE=uefi; export FIRMWARE
    EXTRA_STORAGE_ROWS='block disk vdc /dev/mapper/qci-test\n'; export EXTRA_STORAGE_ROWS
    : > "$CALLS"
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    [ "$(grep -c '^destroy qci-cleanup-test$' "$CALLS" || true)" -eq 0 ]
    [ "$(grep -c '^undefine qci-cleanup-test ' "$CALLS" || true)" -eq 0 ]
    grep -q 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log"
}

@test "cleanup classifies a file cdrom with an existing iso without spoiling the audit" {
    FIRMWARE=uefi; export FIRMWARE
    # The ISO must EXIST, so this row exercises cdrom-ness rather than
    # incidentally exercising the dangling-path branch. Unlike the two rows
    # above this one IS resolvable, so the audit stays complete -- asserted
    # explicitly rather than left unstated.
    local iso="$TMP/installer.iso"; printf iso > "$iso"
    EXTRA_STORAGE_ROWS="file cdrom sda $iso\n"; export EXTRA_STORAGE_ROWS
    : > "$CALLS"
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    [ "$(grep -c '^destroy qci-cleanup-test$' "$CALLS" || true)" -eq 0 ]
    [ "$(grep -c '^undefine qci-cleanup-test ' "$CALLS" || true)" -eq 0 ]
    [ "$(grep -c 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

# A domain that declares a disk which no longer exists (the host's
# `qdistro-template` after its qcow2 was removed) used to abort the WHOLE
# ownership audit, so every stale qci VM and golden was retained forever
# (~80 GiB, cleanup-20260914T193943Z-9547). It is now a named, non-fatal
# finding and reclamation proceeds.
@test "cleanup reports a dangling domain disk without aborting the audit" {
    FIRMWARE=uefi; export FIRMWARE
    EXTRA_STORAGE_ROWS="file disk vdb $QDWIN_IMG_DIR/qci-gone.qcow2\n"
    export EXTRA_STORAGE_ROWS
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    grep -Fq "finding: domain qci-cleanup-test (inactive) declares a disk that does not exist: $QDWIN_IMG_DIR/qci-gone.qcow2" \
        "$RDIR/host/cleanup.log"
    # Reported once per owner/view/path, however often the audit re-runs.
    [ "$(grep -c 'declares a disk that does not exist' "$RDIR/host/cleanup.log")" -eq 2 ]
    # The audit COMPLETED: no domain or orphan was held back on its account.
    [ "$(grep -c 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    # The row itself carries the finding count, so it cannot go unnoticed.
    [[ "$output" == *"missing_disk_refs=2"* ]]
}

# An UNRESOLVABLE path that DOES exist (symlink loop, unreadable parent) is not
# the dangling case and must still fail the audit closed.
@test "cleanup still fails closed on an existing but unresolvable disk path" {
    FIRMWARE=uefi; export FIRMWARE
    ln -s "$TMP/loop-b" "$TMP/loop-a"
    ln -s "$TMP/loop-a" "$TMP/loop-b"
    EXTRA_STORAGE_ROWS="file disk vdb $TMP/loop-a\n"; export EXTRA_STORAGE_ROWS
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    grep -q 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log"
}

# ROUND-1 REGRESSION GUARD (destructive). `readlink -e` failing does NOT prove
# ENOENT, and neither does `[ ! -e ] && [ ! -L ]`: an ancestor without search
# permission makes BOTH predicates false for a path that exists. Here an
# unreadable parent hides a symlink that resolves to a real, old, qci-named
# image owned by another domain. If the audit calls that "missing" it records
# the row under a path matching nothing, the orphan sweep sees no owner, and
# safe_rm_overlay unlinks the human's image. The symlink-loop test above does
# NOT cover this, because there `-L` identifies the loop entry.
@test "unreadable parent hiding an alias fails the audit closed and deletes nothing" {
    [ "$(id -u)" -ne 0 ] || skip "root bypasses directory search permission"
    local hidden="$QDWIN_IMG_DIR/qci-human.qcow2"
    local priv="$TMP/private"
    printf human > "$hidden"
    touch -d '2 hours ago' "$hidden"
    mkdir -p "$priv"
    ln -s "$hidden" "$priv/disk-link"
    chmod 000 "$priv"
    # Prove the premise: neither predicate can see the path that exists.
    [ ! -e "$priv/disk-link" ]
    [ ! -L "$priv/disk-link" ]

    printf 'other-domain\n' > "$DOMAIN"
    OTHER_DISK="$priv/disk-link"
    export OTHER_DISK

    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    # The hidden alias target and every other qci image survive.
    [ -e "$hidden" ]
    [ -e "$DISK" ]
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -q 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log"
    grep -Fq "path cannot be canonicalized: $priv/disk-link" "$RDIR/host/cleanup.log"
    # It must NOT be misreported as a dangling/missing disk.
    [ "$(grep -c 'declares a disk that does not exist' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

# The same shape without any alias behind it: an unsearchable parent alone is
# enough to keep the audit fail-closed, because absence is not demonstrable.
@test "unsearchable parent alone keeps the audit fail-closed" {
    [ "$(id -u)" -ne 0 ] || skip "root bypasses directory search permission"
    local priv="$TMP/private"
    mkdir -p "$priv"
    chmod 000 "$priv"
    FIRMWARE=uefi
    EXTRA_STORAGE_ROWS="file disk vdb $priv/qci-gone.qcow2\n"
    export FIRMWARE EXTRA_STORAGE_ROWS
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    grep -q 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log"
    [ "$(grep -c 'declares a disk that does not exist' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

# The relaxation stores the PROSPECTIVE CANONICAL path, not the raw declared
# one, so a file later created at a declared path reached through a symlinked
# ancestor still reads as owned by that domain.
@test "dangling disk under a symlinked parent is recorded at its canonical path" {
    ln -s "$QDWIN_IMG_DIR" "$TMP/images-alias"
    printf 'other-domain\n' > "$DOMAIN"
    OTHER_DISK="$TMP/images-alias/qci-gone.qcow2"
    export OTHER_DISK

    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ "$(grep -c 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -Fq "declares a disk that does not exist: $TMP/images-alias/qci-gone.qcow2" \
        "$RDIR/host/cleanup.log"
    # Column 7 (canonical) is the resolved parent plus the basename.
    awk -F '\t' -v p="$QDWIN_IMG_DIR/qci-gone.qcow2" \
        '$7==p {found=1} END {exit !found}' \
        "$RDIR/host/cleanup-storage-inventory.tsv"
}

# Absence must be demonstrable within a canonical, searchable parent; `.`/`..`
# name the directories themselves, so the prospective path is ambiguous.
@test "absent-path helper rejects ambiguous and unprovable references" {
    local ok
    refute_absent() {
        if cleanup_absent_canonical_path "$1" >/dev/null 2>&1; then return 1; fi
        return 0
    }
    ok=$(cleanup_absent_canonical_path "$QDWIN_IMG_DIR/qci-not-there.qcow2")
    [ "$ok" = "$QDWIN_IMG_DIR/qci-not-there.qcow2" ]
    # Existing file: not absent.
    refute_absent "$DISK"
    # Dangling symlink: exists as a link, so absence of the NAME is false.
    ln -s "$TMP/nowhere" "$TMP/dangler"
    refute_absent "$TMP/dangler"
    # Parent does not canonicalize.
    refute_absent "$TMP/no-such-dir/qci-x.qcow2"
    # `.`/`..` basenames are ambiguous, never relaxed.
    refute_absent "$TMP/no-such-dir/.."
    refute_absent "$QDWIN_IMG_DIR/."
    # Parent is a file, not a directory.
    refute_absent "$DISK/qci-x.qcow2"
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

# ---------------------------------------------------------------------------
# ROUND-3 REGRESSION GUARDS (all three are destructive-path guards).
# ---------------------------------------------------------------------------

# DEFECT 1 (destructive). A canonical, searchable parent narrows the relaxation
# but does not prove ENOENT for the FINAL component: `[ -e ]` and `[ -L ]` are
# both false for EVERY lookup failure. Here the lookup fails with ENAMETOOLONG
# -- a name longer than NAME_MAX inside a perfectly good directory. The old
# predicate reads that as "the disk is missing", completes the audit, and goes
# on to destroy/undefine the domain and unlink its real overlay. Only an errno
# test can tell "not there" from "could not tell".
@test "final-component lookup error is not absence and keeps the audit closed" {
    FIRMWARE=uefi
    local long
    long=$(printf 'q%.0s' $(seq 1 300))
    # Premise: the shell predicates cannot distinguish this from a real ENOENT.
    [ ! -e "$QDWIN_IMG_DIR/$long" ]
    [ ! -L "$QDWIN_IMG_DIR/$long" ]
    # ...but a real lstat() reports a non-ENOENT errno.
    [ "$(qci_name_lookup_state "$QDWIN_IMG_DIR" "$long")" = error ]

    EXTRA_STORAGE_ROWS="file disk vdb $QDWIN_IMG_DIR/$long\n"
    export FIRMWARE EXTRA_STORAGE_ROWS
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    # Nothing was destroyed, undefined or unlinked.
    [ -s "$DOMAIN" ]
    [ -e "$DISK" ]
    [ "$(grep -c '^destroy qci-cleanup-test$' "$CALLS" || true)" -eq 0 ]
    [ "$(grep -c '^undefine qci-cleanup-test ' "$CALLS" || true)" -eq 0 ]
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -q 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log"
    # And it is NOT laundered into the benign dangling-disk finding.
    [ "$(grep -c 'declares a disk that does not exist' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

# The same helper must also be immune to the parent being REPLACED between the
# caller's `-d`/`-x` checks and the final lookup: the lookup runs against an
# O_DIRECTORY|O_NOFOLLOW handle, so a parent that has become a symlink is an
# error, never an absence.
@test "name lookup refuses a symlinked parent instead of reporting absence" {
    mkdir -p "$TMP/realdir"
    ln -s "$TMP/realdir" "$TMP/swapped"
    # The shell predicate happily calls the name absent through the symlink.
    [ ! -e "$TMP/swapped/qci-x.qcow2" ]
    [ "$(qci_name_lookup_state "$TMP/swapped" "qci-x.qcow2")" = error ]
    [ "$(qci_name_lookup_state "$TMP/realdir" "qci-x.qcow2")" = absent ]
    printf x > "$TMP/realdir/qci-x.qcow2"
    [ "$(qci_name_lookup_state "$TMP/realdir" "qci-x.qcow2")" = present ]
}

# DEFECT 2 (destructive). Re-auditing before the unlink is not TOCTOU coverage:
# a worker can attach the candidate, or create a child of it, after the refresh
# returns and before safe_rm_overlay runs. Cleanup now holds an EXCLUSIVE flock
# on $QDWIN_IMG_DIR/.qci-storage.lock across final-audit -> unlink, and
# scripts/vm/clone-baseweed.sh holds it SHARED across `qemu-img create -b` ..
# `virsh define`. A worker inside that window must make cleanup KEEP the disk.
@test "a worker holding the storage lock blocks the unlink instead of racing it" {
    FIRMWARE=uefi
    QCI_CLEANUP_LOCK_WAIT=1
    export FIRMWARE QCI_CLEANUP_LOCK_WAIT
    local holder
    # Stand in for clone-baseweed.sh's create..define window.
    flock -s "$QDWIN_IMG_DIR/.qci-storage.lock" -c 'sleep 6' >/dev/null 2>&1 3>&- &
    holder=$!
    # Wait until the shared lock is actually held before starting cleanup.
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        flock -w 0 -x "$QDWIN_IMG_DIR/.qci-storage.lock" -c true >/dev/null 2>&1 || break
        sleep 0.2
    done

    run gate_cleanup --age-hours 1
    kill "$holder" 2>/dev/null || true
    wait "$holder" 2>/dev/null || true

    [ "$status" -eq 90 ]
    [ -e "$DISK" ]
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -Fq "storage lock not acquired within 1s" "$RDIR/host/cleanup.log"
}

# DEFECT 3 (destructive, untouched by rounds 1 and 2). The referrer scan only
# ever looked at $QDWIN_IMG_DIR/qci-*.qcow2. A human-named or otherwise
# non-qci-named qcow2 backing onto a qci-named candidate was invisible, so the
# orphan sweep unlinked a LIVE backing file and corrupted the human's image.
@test "orphan sweep preserves a candidate backing a non-qci-named qcow2" {
    local kept="$QDWIN_IMG_DIR/kept.qcow2"
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 -F qcow2 -b "$DISK" "$kept"
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    touch -d '2 hours ago' "$DISK" "$kept"
    : > "$DOMAIN"

    [ "$(backing_referrer_state "$DISK")" = referred ]
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -e "$DISK" ]
    [ -e "$kept" ]
    # The human's image still opens: its backing file was not removed.
    "$REAL_QEMU_IMG" check "$kept" >/dev/null
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -Fq "keep orphan qci-cleanup-test.qcow2 (backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
}

# ...and the referrer's link to the candidate may be several levels down, with
# the intermediate living OUTSIDE the images directory. An immediate-parent
# scan -- even a widened one -- reads `clear` here and deletes the candidate.
@test "orphan sweep walks the whole chain, not just the immediate parent" {
    local kept="$QDWIN_IMG_DIR/kept.qcow2" mid="$TMP/outside-mid.qcow2"
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 -F qcow2 -b "$DISK" "$mid"
    "$REAL_QEMU_IMG" create -q -f qcow2 -F qcow2 -b "$mid" "$kept"
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    touch -d '2 hours ago' "$DISK" "$kept" "$mid"
    : > "$DOMAIN"

    [ "$(backing_referrer_state "$DISK")" = referred ]
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -e "$DISK" ]
    "$REAL_QEMU_IMG" check "$kept" >/dev/null
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -Fq "keep orphan qci-cleanup-test.qcow2 (backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
}

# A referrer libvirt declares but that lives outside the images directory is
# reachable only through the inventory, which cleanup publishes to
# backing_referrer_state as BACKING_REFERRER_EXTRA_LIST.
@test "orphan sweep preserves a candidate backing a domain disk outside the images dir" {
    local outside="$TMP/outside-kept.qcow2"
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 -F qcow2 -b "$DISK" "$outside"
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    touch -d '2 hours ago' "$DISK" "$outside"
    printf 'other-domain\n' > "$DOMAIN"
    printf '%s\n' "$outside" > "$OTHER_DISK_FILE"

    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -e "$DISK" ]
    "$REAL_QEMU_IMG" check "$outside" >/dev/null
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -Fq "keep orphan qci-cleanup-test.qcow2 (backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
}

# An images-directory entry that neither resolves nor is demonstrably absent
# (a symlink whose target hides behind an unsearchable directory) could name a
# real referrer, so the audit reports `unknown` and the candidate is kept.
@test "unresolvable images-dir entry makes the referrer audit unknown not clear" {
    [ "$(id -u)" -ne 0 ] || skip "root bypasses directory search permission"
    local priv="$TMP/private"
    mkdir -p "$priv"
    printf hidden > "$priv/hidden.qcow2"
    ln -s "$priv/hidden.qcow2" "$QDWIN_IMG_DIR/maybe-referrer.qcow2"
    chmod 000 "$priv"
    [ ! -e "$QDWIN_IMG_DIR/maybe-referrer.qcow2" ]

    [ "$(backing_referrer_state "$DISK")" = unknown ]
    : > "$DOMAIN"
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -e "$DISK" ]
    grep -Fq "keep orphan qci-cleanup-test.qcow2 (backing-referrer audit: unknown)" \
        "$RDIR/host/cleanup.log"
}

# ---------------------------------------------------------------------------
# ROUND-3 guards for wrong-deletion routes found by re-reading safe_rm_overlay
# and the orphan sweep (not named by any review round).
# ---------------------------------------------------------------------------

# The orphan sweep audits $f_canonical but hands $f to safe_rm_overlay. For a
# qci-named SYMLINK in the images directory those are different inodes: the
# audit blesses the target and the unlink removes the human's link (and, with a
# less strict safe_rm_overlay, could reach outside the images directory).
@test "orphan sweep refuses an images-dir entry whose audited path is not the unlink path" {
    local real="$TMP/human-image.qcow2" link="$QDWIN_IMG_DIR/qci-linked.qcow2"
    printf real > "$real"
    ln -s "$real" "$link"
    touch -h -d '2 hours ago' "$link"
    touch -d '2 hours ago' "$real"
    : > "$DOMAIN"

    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -L "$link" ]
    [ -e "$real" ]
    grep -Fq "keep qci-linked.qcow2 (images-dir entry is a symlink" "$RDIR/host/cleanup.log"
    # Belt and braces: the primitive itself refuses a symlink.
    ! safe_rm_overlay "$link"
    [ -L "$link" ]
}

# --age-hours is the only age floor between a live image and the unlink, and it
# is evaluated inside $(( )). A non-numeric value is an arithmetic hazard and 0
# makes every image old enough; both are refused rather than defaulted.
@test "cleanup refuses a non-numeric or zero age floor instead of defaulting" {
    local arg
    for arg in "" 0 abc "1 + 999999" -3; do
        : > "$CALLS"
        run gate_cleanup --age-hours "$arg"
        [ "$status" -eq 2 ]
        [ -e "$DISK" ]
        [ "$(grep -c '^destroy ' "$CALLS" || true)" -eq 0 ]
    done
    # A valid floor still works.
    FIRMWARE=uefi; export FIRMWARE
    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ ! -e "$DISK" ]
}

# The ownership audit only ever asks ONE libvirt connection. A domain defined
# under another URI that attaches an image in this images directory makes the
# candidate look unowned; cleanup must keep everything instead.
@test "a foreign libvirt connection claiming our images dir fails the audit closed" {
    cat > "$BIN/foreign-virsh" <<'SH'
#!/usr/bin/env bash
# args: -c <uri> <cmd> ...
case "$3" in
    list) printf 'system-guest\n' ;;
    domblklist) printf 'file disk vda %s\n' "$FOREIGN_DISK" ;;
esac
SH
    chmod +x "$BIN/foreign-virsh"
    # Shadow `virsh` for the foreign probe only (it is invoked as bare `virsh`).
    cp "$BIN/virsh" "$BIN/virsh.real"
    cat > "$BIN/virsh" <<'SH'
#!/usr/bin/env bash
if [ "$1" = "-c" ]; then exec "$(dirname "$0")/foreign-virsh" "$@"; fi
exec "$(dirname "$0")/virsh.real" "$@"
SH
    chmod +x "$BIN/virsh"
    FOREIGN_DISK="$QDWIN_IMG_DIR/qci-cleanup-test.qcow2"
    QCI_CLEANUP_FOREIGN_URIS="qemu:///system"
    export FOREIGN_DISK QCI_CLEANUP_FOREIGN_URIS
    FIRMWARE=uefi; export FIRMWARE

    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -e "$DISK" ]
    [ -s "$DOMAIN" ]
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -Fq "finding: qemu:///system domain system-guest attaches $DISK inside this cleanup's images directory" \
        "$RDIR/host/cleanup.log"
    grep -q 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log"
}

# --- ROUND 5 destructive defects 1-4 ----------------------------------------

# Helper: shadow `virsh -c <uri>` with a scripted foreign connection while the
# session connection keeps using the real stub.
_foreign_stub() {
    cp "$BIN/virsh" "$BIN/virsh.session"
    cat > "$BIN/foreign-virsh" <<'SH'
#!/usr/bin/env bash
# args: -c <uri> <cmd> ...
[ "${FOREIGN_UNREACHABLE:-0}" = 0 ] || exit 1
case "$3" in
    list)
        # FOREIGN_APPEAR_AFTER models an owner that becomes visible only on a
        # LATER query: the domain exists, but the first (pre-lock) audit does
        # not see it. Any under-lock re-query must.
        if [ -n "${FOREIGN_LIST_COUNT_FILE:-}" ]; then
            n=$(( $(cat "$FOREIGN_LIST_COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
            printf '%s' "$n" > "$FOREIGN_LIST_COUNT_FILE"
            [ "$n" -gt "${FOREIGN_APPEAR_AFTER:-0}" ] || exit 0
        fi
        printf '%s\n' "${FOREIGN_DOMAIN:-}" ;;
    domblklist)
        # `--inactive` anywhere in the args selects the persistent view, so a
        # test can make the two views differ (see the single-empty-view case).
        case "$*" in
            *--inactive*)
                # Mirrors the live branch unless a test overrides it: a real
                # persistent config does not silently lose the disks the
                # current XML has.
                if [ -n "${FOREIGN_ROW_INACTIVE+set}" ]; then
                    printf '%b\n' "$FOREIGN_ROW_INACTIVE"
                elif [ -n "${FOREIGN_ROW:-}" ]; then
                    printf '%b\n' "$FOREIGN_ROW"
                else
                    [ -z "${FOREIGN_DISK:-}" ] || printf 'file disk vda %s\n' "$FOREIGN_DISK"
                fi ;;
            *) if [ -n "${FOREIGN_ROW:-}" ]; then
                   printf '%b\n' "$FOREIGN_ROW"
               else
                   [ -z "${FOREIGN_DISK:-}" ] || printf 'file disk vda %s\n' "$FOREIGN_DISK"
               fi ;;
        esac ;;
esac
SH
    chmod +x "$BIN/foreign-virsh"
    cat > "$BIN/virsh" <<'SH'
#!/usr/bin/env bash
if [ "$1" = "-c" ]; then exec "$(dirname "$0")/foreign-virsh" "$@"; fi
exec "$(dirname "$0")/virsh.session" "$@"
SH
    chmod +x "$BIN/virsh"
    export FOREIGN_UNREACHABLE FOREIGN_DOMAIN FOREIGN_DISK
    export FOREIGN_ROW FOREIGN_ROW_INACTIVE FOREIGN_APPEAR_AFTER FOREIGN_LIST_COUNT_FILE
    QCI_CLEANUP_FOREIGN_URIS="qemu:///system"
    export QCI_CLEANUP_FOREIGN_URIS
}

# Hold the storage lock from a separate process until _release_storage_lock.
# `flock -c CMD` is unusable here because CMD inherits the locked descriptor and
# survives killing flock itself; this holder keeps the descriptor in ITS OWN
# process only, so the lock is demonstrably free once the holder is reaped.
_hold_storage_lock() {
    local mode=$1 i
    cat > "$TMP/hold-lock.sh" <<'SH'
#!/usr/bin/env bash
exec 9>>"$1"
flock "$2" 9 || exit 1
: > "$1.held"
while :; do sleep 0.1 9>&-; done
SH
    chmod +x "$TMP/hold-lock.sh"
    rm -f "$QDWIN_IMG_DIR/.qci-storage.lock.held"
    # The /dev/null and 3>&- redirections matter: a holder that inherits bats'
    # output descriptors keeps the whole run alive if an assertion fails before
    # _release_storage_lock (teardown reaps it, but bats waits on the fd).
    "$TMP/hold-lock.sh" "$QDWIN_IMG_DIR/.qci-storage.lock" "$mode" \
        >/dev/null 2>&1 3>&- &
    LOCK_HOLDER_PID=$!
    for i in $(seq 1 100); do
        [ -e "$QDWIN_IMG_DIR/.qci-storage.lock.held" ] && return 0
        sleep 0.05
    done
    return 1
}

_release_storage_lock() {
    local i
    [ -n "${LOCK_HOLDER_PID:-}" ] || return 0
    kill "$LOCK_HOLDER_PID" 2>/dev/null || true
    wait "$LOCK_HOLDER_PID" 2>/dev/null || true
    LOCK_HOLDER_PID=""
    for i in $(seq 1 100); do
        flock -w 0 -x "$QDWIN_IMG_DIR/.qci-storage.lock" -c true >/dev/null 2>&1 \
            && return 0
        sleep 0.05
    done
    return 1
}

# DEFECT 1 (destructive, CRITICAL). Round 3 logged a note for a foreign URI it
# could not reach and returned SUCCESS, leaving audit_complete=1. The commonest
# real configuration -- a session-user qci on a host whose qemu:///system needs
# privileges the session user lacks -- is precisely the unreachable case, so a
# live system-libvirt domain's disk could be unlinked by the orphan sweep.
@test "an unreachable foreign libvirt connection fails the audit closed" {
    FIRMWARE=uefi; export FIRMWARE
    FOREIGN_UNREACHABLE=1
    _foreign_stub

    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -e "$DISK" ]
    [ -s "$DOMAIN" ]
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -Fq "finding: ownership audit could not reach qemu:///system" \
        "$RDIR/host/cleanup.log"
    grep -q 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log"
    # The log must tell the human exactly how to make the decision knowingly.
    grep -Fq "QCI_CLEANUP_ALLOW_UNREACHABLE_URI='qemu:///system=" \
        "$RDIR/host/cleanup.log"
}

# The opt-out is explicit, names the exact URI, and is dated. Today's stamp
# works -- and the log still records the deletion risk that was accepted.
@test "a dated operator excuse lets an unreachable foreign URI proceed" {
    FIRMWARE=uefi; export FIRMWARE
    FOREIGN_UNREACHABLE=1
    _foreign_stub
    QCI_CLEANUP_ALLOW_UNREACHABLE_URI="qemu:///system=$(date +%F)"
    export QCI_CLEANUP_ALLOW_UNREACHABLE_URI

    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ ! -e "$DISK" ]
    grep -Fq "EXCUSED by QCI_CLEANUP_ALLOW_UNREACHABLE_URI" "$RDIR/host/cleanup.log"
    grep -Fq "this run may delete it" "$RDIR/host/cleanup.log"
}

# ...and it EXPIRES, so it cannot be exported once and forgotten.
@test "an expired excuse does not unblock an unreachable foreign URI" {
    FIRMWARE=uefi; export FIRMWARE
    FOREIGN_UNREACHABLE=1
    _foreign_stub
    QCI_CLEANUP_ALLOW_UNREACHABLE_URI="qemu:///system=$(date -d '8 days ago' +%F)"
    export QCI_CLEANUP_ALLOW_UNREACHABLE_URI

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -Fq "finding: ownership audit could not reach qemu:///system" \
        "$RDIR/host/cleanup.log"
}

# A future stamp is a pre-armed permanent bypass; refuse it.
@test "a future-dated excuse does not unblock an unreachable foreign URI" {
    FIRMWARE=uefi; export FIRMWARE
    FOREIGN_UNREACHABLE=1
    _foreign_stub
    QCI_CLEANUP_ALLOW_UNREACHABLE_URI="qemu:///system=$(date -d '30 days' +%F)"
    export QCI_CLEANUP_ALLOW_UNREACHABLE_URI

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    grep -Fq "finding: ownership audit could not reach qemu:///system" \
        "$RDIR/host/cleanup.log"
}

# The excuse is per-URI and must carry a date; neither a boolean nor an excuse
# for some other connection counts.
@test "an undated or mismatched excuse does not unblock an unreachable foreign URI" {
    FIRMWARE=uefi; export FIRMWARE
    FOREIGN_UNREACHABLE=1
    _foreign_stub
    local v
    for v in "qemu:///system" "1" "qemu+ssh://elsewhere/system=$(date +%F)"; do
        : > "$RDIR/host/cleanup.log"
        QCI_CLEANUP_ALLOW_UNREACHABLE_URI="$v"
        export QCI_CLEANUP_ALLOW_UNREACHABLE_URI
        run gate_cleanup --age-hours 1
        [ -e "$DISK" ]
        grep -Fq "finding: ownership audit could not reach qemu:///system" \
            "$RDIR/host/cleanup.log"
    done
}

# Emptying the URI list was the OTHER way to silently switch the foreign audit
# off and forget; it is now fail-closed under the same dated excuse.
@test "an empty foreign-URI list fails the audit closed" {
    FIRMWARE=uefi; export FIRMWARE
    QCI_CLEANUP_FOREIGN_URIS=""
    export QCI_CLEANUP_FOREIGN_URIS

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -Fq "finding: QCI_CLEANUP_FOREIGN_URIS is empty" "$RDIR/host/cleanup.log"

    : > "$RDIR/host/cleanup.log"
    QCI_CLEANUP_ALLOW_UNREACHABLE_URI="none=$(date +%F)"
    export QCI_CLEANUP_ALLOW_UNREACHABLE_URI
    run gate_cleanup --age-hours 1
    [ ! -e "$DISK" ]
    grep -Fq "foreign-URI ownership audit DISABLED" "$RDIR/host/cleanup.log"
}

# DEFECT 2 (destructive, CRITICAL). The reachable foreign URI was checked only
# for a DIRECTLY attached path under our images directory. A foreign domain
# attached to a child that lives elsewhere but whose backing file IS our
# candidate saw no conflict, and the foreign path was never published into
# BACKING_REFERRER_EXTRA_LIST, so the chain was never walked and the orphan
# sweep unlinked a live backing file.
@test "a foreign domain's out-of-tree child protects its backing in our images dir" {
    local outside="$TMP/foreign-child.qcow2"
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 -F qcow2 -b "$DISK" "$outside"
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    touch -d '2 hours ago' "$DISK" "$outside"
    : > "$DOMAIN"
    FOREIGN_UNREACHABLE=0
    FOREIGN_DOMAIN=system-guest
    FOREIGN_DISK="$outside"
    _foreign_stub

    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -e "$DISK" ]
    "$REAL_QEMU_IMG" check "$outside" >/dev/null
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
    grep -Fq "keep orphan qci-cleanup-test.qcow2 (backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
    # The foreign path really was published as a referrer root.
    grep -Fxq "$outside" "$RDIR/host/cleanup-referrer-paths.txt"
}

# A failure to publish the referrer list must not read as an empty one.
@test "an unpublishable referrer list makes the audit incomplete" {
    FIRMWARE=uefi; export FIRMWARE
    # Make the destination directory unwritable for the list only.
    local hostdir="$RDIR/host"
    run gate_cleanup --age-hours 1 --dry-run
    [ -f "$hostdir/cleanup-referrer-paths.txt" ]
    : > "$RDIR/host/cleanup.log"
    [ "$(id -u)" -ne 0 ] || skip "root bypasses directory write permission"
    mkdir -p "$hostdir/cleanup-referrer-paths.txt.d"
    # A directory at the temp-file path makes the atomic write fail.
    mkdir -p "$hostdir/cleanup-referrer-paths.txt.tmp"
    run gate_cleanup --age-hours 1
    rmdir "$hostdir/cleanup-referrer-paths.txt.tmp" 2>/dev/null || true
    [ -e "$DISK" ]
    grep -Fq "could not publish the backing-referrer path list" "$RDIR/host/cleanup.log"
    grep -q 'global storage ownership audit incomplete' "$RDIR/host/cleanup.log"
}

# DEFECT 4 (destructive). cleanup_run_goldens did referrer-check -> unlink with
# NO lock, so a worker inside clone-baseweed.sh's create..define window could
# have its backing golden deleted underneath it. The referrer audit and the
# unlink must be one critical section, and an unavailable lock must PRESERVE.
@test "run-golden reclaim takes the storage lock and preserves when it cannot" {
    local golden="$QDWIN_IMG_DIR/qci-golden-test.qcow2"
    printf golden > "$golden"
    RUN_GOLDEN_DISKS=("$golden")
    GOLDEN_PRESERVE=0
    mkdir -p "$RDIR/vm"
    QCI_CLEANUP_LOCK_WAIT=1; export QCI_CLEANUP_LOCK_WAIT

    _hold_storage_lock -s

    run cleanup_run_goldens
    _release_storage_lock

    [ -e "$golden" ]
    grep -Fq "preserved_golden_disk=$golden" "$RDIR/manifest.txt"

    # With the lock free, the same call reclaims it.
    run cleanup_run_goldens
    [ ! -e "$golden" ]
}

# Every OTHER deletion route (release_vm's leaked-overlay reclaim,
# reap_new_orphans, the golden-build failure paths, abort_run's in-flight
# golden reap) reaches the unlink through safe_rm_overlay, so the protocol is
# enforced there rather than at each call site.
@test "safe_rm_overlay refuses to unlink when the storage lock cannot be taken" {
    local victim="$QDWIN_IMG_DIR/qci-victim.qcow2"
    printf v > "$victim"
    QCI_CLEANUP_LOCK_WAIT=1; export QCI_CLEANUP_LOCK_WAIT

    _hold_storage_lock -x

    run safe_rm_overlay "$victim"
    [ "$status" -ne 0 ]
    [ -e "$victim" ]

    _release_storage_lock
    run safe_rm_overlay "$victim"
    [ "$status" -eq 0 ]
    [ ! -e "$victim" ]
}

# The lock is re-entrant by depth: the cleanup gate holds it across its whole
# critical section and safe_rm_overlay's nested acquisition must not deadlock,
# nor release the outer holder's descriptor early.
@test "the storage lock is re-entrant and released only at depth zero" {
    local victim="$QDWIN_IMG_DIR/qci-nested.qcow2"
    printf v > "$victim"
    QCI_CLEANUP_LOCK_WAIT=2; export QCI_CLEANUP_LOCK_WAIT

    qci_storage_lock_acquire 2
    [ "$QCI_STORAGE_LOCK_DEPTH" -eq 1 ]
    safe_rm_overlay "$victim"
    [ ! -e "$victim" ]
    # safe_rm_overlay nested and unwound; the OUTER hold must survive.
    [ "$QCI_STORAGE_LOCK_DEPTH" -eq 1 ]
    [ -n "$QCI_STORAGE_LOCK_FD" ]
    run flock -w 0 -x "$QDWIN_IMG_DIR/.qci-storage.lock" -c true
    # Same process, different open file description: still held exclusively.
    # ASSERT it. Without this the competing flock's status was silently
    # discarded by the next `run`, and the test proved only that two shell
    # variables had the values it had just set -- never that anyone was
    # actually excluded.
    [ "$status" -ne 0 ]
    qci_storage_lock_release
    [ "$QCI_STORAGE_LOCK_DEPTH" -eq 0 ]
    [ -z "$QCI_STORAGE_LOCK_FD" ]
    run flock -w 0 -x "$QDWIN_IMG_DIR/.qci-storage.lock" -c true
    [ "$status" -eq 0 ]
}


# DEFECT 3 (destructive, CRITICAL). The COOPERATING WRITER was fail-open: if
# flock was missing, the lock file could not be opened, or `flock -s` failed,
# clone-baseweed.sh merely warned and proceeded through `qemu-img create -b`
# and `virsh define` -- while a concurrent cleanup held what it believed was an
# exclusive protocol lock and could unlink either the new child or its backing.
# A safety protocol works only if BOTH sides fail closed.
_clone_harness() {
    printf backing > "$QDWIN_IMG_DIR/baseweed.qcow2"
    printf backing > "$QDWIN_IMG_DIR/baseweed-baked.qcow2"
    cat > "$BIN/virsh" <<'SH'
#!/usr/bin/env bash
case "$*" in
    *"dominfo qdistro-template"*) exit 0 ;;
    *dominfo*) exit 1 ;;
    *define*)  printf 'defined\n'; exit 0 ;;
    *start*)   exit 0 ;;
    *dumpxml*) printf '<domain type="kvm"><name>t</name></domain>\n'; exit 0 ;;
esac
exit 0
SH
    chmod +x "$BIN/virsh"
    export QDWIN_IMG_DIR
}

@test "clone refuses to proceed when the shared storage lock is held exclusively" {
    _clone_harness
    _hold_storage_lock -x
    export QCI_CLONE_LOCK_WAIT=1
    PATH="$BIN:$PATH" run "$REPO_ROOT/scripts/vm/clone-baseweed.sh" \
        qci-locktest --from-baked
    _release_storage_lock
    [ "$status" -ne 0 ]
    printf '%s\n' "$output" | grep -Fq "shared qci storage lock not acquired within 1s"
    # Nothing was created: no overlay landed in the images directory.
    [ -z "$(ls "$QDWIN_IMG_DIR"/qci-locktest-* 2>/dev/null || true)" ]
}

@test "clone refuses to proceed when flock(1) is unavailable" {
    _clone_harness
    # A PATH that contains everything EXCEPT flock. One `ln -s` per PATH
    # directory rather than one per executable: the per-file loop took ~40s.
    local nof="$TMP/nof" d
    mkdir -p "$nof"
    for d in ${PATH//:/ }; do
        [ -d "$d" ] || continue
        ln -s "$d"/* "$nof/" 2>/dev/null || true
    done
    rm -f "$nof/flock"
    [ ! -e "$nof/flock" ]
    PATH="$nof" command -v flock >/dev/null 2>&1 && skip "could not hide flock from PATH"
    PATH="$BIN:$nof" run "$REPO_ROOT/scripts/vm/clone-baseweed.sh" qci-noflock --from-baked
    [ "$status" -ne 0 ]
    printf '%s\n' "$output" | grep -Fq "flock(1) is required to clone safely"
    [ -z "$(ls "$QDWIN_IMG_DIR"/qci-noflock-* 2>/dev/null || true)" ]
}

# ROUND 5 performance work introduced a sweep-spanning immediate-parent cache.
# A cache is only safe if a changed image invalidates its entry: if a referrer
# that was recorded as having NO backing file is later rebased ONTO the
# candidate, the audit must re-inspect it and keep the candidate. Trusting a
# stale entry here would unlink a live backing file.
@test "a referrer rebased after being cached is re-inspected, not trusted" {
    local kept="$QDWIN_IMG_DIR/kept.qcow2"
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 "$kept" 1M
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    touch -d '2 hours ago' "$DISK" "$kept"
    : > "$DOMAIN"

    # Warm the cache: kept.qcow2 has no backing file at all.
    run gate_cleanup --age-hours 1 --dry-run
    [ "$status" -eq 0 ]
    grep -Fq "would remove orphan overlay $DISK" "$RDIR/host/cleanup.log"

    # Now it backs onto the candidate.
    "$REAL_QEMU_IMG" rebase -u -F qcow2 -b "$DISK" "$kept"
    : > "$RDIR/host/cleanup.log"

    run gate_cleanup --age-hours 1
    [ "$status" -eq 0 ]
    [ -e "$DISK" ]
    "$REAL_QEMU_IMG" check "$kept" >/dev/null
    grep -Fq "keep orphan qci-cleanup-test.qcow2 (backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
}

# ROUND 8 review, destructive defect. The sweep-spanning parent cache is
# validated by a stat signature, and round 6 argued that a NONZERO sub-second
# ctime fraction proved the filesystem issues a distinct ctime per write. It
# does not: a clock whose real granularity is coarser than its printed format
# can quantise two writes into one nonzero tick. This test forces exactly that
# host -- a real, nonzero-fraction signature that does not move across two
# size-preserving in-place rebases -- with a `stat` shim, so it reproduces
# deterministically instead of waiting for a cooperative clock.
#
# The point is NOT that the cache survives this; it does not, and the test
# asserts that it is fooled. The point is that being fooled can no longer
# delete anything, because the unlink is gated on an uncached audit taken
# under the exclusive storage lock.
@test "a same-tick rebase fools the cache and still does not delete" {
    local kept="$QDWIN_IMG_DIR/kept.qcow2"
    # Same basename LENGTH as the candidate, so the two header rewrites cannot
    # differ in file size even in principle.
    local decoy="$QDWIN_IMG_DIR/qci-cleanup-alt0.qcow2"
    local frozen size_before size_after
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 "$decoy" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 "$kept" 1M
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    export QCI_BACKING_PARENT_CACHE="$TMP/backing-cache.tsv"
    : > "$QCI_BACKING_PARENT_CACHE"

    # Write #1: kept backs onto something harmless.
    "$REAL_QEMU_IMG" rebase -q -u -F qcow2 -b "$decoy" "$kept"
    size_before=$(/usr/bin/stat -c %s "$kept")
    frozen=$(/usr/bin/stat -c '%d:%i:%s:%.9Y:%.9Z' "$kept")
    # This is the host codex named, not a coarse one: the ctime fraction is
    # present and nonzero, and round 6's gate would have accepted it.
    case "${frozen##*:}" in *.*[1-9]*) ;; *) skip "host ctime has no nonzero fraction" ;; esac

    # Stand in for a clock that quantises both writes into this one tick. Only
    # the signature format is intercepted; %Y and everything else is real.
    export FREEZE_PATH="$kept" FROZEN_SIG="$frozen"
    cat > "$BIN/stat" <<'SH'
#!/bin/sh
fmt=""
[ "$1" = "-c" ] && fmt=$2
case "$fmt" in
    *'%.9Z'*) ;;
    *) exec /usr/bin/stat "$@" ;;
esac
shift 2
[ "$1" = "--" ] && shift
for p in "$@"; do
    if [ "$p" = "$FREEZE_PATH" ]; then
        case "$fmt" in
            *'|%n'*) printf '%s|%s\n' "$FROZEN_SIG" "$p" ;;
            *) printf '%s' "$FROZEN_SIG" ;;
        esac
    else
        /usr/bin/stat -c "$fmt" -- "$p" 2>/dev/null || true
    fi
done
SH
    chmod +x "$BIN/stat"

    # The cached inspection. `$(...)` on purpose: that is how every caller
    # reads this state, and it is why the cache has to be file-backed at all.
    [ "$(backing_referrer_state "$DISK")" = clear ]

    # Write #2, onto the candidate, inside the same frozen tick.
    "$REAL_QEMU_IMG" rebase -q -u -F qcow2 -b "$DISK" "$kept"
    size_after=$(/usr/bin/stat -c %s "$kept")
    # Size-preserving, as the review specified.
    [ "$size_before" = "$size_after" ]
    # The signature really is unchanged, so the cache really is fooled ...
    [ "$(qci_backing_signature "$kept")" = "$frozen" ]
    _QCI_BACKING_SIG=()
    [ "$(backing_referrer_state "$DISK")" = clear ]

    # ... and the audit that authorises deletions is not.
    [ "$(backing_referrer_state_authoritative "$DISK")" = referred ]

    # End to end: the gate reaches the unlink through the fooled pre-scan and
    # is stopped by the authoritative audit under the lock.
    touch -d '2 hours ago' "$DISK"
    : > "$DOMAIN"
    : > "$RDIR/host/cleanup.log"
    run gate_cleanup --age-hours 1
    [ "$status" -eq "$EXIT_RUNNER" ]
    [ -e "$DISK" ]
    "$REAL_QEMU_IMG" check "$kept" >/dev/null
    grep -Fq "keep orphan qci-cleanup-test.qcow2 (final backing-referrer audit: referred)" \
        "$RDIR/host/cleanup.log"
}

# The mechanism the test above depends on, asserted directly: the authoritative
# audit must neither believe a cache entry nor add one. A poisoned entry that
# carries the file's CURRENT signature is the sharpest form of the round-8
# defect -- no signature scheme can reject it -- so the only safe design is to
# not consult it.
@test "the authoritative audit neither reads nor writes the parent cache" {
    local kept="$QDWIN_IMG_DIR/kept.qcow2" before after
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 "$kept" 1M
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    export QCI_BACKING_PARENT_CACHE="$TMP/backing-cache.tsv"

    # kept really does back onto the candidate ...
    "$REAL_QEMU_IMG" rebase -q -u -F qcow2 -b "$DISK" "$kept"
    # ... but the cache says it has no backing file, under the signature the
    # file has RIGHT NOW.
    _QCI_BACKING_SIG=()
    printf '%s\t%s\t%s\n' "$(qci_backing_signature "$kept")" "$kept" NONE \
        > "$QCI_BACKING_PARENT_CACHE"
    _QCI_BACKING_PARENT_CACHE_LOADED=""

    # The cached scan believes it. (Harmless: it authorises nothing.)
    [ "$(backing_referrer_state "$DISK")" = clear ]

    before=$(cat "$QCI_BACKING_PARENT_CACHE")
    [ "$(backing_referrer_state_authoritative "$DISK")" = referred ]
    after=$(cat "$QCI_BACKING_PARENT_CACHE")
    # Nothing learned under the lock is written back either: the authoritative
    # pass is a pure read of the filesystem.
    [ "$before" = "$after" ]
}

# The round-6 precision gate (`qci_backing_signature_is_fine`) is GONE, not
# disabled. Nothing may reintroduce a timestamp-shaped authorisation, and the
# destructive call sites must keep naming the authoritative helper.
@test "no deletion site consults the cached backing audit" {
    local lib="$REPO_ROOT/ci/lib/vm.sh" gate="$REPO_ROOT/ci/lib/gates/cleanup.sh"
    if grep -q 'qci_backing_signature_is_fine' "$lib" "$gate"; then return 1; fi
    if ! declare -F backing_referrer_state_authoritative >/dev/null; then return 1; fi
    if declare -F qci_backing_signature_is_fine >/dev/null; then return 1; fi
    # Both unlink sites in the gate, and the golden reclaim, take the
    # authoritative audit.
    [ "$(grep -c 'backing_referrer_state_authoritative "\$candidate"' "$gate")" = 1 ]
    [ "$(grep -c 'backing_referrer_state_authoritative "\$f_canonical"' "$gate")" = 1 ]
    [ "$(grep -c 'backing_referrer_state_authoritative "\$d"' "$lib")" = 1 ]
}

# --- Foreign-audit counterexamples (sol review, 2026-09-17) -----------------
# Each of these deleted a live candidate before the fix. They are regressions
# for defects reproduced OUTSIDE the suite, so they belong in it.

_foreign_chain_fixture() {
    # $DISK  <- backing of an out-of-tree foreign child.
    OUTSIDE="$TMP/foreign-child.qcow2"
    rm -f "$DISK" "$BIN/qemu-img"
    "$REAL_QEMU_IMG" create -q -f qcow2 "$DISK" 1M
    "$REAL_QEMU_IMG" create -q -f qcow2 -F qcow2 -b "$DISK" "$OUTSIDE"
    ln -s "$REAL_QEMU_IMG" "$BIN/qemu-img"
    touch -d '2 hours ago' "$DISK" "$OUTSIDE"
    : > "$DOMAIN"
    FOREIGN_UNREACHABLE=0
    FOREIGN_DOMAIN=system-guest
    FOREIGN_DISK=
}

@test "a foreign pool volume is unsupported and fails the audit closed" {
    _foreign_chain_fixture
    # REAL interface, verified by sol against libvirt's test driver: a
    # `<source pool='my-pool' volume='my-vol'/>` disk prints the VOLUME NAME
    # ALONE -- `volume disk vda my-vol` -- NOT `pool/volume`. An earlier fix
    # here split the source on `/` to recover a pool; that resolved nothing
    # and the fixture that "proved" it worked modelled an interface libvirt
    # does not have. Pool volumes are now explicitly unsupported and fail
    # closed, which is honest, rather than silently skipped, which deleted.
    FOREIGN_ROW='volume disk vda my-vol'
    _foreign_stub

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    grep -q 'resolving pool volumes is not implemented' "$RDIR/host/cleanup.log"
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

@test "a foreign network disk fails the audit closed, not treated as a local path" {
    _foreign_chain_fixture
    # A LOCAL libvirt connection does not imply LOCAL storage. `domblklist`
    # prints a network source's NAME without the server/protocol that gives it
    # meaning, so canonicalizing it locally answers about the wrong storage --
    # and an absent local leaf then dismissed the referrer and deleted.
    FOREIGN_ROW='network disk vda /export/child.qcow2'
    _foreign_stub

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    grep -q 'network storage source=' "$RDIR/host/cleanup.log"
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

@test "a foreign domain with one empty view fails the audit closed" {
    _foreign_chain_fixture
    # `domblklist` without --inactive reads the CURRENT domain XML, not a
    # live-only view, so an empty current view beside a populated persistent
    # one is unexplained rather than ordinary. Keying inspectability on the
    # DOMAIN let one populated view legitimize an empty one, and that deleted.
    FOREIGN_ROW=''
    FOREIGN_ROW_INACTIVE='file cdrom sda -'
    FOREIGN_DISK=
    _foreign_stub

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    grep -q 'lists no storage in its live view' "$RDIR/host/cleanup.log"
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

@test "a libvirt URI alias is not assumed local" {
    _foreign_chain_fixture
    FOREIGN_ROW="file disk vda $OUTSIDE"
    _foreign_stub
    # libvirt uri_aliases can expand a bare name to qemu+ssh://host/system, so
    # an unresolved alias must fail closed rather than be treated as local.
    QCI_CLEANUP_FOREIGN_URIS='remotealias'
    export QCI_CLEANUP_FOREIGN_URIS

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    grep -q 'not a verified LOCAL libvirt connection' "$RDIR/host/cleanup.log"
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

@test "a foreign block disk is walked as a chain root, not assumed harmless" {
    _foreign_chain_fixture
    # An absolute non-file source used to be excluded from chain traversal on
    # the theory that block storage cannot alias a local pathname. That says
    # nothing about what its BACKING CHAIN reaches.
    FOREIGN_ROW="block disk vda $OUTSIDE"
    _foreign_stub

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    grep -Fxq "$OUTSIDE" "$RDIR/host/cleanup-referrer-paths.txt"
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

@test "a foreign disk with no source fails the audit closed" {
    _foreign_chain_fixture
    # `-` on a DISK means the inventory is incomplete, not that the domain
    # owns nothing. It was silently skipped.
    FOREIGN_ROW='file disk vda -'
    _foreign_stub

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    grep -q 'with no source' "$RDIR/host/cleanup.log"
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

@test "a foreign empty cdrom is legitimate and is not a finding" {
    _foreign_chain_fixture
    # The counterpart to the row above: an empty REMOVABLE device really does
    # own nothing, and must not be turned into a permanent audit failure.
    FOREIGN_ROW='file cdrom sda -'
    _foreign_stub

    run gate_cleanup --age-hours 1
    [ "$(grep -c 'with no source' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

@test "a remote foreign URI fails the audit closed" {
    _foreign_chain_fixture
    FOREIGN_ROW='file disk vda /srv/shared/guest.qcow2'
    _foreign_stub
    # A remote connection's paths are in another filesystem namespace; a local
    # readlink answers about the wrong filesystem entirely.
    QCI_CLEANUP_FOREIGN_URIS='qemu+ssh://elsewhere/system'
    export QCI_CLEANUP_FOREIGN_URIS

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    grep -q 'not a verified LOCAL libvirt connection' "$RDIR/host/cleanup.log"
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

@test "a foreign owner visible only on re-query is caught under the lock" {
    _foreign_chain_fixture
    # THE reproduced deletion: the owner already existed when the critical
    # section opened, but the single pre-lock foreign capture had not seen it.
    # Nothing re-queried foreign ownership under the lock, so the authoritative
    # chain walk ran against a stale root set and authorised the delete.
    FOREIGN_ROW="file disk vda $OUTSIDE"
    FOREIGN_LIST_COUNT_FILE="$TMP/foreign-list-count"
    FOREIGN_APPEAR_AFTER=1
    _foreign_stub

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    "$REAL_QEMU_IMG" check "$OUTSIDE" >/dev/null
    # The stub was asked more than once: the refresh really did re-query.
    [ "$(cat "$TMP/foreign-list-count")" -gt 1 ]
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

@test "a foreign domain with no inspectable storage at all fails the audit closed" {
    _foreign_chain_fixture
    # sol probe 2: libvirt LISTS the domain but domblklist yields nothing in
    # either view. That is an unreadable inventory, not a domain that owns
    # nothing -- the row loop simply ran zero times and authorised the delete.
    FOREIGN_ROW=''
    FOREIGN_DISK=
    _foreign_stub

    run gate_cleanup --age-hours 1
    [ -e "$DISK" ]
    grep -q 'lists no storage in its live view' "$RDIR/host/cleanup.log"
    [ "$(grep -c '^removed ' "$RDIR/host/cleanup.log" || true)" -eq 0 ]
}

@test "cleanup_uri_is_local accepts only an explicit local libvirt URI" {
    # sol round 3 showed the first grammar also accepted `qemu://`, `:///` and
    # `qemu+ssh+unix:///system`, so "only <driver>[+unix]:///<path>" was false.
    local u
    for u in 'qemu:///system' 'qemu:///session' 'test:///default' \
             'qemu+unix:///system'; do
        cleanup_uri_is_local "$u" || { echo "should be LOCAL: $u"; return 1; }
    done
    # Remote transports, hostnames, malformed forms, and UNRESOLVED ALIASES
    # (libvirt uri_aliases can expand a bare name to qemu+ssh://host/system,
    # so an alias is unverified -- not proven remote -- and fails closed).
    for u in 'qemu+ssh://h/system' 'qemu+tls://h/system' 'qemu://h/system' \
             'qemu://' 'qemu:///' ':///' 'qemu+ssh+unix:///system' \
             '+unix:///x' 'remotealias' 'qemu' ''; do
        ! cleanup_uri_is_local "$u" || { echo "should be REJECTED: $u"; return 1; }
    done
}

#!/usr/bin/env bats
#
# Host tests for install_gui_waiters (ci/lib/gates/gui.sh) and the chunked
# copy under it (scripts/vm/lib/vm-push-file.sh): the real library reaches the
# guest byte-identical however large it grows. The fake vm-exec
# maps the guest's /tmp into a test dir and then really execs bash with the
# command as one argv string, as the qga path does, so the kernel's
# MAX_ARG_STRLEN (128 KiB) applies exactly as in production. A one-shot
# delivery of the 128 KiB library failed with E2BIG on every VM in
# full-20261015T072112Z-444803, and every scenario that sources the waiters
# ERRORed in setup.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    TDIR="$(mktemp -d "${BATS_TMPDIR:-/tmp}/gui-waiters-delivery.XXXXXX")"
    GUEST="$TDIR/guest"
    mkdir -p "$GUEST" "$TDIR/tools"
    cat > "$TDIR/tools/vm-exec" <<'EOF'
#!/usr/bin/env bash
cmd=${2//\/tmp\//$FAKE_GUEST_TMP/}
printf '%s\n' "${#2}" >> "$FAKE_ARGSIZES"
exec /bin/bash -c "$cmd"
EOF
    chmod +x "$TDIR/tools/vm-exec"
    export FAKE_GUEST_TMP="$GUEST" FAKE_ARGSIZES="$TDIR/argsizes"
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    source "$REPO_ROOT/scripts/vm/lib/vm-push-file.sh"
    VM_TOOLS="$TDIR/tools"
    QDISTRO_REPO="$REPO_ROOT"
    log() { printf '%s\n' "$*" >> "$TDIR/log"; }
}

teardown() {
    rm -rf "$TDIR"
}

@test "install_gui_waiters: the real library arrives byte-identical and parses" {
    run install_gui_waiters qci-gui-test-vm
    [ "$status" -eq 0 ]
    cmp "$GUEST/qci-gui-waiters.sh" "$REPO_ROOT/ci/lib/guest/gui-waiters.sh"
    [ ! -e "$GUEST/qci-gui-waiters.sh.part" ]
    # More than one chunk once the base64 is past one chunk's size.
    [ "$(wc -l < "$FAKE_ARGSIZES")" -ge 2 ]
}

@test "install_gui_waiters: a library over the one-argument limit still arrives" {
    mkdir -p "$TDIR/repo/ci/lib/guest"
    big="$TDIR/repo/ci/lib/guest/gui-waiters.sh"
    cp "$REPO_ROOT/ci/lib/guest/gui-waiters.sh" "$big"
    # Pad to ~300 KiB so the base64 is well past MAX_ARG_STRLEN.
    head -c 300000 /dev/zero | tr '\0' '#' | fold -w 79 >> "$big"
    QDISTRO_REPO="$TDIR/repo"
    run install_gui_waiters qci-gui-test-vm
    [ "$status" -eq 0 ]
    cmp "$GUEST/qci-gui-waiters.sh" "$big"
    while read -r n; do [ "$n" -lt 131072 ]; done < "$FAKE_ARGSIZES"
}

@test "install_gui_waiters: control: one-shot delivery of the real library hits E2BIG" {
    # Proves the fake applies the kernel limit: with one chunk the size of the
    # whole base64 (the old delivery) the guest file never appears.
    QDISTRO_VM_PUSH_CHUNK=9999999
    run install_gui_waiters qci-gui-test-vm
    [ "$status" -ne 0 ]
    [ ! -e "$GUEST/qci-gui-waiters.sh" ]
    # The kernel refused to start vm-exec at all: it never logged an argument.
    [ ! -s "$FAKE_ARGSIZES" ]
}

@test "install_gui_waiters: a library that does not parse is never installed" {
    mkdir -p "$TDIR/repo/ci/lib/guest"
    printf 'if then\n' > "$TDIR/repo/ci/lib/guest/gui-waiters.sh"
    printf 'previous\n' > "$GUEST/qci-gui-waiters.sh"
    QDISTRO_REPO="$TDIR/repo"
    run install_gui_waiters qci-gui-test-vm
    [ "$status" -ne 0 ]
    [ "$(cat "$GUEST/qci-gui-waiters.sh")" = previous ]
}

@test "qdistro_vm_push_file: a non-positive chunk size is refused, not looped on" {
    local c
    for c in 0 -5 abc 08 18446744073709551616 10000000; do
        run timeout 10 bash -c '. "$1"; QDISTRO_VM_PUSH_CHUNK=$2 qdistro_vm_push_file "$3" vm "$4" /tmp/x' _ \
            "$REPO_ROOT/scripts/vm/lib/vm-push-file.sh" "$c" "$VM_TOOLS/vm-exec" "$REPO_ROOT/ci/lib/guest/gui-waiters.sh"
        [ "$status" -eq 2 ] || { echo "chunk '$c': status $status"; false; }
    done
    [ ! -e "$GUEST/x" ]
}

@test "qdistro_vm_push_file: an empty file arrives empty" {
    : > "$TDIR/empty"
    run qdistro_vm_push_file "$VM_TOOLS/vm-exec" vm "$TDIR/empty" /tmp/empty
    [ "$status" -eq 0 ]
    [ -f "$GUEST/empty" ] && [ ! -s "$GUEST/empty" ]
}

@test "qdistro_vm_push_file: a failing check keeps the previous destination" {
    printf 'new\n' > "$TDIR/src"
    printf 'previous\n' > "$GUEST/dest"
    run qdistro_vm_push_file "$VM_TOOLS/vm-exec" vm "$TDIR/src" /tmp/dest false
    [ "$status" -ne 0 ]
    [ "$(cat "$GUEST/dest")" = previous ]
    run qdistro_vm_push_file "$VM_TOOLS/vm-exec" vm "$TDIR/src" /tmp/dest true
    [ "$status" -eq 0 ]
    [ "$(cat "$GUEST/dest")" = new ]
}

#!/usr/bin/env bats
# qsu compiled-binary lock-in test.
#
# Asserts that /usr/local/bin/qsu is the COMPILED qsu.c ELF binary, not
# the old bash->python wrapper. The whole point of qsu.c is that
# /proc/<pid>/exe resolves to /usr/local/bin/qsu (an ELF) for the life
# of the connection, giving qdistro-root-exec an unambiguous caller_exe
# for audit. A python wrapper makes /proc/<pid>/exe resolve to
# /usr/bin/python3.X, which silently defeated the exe-based identity
# checks — see todo/issues/qsu/qsu-wrapper-loses-name.md.
#
# Unlike the other tests in this dir, this file does NOT `load helpers`
# (which hard-requires a live VM via VM_NAME). Its core assertions run
# on the dev host: it compiles qsu.c to a temp prefix and inspects the
# produced binary + the installer source, so `bats tests/integration/vm/
# qsu-binary.bats` passes without a VM. The live-VM caller_exe assertion
# (that the broker records caller_exe=/usr/local/bin/qsu) is covered
# separately by the end-to-end qsu probes.

# Resolve repo root from this test file's location.
REPO_ROOT="$(git -C "$(dirname "${BATS_TEST_FILENAME}")" rev-parse --show-toplevel 2>/dev/null)"
QSU_SRC="$REPO_ROOT/qsu"
INSTALLER="$REPO_ROOT/scripts/install/install-qsu-for-vm.sh"
# Same hardened flag set the installer + qsu/Makefile use.
QSU_CFLAGS="-O2 -Wall -Wextra -Wformat=2 -Werror=format-security"

# Pick a C compiler the same way the installer does.
pick_cc() {
    local _cc
    for _cc in "${CC:-}" cc gcc clang; do
        [ -n "$_cc" ] || continue
        if command -v "$_cc" >/dev/null 2>&1; then
            printf '%s' "$_cc"
            return 0
        fi
    done
    return 1
}

setup() {
    [ -f "$QSU_SRC/qsu.c" ] || skip "qsu.c not found at $QSU_SRC"
    BATS_TMP="$(mktemp -d)"
}

teardown() {
    [ -n "${BATS_TMP:-}" ] && rm -rf "$BATS_TMP"
}

@test "qsu.c compiles cleanly with the documented flags" {
    local cc
    cc="$(pick_cc)" || skip "no C compiler available (cc/gcc/clang)"
    run "$cc" $QSU_CFLAGS -o "$BATS_TMP/qsu" "$QSU_SRC/qsu.c"
    [ "$status" -eq 0 ]
    # -Wall -Wextra should produce no warnings on a clean build.
    [ -z "$output" ]
    [ -x "$BATS_TMP/qsu" ]
}

@test "compiled qsu is an ELF executable, not a shell script" {
    local cc
    cc="$(pick_cc)" || skip "no C compiler available (cc/gcc/clang)"
    "$cc" $QSU_CFLAGS -o "$BATS_TMP/qsu" "$QSU_SRC/qsu.c"

    run file "$BATS_TMP/qsu"
    [ "$status" -eq 0 ]
    [[ "$output" == *ELF* ]]
    [[ "$output" != *"shell script"* ]]
    [[ "$output" != *"Python script"* ]]
}

@test "qsu --help works and does not exec python (no socket needed)" {
    local cc
    cc="$(pick_cc)" || skip "no C compiler available (cc/gcc/clang)"
    "$cc" $QSU_CFLAGS -o "$BATS_TMP/qsu" "$QSU_SRC/qsu.c"

    # --help is handled in-process and exits 0 before touching the socket.
    run "$BATS_TMP/qsu" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"qdistro sudo replacement"* ]]
}

@test "qsu carries comm=qsu (PR_SET_NAME) — caller identity anchor" {
    # The binary sets its kernel task name to "qsu" so /proc/<pid>/comm
    # and /proc/<pid>/exe both point at qsu, not python3. We can't read
    # another pid's /proc here cheaply, but the symbol must be wired.
    run grep -q 'PR_SET_NAME, "qsu"' "$QSU_SRC/qsu.c"
    [ "$status" -eq 0 ]
}

@test "installer installs the compiled binary to /usr/local/bin/qsu" {
    [ -f "$INSTALLER" ] || skip "installer not found at $INSTALLER"
    # The installer must install(1) a compiled binary as 0755.
    run grep -Eq "install .*-m 0755 .*qsu.* \"\\\$DEST_BIN/qsu\"" "$INSTALLER"
    [ "$status" -eq 0 ]
    # And it must compile qsu.c with the hardened flags (same set as the
    # qsu/Makefile so installer and `make` produce the same binary).
    run grep -q -- '-Wformat=2 -Werror=format-security' "$INSTALLER"
    [ "$status" -eq 0 ]
}

@test "installer fails closed (no python wrapper) when no compiler, unless opt-in" {
    [ -f "$INSTALLER" ] || skip "installer not found at $INSTALLER"
    # Default no-compiler path must be a hard ERROR + exit, NOT a silent
    # python wrapper.
    run grep -q 'cannot build the' "$INSTALLER"
    [ "$status" -eq 0 ]
    # The python heredoc may still exist, but ONLY behind the explicit
    # QSU_ALLOW_PYTHON_FALLBACK opt-in. Assert the exec-python line is
    # gated by that env var appearing before it in the file.
    local gate_line py_line
    gate_line="$(grep -n 'QSU_ALLOW_PYTHON_FALLBACK' "$INSTALLER" | head -1 | cut -d: -f1)"
    py_line="$(grep -n 'exec /usr/bin/python3 /usr/local/lib/qdistro/qsu.py' "$INSTALLER" | head -1 | cut -d: -f1)"
    [ -n "$gate_line" ]
    [ -n "$py_line" ]
    # The python fallback must come AFTER the opt-in gate.
    [ "$py_line" -gt "$gate_line" ]
}

@test "installer removes a stale non-ELF wrapper on the fail-closed path" {
    [ -f "$INSTALLER" ] || skip "installer not found at $INSTALLER"
    # The fail-closed branch must scrub a pre-existing non-ELF entry point so
    # an upgrade on a compiler-less host can't keep the old python wrapper
    # callable. Assert the branch greps for ELF magic and rm's on mismatch.
    run grep -q 'removing stale non-ELF' "$INSTALLER"
    [ "$status" -eq 0 ]
    run grep -q 'rm -f "\$DEST_BIN/qsu"' "$INSTALLER"
    [ "$status" -eq 0 ]
}

# --- Live-VM assertion ----------------------------------------------------
# Only runs when VM_NAME is exported AND scripts/vm/vm-exec exists, so the
# host-side `bats tests/integration/vm/qsu-binary.bats` run above SKIPs it
# cleanly. After fresh-vm-bootstrap, /usr/local/bin/qsu must be the ELF
# binary on the real install — this is what proves the install path (not
# just a temp-dir compile) ships the binary, closing the coverage gap.
@test "VM: installed /usr/local/bin/qsu is an ELF binary, not a wrapper" {
    [ -n "${VM_NAME:-}" ] || skip "VM_NAME not set — host-only run"
    local vm_exec="${VM_EXEC:-$REPO_ROOT/scripts/vm/vm-exec}"
    [ -x "$vm_exec" ] || skip "vm-exec not found at $vm_exec"

    run "$vm_exec" "$VM_NAME" "file -b /usr/local/bin/qsu"
    [ "$status" -eq 0 ]
    [[ "$output" == *ELF* ]]
    [[ "$output" != *"shell script"* ]]

    # Belt-and-braces: the shebang+python exec line must be absent.
    run "$vm_exec" "$VM_NAME" "head -c 4 /usr/local/bin/qsu | od -An -tx1"
    [ "$status" -eq 0 ]
    # ELF magic is 7f 45 4c 46.
    [[ "$output" == *"7f 45 4c 46"* ]]
}

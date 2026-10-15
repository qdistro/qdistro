#!/usr/bin/env bats
#
# The host-side GUI helper libraries stay correct when a driver sources them
# from an interactive shell whose rc file aliases common commands. The GUI
# agent can open a tty bash that reads the user's ~/.bashrc; with
# stat='git status' there, qdwin_vmx_merged's capture check returned 125 and
# presentation-gui/02 ERRORed (full-20261015T072112Z-444803).

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    RC="$BATS_TEST_TMPDIR/rc"
    cat > "$RC" <<'EOF'
# Like the real stat='git status': the aliased command fails.
alias stat='false ALIASED-STAT'
alias rm='rm -i'
alias cp='cp -i'
alias mv='mv -i'
alias grep='false ALIASED-GREP'
shopt -s expand_aliases
EOF
}

# Source $1 in an interactive bash that read $RC, then run $2. They go on
# separate lines: bash expands aliases when it reads a line, so $2 on the
# source line would be read before the library could drop them.
interactive_source() {
    QDISTRO_REPO="$REPO_ROOT" bash --rcfile "$RC" -i -c "source '$1' >/dev/null 2>&1
$2" 2>/dev/null
}

@test "control: the rc file's aliases do expand into functions defined after it" {
    run interactive_source /dev/null 'f() { stat -c %s /dev/null; }; type f'
    [[ "$output" == *ALIASED-STAT* ]]
}

@test "helper libraries define their functions without the caller's aliases" {
    local lib
    for lib in \
        qdwin/tests/gui/qdwin-helpers.sh \
        qdwin/tests/apps/qdwin-apps-helpers.sh \
        tests/integration/presentation-gui/presentation-helpers.sh \
        tests/integration/qdwin-noctalia/noctalia-helpers.sh \
        qdlocker/tests/gui/qdlocker-helpers.sh; do
        run interactive_source "$REPO_ROOT/$lib" \
            'alias | wc -l; declare -f | command grep -c -e ALIASED -e "rm -i" -e "cp -i" -e "mv -i" || true'
        [ "$status" -eq 0 ] || { echo "$lib: status $status"; false; }
        [ "${lines[0]}" = 0 ] || { echo "$lib left aliases: $output"; false; }
        [ "${lines[1]}" = 0 ] || { echo "$lib functions carry aliases: $output"; false; }
    done
}

@test "qdwin_vmx_merged's capture check works under the aliases" {
    local fake="$BATS_TEST_TMPDIR/vm-exec"
    printf '#!/bin/sh\necho guest-output\n' > "$fake"
    chmod +x "$fake"
    run interactive_source "$REPO_ROOT/qdwin/tests/gui/qdwin-helpers.sh" \
        "VMNAME=vm QDWIN_VM_EXEC='$fake' qdwin_vmx_merged true; echo rc=\$?"
    [[ "$output" == *guest-output* ]]
    [[ "$output" == *rc=0* ]]
}

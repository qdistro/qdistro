#!/usr/bin/env bats
#
# Host tests for drop_acct_fragment (tests/integration/vm/tier3s-guest-lib.sh):
# ensure_silo_image re-provisions a silo account that an interrupted launch
# left half-created (full-20261015T072112Z-444803, s120x: the manager's
# reconcile stopped the launch mid-useradd), but it must never delete an
# account that a launch may still be building or that something runs as.
# The system commands are PATH stubs driven by files in $STUB.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    STUB="$BATS_TEST_TMPDIR/stub"
    mkdir -p "$STUB/bin"
    # id: the account exists while $STUB/exists does; uid 60100
    cat > "$STUB/bin/id" <<'EOF'
#!/bin/sh
[ -e "$STUB/exists" ] || exit 1
[ "$1" = -u ] && echo 60100
exit 0
EOF
    # systemctl show -P ActiveState: prints $STUB/state, fails if absent
    cat > "$STUB/bin/systemctl" <<'EOF'
#!/bin/sh
[ -e "$STUB/state" ] || exit 1
cat "$STUB/state"
EOF
    cat > "$STUB/bin/pgrep" <<'EOF'
#!/bin/sh
[ -e "$STUB/procs" ]
EOF
    cat > "$STUB/bin/userdel" <<'EOF'
#!/bin/sh
echo "$*" >> "$STUB/userdel.log"
rm -f "$STUB/exists"
EOF
    printf '#!/bin/sh\n:\n' > "$STUB/bin/sleep"
    chmod +x "$STUB/bin/"*
    export STUB
}

# run drop_acct_fragment with the stubs; acct_complete is true iff $STUB/complete
drop() {
    PATH="$STUB/bin:$PATH" bash -c '
        . "$1" >/dev/null 2>&1; trap - EXIT
        acct_complete() { [ -e "$STUB/complete" ]; }
        drop_acct_fragment s1 qt3s-s1' _ "$REPO_ROOT/tests/integration/vm/tier3s-guest-lib.sh"
}

@test "an inactive unit with nothing running as the uid: the fragment is dropped" {
    touch "$STUB/exists"; echo inactive > "$STUB/state"
    run drop
    [ "$status" -eq 0 ]
    [ "$(cat "$STUB/userdel.log")" = "-f qt3s-s1" ]
    [[ "$output" == *"dropping incomplete qt3s-s1"* ]]
}

@test "a unit that stays active is never deleted under" {
    touch "$STUB/exists"
    local st
    for st in active activating deactivating reloading; do
        echo "$st" > "$STUB/state"
        run drop
        [ "$status" -ne 0 ] || { echo "state $st: status 0"; false; }
        [ ! -e "$STUB/userdel.log" ] || { echo "state $st: userdel ran"; false; }
    done
}

@test "an unreadable unit state is refused, not treated as idle" {
    touch "$STUB/exists"
    run drop
    [ "$status" -ne 0 ]
    [ ! -e "$STUB/userdel.log" ]
    [[ "$output" == *unreadable* ]]
}

@test "a process running as the uid blocks the delete" {
    touch "$STUB/exists" "$STUB/procs"; echo failed > "$STUB/state"
    run drop
    [ "$status" -ne 0 ]
    [ ! -e "$STUB/userdel.log" ]
}

@test "a complete or absent account is left alone" {
    run drop
    [ "$status" -eq 0 ]
    touch "$STUB/exists" "$STUB/complete"; echo active > "$STUB/state"
    run drop
    [ "$status" -eq 0 ]
    [ ! -e "$STUB/userdel.log" ]
}

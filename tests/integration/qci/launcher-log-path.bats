#!/usr/bin/env bats
#
# Host-only regressions for the launch-log path selection shipped in
# deploy/start-admin-app.sh and deploy/start-admin-tui.sh.
#
# History: both launchers used to log to a fixed `/tmp/<name>.log`, which any
# other local uid (or a stale root-owned file in the golden image) could
# pre-create so that the redirection failed with EACCES *before* the program
# started -- a denial of service on the supported launcher plus the classic
# /tmp symlink hazard. The replacement logs into an XDG state directory, but
# only when that directory is provably ours; anything else falls back to a
# private mktemp file and finally /dev/null. The launcher must never die
# because logging failed.
#
# No VM required: the launchers are run directly with a fake `getent` (to
# control the admin home) and a fake `python3`/`qterminal` (to produce output)
# on PATH.

bats_require_minimum_version 1.5.0

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    APP="$REPO_ROOT/deploy/start-admin-app.sh"
    TUI="$REPO_ROOT/deploy/start-admin-tui.sh"
    FAKEBIN="$BATS_TEST_TMPDIR/bin"
    HOMEDIR="$BATS_TEST_TMPDIR/home"
    TMPFALLBACK="$BATS_TEST_TMPDIR/tmp"
    mkdir -p "$FAKEBIN" "$HOMEDIR" "$TMPFALLBACK"

    # getent passwd <uid> -- field 6 is the home directory the launcher uses
    # instead of $HOME.
    cat > "$FAKEBIN/getent" <<EOF
#!/bin/bash
printf 'fakeadmin:x:%s:100:Fake Admin:%s:/bin/bash\n' "\$2" "\$QDT_FAKE_HOME"
EOF
    # Stand-ins for the launched programs: they only have to write a marker to
    # the inherited log fd.
    for prog in python3 qterminal; do
        cat > "$FAKEBIN/$prog" <<'EOF'
#!/bin/bash
echo "LAUNCHED-MARKER"
EOF
        chmod +x "$FAKEBIN/$prog"
    done
    chmod +x "$FAKEBIN/getent"

    QDT_FAKE_HOME="$HOMEDIR"
    HOMELOG="$HOMEDIR/.local/state/qdistro/admin-app.log"
}

# run_launcher <script> [env assignments...] -- runs with the fake PATH.
run_launcher() {
    local script=$1
    shift
    run --separate-stderr env -u XDG_STATE_HOME \
        PATH="$FAKEBIN:$PATH" \
        TMPDIR="$TMPFALLBACK" \
        QDT_FAKE_HOME="$QDT_FAKE_HOME" \
        "$@" bash "$script"
}

# The launcher backgrounds the program and exits; give the child a moment to
# write its marker into the inherited fd.
wait_for_marker() {
    local file=$1 i
    for i in $(seq 1 50); do
        [ -s "$file" ] && grep -q LAUNCHED-MARKER "$file" && return 0
        sleep 0.1
    done
    echo "no marker in $file" >&2
    cat "$file" >&2 || true
    return 1
}

# The launcher must always print the child pid and never fail on logging.
assert_launched() {
    [ "$status" -eq 0 ]
    [[ "$output" =~ ^[0-9]+$ ]]
}

@test "absolute XDG_STATE_HOME is used and the private dir/file modes are enforced" {
    local xdg="$BATS_TEST_TMPDIR/state"
    mkdir -p "$xdg"
    run_launcher "$APP" XDG_STATE_HOME="$xdg"
    assert_launched
    [ -z "$stderr" ]
    wait_for_marker "$xdg/qdistro/admin-app.log"
    [ "$(stat -c '%F %u %a' "$xdg/qdistro")" = "directory $(id -u) 700" ]
    [ "$(stat -c '%a' "$xdg/qdistro/admin-app.log")" = "600" ]
    [ ! -e "$HOMELOG" ]
}

@test "a relative XDG_STATE_HOME is ignored in favour of the admin home" {
    run_launcher "$APP" XDG_STATE_HOME="relative/state"
    assert_launched
    [ -z "$stderr" ]
    wait_for_marker "$HOMELOG"
    [ ! -e "relative/state/qdistro/admin-app.log" ]
}

@test "HOME is never trusted: the log follows getent, not \$HOME" {
    local bogus="$BATS_TEST_TMPDIR/bogus-home"
    mkdir -p "$bogus"
    run_launcher "$APP" HOME="$bogus"
    assert_launched
    wait_for_marker "$HOMELOG"
    [ ! -e "$bogus/.local/state/qdistro/admin-app.log" ]
}

@test "service context with HOME unset still logs to the admin state dir" {
    run --separate-stderr env -i \
        PATH="$FAKEBIN:/usr/bin:/bin" \
        TMPDIR="$TMPFALLBACK" \
        QDT_FAKE_HOME="$QDT_FAKE_HOME" \
        bash "$APP"
    assert_launched
    wait_for_marker "$HOMELOG"
}

@test "a symlinked state directory is refused and its target is left alone" {
    local xdg="$BATS_TEST_TMPDIR/state" victim="$BATS_TEST_TMPDIR/victim"
    mkdir -p "$xdg" "$victim"
    ln -s "$victim" "$xdg/qdistro"
    run_launcher "$APP" XDG_STATE_HOME="$xdg"
    assert_launched
    # Rejected, not followed: nothing was written through the symlink.
    [ ! -e "$victim/admin-app.log" ]
    [ -L "$xdg/qdistro" ]
    wait_for_marker "$HOMELOG"
}

@test "a world-open pre-existing state directory is tightened to 0700 before use" {
    local xdg="$BATS_TEST_TMPDIR/state"
    mkdir -p "$xdg/qdistro"
    chmod 0777 "$xdg/qdistro"
    run_launcher "$APP" XDG_STATE_HOME="$xdg"
    assert_launched
    [ "$(stat -c '%a' "$xdg/qdistro")" = "700" ]
    wait_for_marker "$xdg/qdistro/admin-app.log"
}

@test "a pre-existing log symlink is replaced, never followed" {
    local xdg="$BATS_TEST_TMPDIR/state"
    mkdir -p "$xdg/qdistro"
    echo "PRECIOUS" > "$xdg/qdistro/victim"
    ln -s "$xdg/qdistro/victim" "$xdg/qdistro/admin-app.log"
    run_launcher "$APP" XDG_STATE_HOME="$xdg"
    assert_launched
    [ "$(cat "$xdg/qdistro/victim")" = "PRECIOUS" ]
    [ ! -L "$xdg/qdistro/admin-app.log" ]
    wait_for_marker "$xdg/qdistro/admin-app.log"
}

@test "an unwritable XDG_STATE_HOME falls through to the admin home" {
    local xdg="$BATS_TEST_TMPDIR/ro-state"
    mkdir -p "$xdg"
    chmod 0500 "$xdg"
    run_launcher "$APP" XDG_STATE_HOME="$xdg"
    chmod 0700 "$xdg"
    assert_launched
    [ -z "$stderr" ]
    wait_for_marker "$HOMELOG"
}

@test "unwritable state dir and unwritable admin home fall back to a warned mktemp log" {
    local xdg="$BATS_TEST_TMPDIR/ro-state"
    mkdir -p "$xdg"
    chmod 0500 "$xdg"
    chmod 0500 "$HOMEDIR"
    run_launcher "$APP" XDG_STATE_HOME="$xdg"
    chmod 0700 "$xdg" "$HOMEDIR"
    assert_launched
    [[ "$stderr" == *"no private state dir for admin-app.log"* ]]
    local fallback
    fallback=$(printf '%s' "$stderr" | sed -n 's/.*logging to //p')
    [ -n "$fallback" ]
    [[ "$fallback" == "$TMPFALLBACK"/* ]]
    [ "$(stat -c '%a' "$fallback")" = "600" ]
    wait_for_marker "$fallback"
}

@test "a missing admin home that cannot be created falls back to mktemp" {
    local locked="$BATS_TEST_TMPDIR/locked"
    mkdir -p "$locked"
    chmod 0500 "$locked"
    QDT_FAKE_HOME="$locked/no-such-home"
    run_launcher "$APP"
    chmod 0700 "$locked"
    assert_launched
    [[ "$stderr" == *"no private state dir for admin-app.log"* ]]
}

@test "the launcher still starts when logging is impossible" {
    # No mktemp, unwritable state homes: the last resort is /dev/null and the
    # program must still be launched.
    local xdg="$BATS_TEST_TMPDIR/ro-state"
    mkdir -p "$xdg"
    chmod 0500 "$xdg"
    chmod 0500 "$HOMEDIR"
    cat > "$FAKEBIN/mktemp" <<'EOF'
#!/bin/bash
exit 1
EOF
    chmod +x "$FAKEBIN/mktemp"
    run_launcher "$APP" XDG_STATE_HOME="$xdg"
    chmod 0700 "$xdg" "$HOMEDIR"
    assert_launched
    [[ "$stderr" == *"logging to /dev/null"* ]]
}

@test "start-admin-tui.sh applies the same contract under its own log name" {
    local xdg="$BATS_TEST_TMPDIR/state"
    mkdir -p "$xdg"
    ln -s /etc "$xdg/qdistro-decoy"
    run_launcher "$TUI" XDG_STATE_HOME="$xdg"
    assert_launched
    [ -z "$stderr" ]
    wait_for_marker "$xdg/qdistro/qterminal-tui.log"
    [ "$(stat -c '%F %u %a' "$xdg/qdistro")" = "directory $(id -u) 700" ]
    [ "$(stat -c '%a' "$xdg/qdistro/qterminal-tui.log")" = "600" ]
}

@test "start-admin-tui.sh refuses a symlinked state directory too" {
    local xdg="$BATS_TEST_TMPDIR/state" victim="$BATS_TEST_TMPDIR/victim"
    mkdir -p "$xdg" "$victim"
    ln -s "$victim" "$xdg/qdistro"
    run_launcher "$TUI" XDG_STATE_HOME="$xdg"
    assert_launched
    [ ! -e "$victim/qterminal-tui.log" ]
    wait_for_marker "$HOMEDIR/.local/state/qdistro/qterminal-tui.log"
}

@test "a real root-owned absolute XDG_STATE_HOME falls through to the admin home" {
    # /usr/lib is root-owned and not writable by the test uid; the launcher
    # must neither create nor use a directory there.
    run_launcher "$APP" XDG_STATE_HOME=/usr/lib
    assert_launched
    [ -z "$stderr" ]
    [ ! -e /usr/lib/qdistro/admin-app.log ]
    wait_for_marker "$HOMELOG"
}

@test "a state directory owned by another uid is refused" {
    # The ownership branch of the predicate cannot be exercised by creating a
    # directory owned by someone else without privileges, so flip the other
    # side of the comparison: `id -u` reports a uid that owns nothing here.
    local xdg="$BATS_TEST_TMPDIR/state"
    mkdir -p "$xdg/qdistro"
    chmod 0700 "$xdg/qdistro"
    cat > "$FAKEBIN/id" <<'EOF'
#!/bin/bash
[ "$1" = "-u" ] && { echo 4242; exit 0; }
exec /usr/bin/id "$@"
EOF
    chmod +x "$FAKEBIN/id"
    run_launcher "$APP" XDG_STATE_HOME="$xdg"
    assert_launched
    [[ "$stderr" == *"no private state dir for admin-app.log"* ]]
    [ ! -e "$xdg/qdistro/admin-app.log" ]
    [ ! -e "$HOMELOG" ]
}

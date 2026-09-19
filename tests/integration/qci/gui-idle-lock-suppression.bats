#!/usr/bin/env bats
#
# Host-only tests for ci/lib/gates/gui.sh::suppress_idle_lock.
#
# The function installs a 24h-idle drop-in for qdlocker on every agent GUI VM
# so a multi-minute scenario does not trip the production 5-minute idle lock
# (36b1ce6: "the 'app' screenshots were actually the qdlocker lock screen").
#
# What is tested here is the part that bit back. The function ended with
# `systemctl --user restart qdlocker.service`, and `restart` STARTS a stopped
# unit. qdlocker is Restart=always and cannot initialise without a reachable
# wl_display, so on a VM whose compositor is not up yet this converted a
# dormant unit into a crash-loop for the life of the VM --
# gui-20260919T072913Z reached restart counter 121 at ~2s per cycle, which
# flooded the journal collector and destroyed the evidence window for an
# unrelated failure in the same run.
#
# suppress_idle_lock talks to the VM through $VM_TOOLS/vm-exec, so the test
# stubs vm-exec, decodes the base64 script it was handed, and EXECUTES it
# against fake `id`/`install`/`chown`/`runuser`/`systemctl`. That runs the real
# emitted script rather than asserting on its text.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"

    TDIR="$BATS_TEST_TMPDIR"
    mkdir -p "$TDIR/bin" "$TDIR/tools" "$TDIR/home"
    PATH="$TDIR/bin:$PATH"
    VM_TOOLS="$TDIR/tools"

    # vm-exec stub: decode the base64 script the function hands it, re-root
    # the absolute /home/admin path into the test tree, and EXECUTE it. The
    # re-rooting is the only edit -- every other byte of the emitted script
    # runs as written, which is the point: this exercises the real script, not
    # a paraphrase of it.
    cat > "$TDIR/tools/vm-exec" <<EOF
#!/usr/bin/env bash
# The guest command is: printf '%s' '<base64>' | base64 -d | bash
# The payload is the SECOND single-quoted field; the first is printf's '%s'.
b64=\$(awk -F"'" '{print \$4}' <<<"\$2")
if [ -z "\$b64" ]; then
    echo "vm-exec stub: could not extract the base64 payload" >&2
    touch "$TDIR/STUB-BROKEN"
    exit 90
fi
# Decode to a FILE and check it. Decoding inside a pipeline hides failure:
# without pipefail, \`base64 -d\` can fail while the trailing \`bash\` reads an
# empty stream and exits 0, so a wrong-but-nonempty field reported success.
if ! printf '%s' "\$b64" | base64 -d > "$TDIR/decoded.sh" 2>/dev/null; then
    echo "vm-exec stub: base64 payload did not decode" >&2
    touch "$TDIR/STUB-BROKEN"
    exit 91
fi
if [ ! -s "$TDIR/decoded.sh" ]; then
    echo "vm-exec stub: decoded script is empty" >&2
    touch "$TDIR/STUB-BROKEN"
    exit 92
fi
sed -i "s#/home/admin#$TDIR/home/admin#g" "$TDIR/decoded.sh"
PATH="$TDIR/bin:\$PATH" bash "$TDIR/decoded.sh"
rc=\$?
printf '%s\n' "\$rc" > "$TDIR/script-rc"
exit "\$rc"
EOF
    chmod +x "$TDIR/tools/vm-exec"

    for c in install chown; do
        cat > "$TDIR/bin/$c" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
        chmod +x "$TDIR/bin/$c"
    done
    # `id admin` is the decoded script's FIRST command. Recording it is what
    # proves the script was entered -- a marker written by the stub before
    # execution proves only that the stub was called.
    cat > "$TDIR/bin/id" <<EOF
#!/usr/bin/env bash
printf '%s\n' "id \$*" >> "$TDIR/id.log"
exit 0
EOF
    chmod +x "$TDIR/bin/id"

    # runuser -u X -- env ... systemctl ...  =>  drop the prefix, run the rest.
    cat > "$TDIR/bin/runuser" <<'EOF'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do
    case "$1" in
        -u|--user) shift 2 ;;
        --) shift; break ;;
        *) shift ;;
    esac
done
exec "$@"
EOF
    chmod +x "$TDIR/bin/runuser"

    # systemctl stub: records every verb, and models `is-active`.
    cat > "$TDIR/bin/systemctl" <<EOF
#!/usr/bin/env bash
args=()
for a in "\$@"; do [ "\$a" = "--user" ] || args+=("\$a"); done
printf '%s\n' "\${args[*]}" >> "$TDIR/systemctl.log"
exit 0
EOF
    chmod +x "$TDIR/bin/systemctl"

    # `cat > "\$d/90-ci-gui.conf"` must land somewhere writable.
    mkdir -p "$TDIR/home/admin/.config/systemd/user/qdlocker.service.d"
}

emitted() { cat "$TDIR/systemctl.log" 2>/dev/null; }

# Guard against the vacuous-green failure mode: if the stub could not decode
# the script then no fake ran, and every "it did not do X" assertion is
# worthless. Both positive tests assert the script REACHED systemctl.
# Guard against the vacuous-green failure mode. The decisive observation is
# `id admin`, recorded by the fake from INSIDE the decoded script: a marker the
# stub writes before executing would prove only that the stub was reached.
refute_stub_broken() {
    [ ! -e "$TDIR/STUB-BROKEN" ] || { echo "vm-exec stub could not decode the script"; return 1; }
    grep -q '^id admin$' "$TDIR/id.log" 2>/dev/null \
        || { echo "the decoded script never reached its first command"; return 1; }
}

dropin() { echo "$TDIR/home/admin/.config/systemd/user/qdlocker.service.d/90-ci-gui.conf"; }

@test "idle-lock suppression does not START a stopped qdlocker" {
    # The regression. `restart` on a Restart=always unit that cannot come up
    # is what manufactured the 121-restart crash-loop.
    run suppress_idle_lock somevm
    [ "$status" -eq 0 ]
    refute_stub_broken
    [ -s "$TDIR/systemctl.log" ]   # the emitted script must have REACHED systemctl
    # Assert the recorded argument lines exactly. A substring test is useless
    # here: "restart qdlocker.service" is itself a substring of
    # "try-restart qdlocker.service", so it can never distinguish the two.
    # Exact recorded lines, both directions.
    grep -qxF 'try-restart qdlocker.service' "$TDIR/systemctl.log"
    ! grep -qxF 'restart qdlocker.service' "$TDIR/systemctl.log"
}

@test "idle-lock suppression still reloads and try-restarts" {
    # Non-vacuity: the function must still DO its job. A version that emitted
    # nothing at all would pass the test above.
    run suppress_idle_lock somevm
    [ "$status" -eq 0 ]
    refute_stub_broken
    grep -q 'daemon-reload' "$TDIR/systemctl.log"
    grep -q 'try-restart qdlocker.service' "$TDIR/systemctl.log"
    # THE PAYLOAD, not just the verbs. Without this a function that emitted
    # the two systemctl calls and never wrote the drop-in would pass, and the
    # drop-in is the entire point of the helper.
    [ -f "$(dropin)" ]
    grep -q 'Environment=QDLOCKER_IDLE_MS=86400000' "$(dropin)"
}

@test "idle-lock suppression is best-effort when the VM has no admin user" {
    # `id admin || exit 0` -- a VM without the session user must be a silent
    # no-op, never an aborted scenario.
    # Still records the call -- the test needs to distinguish "id said no" from
    # "the script never got that far".
    cat > "$TDIR/bin/id" <<EOF
#!/usr/bin/env bash
printf '%s\n' "id \$*" >> "$TDIR/id.log"
exit 1
EOF
    chmod +x "$TDIR/bin/id"
    run suppress_idle_lock somevm
    [ "$status" -eq 0 ]
    # The script must have RUN and then bailed, not failed to run at all --
    # otherwise "no systemctl calls" is true for the wrong reason. `id admin`
    # is the proof: it is the script's first command, and the fake records it.
    [ ! -e "$TDIR/STUB-BROKEN" ]
    grep -q '^id admin$' "$TDIR/id.log"
    [ ! -s "$TDIR/systemctl.log" ]
    [ ! -f "$(dropin)" ]
}

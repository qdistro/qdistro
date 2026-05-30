#!/usr/bin/env bats
# guest-image-perms — base-image permission + baked-credential hardening
# for the Tier-4-guest / Tier-5 / Tier-5b guest base-image builders.
#
# Track: feat/guest-image-perms. Source note:
#   todo/open-followups.md "## VM / tier operational watch-list"
#   (guest base images were baked 0644 root:root + always carried a baked
#    debug root password). This suite proves the hardening contract:
#
#   1. The published qcow2 is chmod 0640 (not 0644) in all three builders.
#   2. The baked debug root password is gated by
#        QDISTRO_GUEST_BAKE_DEBUG_PASSWORD  (default 1 = bake; 0 = hardened).
#   3. default mode (flag unset/1) -> --root-password is passed to
#      virt-customize (unchanged historical behavior).
#   4. hardened mode (flag=0)      -> NO --root-password is passed, and
#      QDISTRO_VM_PASSWORD is not required, and the image is still 0640.
#
# These are pure host-side unit tests: NO real image build, NO sudo, NO VM.
# Every heavy / privileged external (virt-*, qemu-img, wget, meson, ninja,
# chmod, chown, mv, install, id, shred, virt-sparsify, virt-resize) is
# replaced by a PATH shim that logs its argv to $CMDLOG. We then assert on
# WHICH commands ran (and with which args) in each mode, for each script.
#
# Mutation sensitivity: the chmod assertions match the EXACT octal mode, so
# reverting 0640->0644 fails the suite; the password assertions match the
# EXACT presence/absence of --root-password, so dropping the gate fails.
#
# Run: bats tests/integration/vm/guest-image-perms.bats

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    T4="$REPO_ROOT/tier4-vm-guest/build-guest-image.sh"
    T5="$REPO_ROOT/tier5-vm/build-guest-image.sh"
    T5B="$REPO_ROOT/tier5b-vm/build-guest-image.sh"
    [ -f "$T4" ]  || skip "tier4-vm-guest builder missing"
    [ -f "$T5" ]  || skip "tier5-vm builder missing"
    [ -f "$T5B" ] || skip "tier5b-vm builder missing"

    TMP="$BATS_TEST_TMPDIR"
    SHIMS="$TMP/shims"
    mkdir -p "$SHIMS"
    CMDLOG="$TMP/cmdlog"
    : >"$CMDLOG"
    export CMDLOG

    # Generic logging shim: records "<name> <args...>" then succeeds.
    _make_shim() {
        local name="$1"
        cat >"$SHIMS/$name" <<SHIM
#!/bin/bash
printf '%s' "$name" >>"\$CMDLOG"
for a in "\$@"; do printf ' %s' "\$a" >>"\$CMDLOG"; done
printf '\n' >>"\$CMDLOG"
exit 0
SHIM
        chmod +x "$SHIMS/$name"
    }

    # Heavy/privileged externals get a logging stub. chmod/chown/mv/install
    # are stubbed too so we can assert the EXACT mode/path on the dest image
    # without touching the real filesystem.
    for c in virt-customize virt-sparsify virt-resize qemu-img wget \
             ninja chmod chown mv install shred fc-cache ls; do
        _make_shim "$c"
    done

    # id: always report root (uid 0) so the root guard passes; pass other
    # id invocations through to the real binary.
    cat >"$SHIMS/id" <<'SHIM'
#!/bin/bash
if [ "$1" = "-u" ]; then echo 0; exit 0; fi
exec /usr/bin/id "$@"
SHIM
    chmod +x "$SHIMS/id"

    # meson: the tier4 builder checks that compile produced specific output
    # artifacts and that they are executable. The shim creates them under the
    # build dir (using the REAL chmod so +x sticks) so the existence checks
    # pass, then logs + succeeds.
    cat >"$SHIMS/meson" <<'SHIM'
#!/bin/bash
printf 'meson' >>"$CMDLOG"; for a in "$@"; do printf ' %s' "$a" >>"$CMDLOG"; done; printf '\n' >>"$CMDLOG"
if [ "$1" = "setup" ]; then
    bdir="$2"
    mkdir -p "$bdir"
elif [ "$1" = "compile" ]; then
    # form: meson compile -C <builddir> <targets...>
    bdir=""
    while [ $# -gt 0 ]; do
        if [ "$1" = "-C" ]; then bdir="$2"; shift 2; continue; fi
        shift
    done
    [ -n "$bdir" ] && {
        mkdir -p "$bdir"
        : >"$bdir/qdwin-shell.so"
        : >"$bdir/qdwin-bystander"; /usr/bin/chmod +x "$bdir/qdwin-bystander" 2>/dev/null || true
        : >"$bdir/qdistro-forward"; /usr/bin/chmod +x "$bdir/qdistro-forward" 2>/dev/null || true
    }
fi
exit 0
SHIM
    chmod +x "$SHIMS/meson"

    export PATH="$SHIMS:$PATH"

    # Fake qdwin + qdistro source trees so tier4's prereq checks pass.
    FAKE_QDWIN="$TMP/qdwin"
    FAKE_QDISTRO="$TMP/qdistro"
    mkdir -p "$FAKE_QDWIN" "$FAKE_QDISTRO/daemons"
    : >"$FAKE_QDWIN/meson.build"
    : >"$FAKE_QDISTRO/daemons/meson.build"

    DEST4="$TMP/out-tier4.qcow2"
    DEST5="$TMP/out-tier5.qcow2"
    DEST5B="$TMP/out-tier5b.qcow2"
}

# ---- helpers ---------------------------------------------------------------

# grep the command log for a chmod against the dest image with an exact mode.
# Accepts both "640" and "0640" spellings; requires the dest path to follow.
chmod_on_dest() {  # <mode-without-leading-zero> <dest>
    grep -Eq "^chmod (0?$1) $2(\$| )" "$CMDLOG"
}

# did virt-customize get a --root-password arg?
vc_has_rootpw() {
    grep '^virt-customize ' "$CMDLOG" | grep -q -- '--root-password'
}

# Invoke a builder with optional per-mode VAR=val env pairs.
#
# Calling convention: run_tN [VAR=val ...]   (pairs only; builder args fixed)
#
# Hermetic env: a real qdistro dev host exports QDISTRO_VM_PASSWORD (and may
# export QDISTRO_GUEST_BAKE_DEBUG_PASSWORD) in the operator's shell. We MUST
# scrub both before each run so a test that means "no password set" actually
# sees none — otherwise the inherited password masks the guard. The per-test
# KV pairs are exported AFTER the scrub, so only the test's intent applies.
#
# We hand the KV pairs + the full command line to a single `bash -c` (rather
# than `run env <pairs> bash ...`, which under bats `run` mangles the empty-
# pair case). The empty-pair case is then a plain scrubbed `bash <builder>`.
_run_builder() {  # <n-pairs> <pair...> <builder> <builder-args...>
    run bash -c '
        unset QDISTRO_VM_PASSWORD QDISTRO_GUEST_BAKE_DEBUG_PASSWORD
        n="$1"; shift
        i=0
        while [ "$i" -lt "$n" ]; do export "$1"; shift; i=$((i+1)); done
        exec bash "$@"
    ' _ "$@"
}
run_t4() {
    local pairs=("$@")
    _run_builder "${#pairs[@]}" "${pairs[@]}" \
        "$T4" --force --dest "$DEST4" \
        --qdwin-src "$FAKE_QDWIN" --qdistro-src "$FAKE_QDISTRO"
}
run_t5() {
    local pairs=("$@")
    _run_builder "${#pairs[@]}" "${pairs[@]}" \
        "$T5" --force --dest "$DEST5"
}
run_t5b() {
    local pairs=("$@")
    _run_builder "${#pairs[@]}" "${pairs[@]}" \
        "$T5B" --force --dest "$DEST5B"
}

# ---- tier-4-guest ----------------------------------------------------------

@test "tier4: default mode bakes root password and writes 0640" {
    run_t4 QDISTRO_VM_PASSWORD=secret
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    vc_has_rootpw      || { echo "expected --root-password in default mode" >&2; cat "$CMDLOG" >&2; return 1; }
    chmod_on_dest 640 "$DEST4" || { echo "dest not chmod 0640" >&2; cat "$CMDLOG" >&2; return 1; }
    # mutation guard: must NOT be the old world-readable mode.
    ! chmod_on_dest 644 "$DEST4"
}

@test "tier4: hardened mode (flag=0) bakes NO password, still 0640, no pw needed" {
    # Deliberately do NOT set QDISTRO_VM_PASSWORD.
    run_t4 QDISTRO_GUEST_BAKE_DEBUG_PASSWORD=0
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    ! vc_has_rootpw    || { echo "hardened mode must NOT bake a password" >&2; cat "$CMDLOG" >&2; return 1; }
    chmod_on_dest 640 "$DEST4" || { echo "dest not chmod 0640" >&2; cat "$CMDLOG" >&2; return 1; }
}

@test "tier4: default mode requires QDISTRO_VM_PASSWORD" {
    run_t4
    [ "$status" -ne 0 ]
    [[ "$output" == *QDISTRO_VM_PASSWORD* ]]
}

# ---- tier-5 ----------------------------------------------------------------

@test "tier5: default mode bakes root password and writes 0640" {
    run_t5 QDISTRO_VM_PASSWORD=secret
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    vc_has_rootpw      || { echo "expected --root-password in default mode" >&2; cat "$CMDLOG" >&2; return 1; }
    chmod_on_dest 640 "$DEST5" || { echo "dest not chmod 0640" >&2; cat "$CMDLOG" >&2; return 1; }
    ! chmod_on_dest 644 "$DEST5"
}

@test "tier5: hardened mode (flag=0) bakes NO password, still 0640, no pw needed" {
    run_t5 QDISTRO_GUEST_BAKE_DEBUG_PASSWORD=0
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    ! vc_has_rootpw    || { echo "hardened mode must NOT bake a password" >&2; cat "$CMDLOG" >&2; return 1; }
    chmod_on_dest 640 "$DEST5" || { echo "dest not chmod 0640" >&2; cat "$CMDLOG" >&2; return 1; }
}

# ---- tier-5b ---------------------------------------------------------------

@test "tier5b: default mode bakes root password and writes 0640" {
    run_t5b QDISTRO_VM_PASSWORD=secret
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    vc_has_rootpw      || { echo "expected --root-password in default mode" >&2; cat "$CMDLOG" >&2; return 1; }
    chmod_on_dest 640 "$DEST5B" || { echo "dest not chmod 0640" >&2; cat "$CMDLOG" >&2; return 1; }
    ! chmod_on_dest 644 "$DEST5B"
}

@test "tier5b: hardened mode (flag=0) bakes NO password, still 0640, no pw needed" {
    run_t5b QDISTRO_GUEST_BAKE_DEBUG_PASSWORD=0
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    ! vc_has_rootpw    || { echo "hardened mode must NOT bake a password" >&2; cat "$CMDLOG" >&2; return 1; }
    chmod_on_dest 640 "$DEST5B" || { echo "dest not chmod 0640" >&2; cat "$CMDLOG" >&2; return 1; }
}

@test "tier5b: default mode requires QDISTRO_VM_PASSWORD" {
    run_t5b
    [ "$status" -ne 0 ]
    [[ "$output" == *QDISTRO_VM_PASSWORD* ]]
}

# ---- cross-script contract consistency -------------------------------------

@test "all three builders share the QDISTRO_GUEST_BAKE_DEBUG_PASSWORD flag name" {
    grep -q 'QDISTRO_GUEST_BAKE_DEBUG_PASSWORD' "$T4"
    grep -q 'QDISTRO_GUEST_BAKE_DEBUG_PASSWORD' "$T5"
    grep -q 'QDISTRO_GUEST_BAKE_DEBUG_PASSWORD' "$T5B"
}

@test "no builder hardcodes the old world-readable 0644 on the dest image" {
    # The only 0644 chmods that may remain are guest-internal (/etc/fstab
    # etc.) run via virt-customize --run-command, never on "$DEST".
    ! grep -Eq 'chmod 0644 "\$DEST"' "$T4"
    ! grep -Eq 'chmod 0644 "\$DEST"' "$T5"
    ! grep -Eq 'chmod 0644 "\$DEST"' "$T5B"
}

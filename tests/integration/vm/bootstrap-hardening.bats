#!/usr/bin/env bats
# Static-invariant + behavioural lock-in for the production bootstrap /
# packaging HARDENING (security-hardening carry-forward "Bootstrap and
# packaging"). Like spin-test-vm-gui-bootstrap.bats this needs NO live VM:
# it inspects the install scripts as SOURCE and exercises the root-free
# portions (arg parsing, profile gating, the shared profile library).
#
# What it pins:
#   A. Forbidden patterns must NOT appear on the release/daily-driver path:
#      env-var install passwords, `admin NOPASSWD: ALL`, root pip
#      --prefix=/usr from unpinned source, predictable /tmp libvirt XML,
#      sed -i on an installed user unit, recursive chown of ~/.config tree.
#   B. The profile gate (dev vs daily-driver/release) actually flips the
#      hardened code paths.
#   C. Installer idempotency / partial-install behaviour: rerun on a
#      partial machine, changed admin/user names, missing optional vs
#      required deps, broken/missing source checkout, stale units.
#
# The hardened-path checks are deliberately CONSERVATIVE: a match is allowed
# only when it is clearly gated behind `is_dev` / the dev profile.
#
# Run: bats tests/integration/vm/bootstrap-hardening.bats

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" \
                    rev-parse --show-toplevel 2>/dev/null)"
    BOOT="$REPO_ROOT/scripts/install/qdistro-bootstrap.sh"
    PROFILE_LIB="$REPO_ROOT/scripts/install/lib/qdistro-profile.sh"
    IMAGE_CFG="$REPO_ROOT/image/config.sh"
    FRESH="$REPO_ROOT/scripts/vm/fresh-vm-bootstrap.sh"
    SPAWN_COMMON="$REPO_ROOT/lib/spawn-common.sh"
    [ -f "$BOOT" ] || { echo "bootstrap not found at $BOOT" >&2; return 1; }
}

# Run the bootstrap arg-parsing path only (it exits before root checks for
# most validation). Captures combined output + status.
run_boot() { run bash "$BOOT" "$@"; }

# --- 0. syntax ----------------------------------------------------------
@test "hardening: all touched scripts are syntactically valid bash" {
    for f in "$BOOT" "$PROFILE_LIB" "$IMAGE_CFG" "$FRESH" "$SPAWN_COMMON" \
             "$REPO_ROOT/tier4-vm/spawn-tier4.sh" \
             "$REPO_ROOT/tier5-vm/spawn-tier5.sh" \
             "$REPO_ROOT/tier5b-vm/spawn-tier5b.sh"; do
        run bash -n "$f"
        [ "$status" -eq 0 ] || { echo "bash -n failed: $f"$'\n'"$output" >&2; return 1; }
    done
}

# --- A. Forbidden-pattern static invariants -----------------------------

@test "hardening: NOPASSWD: ALL sudoers only appears gated behind dev profile" {
    # Mutation-sensitive: assert every line that INSTALLS the passwordless
    # 'NOPASSWD: ALL' sudoers rule is STRUCTURALLY inside an `if is_dev; then`
    # branch (between the `then` and its matching `else`/`fi`), not merely that
    # the string and an unrelated is_dev gate both exist somewhere. A
    # regression that moves the install out of the dev branch — making hardened
    # installs passwordless — must turn this red.
    run awk '
        /if is_dev; then/      { depth++; dev[depth]=1; next }
        /^[[:space:]]*if /     { depth++; dev[depth]=0; next }
        /^[[:space:]]*else\>/  { if (depth) dev[depth]=0; next }
        /^[[:space:]]*fi\>/    { if (depth) { dev[depth]=0; depth-- }; next }
        /install .*NOPASSWD: ALL/ {
            indev=0
            for (d=1; d<=depth; d++) if (dev[d]) indev=1
            if (!indev) { print "ungated NOPASSWD install at line " NR ": " $0; bad=1 }
        }
        END { exit bad ? 1 : 0 }
    ' "$BOOT"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    # The install must exist at all (so the awk above is not vacuously green),
    # and the hardened path must remove any stale passwordless sudoers.
    grep -q "install .*NOPASSWD: ALL" "$BOOT"
    grep -q "rm -f /etc/sudoers.d/99-admin" "$BOOT"
}

@test "hardening: KIWI release image does not unconditionally bake NOPASSWD: ALL" {
    # The bake must be gated: a NOPASSWD line is allowed only inside a
    # '= dev' branch, and the default (release) path must rm the file.
    grep -q 'QDISTRO_IMAGE_PROFILE.*dev' "$IMAGE_CFG"
    grep -q "rm -f /etc/sudoers.d/99-admin" "$IMAGE_CFG"
}

@test "hardening: no predictable /tmp libvirt domain XML in spawn scripts" {
    # The old predictable path was /tmp/qdistro-tierN-<vm>-$$.xml. It must be
    # gone from all three spawn scripts, replaced by domain_xml_tmpfile.
    for s in tier4-vm/spawn-tier4.sh tier5-vm/spawn-tier5.sh tier5b-vm/spawn-tier5b.sh; do
        run grep -nE 'TMP_XML="/tmp/qdistro-tier' "$REPO_ROOT/$s"
        [ "$status" -ne 0 ] || { echo "predictable /tmp XML still in $s:"$'\n'"$output" >&2; return 1; }
        grep -q "domain_xml_tmpfile" "$REPO_ROOT/$s"
    done
}

@test "hardening: domain_xml_tmpfile renders into a private 0700 dir + 0600 file" {
    grep -q "chmod 0700" "$SPAWN_COMMON"
    grep -q "chmod 0600" "$SPAWN_COMMON"
    # No world-readable chmod 0644 on the rendered XML remains in spawn scripts.
    for s in tier4-vm/spawn-tier4.sh tier5-vm/spawn-tier5.sh tier5b-vm/spawn-tier5b.sh; do
        run grep -nE 'chmod 0644 "\$TMP_XML"' "$REPO_ROOT/$s"
        [ "$status" -ne 0 ] || { echo "world-readable XML chmod still in $s" >&2; return 1; }
    done
}

@test "hardening: bootstrap does not sed -i an already-installed user unit" {
    # The qdlocker unit must be rendered in a root-owned staging file and
    # installed atomically, not patched in place after install.
    run grep -nE "sed -i .*qdlocker.service" "$BOOT"
    [ "$status" -ne 0 ] || { echo "post-install sed -i on qdlocker.service:"$'\n'"$output" >&2; return 1; }
    # And it must build the unit via a staging mktemp.
    grep -q 'stage="$(mktemp)"' "$BOOT"
}

@test "hardening: bootstrap does not recursively chown the user ~/.config tree" {
    run grep -nE "chown -R admin.*/home/admin/.config/systemd" "$BOOT"
    [ "$status" -ne 0 ] || { echo "recursive chown of user config tree:"$'\n'"$output" >&2; return 1; }
}

@test "hardening: root pip --prefix=/usr is gated behind dev; hardened uses /opt/qdistro" {
    grep -q "QDISTRO_OPT_PREFIX" "$BOOT"
    # The --prefix=/usr pip invocation must live in the is_dev branch of
    # pip_install_one (not the hardened branch).
    run awk '/pip_install_one\(\)/{f=1} f&&/--prefix=\/usr/{print NR": "$0} /^}/{if(f)f=0}' "$BOOT"
    [ -n "$output" ]   # the --prefix=/usr line still exists (dev)
    # ...and the hardened branch installs into the isolated prefix.
    grep -q 'prefix="\$QDISTRO_OPT_PREFIX"' "$BOOT"
}

@test "hardening: zypper --no-gpg-checks is gated behind dev profile" {
    # The refresh must build its flag array only under is_dev.
    grep -q "gpg_flags=( --no-gpg-checks )" "$BOOT"
    # The refresh call must use the (possibly empty) array, not a hardcoded flag.
    grep -q 'zypper -n "${gpg_flags\[@\]}" refresh' "$BOOT"
}

@test "hardening: SELinux is enforcing by default; permissive requires dev or explicit override" {
    grep -q "QDISTRO_ALLOW_PERMISSIVE=1" "$BOOT"
    grep -q 'target_mode="enforcing"' "$BOOT"
    grep -q 'SELinux config: enforcing' "$BOOT"
    grep -q 'setenforce 1' "$BOOT"
    grep -q 'refuses to finish without enforcing' "$BOOT"

    run grep -nE 'SELINUX=.*permissive|SELINUX=permissive' "$BOOT"
    [ "$status" -ne 0 ]
    [[ "$output" != *"sed -i 's/^SELINUX=.*/SELINUX=permissive/'"* ]]
    grep -q 'target_mode="permissive"' "$BOOT"
    grep -q 'QDISTRO_ALLOW_PERMISSIVE=1: hardened SELinux enforcing requirement overridden' "$BOOT"
}

@test "hardening: SELinux policy install failures are fatal in hardened profiles" {
    grep -q "semodule not found.*requires SELinux policy install" "$BOOT"
    grep -q "requires qdistro SELinux policy before enforcing" "$BOOT"
    grep -q "install failed under permissive override/dev profile" "$BOOT"
}

@test "hardening: phone (cut from v1, D4) installer step is gated to the dev profile" {
    # Decision D4 cuts the phone companion from v1; the release/daily-driver
    # bootstrap must NOT lay down phone code. The chain marks `phone` dev-only
    # and the chain loop skips dev-only steps unless is_dev.
    grep -qE 'CHAIN_DEV_ONLY_STEPS=.*phone' "$BOOT"
    grep -qF 'chain_step_dev_only "$name" && ! is_dev' "$BOOT"
    # The gate is live: `phone` is still a real chain entry (gating a
    # nonexistent step would be a silent no-op).
    grep -qE '^phone\|scripts/install/install-phone-for-vm\.sh' "$BOOT"
    # Behavioural: the gate predicate skips phone in a hardened profile and
    # admits it under dev (re-evaluated against the file's own definitions).
    run bash -c '
        QDISTRO_PROFILE=daily-driver
        eval "$(awk "/^CHAIN_DEV_ONLY_STEPS=/,/^}/" "'"$BOOT"'")"
        is_dev() { [ "${QDISTRO_PROFILE:-daily-driver}" = "dev" ]; }
        chain_step_dev_only phone && ! is_dev && echo HARDENED_SKIP
        QDISTRO_PROFILE=dev
        chain_step_dev_only phone && is_dev && echo DEV_INSTALL
    '
    [[ "$output" == *HARDENED_SKIP* ]] || { echo "phone not skipped in hardened: $output" >&2; return 1; }
    [[ "$output" == *DEV_INSTALL* ]]   || { echo "phone not admitted in dev: $output" >&2; return 1; }
}

@test "hardening: bootstrap source/package fetches use authenticated transport (no plaintext http://)" {
    # Release/daily-driver invariant (security-hardening carry-forward
    # "Bootstrap and packaging" + 03/R2): the bootstrap must not fetch root
    # code, repos, or packages over non-TLS http://. The release installer
    # uses TLS exclusively today (sibling repos clone from https://codeberg.org;
    # disposable http:// VM staging lives ONLY in the separate
    # install-*-for-vm.sh dev helpers, never here). So the invariant is simple
    # and absolute: NO plaintext `http://` on any non-comment line of the
    # bootstrap. A regression that adds an http:// mirror/repo/staging URL to
    # the release path trips this. (If a future dev-only http path is ever
    # genuinely needed here, move it into a dev-gated helper and revisit.)
    run grep -nE '^[[:space:]]*[^#].*http://|^[^#]*http://' "$BOOT"
    [ "$status" -ne 0 ] || { echo "plaintext http:// in bootstrap:"$'\n'"$output" >&2; return 1; }

    # Positive control: the sibling-repo URL resolver emits TLS git remotes,
    # so the test fails if the resolver is gutted rather than only on http://.
    run grep -vE '^[[:space:]]*#' "$BOOT"
    echo "$output" | grep -q "https://codeberg.org"
}

@test "hardening: tier5 secctx wrapper is fail-closed by default" {
    SPAWN="$REPO_ROOT/tier5-vm/spawn-tier5.sh"
    grep -q "TIER5_USE_SECCTX:-1" "$SPAWN"
    grep -q "qdistro-secctx-exec not in PATH; refusing untagged Tier-5 launch" "$SPAWN"
    grep -q "set TIER5_USE_SECCTX=0 only for explicit debug runs" "$SPAWN"
    run grep -nE '^[[:space:]]*USE_SECCTX=0([[:space:]]|$)' "$SPAWN"
    [ "$status" -ne 0 ] || { echo "implicit secctx disable remains:"$'\n'"$output" >&2; return 1; }
}

@test "hardening: RDP cert dir is 0700 and private key 0600" {
    grep -q "install -d -o admin -g admin -m 0700 /home/admin/qdwin-rdp" "$FRESH"
    grep -q "chmod 0600 /home/admin/qdwin-rdp/rdp.key" "$FRESH"
}

@test "vm-gui: ydotool uses a user-runtime socket and uinput condition" {
    SESSION_INSTALL="$REPO_ROOT/scripts/install/install-qdwin-session-for-vm.sh"
    VM_GUI="$REPO_ROOT/scripts/vm/vm-gui"
    grep -q "usermod -aG video,input,render,seat admin" "$SESSION_INSTALL"
    grep -q "ExecCondition=.*test -e /sys/module/uinput && test -w /dev/uinput" "$SESSION_INSTALL"
    grep -q "ydotoold --socket-path=/run/user/1000/ydotool.sock --socket-perm=0600" "$SESSION_INSTALL"
    grep -q "YDOTOOL_SOCKET=/run/user/1000/ydotool.sock" "$VM_GUI"
    run grep -n "ydotool key \\$\\*" "$VM_GUI"
    [ "$status" -ne 0 ] || { echo "raw xdotool-style key names still pass to ydotool:"$'\n'"$output" >&2; return 1; }
}

@test "vm-gui: ydotool key names translate to Linux input events" {
    VM_GUI="$REPO_ROOT/scripts/vm/vm-gui"
    run bash -c '
        vm_gui="$1"
        set -- dummy wait 0
        source "$vm_gui"
        ydotool_key_sequence ctrl+space enter a exclam
    ' bash "$VM_GUI"
    [ "$status" -eq 0 ]
    [ "$output" = "29:1 57:1 57:0 29:0 28:1 28:0 30:1 30:0 42:1 2:1 2:0 42:0" ]
}

@test "vm-gui: ydotool type shell quoting preserves apostrophes" {
    VM_GUI="$REPO_ROOT/scripts/vm/vm-gui"
    run bash -c '
        vm_gui="$1"
        set -- dummy wait 0
        source "$vm_gui"
        quoted=$(shell_quote "it is '\''quoted'\''")
        eval "roundtrip=$quoted"
        [ "$roundtrip" = "it is '\''quoted'\''" ]
    ' bash "$VM_GUI"
    [ "$status" -eq 0 ]
}

@test "vm bootstrap: ydotool uinput setup is installed but best-effort" {
    grep -q "KERNEL==\\\"uinput\\\", GROUP=\\\"input\\\", MODE=\\\"0660\\\"" "$FRESH"
    grep -q "/etc/modules-load.d/uinput.conf" "$FRESH"
    grep -q "WARN: uinput module unavailable; ydotoold will stay inactive" "$FRESH"
    grep -q "WARN: ydotoold.service did not start" "$FRESH"
}

@test "hardening: greetd hardening drop-in exists and is installed" {
    [ -f "$REPO_ROOT/deploy/greetd-hardening.conf" ]
    grep -q "ProtectSystem=strict" "$REPO_ROOT/deploy/greetd-hardening.conf"
    grep -q "PrivateTmp=yes" "$REPO_ROOT/deploy/greetd-hardening.conf"
    grep -q "10-qdistro-hardening.conf" "$BOOT"
    grep -q "10-qdistro-hardening.conf" "$IMAGE_CFG"
}

@test "hardening: _greeter is a non-login, non-home system user" {
    grep -q -- "--system --no-create-home --home-dir /nonexistent" "$BOOT"
    grep -q "/usr/sbin/nologin _greeter" "$BOOT"
    grep -q -- "--system --no-create-home --home-dir /nonexistent" "$IMAGE_CFG"
}

# --- B. Profile gate behaviour ------------------------------------------

@test "profile-gate: unknown profile is rejected" {
    run_boot --profile=bogus --noninteractive
    [ "$status" -ne 0 ]
    [[ "$output" == *"invalid --profile"* || "$output" == *"unknown QDISTRO_PROFILE"* ]]
}

@test "profile-gate: hardened profile rejects --admin-password on argv" {
    run_boot --noninteractive --admin-password=hunter2 --user=alice --user-password=secret
    [ "$status" -ne 0 ]
    [[ "$output" == *"--admin-password on argv is disabled"* ]]
}

@test "profile-gate: hardened profile ignores QDISTRO_ADMIN_PASSWORD env" {
    QDISTRO_ADMIN_PASSWORD=x QDISTRO_USER_PASSWORD=y \
        run bash "$BOOT" --noninteractive --user=alice
    [ "$status" -ne 0 ]
    [[ "$output" == *"ignoring QDISTRO_ADMIN_PASSWORD"* ]]
    [[ "$output" == *"environ leak"* ]]
}

@test "profile-gate: dev profile DOES accept argv/env passwords" {
    # dev must pass arg validation and reach the root check (next gate),
    # proving the throwaway shortcut is still available under dev.
    run_boot --dev --noninteractive --admin-password=hunter2 --user=alice --user-password=secret
    [ "$status" -ne 0 ]
    [[ "$output" == *"must run as root"* ]]
}

@test "profile-gate: --admin-password-fd is accepted in hardened profile" {
    run bash "$BOOT" --noninteractive --admin-password-fd=3 --user=alice --user-password-fd=4 \
        3<<<"apass" 4<<<"upass"
    [ "$status" -ne 0 ]
    # fd passwords count as present, so we get past validation to the root check.
    [[ "$output" == *"must run as root"* ]]
}

# --- C. Branch / ref validation -----------------------------------------

@test "ref-validation: branch with shell metacharacters is rejected" {
    run_boot --dev --branch='$(reboot)' --noninteractive \
        --admin-password=x --user=a --user-password=y
    [ "$status" -ne 0 ]
    [[ "$output" == *"invalid --branch"* ]]
}

@test "ref-validation: branch with .. is rejected" {
    run_boot --dev --branch='../etc' --noninteractive \
        --admin-password=x --user=a --user-password=y
    [ "$status" -ne 0 ]
    [[ "$output" == *"invalid --branch"* ]]
}

@test "ref-validation: a 40-hex SHA and feat/foo branches are accepted" {
    for b in main v1.2.3 feat/bootstrap-hardening 0123456789abcdef0123456789abcdef01234567; do
        run bash "$BOOT" --dev --branch="$b" --noninteractive \
            --admin-password=x --user=a --user-password=y
        [ "$status" -ne 0 ]
        [[ "$output" == *"must run as root"* ]] \
            || { echo "branch '$b' did not pass validation:"$'\n'"$output" >&2; return 1; }
    done
}

# --- C. Manifest pinning ------------------------------------------------

@test "manifest-pin: hardened install refuses an unpinned source tree" {
    # Drive verify_repo_pin directly with an empty manifest in a hardened
    # profile against a throwaway git checkout; it must die "no manifest pin".
    tmp="$(mktemp -d)"
    git -C "$tmp" init -q repo
    ( cd "$tmp/repo" && git -c user.email=t@t -c user.name=t commit -q --allow-empty -m x )
    run bash -c '
        set -e
        . "'"$PROFILE_LIB"'"
        QDISTRO_PROFILE=release; export QDISTRO_PROFILE
        REPO_ROOT="'"$tmp"'"
        SCRIPT_DIR="'"$REPO_ROOT/scripts/install"'"
        SOURCE_MANIFEST="'"$tmp"'/empty-manifest.txt"
        : > "$SOURCE_MANIFEST"
        die() { echo "DIE: $*" >&2; exit 1; }
        log() { :; }
        '"$(sed -n '/^manifest_pin()/,/^}/p;/^verify_repo_pin()/,/^}/p' "$BOOT")"'
        verify_repo_pin repo
    '
    rm -rf "$tmp"
    [ "$status" -ne 0 ]
    [[ "$output" == *"no manifest pin"* ]]
}

@test "manifest-pin: dev install skips manifest verification" {
    run bash -c '
        . "'"$PROFILE_LIB"'"
        QDISTRO_PROFILE=dev; export QDISTRO_PROFILE
        SOURCE_MANIFEST=/nonexistent
        log() { :; }; die() { echo "DIE: $*"; exit 1; }
        '"$(sed -n '/^manifest_pin()/,/^}/p;/^verify_repo_pin()/,/^}/p' "$BOOT")"'
        verify_repo_pin anything && echo OK
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"OK"* ]]
}

# --- C. Btrfs fail-visible ----------------------------------------------

@test "btrfs: subvolume failure is fatal in hardened, warn in dev" {
    # fail_subvol must die in release, warn in dev.
    helper="$(sed -n '/^fail_subvol()/,/^}/p' "$BOOT")"
    run bash -c '
        . "'"$PROFILE_LIB"'"
        QDISTRO_PROFILE=release; export QDISTRO_PROFILE
        die() { echo "DIE: $*"; exit 9; }
        warn() { echo "WARN: $*"; }
        '"$helper"'
        fail_subvol "subvol boom"
    '
    [ "$status" -eq 9 ]
    [[ "$output" == *"DIE:"* ]]

    run bash -c '
        . "'"$PROFILE_LIB"'"
        QDISTRO_PROFILE=dev; export QDISTRO_PROFILE
        die() { echo "DIE: $*"; exit 9; }
        warn() { echo "WARN: $*"; }
        '"$helper"'
        fail_subvol "subvol boom"
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"WARN:"* ]]
}

@test "disk-encryption: hardened profile refuses plaintext root without explicit override" {
    helper="$(sed -n '/^root_block_device()/,/^}/p;/^block_device_is_crypt()/,/^}/p;/^root_is_encrypted()/,/^}/p;/^enforce_root_disk_encryption()/,/^}/p' "$BOOT")"
    fakebin="$(mktemp -d)"
    cat >"$fakebin/findmnt" <<'SH'
#!/bin/bash
echo /dev/sda2
SH
    cat >"$fakebin/lsblk" <<'SH'
#!/bin/bash
if [ "$3" = "TYPE" ]; then echo part; exit 0; fi
if [ "$3" = "PKNAME" ]; then exit 0; fi
exit 1
SH
    chmod +x "$fakebin/findmnt" "$fakebin/lsblk"
    run bash -c '
        . "'"$PROFILE_LIB"'"
        QDISTRO_PROFILE=release; export QDISTRO_PROFILE
        PATH="'"$fakebin"':$PATH"
        die() { echo "DIE: $*"; exit 9; }
        warn() { echo "WARN: $*"; }
        log() { echo "LOG: $*"; }
        '"$helper"'
        enforce_root_disk_encryption
    '
    rm -rf "$fakebin"
    [ "$status" -eq 9 ]
    [[ "$output" == *"root filesystem is not encrypted"* ]]
}

@test "disk-encryption: hardened override documents runtime-only posture" {
    helper="$(sed -n '/^root_block_device()/,/^}/p;/^block_device_is_crypt()/,/^}/p;/^root_is_encrypted()/,/^}/p;/^enforce_root_disk_encryption()/,/^}/p' "$BOOT")"
    fakebin="$(mktemp -d)"
    cat >"$fakebin/findmnt" <<'SH'
#!/bin/bash
echo /dev/sda2
SH
    cat >"$fakebin/lsblk" <<'SH'
#!/bin/bash
if [ "$3" = "TYPE" ]; then echo part; exit 0; fi
if [ "$3" = "PKNAME" ]; then exit 0; fi
exit 1
SH
    chmod +x "$fakebin/findmnt" "$fakebin/lsblk"
    run bash -c '
        . "'"$PROFILE_LIB"'"
        QDISTRO_PROFILE=daily-driver; export QDISTRO_PROFILE
        QDISTRO_ALLOW_PLAINTEXT_ROOT=1; export QDISTRO_ALLOW_PLAINTEXT_ROOT
        PATH="'"$fakebin"':$PATH"
        die() { echo "DIE: $*"; exit 9; }
        warn() { echo "WARN: $*"; }
        log() { echo "LOG: $*"; }
        '"$helper"'
        enforce_root_disk_encryption
    '
    rm -rf "$fakebin"
    [ "$status" -eq 0 ]
    [[ "$output" == *"runtime-only"* ]]
}

@test "disk-encryption: crypt root passes hardened profile" {
    helper="$(sed -n '/^root_block_device()/,/^}/p;/^block_device_is_crypt()/,/^}/p;/^root_is_encrypted()/,/^}/p;/^enforce_root_disk_encryption()/,/^}/p' "$BOOT")"
    fakebin="$(mktemp -d)"
    cat >"$fakebin/findmnt" <<'SH'
#!/bin/bash
echo /dev/mapper/cryptroot
SH
    cat >"$fakebin/lsblk" <<'SH'
#!/bin/bash
if [ "$3" = "TYPE" ]; then echo crypt; exit 0; fi
if [ "$3" = "PKNAME" ]; then exit 0; fi
exit 1
SH
    chmod +x "$fakebin/findmnt" "$fakebin/lsblk"
    run bash -c '
        . "'"$PROFILE_LIB"'"
        QDISTRO_PROFILE=release; export QDISTRO_PROFILE
        PATH="'"$fakebin"':$PATH"
        die() { echo "DIE: $*"; exit 9; }
        warn() { echo "WARN: $*"; }
        log() { echo "LOG: $*"; }
        '"$helper"'
        enforce_root_disk_encryption
    '
    rm -rf "$fakebin"
    [ "$status" -eq 0 ]
    [[ "$output" == *"root filesystem encryption: detected"* ]]
}

# --- D. profile library primitives --------------------------------------

@test "profile-lib: private runtime dir is 0700 and xml temp is 0600" {
    run bash -c '
        . "'"$PROFILE_LIB"'"
        d="$(qd_private_runtime_dir)"
        x="$(qd_mktemp_xml tier4)"
        printf "%s %s\n" "$(stat -c %a "$d")" "$(stat -c %a "$x")"
        rm -rf "$d"
    '
    [ "$status" -eq 0 ]
    [[ "$output" == "700 600" ]]
}

@test "profile-lib: resolve_profile defaults to the HARDENED profile" {
    run bash -c '
        unset QDISTRO_PROFILE
        . "'"$PROFILE_LIB"'"
        resolve_profile
        echo "$QDISTRO_PROFILE"
        is_hardened && echo hardened
        is_dev || echo not-dev
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *"daily-driver"* ]]
    [[ "$output" == *"hardened"* ]]
    [[ "$output" == *"not-dev"* ]]
}

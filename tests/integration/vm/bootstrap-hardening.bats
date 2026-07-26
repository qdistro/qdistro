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
    HARDEN_VT="$REPO_ROOT/scripts/install/harden-compositor-vt.sh"
    [ -f "$BOOT" ] || { echo "bootstrap not found at $BOOT" >&2; return 1; }
}

# Run the bootstrap arg-parsing path only (it exits before root checks for
# most validation). Captures combined output + status.
run_boot() { run bash "$BOOT" "$@"; }

# --- 0. syntax ----------------------------------------------------------
@test "hardening: all touched scripts are syntactically valid bash" {
    for f in "$BOOT" "$PROFILE_LIB" "$IMAGE_CFG" "$FRESH" "$SPAWN_COMMON" \
             "$HARDEN_VT" \
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
    # uses TLS exclusively today (sibling repos clone from https://github.com;
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
    echo "$output" | grep -q "https://github.com"
}

@test "hardening: bootstrap clone URLs point at the current forge (github.com) only" {
    # The project migrated off Codeberg to GitHub (2026-07). A clone URL left
    # on the old forge sends a clean-room install at a repo the project no
    # longer publishes to, so pin the host: every git remote the bootstrap
    # emits must be https://github.com/, and the retired forge must not
    # reappear on any executable line.
    run grep -nE '^[[:space:]]*[^#].*codeberg\.org' "$BOOT"
    [ "$status" -ne 0 ] || { echo "retired forge (codeberg.org) in bootstrap:"$'\n'"$output" >&2; return 1; }

    # Every `.git` clone URL emitted by the resolver is a github.com URL.
    run grep -oE 'https://[A-Za-z0-9._/-]+\.git' "$BOOT"
    [ "$status" -eq 0 ] || { echo "no clone URLs found in bootstrap" >&2; return 1; }
    local url
    while read -r url; do
        [ -z "$url" ] && continue
        case "$url" in
            https://github.com/*) ;;
            *) echo "non-github clone URL in bootstrap: $url" >&2; return 1 ;;
        esac
    done <<< "$output"

    # The renamed repos must resolve to their POST-migration upstream names,
    # not their local checkout names (qfileman -> qdfileman, qterminator ->
    # qdterm), or a clean-room install 404s on clone.
    eval "$(awk '/^repo_url\(\)/,/^}/' "$BOOT")"
    [ "$(repo_url qdistro)" = "https://github.com/qdistro/qdistro.git" ]
    [ "$(repo_url qdwin)" = "https://github.com/qdistro/qdwin.git" ]
    [ "$(repo_url qdshell)" = "https://github.com/qdistro/qdshell.git" ]
    [ "$(repo_url qdlocker)" = "https://github.com/qdistro/qdlocker.git" ]
    [ "$(repo_url qdbrowser)" = "https://github.com/qdistro/qdbrowser.git" ]
    [ "$(repo_url qdgreeter)" = "https://github.com/qdistro/qdgreeter.git" ]
    [ "$(repo_url qfileman)" = "https://github.com/qdistro/qdfileman.git" ]
    [ "$(repo_url qnotebook)" = "https://github.com/qnotebook/qnotebook.git" ]
    [ "$(repo_url qterminator)" = "https://github.com/qterminator/qdterm.git" ]
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

# --- A2. Compositor-VT isolation ----------------------------------------
# The locked-session VT escape: if a getty can take the compositor's VT its
# start-time TTY reset reverts seatd's K_OFF, and keystrokes typed at a locked
# screen fall through to the kernel console into login(1) (the unlock password
# is then recorded in cleartext as a failed-login username). These are STATIC
# invariants only — the load-bearing runtime guard is
# tests/integration/vm/probes/vt-escape-lockdown.sh.

# bats + `set -e`: a `! cmd` that is NOT the last command of a test has its
# failure swallowed, so plain `! grep ...` lines assert nothing. Every negative
# check below therefore goes through `run` + an explicit status assertion.
refute_grep() { # refute_grep <pattern> <file>... — fails if the pattern matches
    run grep -nE "$@"
    [ "$status" -ne 0 ] || { echo "unexpected match:"$'\n'"$output" >&2; return 1; }
}

@test "vt-isolation: both install paths INVOKE harden-compositor-vt.sh" {
    [ -x "$HARDEN_VT" ]
    # Match the invocation, not a comment mentioning the filename: commenting
    # the call out while leaving the rationale behind must turn this red.
    grep -qE '^[^#]*harden-compositor-vt\.sh' "$BOOT"
    grep -qE '^[^#]*harden-compositor-vt\.sh' "$IMAGE_CFG"
}

@test "vt-isolation: the compositor VT is MASKED, not merely disabled" {
    # Mutation-sensitive: logind starts autovt@ttyN *by unit name* on demand
    # for any VT inside NAutoVTs, so `systemctl disable` does not stop it.
    # Downgrading this to disable-only must turn the test red.
    grep -qE 'systemctl mask "\$unit"' "$HARDEN_VT"
    grep -qE '\[ "\$state" != "masked" \]' "$HARDEN_VT"
    # ...and the units it acts on are scoped to the compositor VT.
    grep -q 'getty@tty\$VT.service autovt@tty\$VT.service' "$HARDEN_VT"
}

@test "vt-isolation: a running getty is stopped before masking" {
    # Masking alone leaves an already-running instance holding the VT with a
    # reset keyboard until reboot.
    grep -q 'systemctl stop "\$unit"' "$HARDEN_VT"
}

@test "vt-isolation: the VT is parsed from greetd's [terminal] table, not hardcoded" {
    grep -q 'section == "terminal"' "$HARDEN_VT"
    refute_grep 'getty@tty3\.service' "$HARDEN_VT"
}

@test "vt-isolation: an undeterminable VT is FAIL-CLOSED, not a silent skip" {
    # The whole fatal/warn wiring is decorative if "I could not work out what
    # to harden" exits 0 — that is the likeliest real-world failure.
    run bash "$HARDEN_VT" "$BATS_TEST_TMPDIR/nonexistent.toml"
    [ "$status" -ne 0 ]
    [[ "$output" == *"refusing to report a hardened install"* ]]

    # greetd's own `vt = "next"` is legal config that makes the unit name
    # unknowable at install time; it must fail with an actionable message.
    printf '[terminal]\nvt = "next"\n' > "$BATS_TEST_TMPDIR/next.toml"
    run bash "$HARDEN_VT" "$BATS_TEST_TMPDIR/next.toml"
    [ "$status" -ne 0 ]
    [[ "$output" == *"pin a numeric"* ]]
}

@test "vt-isolation: the parser accepts quoted values and rejects conflicts" {
    extract_vt_parser
    printf '[terminal]\r\nvt = "3"  ; trailing\r\n' > "$BATS_TEST_TMPDIR/quoted.toml"
    run bash -c ". '$BATS_TEST_TMPDIR/parser.sh'; greetd_compositor_vt '$BATS_TEST_TMPDIR/quoted.toml'"
    [ "$status" -eq 0 ]
    [ "$output" = "3" ]

    # Two disagreeing [terminal] tables must not silently pick one — masking
    # the wrong VT would brick the login path.
    printf '[terminal]\nvt = 3\n[terminal]\nvt = 5\n' > "$BATS_TEST_TMPDIR/conflict.toml"
    run bash -c ". '$BATS_TEST_TMPDIR/parser.sh'; greetd_compositor_vt '$BATS_TEST_TMPDIR/conflict.toml'"
    [ "$status" -ne 0 ]
}

@test "vt-isolation: a quoted vt value with trailing junk is rejected" {
    # Stripping ;/# comments BEFORE closing the quote would read
    # `vt = "1;not-comment"` as 1 — a plausible-but-wrong VT, and 1 is tty1,
    # the emergency console. Must be an error, not a silent mis-mask.
    extract_vt_parser
    for bad in '"1;not-comment"' '"3;not-comment"' '"3' '"3" junk'; do
        printf '[terminal]\nvt = %s\n' "$bad" > "$BATS_TEST_TMPDIR/junk.toml"
        run bash -c ". '$BATS_TEST_TMPDIR/parser.sh'; greetd_compositor_vt '$BATS_TEST_TMPDIR/junk.toml'"
        [ "$status" -ne 0 ] || { echo "accepted malformed vt value: $bad -> $output" >&2; return 1; }
    done
}

@test "vt-isolation: a compositor VT of tty1 is refused, not masked" {
    # doc/recovery.md makes tty1 the last-resort login. Hardening a compositor
    # configured there would mask the emergency console.
    extract_vt_parser
    printf '[terminal]\nvt = 1\n' > "$BATS_TEST_TMPDIR/tty1.toml"
    run bash -c ". '$BATS_TEST_TMPDIR/parser.sh'; greetd_compositor_vt '$BATS_TEST_TMPDIR/tty1.toml'"
    [ "$status" -ne 0 ]

    run bash "$HARDEN_VT" "$BATS_TEST_TMPDIR/tty1.toml"
    [ "$status" -ne 0 ]
    [[ "$output" == *"emergency console"* ]]
}

# Extract greetd_compositor_vt() from the installer and run it standalone,
# so the parser is exercised as shipped rather than reimplemented here.
extract_vt_parser() {
    sed -n '/^greetd_compositor_vt()/,/^}/p' "$HARDEN_VT" \
        > "$BATS_TEST_TMPDIR/parser.sh"
    [ -s "$BATS_TEST_TMPDIR/parser.sh" ]
}

@test "vt-isolation: the parser reads vt=3 out of the shipped greetd config" {
    extract_vt_parser
    run bash -c ". '$BATS_TEST_TMPDIR/parser.sh'; greetd_compositor_vt '$REPO_ROOT/deploy/greetd-config.toml'"
    [ "$status" -eq 0 ]
    [ "$output" = "3" ]
}

@test "vt-isolation: the parser ignores a vt key outside [terminal]" {
    # A loose grep would match the commented [initial_session] block or any
    # other table; that would mask the WRONG VT.
    extract_vt_parser
    cat > "$BATS_TEST_TMPDIR/decoy.toml" <<'EOF'
[default_session]
vt = 9
# [terminal]
# vt = 8
[terminal]
vt = 4
EOF
    run bash -c ". '$BATS_TEST_TMPDIR/parser.sh'; greetd_compositor_vt '$BATS_TEST_TMPDIR/decoy.toml'"
    [ "$status" -eq 0 ]
    [ "$output" = "4" ]
}

@test "vt-isolation: tty1's emergency console is NOT collateral damage" {
    # doc/recovery.md keeps tty1 agetty deliberately. Nothing on the hardened
    # path may mask or disable it.
    refute_grep 'systemctl (mask|disable)[^|]*getty@tty1' \
        "$HARDEN_VT" "$BOOT" "$IMAGE_CFG"
}

@test "vt-isolation: production does NOT copy the test lane's NAutoVTs=0/ReserveVT=0" {
    # Right for a single-purpose GUI test VM (spin-test-vm-gui.sh), wrong for
    # the product: tty5+ work sessions and the games VT-switch feature depend
    # on multi-VT allocation, and it is not the security boundary anyway.
    # Ignore comment lines: harden-compositor-vt.sh names both settings in its
    # rationale precisely to say it does NOT apply them.
    run bash -c "grep -vE '^[[:space:]]*#' '$BOOT' '$IMAGE_CFG' '$HARDEN_VT' \
                 | grep -E 'NAutoVTs=0|ReserveVT=0'"
    [ "$status" -ne 0 ] || { echo "unexpected test-lane VT settings on the production path:"$'\n'"$output" >&2; return 1; }
    # ...and they ARE still present in the GUI test lane, so this test is
    # asserting a real separation rather than a string that exists nowhere.
    grep -q "NAutoVTs=0" "$REPO_ROOT/scripts/vm/spin-test-vm-gui.sh"
}

@test "vt-isolation: failure is fatal on the hardened profile, warn on dev" {
    grep -q "fail_compositor_vt" "$BOOT"
    run awk '/^fail_compositor_vt\(\)/,/^}/' "$BOOT"
    [[ "$output" == *"if is_dev"* ]]
    [[ "$output" == *"warn "* ]]
    [[ "$output" == *"die "* ]]
}

@test "vt-isolation: the image build aborts when the VT is not secured" {
    # Anchor on the actual invocation line, not the comment above it, and
    # require the failure branch to exit — `|| true` must turn this red.
    run awk '/^if ! bash .*harden-compositor-vt\.sh/,/^fi/' "$IMAGE_CFG"
    [ -n "$output" ]
    [[ "$output" == *"exit 1"* ]]
    [[ "$output" != *"|| true"* ]]
}

@test "vt-isolation: the inert systemd.default_vt cmdline is gone" {
    # Not a systemd option (absent from logind's config parser,
    # kernel-command-line(7), and the systemd binaries), so it only made it
    # look as though the boot VT was pinned. greetd's [terminal] vt does that.
    # Scoped to the kernelcmdline attribute so the comment explaining the
    # removal (which necessarily names the string) does not trip it.
    refute_grep 'kernelcmdline="[^"]*systemd\.default_vt' "$REPO_ROOT/image/config.xml"
    grep -qE '^[[:space:]]*vt[[:space:]]*=[[:space:]]*3' "$REPO_ROOT/deploy/greetd-config.toml"
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

# --- E. Root checkout / source-tree trust gate --------------------------
#
# Threat model: an unprivileged local user owns (or can write to) the source
# checkout the operator later root-installs from. The signed source manifest
# authenticates COMMIT CONTENT only — it cannot cover .git/hooks, .git/config,
# or untracked files. verify_repo_pin used to run `git checkout --detach` as
# root against that tree, which executes .git/hooks/post-checkout as root.
#
# These tests run as the invoking (non-root) uid; the gate accepts a tree owned
# by root OR by our own euid, so "attacker-writable" is modelled with
# group/other write bits, which is the property that actually matters.

# hookrepo <dir> — build a two-commit git repo at <dir> with a post-checkout
# hook that drops a marker file at <dir>.pwned. Echoes the FIRST commit sha
# (the "pin"); HEAD is left on the second commit so a checkout really moves.
hookrepo() {
    local dir="$1" pin
    mkdir -p "$dir"
    git -C "$dir" init -q
    git -C "$dir" -c user.email=t@t -c user.name=t commit -q --allow-empty -m one
    pin="$(git -C "$dir" rev-parse HEAD)"
    git -C "$dir" -c user.email=t@t -c user.name=t commit -q --allow-empty -m two
    printf '#!/bin/sh\ntouch %s\n' "$dir.pwned" > "$dir/.git/hooks/post-checkout"
    chmod 0755 "$dir/.git/hooks/post-checkout"
    printf '%s\n' "$pin"
}

# run_verify_pin <root> <repo> <pin> — drive the REAL verify_repo_pin from the
# REAL bootstrap in a hardened profile against <root>/<repo>.
run_verify_pin() {
    local root="$1" repo="$2" pin="$3" mf="$1/manifest.txt"
    printf '%s\t%s\n' "$repo" "$pin" > "$mf"
    run bash -c '
        set -uo pipefail
        export QDISTRO_PROFILE=release
        export QDISTRO_REPO_ROOT="'"$root"'"
        export QDISTRO_SOURCE_MANIFEST="'"$mf"'"
        . "'"$BOOT"'" >/dev/null 2>&1
        REPO_ROOT="'"$root"'"
        SOURCE_MANIFEST="'"$mf"'"
        verify_repo_pin "'"$repo"'"
    '
}

@test "root-checkout: a planted post-checkout hook does NOT execute as root" {
    local root="$BATS_TEST_TMPDIR/ok" pin
    mkdir -p "$root"
    pin="$(hookrepo "$root/qdistro")"
    run_verify_pin "$root" qdistro "$pin"
    [ "$status" -eq 0 ] || { echo "verify_repo_pin failed on a TRUSTED tree:"$'\n'"$output" >&2; return 1; }
    # The pin was actually checked out ...
    [ "$(git -C "$root/qdistro" rev-parse HEAD)" = "$pin" ]
    # ... and the hook did NOT run.
    [ ! -e "$root/qdistro.pwned" ] \
        || { echo "post-checkout hook EXECUTED during root pin verification" >&2; return 1; }
}

@test "root-checkout: a user-writable source checkout is REFUSED before any git runs" {
    local root="$BATS_TEST_TMPDIR/ww" pin head_before
    mkdir -p "$root"
    pin="$(hookrepo "$root/qdistro")"
    head_before="$(git -C "$root/qdistro" rev-parse HEAD)"
    chmod 0777 "$root/qdistro"
    run_verify_pin "$root" qdistro "$pin"
    [ "$status" -ne 0 ] \
        || { echo "world-writable checkout was ACCEPTED:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"writable by group/other"* ]] \
        || { echo "wrong refusal reason:"$'\n'"$output" >&2; return 1; }
    # The message must tell the operator what to do.
    [[ "$output" == *"--repo-root"* ]]
    [[ "$output" == *"chown -R root:root"* ]]
    # No checkout happened, and no hook ran.
    [ "$(git -C "$root/qdistro" rev-parse HEAD)" = "$head_before" ]
    [ ! -e "$root/qdistro.pwned" ]
}

@test "root-checkout: a user-writable PARENT of the checkout is refused too" {
    local root="$BATS_TEST_TMPDIR/wwparent" pin
    mkdir -p "$root"
    pin="$(hookrepo "$root/qdistro")"
    chmod 0777 "$root"            # parent writable: the tree can be swapped
    run_verify_pin "$root" qdistro "$pin"
    chmod 0755 "$root"
    [ "$status" -ne 0 ] \
        || { echo "world-writable PARENT was accepted:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"writable by group/other"* ]]
    [ ! -e "$root/qdistro.pwned" ]
}

@test "root-checkout: a group/other-writable file INSIDE the tree is refused" {
    # `chown -R root:root` without `chmod -R go-w` leaves attacker-writable
    # content inside a spine that looks fine. deploy/*, scripts/install/*.sh,
    # meson.build and .git/hooks/* all execute as root, so the gate must look
    # past the pathname spine into the tree.
    local root="$BATS_TEST_TMPDIR/inner" pin
    mkdir -p "$root"
    pin="$(hookrepo "$root/qdistro")"
    chmod 0666 "$root/qdistro/.git/hooks/post-checkout"
    run_verify_pin "$root" qdistro "$pin"
    [ "$status" -ne 0 ] \
        || { echo "writable content inside the tree was accepted:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"writable by group/other or owned by another user"* ]] \
        || { echo "wrong refusal reason:"$'\n'"$output" >&2; return 1; }
    [ ! -e "$root/qdistro.pwned" ]
}

@test "root-checkout: a symlinked .git is refused, not followed" {
    local root="$BATS_TEST_TMPDIR/link" pin
    mkdir -p "$root"
    pin="$(hookrepo "$root/qdistro")"
    mv "$root/qdistro/.git" "$root/elsewhere.git"
    ln -s "$root/elsewhere.git" "$root/qdistro/.git"
    run_verify_pin "$root" qdistro "$pin"
    [ "$status" -ne 0 ] \
        || { echo "symlinked .git was accepted:"$'\n'"$output" >&2; return 1; }
    # Either layer may catch it first (the whole-tree symlink scan sees an
    # escaping link; the spine walk sees a symlinked component) — both are a
    # refusal that names the symlink.
    [[ "$output" == *"symlink"* ]] \
        || { echo "wrong refusal reason:"$'\n'"$output" >&2; return 1; }
    [ ! -e "$root/qdistro.pwned" ]
}

@test "root-checkout: a DIRTY tree at the pinned commit is refused" {
    # HEAD == pin, so `git checkout --detach $pin` is a no-op and leaves both
    # the modified tracked file and the untracked one in place — content the
    # signed manifest never covered. Hardened profiles must refuse it.
    local root="$BATS_TEST_TMPDIR/dirty" dir pin
    dir="$root/qdistro"; mkdir -p "$dir"
    git -C "$dir" init -q
    printf 'clean\n' > "$dir/tracked"
    git -C "$dir" add tracked
    git -C "$dir" -c user.email=t@t -c user.name=t commit -q -m one
    pin="$(git -C "$dir" rev-parse HEAD)"
    printf 'tampered\n' > "$dir/tracked"
    printf 'extra\n' > "$dir/untracked"
    run_verify_pin "$root" qdistro "$pin"
    [ "$status" -ne 0 ] \
        || { echo "dirty tree at the pin was ACCEPTED:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"DIRTY"* ]] || { echo "$output" >&2; return 1; }
    [[ "$output" == *"tracked"* ]]
    [[ "$output" == *"untracked"* ]]
}

@test "root-checkout: dev profile still builds from a user-owned tree" {
    # The gate is hardened-only: disposable dev VMs deliberately build from a
    # developer checkout, so it must stay a no-op there.
    local root="$BATS_TEST_TMPDIR/dev"
    mkdir -p "$root/qdistro"
    chmod 0777 "$root/qdistro"
    run bash -c '
        . "'"$PROFILE_LIB"'"
        QDISTRO_PROFILE=dev; export QDISTRO_PROFILE
        SCRIPT_PATH=x; EUID_UNUSED=
        die() { echo "DIE: $*"; exit 1; }
        '"$(sed -n '/^qd_trusted_component()/,/^}/p;/^assert_trusted_tree()/,/^}/p;/^trust_die()/,/^}/p' "$BOOT")"'
        assert_trusted_tree "'"$root/qdistro"'" "dev tree" && echo DEV_NOOP
    '
    [ "$status" -eq 0 ]
    [[ "$output" == *DEV_NOOP* ]]
}

@test "root-checkout: every root git call in the bootstrap goes through git_pinned" {
    # Mutation guard: a future edit that reintroduces a bare `git -C \
    # "$REPO_ROOT/..."` inside verify_repo_pin re-enables hook execution.
    body="$(sed -n '/^verify_repo_pin()/,/^}/p' "$BOOT")"
    [ -n "$body" ]
    bare="$(printf '%s\n' "$body" | grep -n '^[[:space:]]*[^#]*\bgit -C ' || true)"
    [ -z "$bare" ] \
        || { echo "bare 'git -C' (hooks enabled) in verify_repo_pin:"$'\n'"$bare" >&2; return 1; }
    printf '%s\n' "$body" | grep -q 'assert_trusted_tree' \
        || { echo "verify_repo_pin no longer gates the tree" >&2; return 1; }
}

@test "root-checkout: a symlink pointing OUTSIDE the tree is refused" {
    # `chown -R root:root` makes a planted `build -> /tmp/attacker` symlink
    # root-owned while leaving its nominal 0777 mode and its target untouched;
    # meson/ninja/pip then follow it. The gate must reject escaping symlinks.
    local root="$BATS_TEST_TMPDIR/esc" pin
    mkdir -p "$root" "$BATS_TEST_TMPDIR/outside"
    pin="$(hookrepo "$root/qdistro")"
    ln -s "$BATS_TEST_TMPDIR/outside" "$root/qdistro/build"
    run_verify_pin "$root" qdistro "$pin"
    [ "$status" -ne 0 ] \
        || { echo "escaping symlink accepted:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"absolute or contains '..'"* ]] \
        || { echo "wrong refusal reason:"$'\n'"$output" >&2; return 1; }
    [ ! -e "$root/qdistro.pwned" ]
}

@test "root-checkout: an INCOMPLETE permission scan is fatal, not 'clean'" {
    # "we saw nothing bad" must never be confused with "we looked". An
    # unreadable subdirectory makes find exit non-zero; the gate must refuse.
    local root="$BATS_TEST_TMPDIR/unscannable" pin
    mkdir -p "$root"
    pin="$(hookrepo "$root/qdistro")"
    mkdir -p "$root/qdistro/opaque"
    chmod 0300 "$root/qdistro/opaque"
    run_verify_pin "$root" qdistro "$pin"
    chmod 0755 "$root/qdistro/opaque"
    [ "$status" -ne 0 ] \
        || { echo "unscannable tree accepted:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"did not complete"* ]] \
        || { echo "wrong refusal reason:"$'\n'"$output" >&2; return 1; }
    [ ! -e "$root/qdistro.pwned" ]
}

@test "root-checkout: --skip-sources pin-verifies every EXISTING tree, not just recognised ones" {
    # repo_present only recognises .git / daemons / meson.build / pyproject.toml,
    # but install_python_modules runs $REPO_ROOT/qdistro/scripts/install/*.sh and
    # installs qdlocker's systemd/pam assets, so an unrecognisable-but-present
    # tree must FAIL the pin, never silently skip it.
    body="$(sed -n '/^fetch_sources()/,/^}/p' "$BOOT")"
    [ -n "$body" ]
    printf '%s\n' "$body" | grep -q '\[ -e "\$REPO_ROOT/\$repo" \] && verify_repo_pin' \
        || { echo "--skip-sources no longer pin-verifies every existing tree:"$'\n'"$body" >&2; return 1; }
    printf '%s\n' "$body" | grep -q 'repo_present "\$repo" && verify_repo_pin' \
        && { echo "--skip-sources regressed to the narrow repo_present predicate" >&2; return 1; }
    :
}

@test "root-checkout: git_pinned neutralises hooks, fsmonitor, replace-refs and outside gitconfig" {
    body="$(sed -n '/^git_pinned()/,/^}/p' "$BOOT")"
    for needle in 'core.hooksPath=/dev/null' 'core.fsmonitor=' \
                  'GIT_NO_REPLACE_OBJECTS=1' 'GIT_CONFIG_GLOBAL=/dev/null' \
                  'GIT_CONFIG_SYSTEM=/dev/null'; do
        printf '%s\n' "$body" | grep -qF "$needle" \
            || { echo "git_pinned lost '$needle':"$'\n'"$body" >&2; return 1; }
    done
}

@test "root-checkout: the refusal does NOT advise chowning a hostile tree" {
    # Laundering an attacker-prepared checkout with `chown -R root:root` makes
    # it pass the mechanical check without making its .git/config, ignored
    # build state or symlinks trustworthy. The message must not offer it.
    body="$(sed -n '/^trust_die()/,/^}/p' "$BOOT")"
    printf '%s\n' "$body" | grep -qE '^[^#]*sudo chown -R root:root \$2' \
        && { echo "trust_die still recommends chowning the existing tree" >&2; return 1; }
    printf '%s\n' "$body" | grep -q -- '--repo-root=/opt/qdistro-src' \
        || { echo "trust_die lost the fresh-root remediation" >&2; return 1; }
    printf '%s\n' "$body" | grep -q 'safe.directory' \
        || { echo "trust_die lost the safe.directory warning" >&2; return 1; }
}

@test "root-checkout: an escaping symlink whose NAME contains a newline is refused" {
    # Regression for a fail-open serialization: `find -print` into a command
    # substitution loses NULs and strips trailing newlines, so a link named
    # "decoy\n" beside a benign "decoy" would be checked as "decoy" and the
    # real escaping link never examined. The rule is now decided by find
    # itself against the link's own bytes; no list is ever parsed.
    local root="$BATS_TEST_TMPDIR/nl" pin
    mkdir -p "$root" "$BATS_TEST_TMPDIR/nl-outside"
    pin="$(hookrepo "$root/qdistro")"
    ln -s "." "$root/qdistro/decoy"
    ln -s "$BATS_TEST_TMPDIR/nl-outside" "$root/qdistro/decoy
"
    run_verify_pin "$root" qdistro "$pin"
    [ "$status" -ne 0 ] \
        || { echo "newline-named escaping symlink accepted:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"absolute or contains '..'"* ]] \
        || { echo "wrong refusal reason:"$'\n'"$output" >&2; return 1; }
    [ ! -e "$root/qdistro.pwned" ]
}

@test "root-checkout: a sibling directory named '<tree>NEWLINE' cannot spoof containment" {
    # `rt="$(readlink -f ...)"` strips trailing newlines, so a link to a
    # sibling literally named "qdistro\n" produced a string equal to the
    # gated tree's own path and was accepted. The syntactic rule never
    # resolves the target, so the spoof has nothing to act on.
    local root="$BATS_TEST_TMPDIR/spoof" pin
    mkdir -p "$root"
    pin="$(hookrepo "$root/qdistro")"
    mkdir -p "$root/qdistro
"
    ln -s "$root/qdistro
" "$root/qdistro/sneaky"
    run_verify_pin "$root" qdistro "$pin"
    [ "$status" -ne 0 ] \
        || { echo "newline-sibling spoof accepted:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"absolute or contains '..'"* ]]
    [ ! -e "$root/qdistro.pwned" ]
}

@test "root-checkout: an EXTERNAL mutable relay cannot launder containment" {
    # tree/link -> <outside>/relay/payload, with relay currently pointing back
    # inside the tree. A "where does it land right now" check accepted this and
    # then memoised the tree; the attacker repoints relay afterwards and
    # nothing inside the accepted tree ever changed. A syntactic rule refuses
    # the absolute link outright.
    local root="$BATS_TEST_TMPDIR/relay" pin
    mkdir -p "$root" "$BATS_TEST_TMPDIR/relaydir"
    pin="$(hookrepo "$root/qdistro")"
    mkdir -p "$root/qdistro/safe"
    ln -s "$root/qdistro/safe" "$BATS_TEST_TMPDIR/relaydir/relay"
    ln -s "$BATS_TEST_TMPDIR/relaydir/relay" "$root/qdistro/link"
    run_verify_pin "$root" qdistro "$pin"
    [ "$status" -ne 0 ] \
        || { echo "external mutable relay accepted:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"absolute or contains '..'"* ]]
    [ ! -e "$root/qdistro.pwned" ]
}

@test "root-checkout: a '..' symlink that stays inside the tree is STILL refused" {
    # `..` traversal walks through the parent directory, which the gate does
    # not cover with leaf rules, so it is refused even when today's resolution
    # happens to land back inside.
    local root="$BATS_TEST_TMPDIR/dotdot" pin
    mkdir -p "$root"
    pin="$(hookrepo "$root/qdistro")"
    mkdir -p "$root/qdistro/a" "$root/qdistro/b"
    ln -s "../b" "$root/qdistro/a/up"
    run_verify_pin "$root" qdistro "$pin"
    [ "$status" -ne 0 ] \
        || { echo "'..' symlink accepted:"$'\n'"$output" >&2; return 1; }
    [[ "$output" == *"absolute or contains '..'"* ]]
}

@test "root-checkout: a relative in-tree symlink to a not-yet-existing path is accepted" {
    # Complement to the above: the rule is syntactic containment, not
    # existence. A relative, `..`-free link cannot escape, and its missing leaf
    # lives inside the gated tree where no unprivileged user can create it.
    local root="$BATS_TEST_TMPDIR/danglein" pin
    mkdir -p "$root"
    pin="$(hookrepo "$root/qdistro")"
    ln -s "not-built-yet" "$root/qdistro/artifact"
    git -C "$root/qdistro" add -A
    git -C "$root/qdistro" -c user.email=t@t -c user.name=t commit -q -m link
    pin="$(git -C "$root/qdistro" rev-parse HEAD)"
    run_verify_pin "$root" qdistro "$pin"
    [ "$status" -eq 0 ] \
        || { echo "in-tree dangling symlink wrongly refused:"$'\n'"$output" >&2; return 1; }
    [ ! -e "$root/qdistro.pwned" ]
}

@test "root-checkout: the too-late self-gate is documented, not claimed closed" {
    # The bootstrap and lib/qdistro-profile.sh execute BEFORE any gate can run.
    # That cannot be fixed from inside the script; it must be an explicit,
    # documented distribution precondition. Pin both halves so a later edit
    # cannot quietly upgrade the limitation into a claimed guarantee.
    body="$(sed -n '/^main() {/,/^}/p' "$BOOT")"
    [ -n "$body" ]
    printf '%s\n' "$body" | grep -qi 'CANNOT authenticate' \
        || { echo "the SCRIPT_DIR gate no longer states its limitation:"$'\n'"$body" >&2; return 1; }
    printf '%s\n' "$body" | grep -qi 'out of band' \
        || { echo "the SCRIPT_DIR gate no longer names the out-of-band precondition" >&2; return 1; }
    grep -qi 'Trusting the bootstrap itself' "$REPO_ROOT/doc/release-signing.md" \
        || { echo "doc/release-signing.md lost the bootstrap-trust precondition section" >&2; return 1; }
}

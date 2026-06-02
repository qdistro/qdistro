#!/usr/bin/env bats
# Static-invariant lock-in for the codex2 packaging/session-integration
# fixes (findings #15, #16, #19, #20, #21). Like bootstrap-hardening.bats
# this needs NO live VM: it inspects the install/image/deploy files as
# SOURCE and asserts the new wiring is present and self-consistent.
#
# Each assertion targets a specific gap from the review; a regression that
# re-opens the gap turns the matching test red.

setup() {
    REPO_ROOT="$(git -C "$(dirname "$BATS_TEST_FILENAME")" \
                    rev-parse --show-toplevel 2>/dev/null)"
    BOOT="$REPO_ROOT/scripts/install/qdistro-bootstrap.sh"
    IMAGE_CFG="$REPO_ROOT/image/config.sh"
    IMAGE_BUILD="$REPO_ROOT/image/build.sh"
    VERIFY="$REPO_ROOT/image/verify.sh"
    VERIFY_CONTENTS="$REPO_ROOT/image/verify-contents.sh"
    SESSION_TARGET="$REPO_ROOT/deploy/qdwin-session.target"
    [ -f "$BOOT" ] || { echo "bootstrap not found at $BOOT" >&2; return 1; }
}

# --- syntax -------------------------------------------------------------

@test "codex-pkg: all touched scripts are syntactically valid bash" {
    for f in "$BOOT" "$IMAGE_CFG" "$IMAGE_BUILD" "$VERIFY" "$VERIFY_CONTENTS"; do
        run bash -n "$f"
        [ "$status" -eq 0 ] || { echo "bash -n failed: $f"$'\n'"$output" >&2; return 1; }
    done
}

# --- #15: greeter starts qdwin-session.target; image installs those units ---

@test "#15: image config.sh installs the production qdwin-session units for admin" {
    grep -q 'qdwin-session.target' "$IMAGE_CFG"
    grep -q 'qdwin-compositor.service' "$IMAGE_CFG"
    grep -q 'qdshell.service' "$IMAGE_CFG"
    # installed into admin's user systemd dir
    grep -q '/home/admin/.config/systemd/user' "$IMAGE_CFG"
}

@test "#15: image disables noctalia auto-start so only the greeter path brings up the desktop" {
    # The legacy installer enables noctalia under default.target.wants; the
    # unified path must remove those symlinks so two compositors do not race.
    grep -Eq 'rm -f .*default.target.wants/\$u' "$IMAGE_CFG" \
        || grep -q 'noctalia-session.service noctalia-shell.service' "$IMAGE_CFG"
    grep -q 'noctalia-shell.service' "$IMAGE_CFG"
}

@test "#15: verify.sh asserts the qdwin-session units the greeter starts (not only noctalia)" {
    grep -q 'qdwin-session.target' "$VERIFY"
    grep -q 'qdshell wanted by qdwin-session.target' "$VERIFY"
}

@test "#15: verify-contents.sh requires the qdwin-session unit files for admin" {
    grep -q 'qdwin-session.target (admin user unit)' "$VERIFY_CONTENTS"
    grep -q 'qdwin-compositor.service (admin user unit)' "$VERIFY_CONTENTS"
    grep -q 'qdshell.service (admin user unit)' "$VERIFY_CONTENTS"
}

# --- #16: qdlocker is part of qdwin-session.target ----------------------

@test "#16: qdwin-session.target Wants= qdlocker.service" {
    run grep -E '^Wants=.*qdlocker.service|^Wants=qdlocker.service' "$SESSION_TARGET"
    # Wants may be on its own line; accept either form.
    grep -q 'Wants=qdlocker.service' "$SESSION_TARGET"
}

@test "#16: image installs + wires qdlocker.service into qdwin-session.target.wants" {
    grep -q 'qdlocker.service' "$IMAGE_CFG"
    grep -q 'qdwin-session.target.wants' "$IMAGE_CFG"
}

@test "#16: verify-contents + verify cover qdlocker in the session" {
    grep -q 'qdlocker.service (admin user unit)' "$VERIFY_CONTENTS"
    grep -q 'qdlocker wanted by qdwin-session.target' "$VERIFY"
}

# --- #16 (BROKEN remediation): qdlocker ExecStart path correctness ------
# The image pip-installs qdlocker with --prefix=/usr (binary -> /usr/bin),
# but the upstream unit hardcodes ExecStart=/usr/local/bin/qdlocker. Copying
# the unit verbatim makes qdlocker.service 203/EXEC at boot. config.sh MUST
# rewrite the ExecStart (same sed the bootstrap uses), NOT install it verbatim.

@test "#16: config.sh rewrites qdlocker ExecStart /usr/local/bin -> /usr/bin (not verbatim)" {
    # Must contain the sed that corrects the ExecStart to the installed path.
    grep -Eq 'sed .*ExecStart=/usr/local/bin/qdlocker.*ExecStart=/usr/bin/qdlocker' "$IMAGE_CFG"
    # Must NOT do a verbatim `install ... qdlocker/systemd/qdlocker.service`
    # into the admin user dir (that is exactly the broken copy).
    run grep -E 'install -m 0644 .*qdlocker/systemd/qdlocker.service' "$IMAGE_CFG"
    [ "$status" -ne 0 ] || { echo "config.sh still verbatim-installs qdlocker.service:"$'\n'"$output" >&2; return 1; }
}

@test "#16: config.sh fail-closes if the rewritten qdlocker ExecStart is not executable" {
    # A non-executable ExecStart would 203/EXEC at boot; the build must abort.
    grep -Eq 'ExecStart=\\\([^ ]*\\\)' "$IMAGE_CFG" || \
        grep -q 'qdlocker.service ExecStart' "$IMAGE_CFG"
    grep -Eq 'test -x .*locker_exec|! -x "\$locker_exec"' "$IMAGE_CFG"
    grep -q 'FATAL: qdlocker.service ExecStart' "$IMAGE_CFG"
}

@test "#16: verify.sh GATES on qdlocker.service running (is-active + no 203/EXEC)" {
    # Not just Wanted= — the unit must actually come up without an exec failure.
    grep -q 'systemctl --user is-active qdlocker.service' "$VERIFY"
    grep -Eq '203/EXEC' "$VERIFY"
    grep -q 'qdlocker.service ExecStart resolves to an installed binary' "$VERIFY"
}

# --- #19: image stages + verifies /usr/bin/qdgreeter --------------------

@test "#19: build.sh syncs qdgreeter (and qdlocker) into the image overlay" {
    grep -Eq 'for repo in .*qdgreeter' "$IMAGE_BUILD"
    grep -Eq 'for repo in .*qdlocker' "$IMAGE_BUILD"
}

@test "#19: config.sh pip-installs qdgreeter and hard-fails if /usr/bin/qdgreeter is missing" {
    grep -q 'pip install' "$IMAGE_CFG"
    grep -q 'qdgreeter' "$IMAGE_CFG"
    # Required gate: a missing greeter aborts the build.
    grep -Eq 'if \[ ! -x /usr/bin/qdgreeter \]' "$IMAGE_CFG"
    grep -Eq 'exit 1' "$IMAGE_CFG"
}

@test "#19: verify-contents.sh REQUIRES the qdgreeter binary" {
    # check_req (not check_opt) for the greeter binary.
    grep -Eq 'check_req +"qdgreeter binary" +/usr/bin/qdgreeter' "$VERIFY_CONTENTS"
}

@test "#19: verify.sh asserts qdgreeter binary + no greetd exec failure" {
    grep -q 'qdgreeter binary present' "$VERIFY"
    grep -qi 'greetd execs an existing greeter' "$VERIFY"
}

# --- #20: QTermWidget binding + smoke loop ------------------------------

@test "#20: bootstrap builds the QTermWidget binding before installing qterminator" {
    grep -q 'build_qtermwidget_binding()' "$BOOT"
    # called inside the install loop for qterminator
    grep -q 'build_qtermwidget_binding' "$BOOT"
    grep -q 'util/build-sip.sh' "$BOOT"
}

@test "#20: bootstrap smoke loop imports qterminator AND qfileman" {
    # The smoke loop must reach qterminator.terminal (where QTermWidget is
    # imported) and qfileman — previously only qdgreeter/qdlocker/qdbrowser.
    grep -q 'qterminator.terminal' "$BOOT"
    run grep -E 'smoke_repos=.*qterminator.*qfileman|for smoke in .*qterminator' "$BOOT"
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    grep -Eq 'smoke_repos=.*qfileman' "$BOOT"
}

# --- #21: desktop/metainfo/icon assets ----------------------------------

@test "#21: bootstrap installs desktop/metainfo/icon assets for qterminator + qfileman" {
    grep -q 'install_app_desktop_assets()' "$BOOT"
    grep -q '/usr/share/applications' "$BOOT"
    grep -q '/usr/share/metainfo' "$BOOT"
    grep -q '/usr/share/icons/hicolor' "$BOOT"
    grep -Eq 'qterminator\|qfileman\) install_app_desktop_assets' "$BOOT"
}

@test "#21: verify-contents.sh checks installed app assets" {
    grep -q 'qterminator .desktop' "$VERIFY_CONTENTS"
    grep -q 'qfileman .desktop' "$VERIFY_CONTENTS"
    grep -q 'qterminator metainfo' "$VERIFY_CONTENTS"
}

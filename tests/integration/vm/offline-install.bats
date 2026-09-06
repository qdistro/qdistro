#!/usr/bin/env bats
# The offline-install contract (todo/iso/14 Phase B):
# scripts/install/lib/qdistro-offline.sh and its use by every installer the
# image chain runs. Static invariants (every chain installer sources the
# library; no raw live call outside an offline guard; the image chain is
# fatal) plus behavioural cases of the library driven with stubbed
# systemctl/loginctl/systemd-detect-virt on PATH, in a private mount
# namespace where root-owned paths are involved. No VM, no root.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    LIB="$REPO/scripts/install/lib/qdistro-offline.sh"
    HARDEN_VT="$REPO/scripts/install/harden-compositor-vt.sh"
    CONFIG_SH="$REPO/image/config.sh"
    [ -f "$LIB" ]
    BOOT="$REPO/scripts/install/qdistro-bootstrap.sh"
    # The chain: every installer the bootstrap's installer_chain_entries names
    # (image/config.sh runs the SAME functions -- todo/iso/14 Phase D -- so
    # there is one list) plus the qdwin-session installer config.sh calls
    # directly. Read from the executed definition, not from comments.
    CHAIN=$( { bash -c '. "$1"; installer_chain_entries' _ "$BOOT" | awk -F'|' 'NF{print $2}';
               grep -oE '^bash "\$QD/scripts/install/install-[a-z0-9-]+\.sh"' "$CONFIG_SH" | grep -oE 'scripts/install/install-[a-z0-9-]+\.sh'; } | sort -u)
    # Cardinality from the chain itself (+1 for the direct qdwin-session
    # call), so an installer added to the chain is in the walk or the count
    # check goes red.
    local n_chain
    n_chain=$(bash -c '. "$1"; installer_chain_names' _ "$BOOT" | grep -c .)
    if [ "$(printf '%s\n' "$CHAIN" | wc -l)" -ne $(( n_chain + 1 )) ] || [ "$n_chain" -lt 15 ]; then
        echo "chain parse mismatch: chain=$n_chain parsed: $CHAIN" >&2; return 1
    fi
}

# ---- static invariants ---------------------------------------------------

@test "offline: every chain installer sources the library and resolves the mode" {
    local f
    for f in $CHAIN; do
        grep -q 'lib/qdistro-offline.sh' "$REPO/$f" || { echo "$f does not source lib/qdistro-offline.sh"; return 1; }
        grep -q '^resolve_offline_install' "$REPO/$f" || { echo "$f does not call resolve_offline_install"; return 1; }
    done
}

# Live-only operations may appear ONLY through the library's sd_*/live_only
# helpers or inside an `if is_offline; then ... else ... fi` block (the
# else branch). Anything else is a live call that would run in the chroot.
@test "offline: no raw live call outside an offline guard in any chain installer" {
    local f bad=0
    for f in $CHAIN; do
        run awk '
            function indent(s) { match(s, /^ */); return RLENGTH }
            /^[[:space:]]*#/ { next }
            /^[[:space:]]*if is_offline; then/ { depth++; ind[depth]=indent($0); next }
            depth > 0 && /^[[:space:]]*fi[[:space:]]*$/ && indent($0) == ind[depth] { depth--; next }
            depth > 0 { next }
            /^[[:space:]]*echo / { next }
            /systemctl[[:space:]]+(start|stop|restart|try-restart|reload|daemon-reload|is-active|preset)|--now|busctl|loginctl|dbus-send|systemd-run|gdbus|systemctl[[:space:]]+--user[[:space:]]+(start|enable|daemon-reload)|systemctl[[:space:]]+--global[[:space:]]+(start|daemon-reload)/ {
                print FILENAME ":" FNR ": " $0
            }' "$REPO/$f"
        if [ -n "$output" ]; then echo "$output"; bad=1; fi
    done
    [ "$bad" = 0 ]
}

@test "offline: the image runs the bootstrap's chain -- strict, offline, recorded on the image; no parallel list" {
    # Phase D: config.sh has no INSTALLERS array and invokes no chain
    # installer itself; the only install-*.sh it runs directly is the
    # qdwin-session one (same contract).
    ! grep -q '^INSTALLERS=(' "$CONFIG_SH"
    run bash -c 'grep -vE "^[[:space:]]*#" "$1" | grep -oE "scripts/install/install-[a-z0-9-]+\.sh" | sort -u' _ "$CONFIG_SH"
    [ "$output" = "scripts/install/install-qdwin-session-for-vm.sh" ]
    # The bootstrap is driven through its ENVIRONMENT forms (the source
    # clobbers the internal names), strict, with the state dir on the image.
    grep -q '^export QDISTRO_REPO_ROOT="\$SRC"$' "$CONFIG_SH"
    grep -q '^export QDISTRO_PROFILE="\$QDISTRO_IMAGE_PROFILE"$' "$CONFIG_SH"
    grep -q '^export QDISTRO_STRICT=1$' "$CONFIG_SH"
    grep -q '^export QDISTRO_STATE_DIR=/var/lib/qdistro/bootstrap$' "$CONFIG_SH"
    grep -q '^export QDISTRO_OFFLINE_INSTALL=1$' "$CONFIG_SH"
    ! grep -qE '^(REPO_ROOT|STRICT|RESUME|FROM_STEP|RERUN_STEP)=' "$CONFIG_SH"
    grep -q '^\. "\$QD/scripts/install/qdistro-bootstrap.sh"$' "$CONFIG_SH"
    grep -q '^resolve_profile || ' "$CONFIG_SH"
    grep -q '^install_python_modules$' "$CONFIG_SH"
    # order: exports, source, globals asserted, profile resolved, chain
    local l_exp l_src l_assert l_prof l_chain
    l_exp=$(grep -n '^export QDISTRO_STRICT=1$' "$CONFIG_SH" | cut -d: -f1)
    l_src=$(grep -n '^\. "\$QD/scripts/install/qdistro-bootstrap.sh"$' "$CONFIG_SH" | cut -d: -f1)
    l_assert=$(grep -n 'bootstrap globals did not take the exported values' "$CONFIG_SH" | cut -d: -f1)
    l_prof=$(grep -n '^resolve_profile || ' "$CONFIG_SH" | cut -d: -f1)
    l_chain=$(grep -n '^install_python_modules$' "$CONFIG_SH" | cut -d: -f1)
    [ "$l_exp" -lt "$l_src" ] && [ "$l_src" -lt "$l_assert" ] && [ "$l_assert" -lt "$l_prof" ] && [ "$l_prof" -lt "$l_chain" ]
    # The offline flag is exported before the source (the chain reads it).
    [ "$(grep -n '^export QDISTRO_OFFLINE_INSTALL=1$' "$CONFIG_SH" | cut -d: -f1)" -lt "$l_src" ]
    grep -q '^export QDWIN_SESSION_AUTOSTART=0' "$CONFIG_SH"
    ! grep -q 'SHIMS' "$CONFIG_SH"
    # No fail-open remnant: nothing in config.sh tolerates a chain failure.
    ! grep -q 'verify failed, expected in chroot' "$CONFIG_SH"
}

@test "offline: sourcing the bootstrap with config.sh's exports yields the globals it relies on" {
    # The plan's [codex r2] point, pinned: the ENV forms take; the internal
    # names would be clobbered by the source.
    run env QDISTRO_REPO_ROOT=/tmp/x QDISTRO_STRICT=1 QDISTRO_STATE_DIR=/tmp/y QDISTRO_PROFILE=release \
        REPO_ROOT=/clobbered STRICT=clobbered \
        bash -c '. "$1"; resolve_profile >/dev/null; printf "%s|%s|%s|%s|%s\n" "$REPO_ROOT" "$STRICT" "$QDISTRO_STATE_DIR" "$CHAIN_STATE_FILE" "$QDISTRO_PROFILE"' _ "$BOOT"
    [ "$status" -eq 0 ]
    [ "$output" = "/tmp/x|1|/tmp/y|/tmp/y/installer-chain.state|release" ]
    # dev maps to dev (phone admitted); an unknown profile is refused.
    run env QDISTRO_PROFILE=dev bash -c '. "$1"; resolve_profile && is_dev && echo dev-ok' _ "$BOOT"
    [ "$output" = "dev-ok" ]
    run env QDISTRO_PROFILE=hardened bash -c '. "$1"; resolve_profile' _ "$BOOT"
    [ "$status" -ne 0 ]
}

@test "offline: the library mirrors the hardener's corroboration rule" {
    # Both must reduce to: a real /proc/1 AND systemd-detect-virt --chroot.
    grep -q 'systemd-detect-virt --chroot --quiet' "$LIB"
    grep -q '\[ -e /proc/1/comm \] || return 1' "$LIB"
    run awk '/^offline_root\(\) \{/,/^\}/' "$HARDEN_VT"
    [[ "$output" == *'[ -e /proc/1/comm ] || return 1'* ]]
    [[ "$output" == *'systemd-detect-virt --chroot --quiet'* ]]
}

# ---- behavioural ---------------------------------------------------------

# stub_dir NAME CHROOT(yes|no) -> dir with systemctl/loginctl/runuser/
# systemd-detect-virt stubs that log every invocation to calls.log.
stub_dir() {
    local dir="$BATS_TEST_TMPDIR/stub-$1" chroot="$2"
    mkdir -p "$dir"
    for cmd in systemctl loginctl runuser; do
        cat >"$dir/$cmd" <<EOS
#!/bin/bash
echo "$cmd \$*" >>"$BATS_TEST_TMPDIR/calls.log"
exit 0
EOS
    done
    cat >"$dir/systemd-detect-virt" <<EOS
#!/bin/bash
[ "$chroot" = yes ] && exit 0 || exit 1
EOS
    chmod +x "$dir"/*
    : >"$BATS_TEST_TMPDIR/calls.log"
    printf '%s\n' "$dir"
}

@test "offline: live by default — enable --now becomes enable + start" {
    local stub; stub="$(stub_dir live no)"
    PATH="$stub:$PATH" run bash -c '. "$1"; resolve_offline_install; sd_daemon_reload; sd_enable_now foo.service; live_only "probe" systemctl is-active foo' _ "$LIB"
    [ "$status" -eq 0 ]
    grep -qx 'systemctl daemon-reload' "$BATS_TEST_TMPDIR/calls.log"
    grep -qx 'systemctl enable foo.service' "$BATS_TEST_TMPDIR/calls.log"
    grep -qx 'systemctl start foo.service' "$BATS_TEST_TMPDIR/calls.log"
    grep -qx 'systemctl is-active foo' "$BATS_TEST_TMPDIR/calls.log"
}

@test "offline: a leaked flag on a non-chroot root stays live, with a warning" {
    local stub; stub="$(stub_dir leak no)"
    QDISTRO_OFFLINE_INSTALL=1 PATH="$stub:$PATH" run bash -c '. "$1"; resolve_offline_install; sd_enable_now foo.service' _ "$LIB"
    [ "$status" -eq 0 ]
    [[ "$output" == *"not corroborated as a chroot; ignoring it and running live"* ]]
    grep -qx 'systemctl start foo.service' "$BATS_TEST_TMPDIR/calls.log"
}

@test "offline: corroborated chroot enables but never starts, reloads or probes — each skip logged" {
    local stub; stub="$(stub_dir off yes)"
    QDISTRO_OFFLINE_INSTALL=1 PATH="$stub:$PATH" run bash -c '. "$1"; resolve_offline_install; sd_daemon_reload; sd_reload_dbus; sd_enable_now foo.service bar.socket; sd_try_restart foo.service; live_only "probe foo" systemctl is-active foo; is_offline' _ "$LIB"
    [ "$status" -eq 0 ]
    grep -qx 'systemctl enable foo.service bar.socket' "$BATS_TEST_TMPDIR/calls.log"
    ! grep -qE 'systemctl (start|daemon-reload|reload|try-restart|is-active)' "$BATS_TEST_TMPDIR/calls.log"
    [[ "$output" == *"[offline] skipped (needs a running system manager): systemctl daemon-reload"* ]]
    [[ "$output" == *"[offline] skipped (needs a running system bus): reload dbus policy"* ]]
    [[ "$output" == *"[offline] skipped (needs a running system manager): systemctl start foo.service bar.socket"* ]]
    [[ "$output" == *"[offline] skipped (needs a running system manager): systemctl try-restart foo.service"* ]]
    [[ "$output" == *"[offline] skipped (needs a running system manager): probe foo"* ]]
}

@test "offline: a real failure is not converted into a warning" {
    local stub; stub="$(stub_dir fail yes)"
    printf '#!/bin/bash\nexit 7\n' >"$stub/systemctl"
    QDISTRO_OFFLINE_INSTALL=1 PATH="$stub:$PATH" run bash -c '. "$1"; resolve_offline_install; sd_enable_now foo.service' _ "$LIB"
    [ "$status" -eq 7 ]
}

@test "offline: linger and user-unit enable write the on-disk equivalents" {
    unshare -r --mount true 2>/dev/null || skip "no unprivileged mount namespace"
    local stub; stub="$(stub_dir disk yes)"
    local root="$BATS_TEST_TMPDIR/root"; mkdir -p "$root/linger" "$root/home"
    local me; me="$(id -un)"; local grp; grp="$(id -gn)"
    # A unit that exists and one that does not.
    # Source the library BEFORE binding over $HOME (the checkout lives there).
    run unshare -r --mount bash -c '
        export QDISTRO_OFFLINE_INSTALL=1 PATH="$4:$PATH"
        . "$1"; resolve_offline_install
        mount --make-rprivate / 2>/dev/null || true
        mkdir -p /var/lib/systemd; mount --bind "$2" /var/lib/systemd || exit 111
        mount --bind "$3" "$HOME" || exit 111
        mkdir -p "$HOME/.config/systemd/user"; : >"$HOME/.config/systemd/user/present.service"
        linger_enable "$5" || exit 20
        # A requested unit that is not installed is an error (as `systemctl
        # enable` would refuse it) -- but the present one must still be wired.
        if user_unit_enable "$5" "$6" present.service absent.service; then exit 21; fi
        user_unit_enable "$5" "$6" present.service || exit 25
        [ -e /var/lib/systemd/linger/"$5" ] || exit 22
        [ "$(readlink "$HOME/.config/systemd/user/default.target.wants/present.service")" = ../present.service ] || exit 23
        [ ! -e "$HOME/.config/systemd/user/default.target.wants/absent.service" ] || exit 24
    ' _ "$LIB" "$root" "$root/home" "$stub" "$me" "$grp"
    [ "$status" -eq 0 ]
    [[ "$output" == *"wrote /var/lib/systemd/linger/$me"* ]]
    [[ "$output" == *"absent.service not present"*"cannot enable"* ]]
    ! grep -q loginctl "$BATS_TEST_TMPDIR/calls.log"
    ! grep -q runuser "$BATS_TEST_TMPDIR/calls.log"
}

@test "offline: live linger and user-unit enable go through logind and the user manager" {
    local stub; stub="$(stub_dir livelinger no)"
    local me; me="$(id -un)"; local grp; grp="$(id -gn)"
    PATH="$stub:$PATH" run bash -c '. "$1"; resolve_offline_install; linger_enable "$2"; user_unit_enable "$2" "$3" a.service b.target' _ "$LIB" "$me" "$grp"
    [ "$status" -eq 0 ]
    grep -qx "loginctl enable-linger $me" "$BATS_TEST_TMPDIR/calls.log"
    grep -qx "runuser -l $me -c systemctl --user enable a.service b.target" "$BATS_TEST_TMPDIR/calls.log"
}

@test "offline: the image path leaves qdwin-session.target out of default.target.wants" {
    # The greeter starts the target; config.sh sets QDWIN_SESSION_AUTOSTART=0
    # and the installer must honour it (this replaces the old runuser shim).
    run awk '/^SESSION_UNITS=/,/user_unit_enable admin/' "$REPO/scripts/install/install-qdwin-session-for-vm.sh"
    [[ "$output" == *'QDWIN_SESSION_AUTOSTART'* ]]
    [[ "$output" == *'SESSION_UNITS="qdwin-session.target $SESSION_UNITS"'* ]]
    [[ "$output" == *'user_unit_enable admin "$(id -gn admin)" $SESSION_UNITS'* ]]
    # Offline, a failed enable is fatal (the symlinks ARE the wiring).
    run awk '/user_unit_enable admin/,/^fi/' "$REPO/scripts/install/install-qdwin-session-for-vm.sh"
    [[ "$output" == *'is_offline'* ]]
    [[ "$output" == *'exit 4'* ]]
}

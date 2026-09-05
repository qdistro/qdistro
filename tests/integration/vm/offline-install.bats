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
    # The chain: every installer image/config.sh runs (the INSTALLERS array
    # plus the qdwin-session installer it calls directly).
    CHAIN=$(grep -oE 'scripts/install/install-[a-z0-9-]+\.sh' "$CONFIG_SH" | sort -u)
    [ -n "$CHAIN" ]
}

# ---- static invariants ---------------------------------------------------

@test "offline: every chain installer sources the library and resolves the mode" {
    local f
    for f in $CHAIN; do
        grep -q 'lib/qdistro-offline.sh' "$REPO/$f" || fail "$f does not source lib/qdistro-offline.sh"
        grep -q '^resolve_offline_install' "$REPO/$f" || fail "$f does not call resolve_offline_install"
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
            /systemctl[[:space:]]+(start|stop|restart|try-restart|reload|daemon-reload|is-active)|--now|busctl|loginctl|systemctl[[:space:]]+--user[[:space:]]+(start|enable)/ {
                print FILENAME ":" FNR ": " $0
            }' "$REPO/$f"
        if [ -n "$output" ]; then echo "$output"; bad=1; fi
    done
    [ "$bad" = 0 ]
}

@test "offline: the image chain is fatal (no fail-open loop, missing installer aborts)" {
    run awk '/^for entry in "\$\{INSTALLERS\[@\]\}"; do/,/^done/' "$CONFIG_SH"
    [ -n "$output" ]
    [[ "$output" != *"WARN"* ]]
    [[ "$output" != *"|| echo"* ]]
    [[ "$output" == *"FATAL: chain installer missing"* ]]
    [[ "$output" == *"FATAL: \$installer failed"* ]]
    grep -q '^export QDISTRO_OFFLINE_INSTALL=1' "$CONFIG_SH"
    grep -q '^export QDWIN_SESSION_AUTOSTART=0' "$CONFIG_SH"
    ! grep -q 'SHIMS' "$CONFIG_SH"
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
        user_unit_enable "$5" "$6" present.service absent.service || exit 21
        [ -e /var/lib/systemd/linger/"$5" ] || exit 22
        [ "$(readlink "$HOME/.config/systemd/user/default.target.wants/present.service")" = ../present.service ] || exit 23
        [ ! -e "$HOME/.config/systemd/user/default.target.wants/absent.service" ] || exit 24
    ' _ "$LIB" "$root" "$root/home" "$stub" "$me" "$grp"
    [ "$status" -eq 0 ]
    [[ "$output" == *"wrote /var/lib/systemd/linger/$me"* ]]
    [[ "$output" == *"absent.service not present"* ]]
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
    run awk '/^SESSION_UNITS=/,/^user_unit_enable/' "$REPO/scripts/install/install-qdwin-session-for-vm.sh"
    [[ "$output" == *'QDWIN_SESSION_AUTOSTART'* ]]
    [[ "$output" == *'SESSION_UNITS="qdwin-session.target $SESSION_UNITS"'* ]]
    [[ "$output" == *'user_unit_enable admin users $SESSION_UNITS'* ]]
}

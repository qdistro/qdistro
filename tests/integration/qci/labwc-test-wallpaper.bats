#!/usr/bin/env bats
# The labwc GUI-test lane's desktop wallpaper (F7). labwc's autostart, seeded
# from the openSUSE lxqt-labwc-session package, runs swaybg on an image the
# qdistro image never installs, so the empty desktop was solid black and vm-gui
# refused it as near-black. scripts/vm/assets/labwc-test-wallpaper.sh rewrites
# the test user's copy of that autostart; these tests run the real helper.
# What only a provisioned VM can show (labwc actually reading the file, swaybg
# painting it) is verified live, not here.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    HELPER="$REPO_ROOT/scripts/vm/assets/labwc-test-wallpaper.sh"
    SPINNER="$REPO_ROOT/scripts/vm/spin-test-vm-gui.sh"
    TDIR="$(mktemp -d)"
    mkdir -p "$TDIR/bin" "$TDIR/seed"
    printf '#!/bin/sh\nexit 0\n' > "$TDIR/bin/swaybg"
    chmod +x "$TDIR/bin/swaybg"
    WP="$TDIR/qdistro-test-pattern.png"
    printf 'PNG' > "$WP"
    # The shipped openSUSE autostart, reduced to its live lines.
    cat > "$TDIR/seed/autostart" <<'EOF'
# Example autostart file for a labwc LXQt session
# Set background color or image (below the desktop):
swaybg -i /usr/share/wallpapers/openSUSEdefault/contents/images/default.png  >/dev/null 2>&1 &

# Faster startup for GTK apps:
dbus-update-activation-environment --systemd DISPLAY WAYLAND_DISPLAY > /dev/null 2>&1 &
swayidle -w timeout 300 "wlopm --off \*" resume "wlopm --on \*" > /dev/null 2>&1 &
EOF
    printf '<openbox_config/>\n' > "$TDIR/seed/rc.xml"
}

teardown() { rm -rf "$TDIR"; }

run_helper() { PATH="$TDIR/bin:$PATH" run sh "$HELPER" "$@"; }

# bats does not fail a test on a `! cmd` line (set -e ignores negated
# commands), so every "must NOT match" assertion goes through this.
refute_grep() {
    if grep -q "$@"; then echo "unexpected match: grep -q $*"; return 1; fi
}

@test "seeds a missing config dir from the distro defaults and replaces the distro swaybg" {
    run_helper "$TDIR/cfg" "$WP" "$TDIR/seed"
    [ "$status" -eq 0 ]
    [ -f "$TDIR/cfg/rc.xml" ]                         # the rest of the seed came along
    local auto="$TDIR/cfg/autostart"
    refute_grep 'openSUSEdefault' "$auto"
    [ "$(grep -c '^[^#]*swaybg' "$auto")" -eq 1 ]     # exactly one background client
    grep -Fqx "swaybg -m tile -i '$WP' >\"\${XDG_RUNTIME_DIR:-/tmp}/qdistro-swaybg.log\" 2>&1 &" "$auto"
    grep -q '^dbus-update-activation-environment' "$auto"
    refute_grep '^[^#]*swayidle' "$auto"                # no idle DPMS-off to blank captures
    refute_grep '^[^#]*wlopm' "$auto"
    sh -n "$auto"
}

@test "an existing config dir wins over the seed, and a rerun is byte-identical" {
    mkdir -p "$TDIR/cfg"
    printf 'exec swaybg -c "#000000" &\nfoo &\n  swayidle -w timeout 60 "wlopm --off \\*" &\n# swayidle stays documented\n' > "$TDIR/cfg/autostart"
    run_helper "$TDIR/cfg" "$WP" "$TDIR/seed"
    [ "$status" -eq 0 ]
    [ ! -e "$TDIR/cfg/rc.xml" ]                       # did not re-seed over it
    refute_grep '#000000' "$TDIR/cfg/autostart"          # a conflicting swaybg is removed
    grep -q '^foo &$' "$TDIR/cfg/autostart"
    refute_grep '^[^#]*swayidle' "$TDIR/cfg/autostart"    # an indented swayidle is removed too
    grep -q '^# swayidle stays documented$' "$TDIR/cfg/autostart"   # comments are kept
    cp "$TDIR/cfg/autostart" "$TDIR/first"
    run_helper "$TDIR/cfg" "$WP" "$TDIR/seed"
    [ "$status" -eq 0 ]
    cmp "$TDIR/first" "$TDIR/cfg/autostart"
}

@test "fails closed when swaybg is not installed" {
    mkdir -p "$TDIR/nobin"
    local t
    for t in sh awk cp mkdir dirname mv; do ln -s "$(command -v "$t")" "$TDIR/nobin/$t"; done
    PATH="$TDIR/nobin" run "$TDIR/nobin/sh" "$HELPER" "$TDIR/cfg" "$WP" "$TDIR/seed"
    [ "$status" -ne 0 ]
    [[ "$output" == *"swaybg is not installed"* ]]
    [ ! -e "$TDIR/cfg" ]
}

@test "fails closed when the wallpaper asset is missing" {
    run_helper "$TDIR/cfg" "$TDIR/absent.png" "$TDIR/seed"
    [ "$status" -ne 0 ]
    [ ! -e "$TDIR/cfg" ]
}

@test "the labwc bake renders the asset and runs the helper before labwc first starts" {
    local labwc_block
    labwc_block=$(sed -n '/^if \[ "\$SESSION" = labwc \]; then$/,/^fi  # end labwc-only steps 6-8$/p' "$SPINNER")
    [[ "$labwc_block" == *"install_test_wallpaper"* ]]
    [[ "$labwc_block" == *'scripts/vm/assets/labwc-test-wallpaper.sh'* ]]
    [[ "$labwc_block" == *'/home/admin/.config/lxqt/labwc "$TEST_WALLPAPER"'* ]]
    # startlxqtwayland seeds the config dir on first start; the helper must
    # have created it before then or its copy would be the one labwc reads.
    local helper_line start_line
    helper_line=$(grep -n 'assets/labwc-test-wallpaper.sh' "$SPINNER" | tail -1 | cut -d: -f1)
    start_line=$(grep -n "enable --now qdistro-labwc.service" "$SPINNER" | cut -d: -f1)
    [ "$helper_line" -lt "$start_line" ]
    # The seed path the helper defaults to is the one startlxqtwayland copies.
    grep -Fq 'cp -av /usr/share/lxqt/wayland/labwc "$XDG_CONFIG_HOME/lxqt/labwc"' \
        "$REPO_ROOT/deploy/qdistro-startlxqtwayland.sh"
    grep -Fq 'seed=${3:-/usr/share/lxqt/wayland/labwc}' "$HELPER"
    grep -Fq 'export LABWC_CONFIG_DIR="$XDG_CONFIG_HOME/lxqt/labwc"' \
        "$REPO_ROOT/deploy/qdistro-startlxqtwayland.sh"
}

@test "the golden-clone check accepts exactly what the helper writes" {
    # Take the clone check's pattern from the spinner and run it against the
    # helper's real output for the real asset path.
    local pat
    pat=$(grep -o "grep -q \"^swaybg -m tile -i '[^']*'\"" "$SPINNER" | head -1 | sed 's/^grep -q "//; s/"$//')
    [ -n "$pat" ]
    run_helper "$TDIR/cfg" "$WP" "$TDIR/seed"
    [ "$status" -eq 0 ]
    local real=/home/admin/Pictures/Wallpapers/qdistro-test-pattern.png
    sed "s#$WP#$real#" "$TDIR/cfg/autostart" | grep -q "$pat"
    grep -Fq "TEST_WALLPAPER=$real" "$SPINNER"
}

@test "the wallpaper generator is still extractable after moving into the shared function" {
    local spin_out="$TDIR/wp.png"
    bash "$REPO_ROOT/scripts/vm/assets/make-test-wallpaper.sh" "$spin_out"
    [ "$(head -c 8 "$spin_out" | od -An -tx1 | tr -d ' \n')" = 89504e470d0a1a0a ]
}

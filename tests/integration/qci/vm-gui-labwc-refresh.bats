#!/usr/bin/env bats
# vm-gui makes labwc present a fresh frame before EVERY host capture (F6,
# 2026-09-30): in the labwc/XWayland lane the last committed frame can hold a
# partly drawn XWayland window, labwc commits nothing newer on its own, and
# `virsh screenshot` returned that stale frame for 15 s+ (permissions-gui/22
# S1, 04 S2). The host side runs the REAL vm-gui file, copied beside a fake
# vm-script that plays the guest's reply, against a fake virsh that serves a
# scripted sequence of host frames; both log into one file so the ORDER
# refresh -> capture is observable. The guest side runs the REAL
# lib/labwc-present-frame.sh against a fake /proc, pgrep, runuser and grim.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    command -v magick >/dev/null 2>&1 || skip "ImageMagick magick not installed"
    T="$BATS_TEST_TMPDIR"
    mkdir -p "$T/vm" "$T/bin" "$T/artifacts" "$T/frames"
    cp "$REPO_ROOT/scripts/vm/vm-gui" "$T/vm/vm-gui"
    cp -r "$REPO_ROOT/scripts/vm/lib" "$T/vm/lib"
    VM_GUI="$T/vm/vm-gui"
    export ORDER_LOG="$T/order.log" GUEST_REPLY="$T/guest-reply" FRAMES="$T/frames" SHOTS="$T/shots"
    : > "$ORDER_LOG"; echo 0 > "$SHOTS"
    # The complete window, the same with a stale black band (the failure),
    # and the same with a 2x16 caret (a legitimate difference).
    FRESH="$T/fresh.png"; STALE="$T/stale.png"; CARET="$T/caret.png"
    magick -size 1280x800 xc:'#20252b' -fill white -draw 'rectangle 190,82 1090,718' \
        -fill black -pointsize 24 -annotate +560+100 'admin approvals' "$FRESH"
    magick "$FRESH" -fill black -draw 'rectangle 190,640 1090,718' "$STALE"
    magick "$FRESH" -fill black -draw 'rectangle 600,400 601,415' "$CARET"

    cat > "$T/vm/vm-script" <<'EOF'
#!/usr/bin/env bash
# The guest: record that the refresh script arrived, then answer as scripted.
body=$(cat)
case "$body" in *"grim -c -t png -"*) echo refresh >> "$ORDER_LOG" ;; *) echo other-script >> "$ORDER_LOG" ;; esac
[ -f "$GUEST_REPLY" ] && cat "$GUEST_REPLY"
exit "${GUEST_RC:-0}"
EOF
    chmod +x "$T/vm/vm-script"

    # Host frame N is $FRAMES/N.png, or the highest one there is.
    cat > "$T/bin/virsh" <<'EOF'
#!/usr/bin/env bash
case " $* " in
    *" screenshot "*)
        n=$(( $(cat "$SHOTS") + 1 )); echo "$n" > "$SHOTS"
        echo capture >> "$ORDER_LOG"
        f="$FRAMES/$n.png"
        [ -f "$f" ] || f=$(ls "$FRAMES"/*.png | sort -V | tail -n1)
        cp "$f" "${!#}" ;;
    *" qemu-monitor-command "*) ;;
    *) echo "unexpected fake virsh invocation: $*" >&2; exit 2 ;;
esac
EOF
    chmod +x "$T/bin/virsh"
    export PATH="$T/bin:$PATH" QCI_GUI_ARTIFACT_DIR="$T/artifacts"
    unset QCI_VM_GUI_SESSION
}

# reply <token> [frame.png]: what the guest prints (vm-exec noise included).
reply() {
    {
        echo '[vm-exec] guest identity pinned (pid 1 start 1)'
        [ -n "${2:-}" ] && echo "qci-labwc-frame=$(base64 -w0 "$2")"
        echo "qci-labwc-refresh=$1"
    } > "$GUEST_REPLY"
}
host_frames() { local i=1 f; for f in "$@"; do cp "$f" "$FRAMES/$i.png"; i=$((i + 1)); done; }
order() { tr '\n' ' ' < "$ORDER_LOG"; }
# A published frame is padded to a view-unique size; its raw frame is the
# top-left crop (view-geometry.sh, invariant I6).
same_pixels() {
    magick "$1" -crop 1280x800+0+0 +repage "$T/crop.png"
    local ae
    ae=$(magick compare -metric AE "$T/crop.png" "$2" null: 2>&1) || true
    [ "${ae%% *}" = 0 ]
}

@test "labwc lane: refresh first, then the host frame that matches labwc's frame is delivered" {
    reply ok "$FRESH"; host_frames "$FRESH"
    run "$VM_GUI" test-vm screenshot "$T/artifacts/s1.png"
    [ "$status" -eq 0 ]
    [ "$(order)" = "refresh capture " ]
    same_pixels "$T/artifacts/s1.png" "$FRESH"
}

@test "a host frame still showing the stale window is re-captured until it matches" {
    reply ok "$FRESH"; host_frames "$STALE" "$STALE" "$FRESH"
    run "$VM_GUI" test-vm screenshot "$T/artifacts/s1.png"
    [ "$status" -eq 0 ]
    [ "$(order)" = "refresh capture capture capture " ]
    same_pixels "$T/artifacts/s1.png" "$FRESH"
}

@test "a host frame that never shows labwc's frame is refused, nothing delivered" {
    reply ok "$FRESH"; host_frames "$STALE"
    QCI_VM_GUI_PRESENT_TRIES=3 run "$VM_GUI" test-vm screenshot "$T/artifacts/s1.png"
    [ "$status" -ne 0 ]
    [[ "$output" == *"never showed the frame labwc presented"* ]]
    [ "$(order)" = "refresh capture capture capture " ]
    [ ! -e "$T/artifacts/s1.png" ]
}

@test "a caret-sized difference is within tolerance" {
    reply ok "$FRESH"; host_frames "$CARET"
    run "$VM_GUI" test-vm screenshot "$T/artifacts/s1.png"
    [ "$status" -eq 0 ]
    [ "$(order)" = "refresh capture " ]
}

@test "a guest without labwc captures directly; unless labwc was declared" {
    reply not-labwc; host_frames "$FRESH"
    run "$VM_GUI" test-vm screenshot "$T/artifacts/s1.png"
    [ "$status" -eq 0 ]
    [ "$(order)" = "refresh capture " ]
    : > "$ORDER_LOG"; rm -f "$T/artifacts/s1.png"
    QCI_VM_GUI_SESSION=labwc run "$VM_GUI" test-vm screenshot "$T/artifacts/s1.png"
    [ "$status" -ne 0 ]
    [ "$(order)" = "refresh " ]
}

@test "every answer that is not ok/not-labwc refuses the capture before any host frame" {
    local case
    for case in no-answer malformed ok-no-frame no-grim grim-failed ambiguous probe-failed no-socket; do
        : > "$ORDER_LOG"; rm -f "$T/artifacts/s1.png"
        case "$case" in
            no-answer) rm -f "$GUEST_REPLY" ;;
            malformed) printf 'qci-labwc-refresh=ok extra\n' > "$GUEST_REPLY" ;;
            ok-no-frame) reply ok ;;
            *) reply "$case" ;;
        esac
        host_frames "$FRESH"
        run "$VM_GUI" test-vm screenshot "$T/artifacts/s1.png"
        echo "case=$case status=$status order=$(order) output=$output"
        [ "$status" -ne 0 ]
        [ "$(order)" = "refresh " ]
        [ ! -e "$T/artifacts/s1.png" ]
    done
}

@test "a guest command that failed is refused even when its output looks like ok" {
    reply ok "$FRESH"; host_frames "$FRESH"
    GUEST_RC=1 run "$VM_GUI" test-vm screenshot "$T/artifacts/s1.png"
    [ "$status" -ne 0 ]
    [[ "$output" == *"labwc frame refresh failed (status 1)"* ]]
    [ "$(order)" = "refresh " ]
    [ ! -e "$T/artifacts/s1.png" ]
}

@test "without ImageMagick the barrier cannot run, so the capture is refused" {
    # A PATH with no magick at all (a failing stub would still be FOUND); the
    # fake virsh copies files, so it needs none.
    local farm="$T/farm" c p
    mkdir -p "$farm"
    for c in bash sh mktemp sha256sum awk cp rm date stat readlink flock grep tail head \
             sed dirname basename chmod mv cat id sleep find sort cut tr wc base64 ls; do
        p=$(command -v "$c" 2>/dev/null) && ln -sf "$p" "$farm/$c"
    done
    [ ! -e "$farm/magick" ]
    cp "$T/bin/virsh" "$farm/virsh"
    reply ok "$FRESH"; host_frames "$STALE"
    run env PATH="$farm" "$VM_GUI" test-vm screenshot "$T/artifacts/s1.png"
    [ "$status" -ne 0 ]
    [[ "$output" == *"without ImageMagick the host frame cannot be checked"* ]]
    [ "$(order)" = "refresh " ]
    [ ! -e "$T/artifacts/s1.png" ]
}

@test "the gate declares the admin lane as labwc, so a false not-labwc cannot skip the refresh" {
    local g="$REPO_ROOT/ci/lib/gates/gui.sh"
    grep -q '\[ "\$lane" = admin \] && vm_gui_session=\${QCI_VM_GUI_SESSION:-labwc}' "$g"
    grep -q 'QCI_VM_GUI_SESSION="\$vm_gui_session"' "$g"
}

@test "QCI_VM_GUI_SESSION=none declares no labwc: no guest call at all" {
    rm -f "$GUEST_REPLY"; host_frames "$FRESH"
    QCI_VM_GUI_SESSION=none run "$VM_GUI" test-vm screenshot "$T/artifacts/s1.png"
    [ "$status" -eq 0 ]
    [ "$(order)" = "capture " ]
    QCI_VM_GUI_SESSION=bogus run "$VM_GUI" test-vm screenshot "$T/artifacts/s2.png"
    [ "$status" -ne 0 ]
}

@test "screenshot-fresh and click-preview go through the same refresh" {
    reply grim-failed; host_frames "$FRESH"
    run "$VM_GUI" test-vm screenshot-fresh "$T/artifacts/f.png"
    [ "$status" -ne 0 ]
    [ "$(order)" = "refresh " ]
    : > "$ORDER_LOG"
    run "$VM_GUI" test-vm click-preview 490 522 "radio"
    [ "$status" -ne 0 ]
    [ "$(order)" = "refresh " ]
    reply ok "$FRESH"; : > "$ORDER_LOG"
    run "$VM_GUI" test-vm click-preview 490 522 "radio"
    [ "$status" -eq 0 ]
    [ "$(head -n2 "$ORDER_LOG" | tr '\n' ' ')" = "refresh capture " ]
}

@test "vm-gui takes no host frame outside capture_presented_shot" {
    local f="$REPO_ROOT/scripts/vm/vm-gui" start end
    start=$(grep -n '^capture_presented_shot() {' "$f" | cut -d: -f1)
    end=$(awk -v s="$start" 'NR > s && /^}/ { print NR; exit }' "$f")
    [ -n "$start" ] && [ -n "$end" ]
    # every capture_virsh_shot call sits inside that function
    run awk -v s="$start" -v e="$end" '/capture_virsh_shot "/ && (NR < s || NR > e) { print NR": "$0 }' "$f"
    [ -z "$output" ]
    run grep -c 'capture_presented_shot "\$VM"' "$f"
    [ "$output" -eq 4 ]
}

# --- guest side: lib/labwc-present-frame.sh ----------------------------------

guest_setup() {
    G="$T/guest"; mkdir -p "$G/bin" "$G/proc" "$G/run/1000"
    export GRIM_LOG="$G/grim.log"; : > "$GRIM_LOG"
    # pgrep -x labwc -> $FAKE_LABWC_PIDS; pgrep -P <pid> -> $FAKE_CHILDREN
    cat > "$G/bin/pgrep" <<'EOF'
#!/bin/bash
# like pgrep: exit 1 when nothing matches
case "$1" in
    -x) [ -n "${FAKE_LABWC_PIDS:-}" ] || exit 1; printf '%s\n' $FAKE_LABWC_PIDS ;;
    -P) [ -n "${FAKE_CHILDREN:-}" ] || exit 1; printf '%s\n' $FAKE_CHILDREN ;;
esac
EOF
    cat > "$G/bin/runuser" <<'EOF'
#!/bin/bash
[ "$1" = -u ] && [ "$3" = -- ] || { echo "bad runuser $*" >&2; exit 2; }
shift 3; exec "$@"
EOF
    cat > "$G/bin/getent" <<'EOF'
#!/bin/bash
[ "$1 $2" = "passwd 1000" ] && echo "admin:x:1000:100::/home/admin:/bin/bash"
EOF
    cat > "$G/bin/grim" <<'EOF'
#!/bin/bash
echo "WAYLAND_DISPLAY=$WAYLAND_DISPLAY args=$*" >> "$GRIM_LOG"
[ "${FAKE_GRIM_FAIL:-0}" = 1 ] && exit 1
printf 'PNGDATA'
EOF
    chmod +x "$G/bin/"*
    # labwc (pid 100, uid 1000); two sockets in its runtime dir
    mkdir -p "$G/proc/100"; printf 'Name:\tlabwc\nUid:\t1000\t1000\t1000\t1000\n' > "$G/proc/100/status"
    local s
    for s in wayland-0 wayland-1; do
        python3 -c 'import socket,sys; socket.socket(socket.AF_UNIX).bind(sys.argv[1])' "$G/run/1000/$s"
    done
}
child_env() { mkdir -p "$G/proc/$1"; printf '%s\0' "HOME=/home/admin" "$2" > "$G/proc/$1/environ"; }
guest_run() {
    run env PATH="$G/bin:$PATH" QCI_PROC_ROOT="$G/proc" QCI_RUNTIME_ROOT="$G/run" \
        bash "$REPO_ROOT/scripts/vm/lib/labwc-present-frame.sh"
}

@test "guest: the screencopy targets labwc's OWN socket, not another compositor's" {
    guest_setup
    child_env 201 WAYLAND_DISPLAY=wayland-0
    FAKE_LABWC_PIDS=100 FAKE_CHILDREN=201 guest_run
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "qci-labwc-refresh=ok" ]
    [ "${lines[-2]}" = "qci-labwc-frame=$(printf PNGDATA | base64 -w0)" ]
    [ "$(cat "$GRIM_LOG")" = "WAYLAND_DISPLAY=wayland-0 args=-c -t png -" ]
}

@test "guest: labwc's socket unknown or ambiguous is refused even when another socket works" {
    guest_setup
    child_env 201 HOME=/x          # no WAYLAND_DISPLAY in labwc's children
    FAKE_LABWC_PIDS=100 FAKE_CHILDREN=201 guest_run
    [ "${lines[-1]}" = "qci-labwc-refresh=no-socket" ]
    child_env 201 WAYLAND_DISPLAY=wayland-0
    child_env 202 WAYLAND_DISPLAY=wayland-1   # children disagree
    FAKE_LABWC_PIDS=100 FAKE_CHILDREN="201 202" guest_run
    [ "${lines[-1]}" = "qci-labwc-refresh=no-socket" ]
    child_env 202 WAYLAND_DISPLAY=wayland-9   # named socket does not exist
    FAKE_LABWC_PIDS=100 FAKE_CHILDREN=202 guest_run
    [ "${lines[-1]}" = "qci-labwc-refresh=no-socket" ]
    [ ! -s "$GRIM_LOG" ]
}

@test "guest: a failed process probe is not \"no labwc\"" {
    guest_setup
    printf '#!/bin/bash\nexit 2\n' > "$G/bin/pgrep"
    FAKE_LABWC_PIDS=100 guest_run
    [ "${lines[-1]}" = "qci-labwc-refresh=probe-failed" ]
    printf '#!/bin/bash\nexit 127\n' > "$G/bin/pgrep"
    guest_run
    [ "${lines[-1]}" = "qci-labwc-refresh=probe-failed" ]
    # labwc found, but listing its children fails
    cat > "$G/bin/pgrep" <<'EOF'
#!/bin/bash
[ "$1" = -x ] && { echo 100; exit 0; }
exit 3
EOF
    guest_run
    [ "${lines[-1]}" = "qci-labwc-refresh=probe-failed" ]
    [ ! -s "$GRIM_LOG" ]
}

@test "guest: no labwc, two labwc, and a failing grim" {
    guest_setup
    FAKE_LABWC_PIDS="" guest_run
    [ "${lines[-1]}" = "qci-labwc-refresh=not-labwc" ]
    FAKE_LABWC_PIDS="100 101" guest_run
    [ "${lines[-1]}" = "qci-labwc-refresh=ambiguous" ]
    child_env 201 WAYLAND_DISPLAY=wayland-0
    FAKE_GRIM_FAIL=1 FAKE_LABWC_PIDS=100 FAKE_CHILDREN=201 guest_run
    [ "${lines[-1]}" = "qci-labwc-refresh=grim-failed" ]
    [[ "$output" != *qci-labwc-frame=* ]]
}

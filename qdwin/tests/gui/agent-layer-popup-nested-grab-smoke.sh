#!/usr/bin/env bash
# Deterministic live lane for D9: a grabbed layer popup confines input to its
# own family — the popup, its layer parent and a same-client NESTED submenu
# popup — and never hands a grabbed press to a same-client sibling toplevel.
# Host-side the refilter is pinned by qdwin/test_popup_grab_hardening.py
# (check_d9_layer_popup_scope); this drives the real compositor with a real
# grab serial.
#
# Driver: qdwin-nested-grab-probe (one wl_client owning a TOP layer surface L,
# a sibling xdg_toplevel T, a popup P1 on L and, nested, a popup P2 on P1). It
# opens the chain with the serial of a real press on L and logs every pointer
# enter/leave/button per surface. zwlr_layer_shell_v1 is shell-only while a
# shell is bound, so qdshell is stopped for the lane and restarted on exit.
#
#   control  P1 only: pointer over P1 enters P1; a press over T dismisses P1
#            (popup_done) and T gets no press, and no enter before the dismiss.
#   nested   P1+P2: pointer over P2 enters P2; a press on P2 reaches P2 (the
#            nested submenu gets grabbed input) without dismissing; a press
#            over T dismisses the chain and T gets no press, and no enter
#            before the dismiss.
#
# Exit: 0 pass; 1 assertion failed; 2 setup/environment failure.
set -uo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
QDWIN_REPO=${QDWIN_REPO:-$ROOT}
VMNAME=${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -n1)}
export QDWIN_REPO VMNAME
export QDWIN_VIRSH=${QDWIN_VIRSH:-virsh -c qemu:///session}
# shellcheck source=qdwin-helpers.sh
source "$QDWIN_REPO/tests/gui/qdwin-helpers.sh"
qdwin_set_vm "$VMNAME"
# shellcheck source=../lib/journal-smoke.sh
source "$QDWIN_REPO/tests/lib/journal-smoke.sh"

AS_ADMIN='runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1'
PROBE=/usr/bin/qdwin-nested-grab-probe
user_systemctl() {
    js_guest "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user $*"
}
kill_probe() {
    js_guest "pkill -KILL -u admin -f '^$PROBE( |\$)' 2>/dev/null; sleep 0.3; true" >/dev/null
}
restore() {
    kill_probe
    user_systemctl reset-failed qdshell.service >/dev/null 2>&1 || true
    user_systemctl start qdshell.service >/dev/null 2>&1 || true
    local _
    for _ in $(seq 1 30); do
        js_qs_ipc capabilities | grep -q 'bound=true' && return 0
        sleep 1
    done
    echo "SETUP: qdshell did not rebind within 30s after the smoke; later scenarios on this VM will fail setup" >&2
}
trap restore EXIT

js_require_session
js_guest "test -x $PROBE && echo PROBE-OK" | grep -qx PROBE-OK \
    || js_setup_fail "$PROBE missing from the golden (qdwin install regressed)"
user_systemctl stop qdshell.service >/dev/null \
    || js_setup_fail "could not stop qdshell.service"
sleep 1
qdwin_prime_pointer || js_setup_fail "could not prime the pointer"

LOG=""
probe_log() { js_guest "cat $LOG 2>/dev/null"; }
# wait_log <ERE> <timeout_s> — poll the probe log; prints the match.
wait_log() {
    local re=$1 end=$((SECONDS + $2)) m
    while :; do
        m=$(probe_log | grep -E -- "$re")
        [ -n "$m" ] && { printf '%s\n' "$m"; return 0; }
        [ "$SECONDS" -ge "$end" ] && return 1
        sleep 0.25
    done
}
# rect_of <name> — "x y w h" from the probe's GEOM line.
rect_of() { probe_log | sed -nE "s/^GEOM $1 ([0-9-]+) ([0-9-]+) ([0-9]+) ([0-9]+)\$/\1 \2 \3 \4/p" | tail -1; }
centre() { local x y w h; read -r x y w h <<<"$1"; echo "$((x + w / 2)) $((y + h / 2))"; }
inside() { # inside <px> <py> <rect>
    local px=$1 py=$2 x y w h; read -r x y w h <<<"$3"
    [ -n "$x" ] && [ "$px" -ge "$x" ] && [ "$px" -lt $((x + w)) ] \
        && [ "$py" -ge "$y" ] && [ "$py" -lt $((y + h)) ]
}

run_phase() {
    local phase=$1 flag="" C H line T L P1 P2 r target tx ty n0 tail_ev
    [ "$phase" = nested ] && flag=--nested
    kill_probe
    LOG=/tmp/qdwin-nested-grab-$phase.log
    C=$(js_cursor); [ -n "$C" ] || js_setup_fail "no journal cursor"
    js_guest "rm -f $LOG; setsid -f $AS_ADMIN $PROBE $flag --duration 90 >$LOG 2>&1 </dev/null" >/dev/null
    wait_log '^READY ' 15 >/dev/null \
        || js_setup_fail "$phase: probe never became ready: $(probe_log | tr '\n' ' ')"

    # T's on-screen rect from the compositor (the client cannot know it).
    H=$(js_wait "$C" 'qdwin: toplevel_added handle=[0-9]+ uid=1000 pid=[0-9]+ app_id=qdwin-nested-grab-probe' 10 \
        | sed -nE 's/.*toplevel_added handle=([0-9]+) .*/\1/p' | head -1)
    [ -n "$H" ] || js_setup_fail "$phase: the probe's toplevel never reached qdwin"
    line=$(js_wait "$C" "qdwin: mapped handle=$H size=[0-9]+x[0-9]+ pos=-?[0-9]+,-?[0-9]+" 10 | tail -1)
    T=$(printf '%s' "$line" | sed -nE 's/.*size=([0-9]+)x([0-9]+) pos=(-?[0-9]+),(-?[0-9]+).*/\3 \4 \1 \2/p')
    [ -n "$T" ] || js_setup_fail "$phase: no 'mapped handle=$H' line for the probe toplevel"
    case "$line" in *"(normal)"*) ;; *) js_setup_fail "$phase: probe toplevel not on the normal layer: $line" ;; esac

    # Open the chain with a real press on L.
    L=$(rect_of L); [ -n "$L" ] || js_setup_fail "$phase: no GEOM L line"
    read -r tx ty <<<"$(centre "$L")"
    qdwin_click "$tx" "$ty"
    if ! wait_log '^OPEN ' 10 >/dev/null; then
        probe_log | grep -q '^GRAB_REFUSED' \
            && js_fail "$phase: the popup grab with the serial of a real press on L was refused: $(probe_log | tr '\n' ' ')"
        js_setup_fail "$phase: the popup chain never opened: $(probe_log | tr '\n' ' ')"
    fi
    P1=$(rect_of P1); P2=$(rect_of P2)

    target=P1; [ "$phase" = nested ] && target=P2
    read -r tx ty <<<"$(centre "$(rect_of "$target")")"
    qdwin_mouse_move "$tx" "$ty"
    if ! wait_log "^EV ENTER $target\$" 5 >/dev/null; then
        # Nested phase: delivery to the submenu IS the D9 assertion.
        [ "$phase" = nested ] \
            && js_fail "nested: pointer over the submenu P2 ($tx,$ty) never entered it under the grab: $(probe_log | tr '\n' ' ')"
        js_setup_fail "$phase: pointer over $target ($tx,$ty) never entered it: $(probe_log | tr '\n' ' ')"
    fi
    if [ "$phase" = nested ]; then
        qdwin_click "$tx" "$ty"
        wait_log '^EV BUTTON P2 pressed$' 5 >/dev/null \
            || js_fail "nested: a press on the nested submenu P2 did not reach it under the grab: $(probe_log | tr '\n' ' ')"
        probe_log | grep -q '^EV POPUP_DONE' \
            && js_fail "nested: a press inside the nested submenu dismissed the chain: $(probe_log | tr '\n' ' ')"
        js_pass "nested: grabbed press on the nested submenu P2 delivered, chain kept"
    fi

    # The outside press over the sibling toplevel T.
    read -r tx ty <<<"$(centre "$T")"
    for r in "$L" "$P1" "$P2"; do
        [ -n "$r" ] && inside "$tx" "$ty" "$r" \
            && js_setup_fail "$phase: T's centre ($tx,$ty) overlaps the popup chain ($r)"
    done
    n0=$(probe_log | wc -l)
    qdwin_mouse_move "$tx" "$ty"
    sleep 0.3
    qdwin_click "$tx" "$ty"
    sleep 1
    tail_ev=$(probe_log | tail -n +"$((n0 + 1))")
    printf '%s\n' "$tail_ev" | grep -q '^EV BUTTON T pressed$' \
        && js_fail "$phase: a press over the same-client toplevel T was delivered to T while the popup grab was active: $(printf '%s' "$tail_ev" | tr '\n' ' ')"
    printf '%s\n' "$tail_ev" | grep -q '^EV POPUP_DONE' \
        || js_fail "$phase: a press outside the popup chain (over T) did not dismiss it: $(printf '%s' "$tail_ev" | tr '\n' ' ')"
    # T may gain focus once the grab is gone, never before the dismiss.
    printf '%s\n' "$tail_ev" | awk '/^EV POPUP_DONE/{exit 1} /^EV ENTER T$/{exit 2}'
    [ $? -eq 2 ] \
        && js_fail "$phase: pointer focus entered T while the popup grab was active: $(printf '%s' "$tail_ev" | tr '\n' ' ')"
    js_pass "$phase: press over the sibling toplevel dismissed the chain, T got no grabbed input"
    kill_probe
}

run_phase control
run_phase nested
echo "PASS: layer-popup grab scope holds for nested submenus"

#!/bin/bash
# s124-tier3s-app.sh — GUEST driver (root) for phase7-tier3s-app.bats.
# Phase B (ΔB7/ΔB8): the two shipped GUI workloads render and accept input
# through the waypipe bridge:
#   - weston-terminal and foot each map a toplevel that qdshell logs as
#     "[tier3s] toplevel observed silo=<silo> secctx=qdistro.tier3s.<silo>
#     color=<#hex> handle=<N>" and that the compositor maps (a `qdwin:
#     mapped handle=N` line proves a frame was committed through the bridge);
#   - title evidence: the window title carries waypipe's "[3s:<silo>] "
#     prefix (compositor toplevel_added/toplevel_title lines);
#   - input evidence: tier3focus injectFocus lands keyboard focus on the
#     silo's toplevel, then ydotool-typed keys reach the sandboxed shell —
#     the typed command creates a marker file inside the CONTAINER's /tmp
#     (checked via `podman exec`, so the observability is the sandbox's own
#     filesystem, not the host's);
#   - teardown removes the toplevel (compositor toplevel_removed + qdshell
#     model drop).
# Runs after tier3s-guest-setup.sh --gui. One PASS/FAIL line per check;
# `[s124] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s124
. "$(dirname "$0")/tier3s-guest-lib.sh"
# wait_for runs some checks in a child `bash -c`; it needs comp_log.
export -f comp_log
SW=s124w; SF=s124f
ACT_W="qdistro.tier3s.spawn:weston-terminal/weston-terminal"
ACT_F="qdistro.tier3s.spawn:foot/foot"

step "0. preconditions"
is "probe PASS (admin substrate)" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0
is "weston-terminal image staged in admin's store" "$(yes_no pm image exists localhost/qdistro/tier3s-weston-terminal:latest)" yes
is "foot image staged in admin's store" "$(yes_no pm image exists localhost/qdistro/tier3s-foot:latest)" yes
is "admin compositor socket present" "$(yes_no test -S $ADMIN_RT/$GUI_DISPLAY)" yes
is "qdshell is up" "$(as_admin systemctl --user is-active qdshell.service 2>/dev/null)" active
t3s_guard_idle_locker
is "ydotoold socket present (input injection path)" "$(yes_no test -S /run/user/1000/ydotool.sock)" yes
is "profile is dev" "$(sed -n 's/^QDISTRO_PROFILE=//p' /etc/qdistro/profile | tail -1)" dev
assert_all_clear pre
sm CreateTier3sSilo ssss "$SW" weston-terminal "$SW" none > /dev/null
is "CreateTier3sSilo $SW" "$(silo_state "$SW")" Created
sm CreateTier3sSilo ssss "$SF" foot "$SF" none > /dev/null
is "CreateTier3sSilo $SF" "$(silo_state "$SF")" Created
set_rules "allow:$ACT_W" "allow:$ACT_F"
is "broker answers allow (weston-terminal spawn)" "$(broker_check "$ACT_W")" allow
is "broker answers allow (foot spawn)" "$(broker_check "$ACT_F")" allow

# Journal cursor for this run: the mapped/seat_focus/title count greps below
# are scoped to lines written AFTER this point — a preserved VM's prior-run
# journal holds identical handle/silo lines that would double-count.
J0=$(journal_cursor)

# qdwin_mapped <handle> — the compositor processed a real frame commit for
# this toplevel (the `qdwin: mapped handle=N` line is emitted while it
# processes the client's first buffer).
qdwin_mapped() { comp_log "$J0" | grep -q "mapped handle=$1"; }

# container_has_shell <silo> <ctr>: a shell other than this probe's own
# podman-exec sh (and its children) runs inside, i.e. the terminal's.
container_has_shell() {
    pm_s "$1" exec "$2" sh -c '
        self=$$
        for d in /proc/[0-9]*; do
            p=${d#/proc/}
            [ "$p" = "$self" ] && continue
            pp=$(sed -n "s/^PPid:[[:space:]]*//p" "$d/status" 2>/dev/null)
            [ "$pp" = "$self" ] && continue
            c=$(cat "$d/comm" 2>/dev/null) || continue
            case "$c" in sh|bash|dash) echo "$p"; exit 0 ;; esac
        done
        exit 1' >/dev/null
}

# drive_gui <silo> <tag>: focus + type a marker command, then prove the
# marker exists INSIDE the container.
drive_gui() {
    local s="$1" tag="$2" h pid ctr
    h=$(t3s_window_handle "$s" "$J0")
    [ -n "$h" ] || { fail "$tag: no qdshell handle for $s"; return; }
    is "$tag: findSiloHandle resolves the tier3s toplevel" \
        "$(qs_ipc tier3focus findSiloHandle "$s" 2>/dev/null)" "HANDLE=$h"
    # the toplevel may already hold a focus event from map-time; the proof
    # is a NEW seat_focus_changed after the inject, not exactly one total.
    local fbefore
    fbefore=$(comp_log "$J0" | grep -c "seat_focus_changed seat=default handle=$h")
    is "$tag: injectFocus accepted for the tier3s handle" \
        "$(qs_ipc tier3focus injectFocus "$h" default 2>/dev/null)" "ok handle=$h seat=default"
    wait_for 20 bash -c "[ \$(journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service --no-pager -o cat --after-cursor='$J0' | grep -c 'seat_focus_changed seat=default handle=$h') -gt ${fbefore:-0} ]"
    is "$tag: compositor reports focus on handle $h" \
        "$(yes_no test "$(( $(comp_log "$J0" | grep -c "seat_focus_changed seat=default handle=$h") - ${fbefore:-0} ))" -ge 1)" yes
    wait_for 15 qdwin_mapped "$h"
    is "$tag: compositor mapped a committed frame (mapped handle=$h)" \
        "$(comp_log "$J0" | grep -c "mapped handle=$h")" 1
    # the typed command runs in the sandboxed shell; the marker it creates
    # lives in the container's /tmp tmpfs — `podman exec` reads the sandbox's
    # own filesystem, so this is end-to-end input through the bridge.
    ctr=$(ctr_of "$s")
    # The terminal's shell must exist before keys can reach it; under load
    # the toplevel can map before its shell child has started.
    wait_for 30 container_has_shell "$s" "$ctr" \
        || fail "$tag: no shell process inside the container for the typed command"
    # Up to three focus+type attempts; each clears the line first (Ctrl-U)
    # so a partial earlier attempt cannot corrupt the command. The proof is
    # still the marker the typed command creates inside the container.
    local attempt typed=no
    for attempt in 1 2 3; do
        if [ "$attempt" -gt 1 ]; then
            info "$tag: attempt $((attempt - 1)) left no marker; refocusing handle $h"
            qs_ipc tier3focus injectFocus "$h" default >/dev/null 2>&1
            sleep 1
        fi
        as_admin env YDOTOOL_SOCKET=/run/user/1000/ydotool.sock \
            ydotool key 29:1 22:1 22:0 29:0 || fail "$tag: ydotool ctrl-u failed"
        as_admin env YDOTOOL_SOCKET=/run/user/1000/ydotool.sock \
            ydotool type "touch /tmp/s124-$s-typed" || fail "$tag: ydotool type failed"
        as_admin env YDOTOOL_SOCKET=/run/user/1000/ydotool.sock \
            ydotool key 28:1 28:0 || fail "$tag: ydotool enter failed"
        if wait_for 15 pm_s "$s" exec "$ctr" test -f "/tmp/s124-$s-typed"; then
            typed=yes; break
        fi
    done
    if [ "$typed" = yes ] && pm_s "$s" exec "$ctr" test -f "/tmp/s124-$s-typed" 2>/dev/null; then
        pass "$tag: typed command created /tmp/s124-$s-typed INSIDE the sandbox"
        [ "$attempt" -eq 1 ] || info "$tag: marker appeared on attempt $attempt"
    else
        fail "$tag: typed input never reached the sandboxed shell (no marker in the container)"
        info "$tag: last focus events: $(comp_log "$J0" | grep -E 'seat_focus_changed|set_keyboard_focus' | tail -4 | tr '\n' ' ')"
        info "$tag: container processes: $(pm_s "$s" exec "$ctr" sh -c 'cat /proc/[0-9]*/comm 2>/dev/null' | sort | uniq -c | tr '\n' ' ')"
        info "$tag: ydotoold: $(systemctl is-active ydotoold.service 2>/dev/null; ls -l /run/user/1000/ydotool.sock 2>&1)"
    fi
}

step "1. weston-terminal renders through the bridge"
TW=$(up_gui_silo "$SW")
[ -n "$TW" ] && pass "$SW launch up (token $TW)" || fail "$SW did not come up"
# the surface's own app_id is the app's (weston-terminal); the launch's
# marking lands on the title via waypipe's --title-prefix "[3s:<silo>] " —
# a separate toplevel_title event ~60ms after toplevel_added (title=""),
# so wait for it rather than racing the journal.
wait_for 30 bash -c "comp_log \"\$1\" | grep -q 'toplevel_\(added\|title\) .*title=\"\[3s:$SW\] '" _ "$J0"
is "weston toplevel carries the [3s:$SW] title prefix" \
    "$(comp_log "$J0" | grep -c "toplevel_\(added\|title\) .*title=\"\[3s:$SW\] ")" 1
drive_gui "$SW" "weston"

step "2. foot renders through the bridge"
TF=$(up_gui_silo "$SF")
[ -n "$TF" ] && pass "$SF launch up (token $TF)" || fail "$SF did not come up"
wait_for 30 bash -c "comp_log \"\$1\" | grep -q 'toplevel_\(added\|title\) .*title=\"\[3s:$SF\] '" _ "$J0"
is "foot toplevel carries the [3s:$SF] title prefix" \
    "$(comp_log "$J0" | grep -c "toplevel_\(added\|title\) .*title=\"\[3s:$SF\] ")" 1
drive_gui "$SF" "foot"

step "3. per-workload seccomp profile exercised inside each running container"
# CONTRACT §7/ΔA4: each workload carries its OWN rendered profile file;
# the decisions reachable with the in-image toolset (bash + coreutils) are
# exercised live. fchmodat2 is the interesting one: runsc's converter drops
# the name so an ALLOW would be inert — the profile denies it and the
# `chmod -h` path must EPERM while plain chmod (fchmodat) works.
seccomp_probe() {
    # $1=silo tag $2=container $3=profile-file-name (model A: the container
    # lives in qt3s-<silo>'s store — every podman call goes through pm_s)
    local s="$1" ctr="$2" prof="$3" out
    # podman inlines the parsed profile into the OCI spec — no seccomp
    # annotation exists. The launch argv's --security-opt element (from
    # .Config.CreateCommand) names the per-workload file; the EPERM
    # exercises below prove a filter actually applies its decisions.
    out=$(pm_s "$s" inspect "$ctr" --format '{{json .Config.CreateCommand}}' 2>/dev/null \
        | grep -o 'seccomp=[^,"]*' | head -1)
    is "$s: launch argv names the $prof profile" "${out##*/}" "$prof"
    out=$(pm_s "$s" exec "$ctr" sh -c 'f=/tmp/t3s-sc-p-$$; : > "$f"; chmod 600 "$f" && printf "chmod_rc=0 mode=%s\n" "$(stat -c %a "$f")" || printf "chmod_rc=%s\n" "$?"' 2>&1)
    is "$s: fchmodat ALLOW effective (plain chmod)" "$out" "chmod_rc=0 mode=600"
    out=$(pm_s "$s" exec "$ctr" sh -c 'f=/tmp/t3s-sc-h-$$; : > "$f"; chmod 600 "$f"; chmod -h 700 "$f" 2>/tmp/t3s-sc-e1-$$; printf "nofollow_rc=%s eperm=%s mode=%s\n" "$?" "$(grep -c "Operation not permitted" /tmp/t3s-sc-e1-$$)" "$(stat -c %a "$f")"' 2>&1)
    is "$s: fchmodat2 path (chmod -h) EPERM, mode unchanged" "$out" "nofollow_rc=1 eperm=1 mode=600"
    out=$(pm_s "$s" exec "$ctr" sh -c 'f=/tmp/t3s-sc-l-$$; : > "$f"; ln "$f" /tmp/t3s-sc-ln-$$ 2>/tmp/t3s-sc-e2-$$; printf "ln_rc=%s eperm=%s\n" "$?" "$(grep -c "Operation not permitted" /tmp/t3s-sc-e2-$$)"' 2>&1)
    is "$s: link/linkat DENY effective" "$out" "ln_rc=1 eperm=1"
    out=$(pm_s "$s" exec "$ctr" sh -c 'f=/tmp/t3s-sc-x-$$; : > "$f"; ls -l "$f" 2>/tmp/t3s-sc-e-$$ >/dev/null; printf "ls_rc=%s stderr_bytes=%s\n" "$?" "$(wc -c < /tmp/t3s-sc-e-$$)"' 2>&1)
    is "$s: llistxattr ALLOW effective (ls -l clean)" "$out" "ls_rc=0 stderr_bytes=0"
    out=$(pm_s "$s" exec "$ctr" sh -c 'printf "nnp=%s seccomp=%s\n" "$(grep "^NoNewPrivs:" /proc/self/status | cut -f2)" "$(grep "^Seccomp:" /proc/self/status | cut -f2)"' 2>&1)
    is "$s: NoNewPrivs + seccomp filter mode inside" "$out" "nnp=1 seccomp=2"
}
seccomp_probe "$SW" "$(ctr_of "$SW")" weston-terminal.json
seccomp_probe "$SF" "$(ctr_of "$SF")" foot.json

step "4. teardown removes both toplevels"
HW=$(t3s_window_handle "$SW"); HF=$(t3s_window_handle "$SF")
cur=$(journal_cursor)
for s in "$SW" "$SF"; do sm StopSilo si "$s" 10 > /dev/null; is "StopSilo $s" "$(silo_state "$s")" Stopped; done
for s in "$SW" "$SF"; do
    u=$(unit_of "$s"); t=$TW; h=$HW; [ "$s" = "$SF" ] && { t=$TF; h=$HF; }
    wait_for 90 unit_down "$u"
    assert_launch_gone "teardown/$s" "$t" "$s"
    assert_bridge_gone "teardown/$s" "$t"
    wait_for 30 bash -c "journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service --no-pager -o cat --after-cursor='$cur' | grep -q 'toplevel_removed handle=$h'"
    is "$s: compositor logged toplevel_removed for handle $h" \
        "$(comp_log "$cur" | grep -c "toplevel_removed handle=$h")" "1"
done
for s in "$SW" "$SF"; do sm DeleteSilo s "$s" > /dev/null; is "DeleteSilo $s" "$(silo_state "$s")" absent; done
set_rules none
assert_all_clear end
finish

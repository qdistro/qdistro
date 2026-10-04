#!/bin/bash
# s123-tier3s-waypipe.sh — GUEST driver (root) for phase7-tier3s-waypipe.bats.
# Phase B (ΔB7) bridge topology, tier3s/CONTRACT.md §5 step 12:
#   - a GUI=1 launch stands up the host waypipe bridge pair in the LAUNCH
#     UNIT's cgroup (spawn -> runuser(uid0->admin) -> qdistro-secctx-exec ->
#     waypipe client), distinct from the sandbox scope;
#   - the control record carries gui=1, the launch-record path and both
#     bridge pids+starttimes; the launch record file itself is consumed
#     (removed) right after RegisterLaunch;
#   - the bridge client runs secctx-tagged (engine qdistro.tier3s, app-id
#     qdistro.tier3s.<silo>, instance = launch token) as uid 1000 and binds
#     $LAUNCHES/<token>/link.sock, bind-mounted into the sandbox at
#     /run/qdistro/link; the container runs under the runsc wrapper with
#     --runtime-flag=host-uds=open;
#   - RegisterLaunch writes a qdistro.lineage.register:<silo> audit row.
# Runs after tier3s-guest-setup.sh --gui. One PASS/FAIL line per check;
# `[s123] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s123
. "$(dirname "$0")/tier3s-guest-lib.sh"
SILO=s123a
APPID="qdistro.tier3s.$SILO"
GUISPAWN="qdistro.tier3s.spawn:weston-terminal/weston-terminal"

step "0. preconditions"
is "probe PASS" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0
is "weston-terminal image loaded" "$(yes_no pm image exists localhost/qdistro/tier3s-weston-terminal:latest)" yes
is "admin compositor socket present" "$(yes_no test -S $ADMIN_RT/$GUI_DISPLAY)" yes
is "qdshell is up" "$(as_admin systemctl --user is-active qdshell.service 2>/dev/null)" active
is "profile is dev" "$(sed -n 's/^QDISTRO_PROFILE=//p' /etc/qdistro/profile | tail -1)" dev
assert_all_clear pre
sm CreateTier3sSilo ssss "$SILO" weston-terminal "$SILO" none > /dev/null
is "CreateTier3sSilo $SILO" "$(silo_state "$SILO")" Created
set_rules "allow:$GUISPAWN"
is "broker answers allow for the GUI spawn" "$(broker_check "$GUISPAWN")" allow

step "1. GUI launch brings up the bridge pair"
reg_before=$(audit_count "qdistro.lineage.register:$SILO")
cur=$(journal_cursor)
# link.sock exists only between the waypipe client's bind() (step 11b,
# before podman run) and the sandbox waypipe server's connect (waypipe -o
# unlinks at accept — single-attach). Capture its mode+owner in that
# window; post-attach evidence is the fd/netns topology below.
: > "$WORK/link-sock-stat"
( end=$((SECONDS + 120)); while [ "$SECONDS" -lt "$end" ]; do
      for f in "$LAUNCHES"/*/link.sock; do
          [ -e "$f" ] || continue
          stat -c '%a:%u' "$f" > "$WORK/link-sock-stat" 2>/dev/null
          exit 0
      done
      sleep 0.02
  done ) &
SOCKPOLL=$!
TOK=$(up_gui_silo "$SILO")
if [ -n "$TOK" ]; then pass "launch up (token $TOK)"; else fail "launch did not come up"; fi
UNIT=$(unit_of "$SILO"); CTR=$(ctr_of "$SILO")
unit_log "$UNIT" "$cur" | grep -v pam_unix | sed 's/^/    unit: /'

step "2. control record carries the bridge identity"
is "record: gui flag" "$(rec "$TOK" gui)" 1
is "record: phase" "$(rec "$TOK" phase)" running
BP=$(rec "$TOK" bridge_client_pid); BS=$(rec "$TOK" bridge_client_starttime)
WP=$(rec "$TOK" bridge_wrapper_pid); WS=$(rec "$TOK" bridge_wrapper_starttime)
[ -n "$BP" ] && [ -n "$BS" ] && [ -n "$WP" ] && [ -n "$WS" ] \
    && pass "record: bridge client+wrapper pid/starttime recorded ($BP/$WP)" \
    || fail "record missing bridge pids: client=$BP wrapper=$WP"
LR=$(rec "$TOK" launch_record)
case "$LR" in
    /run/user/1000/qdistro-tier3s-launchrec-????????????????????????????????.pid)
        pass "record: launch_record path under admin runtime ($LR)" ;;
    *)  fail "record: unexpected launch_record path '$LR'" ;;
esac
assert_gui_bridge_up "bridge" "$TOK"

step "3. bridge client is the secctx-tagged waypipe client"
# argv: waypipe -s <LAUNCH_DIR>/link.sock -o --no-gpu --title-prefix "[3s:<silo>] " client
is "bridge client argv" "$(tr '\0' ' ' < "/proc/$BP/cmdline" 2>/dev/null | sed 's/ *$//')" \
    "waypipe -s $LAUNCHES/$TOK/link.sock -o --no-gpu --title-prefix [3s:$SILO]  client"
listener=$(cat "$WORK/$TOK.listener" 2>/dev/null)
case "$listener" in
    wayland-secctx-*)
        pass "bridge client connects through secctx listener $listener" ;;
    *)  fail "no wayland-secctx-* listener in the bridge client's environ (got '$listener')" ;;
esac
# runuser may exec its payload (comm becomes qdistro-secctx-e) or fork it —
# either way the wrapper pid is uid 0 and an ancestor of the admin waypipe.
anc_ok=no
ancp=$BP
for _ in 1 2 3 4 5; do
    s=""; { read -r s < "/proc/$ancp/stat"; } 2>/dev/null || break
    s="${s##*) }"; set -- $s; ancp="$2"    # state ppid ...
    [ "$ancp" = "$WP" ] && { anc_ok=yes; break; }
    [ "${ancp:-0}" -le 1 ] && break
done
is "bridge wrapper is root and the waypipe client's ancestor" \
    "$(stat -c %u "/proc/$WP" 2>/dev/null):$anc_ok" "0:yes"

step "4. the sandbox half: container, runsc runtime, bridge mount"
is "container runs under the token label" \
    "$(ctr_status "$CTR"):$(pm inspect --format '{{index .Config.Labels "qdistro_tier3s_token"}}' "$CTR" 2>/dev/null)" \
    "running:$TOK"
is "container OCIRuntime is the tier3s wrapper" \
    "$(pm inspect --format '{{.OCIRuntime}}' "$CTR" 2>/dev/null)" /usr/libexec/qdistro/tier3s-runsc
is "container carries the launch-dir bind mount" \
    "$(pm inspect --format '{{range .Mounts}}{{if eq .Destination "/run/qdistro/link"}}{{.Source}}:{{.Destination}}{{end}}{{end}}' "$CTR" 2>/dev/null)" \
    "$LAUNCHES/$TOK:/run/qdistro/link"
is "bridge mount is READ-ONLY in the sandbox view (P2-1: a hostile guest must not fill host /run)" \
    "$(pm inspect --format '{{range .Mounts}}{{if eq .Destination "/run/qdistro/link"}}{{.RW}}{{end}}{{end}}' "$CTR" 2>/dev/null)" \
    "false"
is "sandbox write to the bridge dir is refused" \
    "$(pm exec "$CTR" sh -c 'touch /run/qdistro/link/.w 2>/dev/null; printf "rc=%s" "$?"' 2>/dev/null)" "rc=1"
wait "$SOCKPOLL" 2>/dev/null || true
is "host link.sock was admin-owned 0600 at bind (umask 0177 wrap, pre-attach)" \
    "$(cat "$WORK/link-sock-stat" 2>/dev/null)" "600:1000"
is "launch dir is admin-owned 0700" \
    "$(stat -c '%a:%u' "$LAUNCHES/$TOK" 2>/dev/null)" "700:1000"
is "container NetworkMode is none" \
    "$(pm inspect --format '{{.HostConfig.NetworkMode}}' "$CTR" 2>/dev/null)" none
# host-uds=open evidence: the accepted channel's host end lives in the
# runsc GOFER's netns (gVisor moves the accepted socket off the host
# netns; a host-namespace ss does NOT see it). `nsenter -t <gofer> -n
# ss` shows the ESTABLISHED unix stream still NAMED link.sock — that is
# the passthrough: the sandbox's waypipe server could only connect to a
# host unix socket because runsc's host-uds flag exposed it.
GP=$(gofer_pid_of "$TOK")
is "runsc gofer resolved" "$(yes_no test -n "$GP")" yes
is "link.sock channel is an ESTABLISHED host unix socket through the gofer netns (host-uds=open)" \
    "$(nsenter -t "$GP" -n ss -xpH 2>/dev/null | grep -c 'ESTAB.*link\.sock\|link\.sock.*ESTAB' | awk '{print ($1>=1)?1:0}')" 1
# the recorded podman create argv is the exact launch argv: assert the
# security-critical entries literally, not by pattern family.
CC=$(pm inspect --format '{{json .Config.CreateCommand}}' "$CTR" 2>/dev/null)
is "create argv pins --runtime-flag=host-uds=open" \
    "$(printf '%s' "$CC" | grep -c 'host-uds=open')" 1
is "create argv pins --network=none" \
    "$(printf '%s' "$CC" | grep -c -- '"--network=none"')" 1
is "create argv pins --cap-drop=ALL and no-new-privileges" \
    "$(printf '%s' "$CC" | grep -c '"--cap-drop=ALL"')+$(printf '%s' "$CC" | grep -c 'no-new-privileges')" "1+1"
is "create argv pins the weston-terminal seccomp profile" \
    "$(printf '%s' "$CC" | grep -c 'seccomp=/usr/lib/qdistro/tier3s/seccomp/weston-terminal.json')" 1
is "create argv mounts ONLY the launch dir under /run/qdistro (no whole-/run or host-root bind)" \
    "$(printf '%s' "$CC" | grep -oE '"-v","[^"]+:[^"]+"' | grep -vc "$LAUNCHES/$TOK:/run/qdistro/link")" 0
is "create argv: no --privileged, no root user, no host userns" \
    "$(printf '%s' "$CC" | grep -cE '"--privileged"|--userns=host|"--user","?0')+$(printf '%s' "$CC" | grep -c '"--userns=keep-id"')" "0+1"
# the COMPLETE mount set: every host bind whose source lives outside the
# launch dir or podman's own per-container userdata dir (hosts/resolv/
# .containerenv) is a leak — log the full list, assert none.
MTS=$(pm inspect --format '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}:{{.Destination}}:{{.RW}} {{end}}{{end}}' "$CTR" 2>/dev/null)
info "container mounts: $MTS"
is "no host bind outside the launch dir and podman-internal userdata" \
    "$(printf '%s\n' $MTS | sed 's/:.*//' | grep -v "^$LAUNCHES/$TOK$" | grep -vc '^/run/containers/')" 0
is "sentry pid is the runsc bundle" \
    "$(readlink "/proc/$(rec "$TOK" sentry_pid)/exe" 2>/dev/null | grep -c '^/usr/libexec/qdistro/runsc/')" 1

step "5. RegisterLaunch audit row + consumed launch record"
reg_after=$(audit_count "qdistro.lineage.register:$SILO")
if [ "$reg_before" != QUERY-FAILED ] && [ "$reg_after" != QUERY-FAILED ] && [ "$reg_after" -gt "$reg_before" ]; then
    pass "broker wrote qdistro.lineage.register:$SILO (before=$reg_before after=$reg_after)"
else fail "no new qdistro.lineage.register:$SILO audit row (before=$reg_before after=$reg_after)"; fi
is "register row names engine+app" \
    "$(audit_last_source "qdistro.lineage.register:$SILO" | grep -c "engine='qdistro.tier3s' app='qdistro.tier3s.$SILO'")" 1
is "register row records the bridge client pid" \
    "$(audit_last_source "qdistro.lineage.register:$SILO" | grep -c "pid=$BP ")" 1
is "launch record consumed after RegisterLaunch" "$(yes_no test -e "$LR")" no
# waypipe -o unlinks link.sock at accept (single-attach): once the bridge is
# live the per-launch dir must be EMPTY — any other file there is a leftover.
is "launch dir empty once the bridge attached" "$(find "$LAUNCHES/$TOK" -mindepth 1 | grep -c .)" 0
# Single-attach is enforced by UNLINK, not by refusing a second connect on
# a live listener: prove the mechanism — the pathname is gone while the
# client's -o listener fd still holds a LISTEN socket (unlinked sockets
# stay visible in /proc/net/unix by path), and a fresh connect fails with
# ENOENT. A connect failure WITHOUT the live listener would mean the
# bridge died, not single-attach.
is "single-attach: the consumed link.sock listener fd still listens (unlinked, not closed)" \
    "$(ss -xlH 2>/dev/null | grep -c 'link\.sock')" 1
is "single-attach: a second connect fails — the pathname was consumed at accept" \
    "$(python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])' "$LAUNCHES/$TOK/link.sock" 2>&1 | grep -c 'No such file')" 1

step "6. qdshell observed the tagged toplevel"
# every count grep is scoped to $cur (captured just before the launch):
# on a preserved VM the boot journal still holds a prior run's identical
# silo/title lines, which would double-count.
is "qdshell: [tier3s] toplevel observed" \
    "$(qdshell_log "$cur" | grep -c "\[tier3s\] toplevel observed silo=$SILO secctx=$APPID color=#...... handle=[0-9]")" 1
is "qdshell: colour line" \
    "$(qdshell_log "$cur" | grep -c "\[tier3s\] silo=$SILO color=#......")" 1
# toplevel_added's app_id is the surface's own (the tagged identity lands on
# toplevel_security_context below); pid= is the waypipe bridge client's.
is "compositor: toplevel_added names the bridge client pid" \
    "$(comp_log "$cur" | grep -c "toplevel_added handle=[0-9]* uid=1000 pid=$BP ")" 1
# waypipe's --title-prefix marks the forwarded windows visibly
is "compositor: a toplevel carries the [3s:$SILO] title prefix" \
    "$(comp_log "$cur" | grep -c "toplevel_\(added\|title\) .*title=\"\[3s:$SILO\] ")" 1
is "compositor: toplevel_security_context carries the launch token as instance" \
    "$(comp_log "$cur" | grep -c "toplevel_security_context handle=[0-9]* engine=qdistro.tier3s app_id=$APPID instance=$TOK")" 1
is "compositor: peer identity names the live bridge client" \
    "$(comp_log "$cur" | grep -c "toplevel_peer_identity handle=[0-9]* pid=$BP starttime=$BS uid=1000")" 1

step "7. teardown reaps the bridge too"
snapshot_bridge "$TOK"
sm StopSilo si "$SILO" 10 > /dev/null; is "StopSilo $SILO" "$(silo_state "$SILO")" Stopped
wait_for 90 unit_down "$UNIT"
assert_launch_gone teardown "$TOK" "$CTR"
assert_bridge_gone teardown "$TOK"
sm DeleteSilo s "$SILO" > /dev/null; is "DeleteSilo $SILO" "$(silo_state "$SILO")" absent
set_rules none
assert_all_clear end
finish

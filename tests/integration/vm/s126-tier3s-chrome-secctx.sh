#!/bin/bash
# s126-tier3s-chrome-secctx.sh — GUEST driver (root) for
# phase7-tier3s-chrome-secctx.bats. Phase B (ΔB7-ΔB9) compositor identity:
#   - the sandbox's windows reach qdwin tagged with the secctx triple
#     (engine qdistro.tier3s, app_id qdistro.tier3s.<silo>, instance = the
#     launch token) — the compositor's own toplevel_security_context and
#     toplevel_peer_identity journal lines name the REAL tagged peer
#     (pid/starttime/uid of the bridge client, not a claimed string);
#   - qdshell derives the silo from secctx (Tier3sApps), paints the
#     deterministic palette colour, and Tier3FocusIPC scopes injectFocus to
#     tier-3/tier3s/tier-4 handles only — a bogus handle is rejected;
#   - per-interface: the same secctx tag that marks the bridge client also
#     gates wl_registry visibility — a pywayland probe run under the same
#     qdistro-secctx-exec wrap sees NONE of the privileged globals
#     (qdwin_shell_v1, zwlr_layer_shell_v1, qdwin_nested_manager_v1,
#     qdwin_locker_v1, wp_security_context_manager_v1, virtual keyboard /
#     input-method) that an untagged admin client sees;
#   - the RegisterLaunch record binds (pid,starttime) -> the silo/engine/
#     app/instance the launch claimed; an audit row records it.
# Runs after tier3s-guest-setup.sh --gui. One PASS/FAIL line per check;
# `[s126] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s126
. "$(dirname "$0")/tier3s-guest-lib.sh"
SILO=s126a
APPID="qdistro.tier3s.$SILO"
GUISPAWN="qdistro.tier3s.spawn:weston-terminal/weston-terminal"

step "0. preconditions"
is "probe PASS" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0
is "weston-terminal image loaded" "$(yes_no pm image exists localhost/qdistro/tier3s-weston-terminal:latest)" yes
is "admin compositor socket present" "$(yes_no test -S $ADMIN_RT/$GUI_DISPLAY)" yes
is "qdshell is up" "$(as_admin systemctl --user is-active qdshell.service 2>/dev/null)" active
is "pywayland available for admin" \
    "$(yes_no as_admin python3 -c 'import pywayland.client' 2>/dev/null)" yes
is "profile is dev" "$(sed -n 's/^QDISTRO_PROFILE=//p' /etc/qdistro/profile | tail -1)" dev
assert_all_clear pre
sm CreateTier3sSilo ssss "$SILO" weston-terminal "$SILO" none > /dev/null
is "CreateTier3sSilo $SILO" "$(silo_state "$SILO")" Created
set_rules "allow:$GUISPAWN"
is "broker answers allow for the GUI spawn" "$(broker_check "$GUISPAWN")" allow

step "1. live GUI launch; the compositor names the tagged peer"
TOK=$(up_gui_silo "$SILO")
[ -n "$TOK" ] && pass "launch up ($TOK)" || fail "launch did not come up"
BP=$(rec "$TOK" bridge_client_pid); BS=$(rec "$TOK" bridge_client_starttime)
[ -n "$BP" ] && [ -n "$BS" ] || fail "no bridge client identity recorded"
h=$(t3s_window_handle "$SILO")
is "qdshell observed the toplevel (handle $h)" "$(yes_no test -n "$h")" yes
is "compositor: toplevel_added names uid 1000 + the bridge client pid" \
    "$(comp_log | grep -c "toplevel_added handle=$h uid=1000 pid=$BP ")" 1
is "compositor: toplevel_security_context carries engine/app_id/instance=token" \
    "$(comp_log | grep -c "toplevel_security_context handle=$h engine=qdistro.tier3s app_id=$APPID instance=$TOK")" 1
is "compositor: toplevel_peer_identity names pid+starttime+uid of the real bridge client" \
    "$(comp_log | grep -c "toplevel_peer_identity handle=$h pid=$BP starttime=$BS uid=1000")" 1

step "2. qdshell chrome: secctx-derived silo + deterministic colour"
want_color=$(python3 - "$SILO" <<'PY'
import sys
palette = ["#4caf50","#ffb300","#2196f3","#ab47bc","#26c6da",
           "#8bc34a","#ffe54c","#64b5f6","#ce93d8","#80deea"]
h = 0
for ch in sys.argv[1]:
    h = (h * 31 + ord(ch)) & 0xffffffff
print(palette[h % len(palette)])
PY
)
is "qdshell logged the deterministic palette colour for $SILO" \
    "$(qdshell_log | grep -c "\[tier3s\] silo=$SILO color=$want_color")" 1
is "observed line carries secctx app_id, colour and handle" \
    "$(qdshell_log | grep -c "\[tier3s\] toplevel observed silo=$SILO secctx=$APPID color=$want_color handle=$h")" 1
is "Tier3FocusIPC resolves the handle from the silo" \
    "$(qs_ipc tier3focus findSiloHandle "$SILO" 2>/dev/null)" "HANDLE=$h"
is "Tier3FocusIPC rejects a non-tier handle (9999)" \
    "$(qs_ipc tier3focus injectFocus 9999 default 2>/dev/null | head -1)" \
    "error: handle=9999 is not a tier-3 toplevel"

step "3. RegisterLaunch bound the real peer (broker audit evidence)"
is "audit: register row for $SILO" \
    "$(audit_last_source "qdistro.lineage.register:$SILO" | grep -c "pid=$BP starttime=$BS")" 1
is "audit: register row records engine/app/instance claim" \
    "$(audit_last_source "qdistro.lineage.register:$SILO" | grep -c "engine='qdistro.tier3s' app='qdistro.tier3s.$SILO'")" 1
# the broker-side line claims engine+app; the instance binding is proven by
# the record itself (RegisterLaunch was called with instance=$TOK) and by
# the compositor's instance= log above.

step "4. per-interface: the secctx tag hides the privileged globals"
# A pywayland registry lister; run once untagged (admin) and once through
# the SAME wrap the bridge client uses (runuser -> env -i ->
# TRUSTED_LAUNCHER -> qdistro-secctx-exec tagged qdistro.tier3s).
PROBE=/var/tmp/s126-list-globals.py
cat > "$PROBE" <<'PY'
import sys
from pywayland.client import Display
d = Display()
d.connect()
names = []
reg = d.get_registry()
reg.dispatcher["global"] = lambda r, name, interface, version: names.append(interface)
d.roundtrip()
d.disconnect()
print("\n".join(sorted(set(names))))
PY
chmod 0755 "$PROBE"; chown admin:admin "$PROBE" 2>/dev/null || true
as_admin env WAYLAND_DISPLAY=wayland-1 python3 "$PROBE" > "$WORK/globals.plain" 2>"$WORK/globals.plain.err"
rc=$?
is "registry probe as plain admin ran" "$rc" 0
# tagged probe: an independently-launched client with the tier3s engine/app;
# its own instance id — the per-interface filter keys on the secctx tag.
runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin USER=admin \
    LOGNAME=admin XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 \
    qdistro-secctx-exec --sandbox-engine qdistro.tier3s \
        --app-id "qdistro.tier3s.s126probe" --instance-id deadbeefcafe0000 \
        -- python3 "$PROBE" > "$WORK/globals.tagged" 2>"$WORK/globals.tagged.err"
rc=$?
if [ "$rc" -ne 0 ]; then sed 's/^/    tagged probe: /' "$WORK/globals.tagged.err"; fi
is "registry probe under the tier3s secctx tag ran" "$rc" 0
# control: ordinary globals visible to BOTH classes
is "control: wl_compositor visible to the tagged client" \
    "$(grep -cx wl_compositor < "$WORK/globals.tagged")" 1
is "control: wl_compositor visible to plain admin" \
    "$(grep -cx wl_compositor < "$WORK/globals.plain")" 1
# the gated set: visible to plain admin (ORDINARY), hidden under secctx —
# includes the input-method/vkbd pair (keystroke capture/injection surfaces)
for g in qdwin_shell_v1 zwlr_layer_shell_v1 qdwin_nested_manager_v1 qdwin_locker_v1 \
         zwp_input_method_manager_v2 zwp_virtual_keyboard_manager_v1; do
    is "plain admin sees $g" "$(grep -cx "$g" < "$WORK/globals.plain")" 1
    is "tier3s-tagged client does NOT see $g" "$(grep -cx "$g" < "$WORK/globals.tagged")" 0
done
# shell-only globals are hidden even from plain admin
for g in wp_security_context_manager_v1 weston_capture_v1; do
    is "plain admin does NOT see $g (shell-only)" "$(grep -cx "$g" < "$WORK/globals.plain")" 0
    is "tier3s-tagged client does NOT see $g" "$(grep -cx "$g" < "$WORK/globals.tagged")" 0
done
# the compositor logged our probe as a tagged peer with the tier3s engine
is "compositor logged the probe's tagged client acceptance" \
    "$(comp_log | grep -c 'qdwin/secctx: client accepted engine=qdistro.tier3s app_id=qdistro.tier3s.s126probe')" 1

step "5. per-interface operation denials (overrides OFF, real bridge path)"
# Registry visibility is not the gate for these two interfaces — both are
# QDWIN_GLOBAL_ORDINARY and deliberately stay enumerable; the denial must
# happen on the OPERATION. Two paths are exercised:
#  (i)  THE BRIDGE PATH — the probes run INSIDE the live sandbox on the
#       waypipe server's display socket: their requests cross the real
#       byte-stream bridge and reach qdwin on the running launch's tagged
#       bridge client channel. This is the same peer whose toplevel the
#       compositor tagged in step 1 — not an extra host-side client.
#  (ii) a host-side tagged peer under the same secctx wrap (unique
#       instance-id per connection — duplicate ids silently degrade the
#       tagged channel): supplementary tag-policy evidence.
# Overrides-off evidence is printed, not assumed: the running compositor
# carries no --qdwin-allowed-uid grant, and the mutation gate denies
# secctx clients outright (before any uid fallback). The denials below —
# IMPLEMENTATION errors, not transport failures — are the proof.
sctx_tagged() {   # sctx_tagged <iid-suffix> <cmd...> — stdout only (secctx-exec logs its wrap on stderr)
    local iid="$1"; shift
    runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin USER=admin \
        LOGNAME=admin XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
        QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1 \
        qdistro-secctx-exec --sandbox-engine qdistro.tier3s \
            --app-id "qdistro.tier3s.s126probe" --instance-id "$iid" -- "$@" \
            2>>"$WORK/sctx_tagged.err"
}
is "running compositor carries NO --qdwin-allowed-uid authorization override" \
    "$(tr '\0' '\n' < /proc/"$(comp_pid)"/cmdline 2>/dev/null | grep -c 'qdwin-allowed-uid')" 0

# --- (i) through the live bridge -----------------------------------------
CTR=$(ctr_of "$SILO")
# pm cp is not relied on here (runsc mount semantics): stream the probe
# binaries in over `exec -i` — they link only libwayland-client, already in
# the image.
pm exec -i "$CTR" sh -c 'cat > /tmp/qp; chmod 755 /tmp/qp' \
        < /usr/bin/qdwin-output-probe \
    && pm exec -i "$CTR" sh -c 'cat > /tmp/sclaim; chmod 755 /tmp/sclaim' \
        < /usr/bin/qdistro-test-stream-claim-probe \
    && pass "probes staged inside the live sandbox" \
    || fail "probe staging into $CTR failed"
INWL=$(pm exec "$CTR" sh -c 'for s in /run/user/1000/wayland-*; do [ -S "$s" ] && basename "$s"; done' 2>/dev/null | head -1 | tr -d '[:space:]')
is "in-sandbox waypipe display socket found" "$(yes_no test -n "$INWL")" yes
bridge_probe() {  # bridge_probe <in-ctr-cmd...> — runs inside the sandbox on the waypipe display
    pm exec "$CTR" env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY="$INWL" "$@" 2>&1
}
# (a) zwlr_output_manager_v1 via the bridge: enumerated but test/apply
# refuse with an IMPLEMENTATION error (the gate's own denial — the probe
# now requires that error class, so EPIPE/truncation can't fake it).
out=$(bridge_probe /tmp/qp --test --expect-denied); rc=$?
is "bridge path: output-manager test refused (implementation error)" "$rc" 0
is "bridge path: test denial carried the implementation-error line" \
    "$(printf '%s' "$out" | grep -c 'denied (implementation error')" 1
out=$(bridge_probe /tmp/qp --apply --expect-denied); rc=$?
is "bridge path: output-manager apply refused (implementation error)" "$rc" 0
is "bridge path: apply denial carried the implementation-error line" \
    "$(printf '%s' "$out" | grep -c 'denied (implementation error')" 1
# (b) qdwin_stream_input_v1 via the bridge: a bogus-token claim must get
# INVALID_TOKEN — the interface's own protocol error, reached across the
# byte stream.
out=$(bridge_probe /tmp/sclaim); rc=$?
is "bridge path: stream-input claim(bogus) -> INVALID_TOKEN" \
    "$rc:$out" "0:[qdistro-test-stream-claim-probe] claim -> invalid_token (as expected)"

# --- (ii) same-tag host-side peer (supplementary) ------------------------
# (a) zwlr_output_manager_v1: enumerated (ORDINARY) but test/apply refuse.
is "tagged peer still enumerates zwlr_output_manager_v1" \
    "$(grep -cx zwlr_output_manager_v1 < "$WORK/globals.tagged")" 1
out=$(sctx_tagged "$TOK-a1" qdwin-output-probe --test --expect-denied); rc=$?
is "tagged peer: output-manager test refused (implementation error)" "$rc" 0
is "tagged peer: test denial carried the implementation-error line" \
    "$(printf '%s' "$out" | grep -c 'denied (implementation error')" 1
out=$(sctx_tagged "$TOK-a2" qdwin-output-probe --apply --expect-denied); rc=$?
is "tagged peer: output-manager apply refused (implementation error)" "$rc" 0
is "tagged peer: apply denial carried the implementation-error line" \
    "$(printf '%s' "$out" | grep -c 'denied (implementation error')" 1
# (b) qdwin_stream_input_v1: enumerated for everyone by design (the token
# in claim() is the gate); a bogus token must get INVALID_TOKEN.
is "tagged peer enumerates qdwin_stream_input_v1 (public by design)" \
    "$(grep -cx qdwin_stream_input_v1 < "$WORK/globals.tagged")" 1
out=$(sctx_tagged "$TOK-b1" qdistro-test-stream-claim-probe); rc=$?
is "tagged peer: stream-input claim(bogus) -> INVALID_TOKEN" \
    "$rc:$out" "0:[qdistro-test-stream-claim-probe] claim -> invalid_token (as expected)"
is "compositor logged INVALID_TOKEN for the tagged claim" \
    "$(yes_no test "$(comp_log | grep -c 'stream_input claim INVALID_TOKEN')" -ge 1)" yes

step "5b. single-attach: a consumed tier3s context refuses a second client"
# ΔB10 binding soundness: the qdshell clipboard gate binds a tagged
# selection source to the focused toplevel by (engine,app_id,instance)
# equality and relays that toplevel client's verified pid — sound only
# when one attested tuple names exactly ONE peer. The compositor enforces
# single-attach on engine qdistro.tier3s: the first client consumes the
# context; later connections on the same listener get a live refusal.
# Probe: hold a tagged client under its own secctx-exec listener, then
# connect to the SAME listener path again — the compositor must refuse.
J5B=$(journal_cursor)
PRE_SECCTX=$(for s in /run/user/1000/wayland-secctx-*; do [ -S "$s" ] && echo "$s"; done)
sctx_tagged "$TOK-hold" qdistro-test-window --title "s126hold" &
HOLD_WRAP=$!
wait_for 15 bash -c 'for s in /run/user/1000/wayland-secctx-*; do [ -S "$s" ] && echo "$s"; done | grep -q .'
SECPATH=$(for s in /run/user/1000/wayland-secctx-*; do [ -S "$s" ] && echo "$s"; done \
    | { [ -n "$PRE_SECCTX" ] && grep -vxF "$PRE_SECCTX" || cat; } | tail -1)
is "secctx listener path for the held tagged client" "$(yes_no test -S "$SECPATH")" yes
wait_for 20 bash -c "comp_log \"\$1\" | grep -q 'client accepted engine=qdistro.tier3s app_id=qdistro.tier3s.s126probe instance_id=$TOK-hold'" _ "$J5B" \
    && pass "compositor tagged the held client (context consumed)" \
    || fail "no client-accepted line for the held client"
# second connect on the SAME consumed listener: accept-and-close gives the
# client a live EOF, and the compositor logs the refusal — never a second
# tagged client for the same tuple.
second=$(python3 - "$SECPATH" <<'PY'
import socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(5)
try:
    s.connect(sys.argv[1])
except OSError as e:
    print(f"connect_failed:{e}"); sys.exit(0)
try:
    data = s.recv(64)
    print("recv_eof" if data == b"" else f"recv_data:{len(data)}")
except socket.timeout:
    print("recv_timeout")
PY
)
is "second connect on the consumed context is refused (live EOF)" \
    "$second" "recv_eof"
is "compositor logged the refused extra connection" \
    "$(comp_log "$J5B" | grep -c 'refused extra connection on consumed context engine=qdistro.tier3s app_id=qdistro.tier3s.s126probe instance_id='"$TOK"'-hold')" 1
is "the held context still has exactly ONE accepted client (no second tag)" \
    "$(comp_log "$J5B" | grep -c 'client accepted engine=qdistro.tier3s app_id=qdistro.tier3s.s126probe instance_id='"$TOK"'-hold')" 1
# same sweep shape as kill_clip_src: TERM the wrapper chain, then orphan-sweep
# so no tagged client or secctx listener lingers into teardown.
kill -TERM "$HOLD_WRAP" 2>/dev/null; wait "$HOLD_WRAP" 2>/dev/null || :
pkill -u admin -f 'qdistro-test-window --title s126hold' 2>/dev/null || :
pkill -u admin -f "secctx-exec .*instance-id ${TOK}-hold" 2>/dev/null || :

step "6. teardown"
sm StopSilo si "$SILO" 10 > /dev/null; is "StopSilo $SILO" "$(silo_state "$SILO")" Stopped
wait_for 90 unit_down "$(unit_of "$SILO")"
assert_launch_gone teardown "$TOK" "$(ctr_of "$SILO")"
assert_bridge_gone teardown "$TOK"
sm DeleteSilo s "$SILO" > /dev/null; is "DeleteSilo $SILO" "$(silo_state "$SILO")" absent
set_rules none
assert_all_clear end
finish

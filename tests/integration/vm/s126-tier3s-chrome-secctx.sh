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

step "5. teardown"
sm StopSilo si "$SILO" 10 > /dev/null; is "StopSilo $SILO" "$(silo_state "$SILO")" Stopped
wait_for 90 unit_down "$(unit_of "$SILO")"
assert_launch_gone teardown "$TOK" "$(ctr_of "$SILO")"
assert_bridge_gone teardown "$TOK"
sm DeleteSilo s "$SILO" > /dev/null; is "DeleteSilo $SILO" "$(silo_state "$SILO")" absent
set_rules none
assert_all_clear end
finish

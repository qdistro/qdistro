# Helpers specific to driving the qdwin+qdshell session. Source this AFTER
# `qdwin/tests/gui/qdwin-helpers.sh`.
#
# Reuses every helper from qdwin-helpers.sh; adds session checks against
# the PRODUCTION deploy unit names (qdwin-compositor.service /
# qdshell.service / qdwin-session.target) so these lanes validate the
# units deploy actually ships. (The legacy noctalia-session /
# noctalia-shell names were retired 2026-06-16 — the dir name stays
# qdwin-noctalia for historical continuity; only the unit names changed.)

# Returns 0 if qdshell.service (admin's user unit) is active and the qs
# process is alive. Uses single-quoted commands to avoid vm-exec's
# JSON-quoting fragility (embedded " in the inner runuser command breaks
# the harness — see memory vm_exec_quoting_fragility).
noct_session_healthy() {
    qdwin_require_vm || return 2
    local out
    out=$("$QDWIN_VM_EXEC" "$VMNAME" \
        "su - admin -c 'systemctl --user is-active qdshell.service' && pgrep -f /usr/bin/qs >/dev/null && echo OK" \
        2>/dev/null | tail -1)
    [ "$out" = "OK" ]
}

# Move cursor + take a screenshot. Wraps qdwin_screenshot with an
# initial cursor wake so DPMS doesn't blank the panel.
noct_screenshot_awake() {
    local out="${1:-/tmp/noct-shot.png}"
    qdwin_mouse_move 800 400
    sleep 0.3
    qdwin_mouse_move 850 450
    sleep 0.5
    qdwin_screenshot "$out"
}

# Count the number of "qdwin: layer-shell mapped" entries in the
# session journal since a given systemd timestamp ("HH:MM:SS" or
# free-form). Useful for asserting the shell mapped the expected
# number of surfaces.
noct_layer_mapped_count_since() {
    local since="${1:-10 minutes ago}"
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "runuser -l admin -c \"journalctl --user -u qdwin-compositor.service --since '$since' --no-pager\" | grep -c 'layer-shell mapped'" \
        2>/dev/null | tail -1
}

# Restart the shell cleanly (used when a scenario knocks it over). Restarts
# qdshell.service; its Requires=qdwin-compositor.service pulls the
# compositor back if it is also down.
noct_restart() {
    "$QDWIN_VM_EXEC" "$VMNAME" \
        'runuser -l admin -c "systemctl --user reset-failed qdshell.service qdwin-compositor.service && systemctl --user restart qdshell.service"' \
        >/dev/null
    sleep 8
}

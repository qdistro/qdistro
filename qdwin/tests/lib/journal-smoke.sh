#!/bin/bash
# journal-smoke.sh — shared plumbing for the deterministic (no-agent) qdwin
# executable smokes that replaced the `qci:visual: none` agent scenarios
# (tests/gui/13-17, 20, 21; qdlocker 02; qdwin-noctalia 04).
#
# Sourced, not executed. The caller sets VMNAME (qci's run_qdwin_executable_
# gui_smokes exports it) and sources this AFTER qdwin-helpers.sh.
#
# Conventions (same as agent-*-smoke.sh):
#   exit 0 = pass, 1 = a product assertion failed, 2 = setup/environment.
#
# Every journal read is CURSOR-scoped and UNIT-scoped (`_SYSTEMD_USER_UNIT=`):
# qemu-ga logs the text of every guest-exec it runs, so an unscoped grep can
# match the test's own command line (memory: qemu-ga-journal-self-match). The
# polls run INSIDE the guest (one vm-exec per wait, not one per poll tick), and
# every guest script travels base64-encoded so vm-exec's JSON quoting never
# sees an embedded quote.

: "${VMNAME:?set VMNAME to the qdwin gui VM}"

js_fail()       { echo "FAIL: $*" >&2; exit 1; }
js_setup_fail() { echo "SETUP: $*" >&2; exit 2; }
js_pass()       { echo "PASS: $*"; }

# js_guest <script> — run a bash script in the guest as root; prints its merged
# output with vm-exec's own "[vm-exec] ..." diagnostics removed; returns the
# guest status (125 = capture infrastructure failure, see qdwin_vmx_merged).
js_guest() {
    local b64 out rc=0
    b64=$(printf '%s' "$1" | base64 -w0)
    out=$(qdwin_vmx_merged "echo $b64 | base64 -d | bash") || rc=$?
    printf '%s\n' "$out" | grep -v '^\[vm-exec\]' || :
    return "$rc"
}

# js_cursor — the cursor of the newest admin (uid 1000) journal entry, the form
# the scenarios this replaces used. Empty output is a setup failure for the
# caller to handle.
js_cursor() {
    js_guest 'journalctl _UID=1000 -n 1 --show-cursor --no-pager 2>/dev/null | sed -n "s/^-- cursor: //p" | tail -1' \
        | grep -E '^s=' | tail -1
}

# js_after <cursor> [unit] — journal lines after <cursor> from one admin user
# unit (default qdwin-compositor.service), short output (strips the ANSI codes
# Quickshell writes into its messages).
js_after() {
    local cur=$1 unit=${2:-qdwin-compositor.service}
    js_guest "journalctl _UID=1000 _SYSTEMD_USER_UNIT=$unit --after-cursor=$(printf '%q' "$cur") --no-pager 2>/dev/null"
}

# js_wait <cursor> <ERE> [timeout_s] [unit] [min_count] — poll in the guest
# until at least <min_count> (default 1) lines after <cursor> from <unit> match
# <ERE>; prints the matching lines. Returns 0 on match, 1 on timeout.
js_wait() {
    local cur=$1 re=$2 t=${3:-10} unit=${4:-qdwin-compositor.service} n=${5:-1}
    local script
    script=$(cat <<EOF
cur=$(printf '%q' "$cur"); re=$(printf '%q' "$re"); n=$n
end=\$(( \$(date +%s) + $t ))
while :; do
    m=\$(journalctl _UID=1000 _SYSTEMD_USER_UNIT=$unit --after-cursor="\$cur" --no-pager 2>/dev/null | grep -E -- "\$re")
    if [ -n "\$m" ] && [ "\$(printf '%s\n' "\$m" | wc -l)" -ge "\$n" ]; then
        printf '%s\n' "\$m"; exit 0
    fi
    [ "\$(date +%s)" -ge "\$end" ] && { printf '%s\n' "\$m"; exit 1; }
    sleep 0.25
done
EOF
)
    js_guest "$script"
}

# js_count <cursor> <ERE> [unit] — number of matching lines after <cursor>.
js_count() {
    local cur=$1 re=$2 unit=${3:-qdwin-compositor.service}
    js_guest "journalctl _UID=1000 _SYSTEMD_USER_UNIT=$unit --after-cursor=$(printf '%q' "$cur") --no-pager 2>/dev/null | grep -cE -- $(printf '%q' "$re") || true" \
        | grep -E '^[0-9]+$' | tail -1
}

# js_spawn_window <title> [color] — launch qdistro-test-window (baked on every
# qdwin golden, unlike foot) as admin on wayland-1, detached.
js_spawn_window() {
    local title=$1 color=${2:-0xff304050}
    js_guest "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title $(printf '%q' "$title") --width 320 --height 200 --color $color >/tmp/js-$title.log 2>&1 </dev/null" >/dev/null
}

# js_kill_windows — kill every admin qdistro-test-window. NOT `pkill -x`: the
# comm is truncated to 15 chars ("qdistro-test-wi"), so -x never matches. -f
# is safe here because it is anchored at argv[0] and scoped to uid admin (the
# qemu-ga guest-exec shell runs as root and starts with "bash"). SIGKILL:
# qdistro-test-window catches SIGTERM and does not exit while idle in its
# Wayland dispatch loop.
js_kill_windows() {
    js_guest "pkill -KILL -u admin -f '^qdistro-test-window( |\$)' 2>/dev/null; sleep 0.3; true" >/dev/null
}

# js_window_handle <cursor> <timeout_s> [nth] — the handle of the nth (default
# last-seen-first: 1) qdistro-test-window toplevel_added after <cursor>.
js_window_handles() {
    local cur=$1 t=${2:-10} want=${3:-1}
    js_wait "$cur" 'qdwin: toplevel_added handle=[0-9]+ uid=1000 pid=[0-9]+ app_id=qdistro-test-window' "$t" qdwin-compositor.service "$want" \
        | sed -nE 's/.*toplevel_added handle=([0-9]+) uid=1000 pid=([0-9]+) .*/\1 \2/p'
}

# js_qs_ipc <args...> — `qs ipc call qdwin <args>` against the running qdshell,
# the proven invocation (runuser -u admin -- env ... qs ipc -p PATH call ...),
# with the PID fallback the 16/17/20 scenarios use.
JS_QS_PATH=/usr/share/quickshell/qdshell
js_qs_ipc() {
    local args out
    args=$(printf '%q ' "$@")
    out=$(js_guest "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qs ipc -p $JS_QS_PATH call qdwin $args 2>&1")
    if printf '%s' "$out" | grep -qiE 'no running instance|No such'; then
        out=$(js_guest "pid=\$(pgrep -u admin -x qs | while read p; do grep -q dbus-run-session /proc/\$p/cmdline 2>/dev/null || { echo \$p; break; }; done); [ -n \"\$pid\" ] || exit 3; runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 qs ipc --pid \$pid call qdwin $args 2>&1")
    fi
    printf '%s\n' "$out"
}

# js_require_session — qdwin compositor + qdshell active on wayland-1 and the
# test client present; otherwise exit 2 (setup, not a product failure).
js_require_session() {
    js_guest 'test -S /run/user/1000/wayland-1 \
      && runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active --quiet qdwin-compositor.service qdshell.service \
      && command -v qdistro-test-window >/dev/null && echo SESSION-OK' | grep -qx SESSION-OK \
        || js_setup_fail "qdwin compositor/qdshell session or qdistro-test-window not available on $VMNAME"
}

# js_no_protocol_errors <cursor> — no `protocol error` / `error <N>:` in the
# compositor journal since <cursor>.
js_no_protocol_errors() {
    local n
    n=$(js_count "$1" 'protocol error')
    [ "${n:-0}" -eq 0 ] || js_fail "$n protocol-error line(s) in the compositor journal since the step cursor"
}

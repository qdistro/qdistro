#!/bin/bash
# qdwin-helpers.sh — host-side helpers for driving a qdwin-on-tty3
# session from automated tests / LLM agents.
#
# Why this is separate from phase1/gui-tests/../vm-gui:
# - vm-gui assumes labwc + XWayland (Phase 1-5). It uses xdotool via
#   DISPLAY=:0, which doesn't exist under the qdwin compositor.
# - qdwin runs on bare DRM via libweston. Input goes through evdev
#   (kernel input layer) and key bindings fire in the compositor.
# - The launcher overlay does NOT redirect keyboard input — it can
#   only be driven via qdshell's ctrl-socket. (See launcher.py:17-23
#   for the upstream gap; a real wl_keyboard grab is a §6.8 follow-up.)
#
# The three primitives that DO work end-to-end:
#   1. virsh send-key — injects at QEMU's emulated keyboard (evdev
#      layer, below Wayland entirely). Compositor key bindings
#      (Ctrl+Space launcher, Alt+Tab switcher, Ctrl+Alt+L lock) AND
#      input directed at focused toplevels both flow through this.
#   2. qdshell ctrl-socket — `socat - UNIX-CONNECT:/run/user/1000/qdshell.sock`
#      drives launcher / switcher / locker / windows, and returns
#      machine-readable status snapshots.
#   3. qdshell shell-authorized capture — captures qdwin's real Virtual-1
#      compositor framebuffer. virsh screenshot is retained only as a tty/
#      VM diagnostic and must never back a content assertion.
#
# Usage:
#     source phase1/gui-tests/qdwin/qdwin-helpers.sh
#     qdwin_set_vm demo-260430-0805
#     qdwin_send_key KEY_LEFTCTRL KEY_SPACE        # open launcher
#     qdwin_ctrl "launcher-type foot"
#     qdwin_ctrl "launcher-activate"
#     qdwin_screenshot /tmp/foo.png

# NOTE: this file is sourced — do not `set -e/-u` here; those flags
# bleed into the caller's shell and break interactive use. Helpers
# return nonzero on failure; callers can `set -e` themselves.

# qdwin_find_workspace() — shared with tests/apps/qdwin-apps-helpers.sh; see
# tests/lib/workspace.sh for the rationale (worktree-aware upward search).
# shellcheck source=../lib/workspace.sh
source "$(dirname "${BASH_SOURCE[0]}")/../lib/workspace.sh"

: "${VMNAME:=}"
: "${QDWIN_VIRSH:=virsh -c qemu:///session}"
# Anchor the upward search at the qdwin checkout (QDWIN_REPO when the caller
# set it, else this file's own repo root); fall back to the legacy two-up path
# only when no qdistro sibling exists anywhere above (degraded, but no worse
# than before).
if [ -z "${QDWIN_WORKSPACE:-}" ]; then
    QDWIN_WORKSPACE=$(qdwin_find_workspace "${QDWIN_REPO:-$(dirname "${BASH_SOURCE[0]}")/../..}") \
        || QDWIN_WORKSPACE=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)
fi
export QDWIN_WORKSPACE
: "${QDWIN_VM_EXEC:=$QDWIN_WORKSPACE/qdistro/scripts/vm/vm-exec}"
export QDWIN_VM_EXEC

# HARNESS CAPTURE ATTESTATION (qci GUI visual-evidence contract).
#
# qdwin_screenshot is the qdwin lane's CAPTURE TOOL -- the counterpart of
# qdistro/scripts/vm/vm-gui's `virsh screenshot` for the labwc lane. The qci GUI
# gate grades only frames its own capture tools took, so this helper must record
# its captures in the same ledger or every `qci:visual: required` qdwin/qdlocker
# scenario would be recorded ERROR for want of an attested frame.
#
# Optional by design: sourcing failure degrades to a no-op stub so these helpers
# keep working outside a qci run and against an older qdistro checkout.
if [ -r "$QDWIN_WORKSPACE/qdistro/scripts/vm/lib/capture-attest.sh" ]; then
    # shellcheck source=/dev/null
    . "$QDWIN_WORKSPACE/qdistro/scripts/vm/lib/capture-attest.sh"
fi
if ! declare -f capture_attest_frame >/dev/null 2>&1; then
    capture_attest_frame() { :; }
fi
: "${QDWIN_HTTP_DIR:=${QDWIN_REPO}/extra}"
: "${QDWIN_HTTP_URL:=http://10.0.2.2:8765/extra}"

qdwin_set_vm() {
    VMNAME="$1"
}

qdwin_require_vm() {
    if [ -z "${VMNAME:-}" ]; then
        VMNAME=$($QDWIN_VIRSH list --name --state-running | head -1)
    fi
    if [ -z "$VMNAME" ]; then
        echo "qdwin-helpers: no running VM (set VMNAME or qdwin_set_vm)" >&2
        return 1
    fi
}

# ------------------------------------------------- merged vm-exec capture
#
# qdwin_vmx_merged <guest-cmd...> -> runs it in $VMNAME via vm-exec with BOTH
# of vm-exec's descriptors on a private capture file, then prints (a bounded
# prefix of) what was written. Returns vm-exec's exit status.
#
# WHY THIS EXISTS. `x=$(vm-exec ... 2>&1)` and `vm-exec ... 2>&1 | reader` both
# hand vm-exec's fd 2 to a PIPE, and the shell then waits for that pipe to reach
# EOF -- which happens when the LAST writer closes it, not when vm-exec exits.
# vm-exec redirects its own children's fd 1 to an internal capture file, but
# fd 2 goes straight through to every virsh/jq descendant it starts; one that
# outlives vm-exec holds the pipe, and the caller, open long after the guest
# command is dead. An outer `timeout` on vm-exec does not help, because the
# shell is blocked on the read rather than on the child. Reading a regular file
# has no such dependency.
#
# The capture is created, opened and UNLINKED before the command starts, so a
# survivor of an earlier call can never write into a later call's capture and no
# capture is left NAMED while a command runs -- the same shape as bounded_run()
# in qdistro/scripts/vm/vm-exec.
# The replay is ceilinged in BYTES (`head -c`), which is what the knob name has
# always promised. It used to be `read -N`, which counts stored CHARACTERS and
# silently drops NULs without counting them -- so on a NUL-bearing capture the
# ceiling bounded neither bytes nor the amount read. Bash still cannot STORE a
# NUL, so such bytes are lost on assignment regardless of how they are read: the
# ceiling is honest about SIZE, it does not make the value byte-exact.
#
# What bounds this is the BYTE COUNT, not the filesystem. The old comment here
# argued it "cannot hang, because a read of a regular file returns at the
# current EOF however many writers still hold it open"; that reasoning is
# wrong, EOF is re-tested on every read, and `read -N` was measured at 9.57s
# replaying a nominal 256 KiB against a writer staying ahead of it.
#
# WHAT THIS DOES NOT DO: it does not reap a surviving descendant, and it adds no
# size limit of its own -- the only bound on the nameless inode is whatever
# RLIMIT_FSIZE vm-exec installed on its own children, so with
# an older QDWIN_VM_EXEC there is no capture-size bound at all.
# QDISTRO_VM_ALLOW_UNBOUNDED_CAPTURE=1 does NOT itself remove the bound: vm-exec
# still attempts `ulimit -f`, and the flag only permits it to CONTINUE when the
# probe fails rather than refusing to start. Where the limit applies, it applies.
#
# One visible side effect of the `$( )` replay: on any NUL-bearing capture
# bash writes `warning: command substitution: ignored null byte in input`
# to this helper's stderr, once per call. Harmless, and not a failure. Bounding the REPLAY does not bound that GROWTH.
# Callers that need a wall clock set QDWIN_VMX_TIMEOUT (seconds); `timeout -k`
# then bounds THAT INVOCATION -- it is not a whole-helper resource guarantee.
QDWIN_VMX_CAP_BYTES=${QDWIN_VMX_CAP_BYTES:-262144}
# VALIDATE IT. `head -c` accepts things that are not a positive byte count and
# quietly means something else: GNU `head -c -1` is "all but the LAST byte",
# which on a 100-byte capture returns 99 bytes and exit 0 -- a cap that reads
# almost everything while looking like it capped at one. Non-numeric input
# makes head fail, and the `|| :` on the replay would hide that too, returning
# an empty capture indistinguishable from a command that printed nothing.
# (sol, todo/reviews/qci-A-260917-sol-review.md section 1.)
case "$QDWIN_VMX_CAP_BYTES" in
    ''|*[!0-9]*|0|0*)
        echo "qdwin-helpers: QDWIN_VMX_CAP_BYTES must be a positive integer number of bytes, got '$QDWIN_VMX_CAP_BYTES'" >&2
        return 2 2>/dev/null || exit 2 ;;
esac
QDWIN_VMX_KILL_GRACE=${QDWIN_VMX_KILL_GRACE:-5}
qdwin_vmx_merged() {
    local cf wfd rfd out="" rc=0 _qvm_size=""
    cf=$(mktemp "${TMPDIR:-/tmp}/qdwin-vmx.XXXXXXXX") || return 125
    # Every step is checked: an unchecked failure here would leave the capture
    # NAMED while the command runs, which is exactly what this shape promises
    # not to do. 125 is this helper's INFRASTRUCTURE status by convention. It is
    # not a reserved value -- a guest command can exit 125 too, so a caller that
    # must tell them apart cannot do it from the status alone; the stderr line
    # is what distinguishes them.
    exec {wfd}>"$cf" || { rm -f "$cf"; return 125; }
    exec {rfd}<"$cf" || { exec {wfd}>&-; rm -f "$cf"; return 125; }
    rm -f "$cf" || { exec {wfd}>&- {rfd}<&-; return 125; }
    # The child and every descendant get the capture FILE on fd 1 and fd 2; the
    # bookkeeping descriptors are closed for it so nothing downstream inherits a
    # second handle or the read end.
    if [ -n "${QDWIN_VMX_TIMEOUT:-}" ]; then
        timeout -k "${QDWIN_VMX_KILL_GRACE}s" "${QDWIN_VMX_TIMEOUT}s" \
            "$QDWIN_VM_EXEC" "$VMNAME" "$@" \
            >&"$wfd" 2>&"$wfd" {wfd}>&- {rfd}<&- || rc=$?
    else
        "$QDWIN_VM_EXEC" "$VMNAME" "$@" \
            >&"$wfd" 2>&"$wfd" {wfd}>&- {rfd}<&- || rc=$?
    fi
    exec {wfd}>&-
    # BYTE-bounded replay. `read -N` counts stored CHARACTERS, not bytes
    # consumed, and bash silently discards NULs without counting them -- so
    # with a NUL-bearing capture the "CAP_BYTES" ceiling was neither a byte
    # bound nor a bound on how much was read (measured: 204 bytes written ->
    # 4 characters returned). `head -c` bounds the actual bytes, which is what
    # the name has always promised.
    #
    # The substitution is safe here for the same reason it is unsafe around
    # vm-exec: its only writer is our own `head`, which always closes. `head`
    # reads from the unlinked capture FILE, never from a pipe a guest
    # descendant could hold open.
    #
    # STILL TRUE, and not fixable in a shell variable: bash cannot store NUL,
    # so a NUL-bearing capture loses those bytes on assignment regardless of
    # how it is read. The ceiling is now honest about SIZE; it does not make
    # the value byte-exact. A caller needing exact bytes must read the file.
    # `$(...)` also strips trailing newlines, where `read -N` kept them --
    # every caller here greps or compares tokens, so that is immaterial.
        # A FAILED replay is a capture failure, not empty output. `|| :` made
    # `head` returning nonzero indistinguishable from a command that printed
    # nothing, so an I/O error surfaced as rc=0 with an empty string -- sol
    # injected `head() { return 1; }` and got exactly that (A3 finding 3).
    if ! out=$(head -c "$QDWIN_VMX_CAP_BYTES" <&"$rfd"); then
        echo "qdwin-helpers: capture replay FAILED; the output is unavailable, not empty" >&2
        exec {rfd}<&-
        return 125
    fi
    # OVERFLOW IS NOT SILENT. Returning a capped PREFIX with the command's own
    # status tells the caller "here is the output, it succeeded" when part of
    # the output is gone. Every consumer here scans the text for a marker --
    # the screenshot reply grammar at :536, the `no running instance|No such`
    # PID fallback in the scenarios, MMNET-DEV= in the mmnet gate -- and a
    # marker past the cap is indistinguishable from a marker that never
    # appeared, which turns a working product into a FAIL or a broken one into
    # a PASS. (sol, qci-A-260917-sol-review.md section 5.)
    #
    # WHAT THIS ESTABLISHES, AND WHAT IT DOES NOT. Stated plainly because the
    # two previous attempts here both overclaimed.
    #
    # DETECTED: the capture holds more bytes than the replay returned -- a
    # stored suffix this function did not deliver. A real, local fact about
    # this file and this cap.
    #
    # NOT DETECTED: a writer that hit its own RLIMIT_FSIZE, handled EFBIG and
    # exited zero. The previous version compared the size against the PARENT's
    # `ulimit -f -H` and claimed to catch exactly that. It cannot, for two
    # independent reasons (astra, A6 findings 1 and 2):
    #   * `-H` is the HARD limit, but writes are constrained by the SOFT one,
    #     so `ulimit -S -f 1` under an unlimited hard limit went undetected.
    #   * The writer is not this process. vm-exec is a child, `bounded_run`
    #     sets its own limit for the inner RPC capture, and a descendant
    #     writing into the outer merged capture can have a different limit
    #     again -- one file, several limits, so no single parent-side number
    #     describes them.
    # And `>=` against that number turned a HEALTHY exact-fit write into a
    # fatal 125 that positively asserted the output "was cut off": a false red,
    # the very class of defect this workstream exists to remove.
    #
    # Proving completeness needs evidence from the WRITER (an explicit
    # completion marker), not a size. Until that exists this reports what it
    # can and stays silent about what it cannot. Equality at the cap is
    # ACCEPTED -- an exact fit is a legitimate write.
    if ! _qvm_size=$(stat -Lc %s "/proc/self/fd/$rfd" 2>/dev/null); then
        echo "qdwin-helpers: could not stat the capture to check completeness; whether the output is complete is UNKNOWN, not verified" >&2
        exec {rfd}<&-
        return 125
    elif [ "$_qvm_size" -gt "$QDWIN_VMX_CAP_BYTES" ]; then
        echo "qdwin-helpers: vm-exec output (${_qvm_size} bytes) exceeded the ${QDWIN_VMX_CAP_BYTES}-byte capture cap; the returned text is INCOMPLETE and any marker beyond the cap is lost. Raise QDWIN_VMX_CAP_BYTES, or set QDWIN_VMX_ALLOW_TRUNCATION=1 to accept a prefix." >&2
        if [ "${QDWIN_VMX_ALLOW_TRUNCATION:-0}" != 1 ]; then
            exec {rfd}<&-
            return 125
        fi
    fi
    exec {rfd}<&-
    printf '%s' "$out"
    return "$rc"
}

# ---------------------------------------------------------------- key
#
# All key injection goes through QMP input-send-event so modifier
# state stays consistent across calls. Mixing virsh send-key with the
# QMP qcode path leaves dangling modifiers — KEY_ENTER right after a
# qdwin_chord ctrl alt -- l ends up sent with ctrl/alt still held in
# QEMU's input layer, and a focused terminal sees `^[[13;7~` (xterm
# CSI for Ctrl+Shift+Enter) instead of CR.

# Linux KEY_* → qemu qcode mapping (subset covering the harness's needs).
_qdwin_linux_to_qcode() {
    case "$1" in
        KEY_LEFTCTRL)  echo ctrl ;;
        KEY_RIGHTCTRL) echo ctrl_r ;;
        KEY_LEFTALT)   echo alt ;;
        KEY_RIGHTALT)  echo alt_r ;;
        KEY_LEFTSHIFT) echo shift ;;
        KEY_RIGHTSHIFT) echo shift_r ;;
        KEY_LEFTMETA|KEY_RIGHTMETA) echo meta_l ;;
        KEY_TAB)       echo tab ;;
        KEY_ESC)       echo esc ;;
        KEY_ENTER|KEY_KPENTER) echo ret ;;
        KEY_SPACE)     echo spc ;;
        KEY_BACKSPACE) echo backspace ;;
        KEY_DOT)       echo dot ;;
        KEY_MINUS)     echo minus ;;
        KEY_UP)        echo up ;;
        KEY_DOWN)      echo down ;;
        KEY_LEFT)      echo left ;;
        KEY_RIGHT)     echo right ;;
        KEY_[A-Z])     printf "%s" "${1#KEY_}" | tr A-Z a-z ;;
        KEY_[0-9])     echo "${1#KEY_}" ;;
        *)
            echo "qdwin: no qcode for $1" >&2
            return 1 ;;
    esac
}

# Send each linux KEY_* arg as a discrete press+release in order.
# For chord-style (hold-and-tap) use qdwin_chord instead.
qdwin_send_key() {
    qdwin_require_vm
    local k qcode
    for k in "$@"; do
        qcode=$(_qdwin_linux_to_qcode "$k") || return 1
        qdwin_qmp_key "$qcode" down
        sleep 0.03
        qdwin_qmp_key "$qcode" up
        sleep 0.03
    done
}

# --------------------------------------------- QMP input (real chords)
#
# `virsh send-key` presses all listed keys, holds, and releases them
# all in reverse order. That doesn't match a real-keyboard chord like
# Alt+Tab — there's no "Alt held alone, Tab released" intermediate
# state. weston's modifier-release binding (and the qdwin switcher
# grab's modifiers callback) require that intermediate.
#
# QMP `input-send-event` lets us push individual key-down/key-up
# events as separate atomic ops. qcodes (qemu key codes) are NOT
# linux KEY_* names — see qapi/ui.json `QKeyCode` enum. Common ones:
#   alt, alt_r, ctrl, ctrl_r, shift, shift_r, meta_l, meta_r
#   tab, esc, ret, spc, backspace
#   a..z, 0..9, f1..f12, left/right/up/down
qdwin_qmp_key() {
    # qdwin_qmp_key <qcode> <down|up>
    qdwin_require_vm
    local qcode="$1" updown="$2"
    local down=true
    [ "$updown" = up ] && down=false
    $QDWIN_VIRSH qemu-monitor-command "$VMNAME" \
        "{\"execute\": \"input-send-event\", \"arguments\": {\"events\": [{\"type\": \"key\", \"data\": {\"down\": $down, \"key\": {\"type\": \"qcode\", \"data\": \"$qcode\"}}}]}}" \
        >/dev/null
}

# Force every modifier into the released state. A compositor lock transition
# can happen between a chord's key-down and key-up events; QEMU has delivered
# the releases, but the newly promoted lock surface may consume them before
# libweston's normal seat state observes them. Releasing is idempotent, so GUI
# scenarios should call this after an unlock/restart boundary before asserting
# ordinary text input.
qdwin_release_modifiers() {
    qdwin_require_vm
    local k
    for k in ctrl ctrl_r alt alt_r shift shift_r meta_l meta_r; do
        qdwin_qmp_key "$k" up || return $?
        sleep 0.02
    done
}

# Real-keyboard chord: hold modifier(s), tap key(s), release modifier(s).
# Args: <hold-key1> [hold-key2 ...] -- <tap-key1> [tap-key2 ...]
# Example: qdwin_chord alt -- tab           # hold Alt, tap Tab, release Alt
#          qdwin_chord ctrl alt -- l        # Ctrl+Alt+L
#          qdwin_chord alt -- tab tab       # hold Alt, tap Tab twice, release
qdwin_chord() {
    local hold=()
    local tap=()
    local in_tap=0
    for arg in "$@"; do
        if [ "$arg" = "--" ]; then in_tap=1; continue; fi
        if [ "$in_tap" = 0 ]; then hold+=("$arg"); else tap+=("$arg"); fi
    done
    local k
    # Press all holds in order
    for k in "${hold[@]}"; do qdwin_qmp_key "$k" down; sleep 0.03; done
    # Tap each tap-key (down + up)
    for k in "${tap[@]}"; do
        qdwin_qmp_key "$k" down; sleep 0.05
        qdwin_qmp_key "$k" up;   sleep 0.05
    done
    # Release holds in reverse order
    for ((i=${#hold[@]}-1; i>=0; i--)); do
        qdwin_qmp_key "${hold[i]}" up; sleep 0.03
    done
}

# Type a string letter-by-letter via QMP. Lowercase ASCII + space + a
# few punctuation. Slow (~10/sec) — prefer the launcher ctrl-socket
# when the launcher itself is the target; this is for typing into
# focused terminals.
qdwin_type_lower() {
    qdwin_require_vm
    local s="$1"
    local i ch qcode
    for ((i = 0; i < ${#s}; i++)); do
        ch="${s:i:1}"
        case "$ch" in
            ' ') qcode=spc ;;
            '.') qcode=dot ;;
            '-') qcode=minus ;;
            [a-z]) qcode="$ch" ;;
            [0-9]) qcode="$ch" ;;
            *)
                echo "qdwin_type_lower: unsupported char '$ch'" >&2
                return 1 ;;
        esac
        qdwin_qmp_key "$qcode" down
        sleep 0.03
        qdwin_qmp_key "$qcode" up
        sleep 0.04
    done
}

# --------------------------------------------------------- ctrl-socket
#
# Sends a one-line command to qdshell's ctrl-socket and prints the
# response. Runs as `admin` because the socket is owned by uid 1000.
# Available commands (qdshell.py around line 1466+):
#   launcher                       (snapshot)
#   launcher-toggle
#   launcher-type <text>           (sets filter)
#   launcher-activate              (spawns selected entry)
#   switcher
#   switcher-next / switcher-commit
#   list                           (lists toplevels)
#   tray, panel, locker            (snapshots)
#
# Pushing the runner script via the existing host:8765 server is the
# robust path; vm-exec's JSON quoting trips on embedded `"`.
qdwin_ctrl() {
    qdwin_require_vm
    local cmd="$1"
    local script="qdwin-ctrl-$$.sh"
    cat > "$QDWIN_HTTP_DIR/$script" <<EOF
#!/bin/bash
runuser -u admin -- bash -c "echo '$cmd' | socat -t 2 - UNIX-CONNECT:/run/user/1000/qdshell.sock"
EOF
    # File capture, never `... 2>&1 | grep`: see qdwin_vmx_merged above for
    # why a host pipeline on vm-exec's fd 2 can hang this call. The filtering
    # pipeline below is fed by this shell's own `printf`, which always closes.
    #
    # The GUEST command's status is deliberately NOT propagated -- callers read
    # the printed reply -- but a CAPTURE failure is not a guest result and must
    # not be presented as success. 125 means the helper could not produce the
    # output at all (setup failed, or the reply overflowed the byte cap and was
    # suppressed rather than returned as a prefix). Swallowing it made this
    # wrapper return rc=0 with EMPTY output, which a caller reads as "the
    # command ran and said nothing" (sol, qci-A2-260917-sol-review.md
    # section 2: cap 4 against an 8-byte reply gave rc=0, empty).
    local out rc=0
    out=$(qdwin_vmx_merged "wget -qO /tmp/qc.sh $QDWIN_HTTP_URL/$script && bash /tmp/qc.sh") || rc=$?
    # `|| :` on the filter: grep exits 1 when it emits NOTHING, which is the
    # NORMAL case for an empty or fully-suppressed capture. Unguarded, under
    # `set -e` a bare `qdwin_ctrl ...` died right here -- before the cleanup
    # below and before the 125 branch -- leaving the HTTP script behind and
    # never delivering the status this function advertises (sol,
    # qci-A3-260917-sol-review.md finding 2).
    printf '%s' "$out" | grep -v '^\[vm-exec\]' || :
    rm -f "$QDWIN_HTTP_DIR/$script"
    if [ "$rc" = 125 ]; then
        echo "qdwin_ctrl: capture failed for '$cmd' (no usable reply)" >&2
        return 125
    fi
    return 0
}

# ----------------------------------------------------- mouse (QMP)
#
# QMP `input-send-event` also covers mouse via three event types:
#   abs: {"type":"abs", "data":{"axis":"x|y", "value":<0..32767>}}
#   rel: {"type":"rel", "data":{"axis":"x|y", "value":<int>}}
#   btn: {"type":"btn", "data":{"button":"left|middle|right", "down":bool}}
# Multiple events can ship in one call (atomic at QEMU's input layer).
# QEMU's USB Tablet uses absolute coordinates 0..32767 mapped across
# the screen — we convert pixel coords to that range based on output
# size (default 1280x800, override via QDWIN_SCREEN_W / _H).
: "${QDWIN_SCREEN_W:=1280}"
: "${QDWIN_SCREEN_H:=800}"

# Move pointer to absolute pixel (x, y).
qdwin_mouse_move() {
    qdwin_require_vm
    local x="$1" y="$2"
    local ax=$(( x * 32767 / QDWIN_SCREEN_W ))
    local ay=$(( y * 32767 / QDWIN_SCREEN_H ))
    $QDWIN_VIRSH qemu-monitor-command "$VMNAME" \
        "{\"execute\": \"input-send-event\", \"arguments\": {\"events\": [
            {\"type\":\"abs\",\"data\":{\"axis\":\"x\",\"value\":$ax}},
            {\"type\":\"abs\",\"data\":{\"axis\":\"y\",\"value\":$ay}}
        ]}}" >/dev/null
}

# Send a mouse button event (left/middle/right) without moving.
qdwin_mouse_button() {
    qdwin_require_vm
    local btn="$1" updown="$2"
    local down=true
    [ "$updown" = up ] && down=false
    $QDWIN_VIRSH qemu-monitor-command "$VMNAME" \
        "{\"execute\": \"input-send-event\", \"arguments\": {\"events\": [
            {\"type\":\"btn\",\"data\":{\"button\":\"$btn\",\"down\":$down}}
        ]}}" >/dev/null
}

# Send an abs-move AND a button event in a SINGLE input-send-event so the
# pointer position and the button transition arrive atomically at QEMU's
# input layer. QEMU's USB tablet can drop a standalone `btn` that is not
# accompanied by a position update on the same report; coalescing the move
# with the press/release is the reliable shape (see comment above re:
# atomic multi-event batches).
qdwin_mouse_move_button() {
    qdwin_require_vm
    local x="$1" y="$2" btn="$3" updown="$4"
    local ax=$(( x * 32767 / QDWIN_SCREEN_W ))
    local ay=$(( y * 32767 / QDWIN_SCREEN_H ))
    local down=true
    [ "$updown" = up ] && down=false
    $QDWIN_VIRSH qemu-monitor-command "$VMNAME" \
        "{\"execute\": \"input-send-event\", \"arguments\": {\"events\": [
            {\"type\":\"abs\",\"data\":{\"axis\":\"x\",\"value\":$ax}},
            {\"type\":\"abs\",\"data\":{\"axis\":\"y\",\"value\":$ay}},
            {\"type\":\"btn\",\"data\":{\"button\":\"$btn\",\"down\":$down}}
        ]}}" >/dev/null
}

# Click left button at (x, y) — press (with move) then release (with move).
# The move is batched into BOTH the press and release events so the button
# transition is never sent as a standalone report (which QEMU's tablet can
# drop). See qdwin_mouse_move_button.
qdwin_click() {
    local x="$1" y="$2" btn="${3:-left}"
    qdwin_mouse_move "$x" "$y"
    sleep 0.05
    qdwin_mouse_move_button "$x" "$y" "$btn" down
    sleep 0.05
    qdwin_mouse_move_button "$x" "$y" "$btn" up
    sleep 0.05
}

# Mouse-drag from (x1,y1) to (x2,y2) with the left button held.
# Emits: move-to-start → button-down → N intermediate moves → button-up.
# The intermediate steps matter — qdwin's move grab translates the
# toplevel on every motion event; a single jump from start to end gives
# a much-less-realistic test (and on some compositors collapses to no
# visible motion). Default 8 steps with 30ms gaps; override via
# QDWIN_DRAG_STEPS / QDWIN_DRAG_STEP_MS.
: "${QDWIN_DRAG_STEPS:=8}"
: "${QDWIN_DRAG_STEP_MS:=30}"
qdwin_drag() {
    qdwin_require_vm
    local x1="$1" y1="$2" x2="$3" y2="$4" btn="${5:-left}"
    qdwin_mouse_move "$x1" "$y1"
    sleep 0.08
    qdwin_mouse_button "$btn" down
    sleep 0.08
    local steps="$QDWIN_DRAG_STEPS"
    local i
    for (( i=1; i<=steps; i++ )); do
        local cx=$(( x1 + (x2 - x1) * i / steps ))
        local cy=$(( y1 + (y2 - y1) * i / steps ))
        qdwin_mouse_move "$cx" "$cy"
        # bash sleep accepts fractional seconds
        sleep "$(awk "BEGIN { printf \"%.3f\", $QDWIN_DRAG_STEP_MS/1000 }")"
    done
    qdwin_mouse_button "$btn" up
    sleep 0.1
}

# --------------------------------------------------------- screenshot
qdwin_screenshot_virsh_diag() {
    qdwin_require_vm
    local out="${1:-/tmp/qdwin-virsh-diag.png}"
    local tmp="${out%.png}.ppm"
    $QDWIN_VIRSH screenshot "$VMNAME" "$tmp" >/dev/null || return 1
    mv "$tmp" "$out"
    echo "$out"
}
# Host-side deadlines for the capture round-trip, in seconds, scaled by
# QDWIN_CAPTURE_TIMEOUT_SCALE (integer, default 1).
#
# These bound LIVENESS, not latency: the happy path returns as soon as the guest
# answers, so a larger ceiling costs nothing when things work and only changes
# how long a genuinely wedged capture waits. They are measured on the HOST while
# the work happens in the GUEST, so host contention alone can blow them — the
# `qci full` run that surfaced this had 7 concurrent qemu VMs and peak loadavg
# 3.23, and a 1920x1080 PNG has to be base64'd through qemu-guest-agent. Raise
# the scale on a loaded runner rather than editing the numbers.
#
# INVARIANT: the outer `timeout` must exceed the inner `socat -T`, or the host
# gives up first and the error says "timed out" instead of socat's reason.
#
# The capture socat needs BOTH `-T` and `-t`. `-T` is only the INACTIVITY
# timeout; `-t` is how long socat keeps reading the socket after its stdin (the
# one-line request) hits EOF, and it DEFAULTS TO 0.5s. qdshell answers only
# after the capture completes, so any capture slower than half a second — a
# loaded host, or a qdshell still busy starting up — made socat exit 0 with an
# EMPTY reply while qdshell went on to write a perfectly good PNG
# (qdlocker/tests/gui/06 in full-20260922T193137Z-881799: qdshell logged
# `capture complete ... live` 0.7s after the request; the helper saw "no usable
# reply"). Reproduce on any host: a UNIX server that sleeps 0.8s before
# replying returns nothing to `printf x | socat -T 25 - UNIX-CONNECT:s` and the
# reply to the same command with `-t 25`.
QDWIN_CAPTURE_TIMEOUT_SCALE=${QDWIN_CAPTURE_TIMEOUT_SCALE:-1}
QDWIN_CAPTURE_T=$((30 * QDWIN_CAPTURE_TIMEOUT_SCALE))   # outer, capture request
QDWIN_CAPTURE_SOCAT_T=$((25 * QDWIN_CAPTURE_TIMEOUT_SCALE))  # inner, socat
QDWIN_CAPTURE_COPY_T=$((20 * QDWIN_CAPTURE_TIMEOUT_SCALE))   # stat/hash + base64
# In-shell Wayland pump deadline, passed through the ctrl verb. qdshell's
# built-in 8s is otherwise the binding constraint on a loaded host — the
# knobs above only stretch the transport around it. Sent ONLY when the scale
# is raised so the default 3-arg ctrl form keeps working against goldens
# whose qdshell predates the [timeout-ms] argument. Must stay below the
# socat -T above or socat gives up first (8*scale < 25*scale holds).
QDWIN_CAPTURE_SHELL_T_MS=$((8000 * QDWIN_CAPTURE_TIMEOUT_SCALE))
[ "$QDWIN_CAPTURE_SHELL_T_MS" -gt 120000 ] && QDWIN_CAPTURE_SHELL_T_MS=120000  # ctrl-verb upper bound
# How long a capture waits for qdshell's ctrl socket to come back when the
# first attempt found it REFUSED or ABSENT, i.e. qdshell was mid-restart
# (a scenario's own `systemctl --user restart qdshell`, or the unit's
# Restart=on-failure respawn after a crash test killed it). Bounded: a shell
# that stays down still fails the capture, just after this many seconds.
QDWIN_CAPTURE_SHELL_WAIT_T=$((20 * QDWIN_CAPTURE_TIMEOUT_SCALE))

# The qdshell ctrl socket is refusing/absent -> qdshell is (re)starting.
# Matches socat's connect() failure text, nothing else: a capture that the
# shell ANSWERED with `error: ...` is a real refusal and must not be retried.
qdwin_capture_reply_shell_down() {
    case "$1" in
        *'qdshell.sock'*'Connection refused'*|*'qdshell.sock'*'No such file or directory'*) return 0 ;;
    esac
    return 1
}

# Wait (bounded) until qdshell's ctrl socket answers `status` with `ok`, then
# (bounded, best-effort) until THIS qdshell instance has mapped its wallpaper
# on Virtual-1. The ctrl socket listens ~1-2s before the wallpaper paints, so
# stopping at `status ok` hands the capture a near-black startup frame — which
# the graders then rightly reject as unusable. The wallpaper line is qdwin's
# (`layer-shell mapped ns=qdshell-wallpaper-Virtual-1`), read from the
# compositor unit's journal only, since the unit's current ActiveEnterTimestamp
# (never a whole-journal grep: qemu-ga logs this very command). If it never
# appears (wallpaper disabled) the wait just expires and the capture proceeds.
# Connects as root, like the capture itself; one vm-exec round trip.
qdwin_wait_shell_ctrl() {
    local secs="${1:-$QDWIN_CAPTURE_SHELL_WAIT_T}" out
    local paint=$((10 * QDWIN_CAPTURE_TIMEOUT_SCALE))
    out=$(QDWIN_VMX_TIMEOUT=$((secs + paint + 15)) qdwin_vmx_merged "
        i=0; up=
        while [ \$i -lt $((secs * 2)) ]; do
            r=\$(printf 'status\\n' | socat -T 2 -t 2 - UNIX-CONNECT:/run/user/1000/qdshell.sock 2>/dev/null)
            [ \"\$r\" = ok ] && { up=1; break; }
            i=\$((i + 1)); sleep 0.5
        done
        [ -n \"\$up\" ] || { echo SHELL_CTRL_DOWN; exit 1; }
        echo SHELL_CTRL_UP
        since=\$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show qdshell.service -p ActiveEnterTimestamp --value 2>/dev/null)
        i=0
        while [ -n \"\$since\" ] && [ \$i -lt $((paint * 2)) ]; do
            journalctl -b _SYSTEMD_USER_UNIT=qdwin-compositor.service --since \"\$since\" --no-pager -o cat 2>/dev/null |
                grep -q 'layer-shell mapped ns=qdshell-wallpaper-Virtual-1 ' && { echo SHELL_PAINTED; exit 0; }
            i=\$((i + 1)); sleep 0.5
        done
        echo SHELL_PAINT_UNCONFIRMED; exit 0") || :
    case "$out" in *SHELL_CTRL_UP*) ;; *) return 1 ;; esac
    case "$out" in
        *SHELL_PAINTED*) ;;
        *) echo "WARN: qdshell is back but its wallpaper map was not observed within ${paint}s; the frame may predate the shell's first paint" >&2 ;;
    esac
    return 0
}


qdwin_screenshot() {
    qdwin_require_vm || return 2
    local out="${1:-/tmp/qdwin-shot.png}"
    local guest="/run/user/1000/qdwin-capture-$$-${RANDOM}.png"
    local host_tmp="${out}.partial.$$"
    local reply reply_raw reply_diag b64 dims width height reply_w reply_h
    local guest_meta guest_size guest_sha host_size host_sha
    local pid_before pid_after

    # Remove any prior capture up front: publish is an atomic mv at the end,
    # so a failed capture leaves NO file at $out — a stale image from an
    # earlier attempt can never satisfy a content assertion, while reruns
    # and QCI_GUI_RETRY on the same hardcoded path keep working. The removal
    # itself must be fail-closed: if the old file cannot be deleted, a later
    # capture failure would leave it in place as stale evidence.
    if ! rm -f "$out" "$out.meta" "$host_tmp" || [ -e "$out" ] || [ -e "$host_tmp" ]; then
        echo "ERROR: stale-capture-path: could not remove prior $out" >&2
        return 1
    fi
    # A capture needs a compositor that can still repaint, i.e. one holding DRM
    # master. When that is missing mid-scenario the usual cause is an EXTERNAL
    # VT takeaway, not a product fault: the seat is disabled, repaints stop, and
    # every later capture in the scenario fails too — turning an already-passing
    # product assertion into an ERROR for want of a screenshot. Try once to
    # switch back to the stamped VT and re-probe.
    #
    # Deliberately NOT silent: a recovered takeaway prints a WARN and stays in
    # the scenario log, so the environment event is still auditable and this
    # cannot quietly paper over a compositor that really does lose its seat.
    # qdwin_vt_recover returns nonzero unless it actually switched, so a
    # genuinely unhealthy session still fails here rather than retrying blind.
    # NB: capture the status on its own line. Inside `if ! cmd; then`, `$?` is
    # the status of the NEGATION (always 0), so reading it there would return 0
    # from this function and report a successful capture for an unhealthy
    # session — a false green on the exact path this block exists to handle.
    local heal_err heal_rc
    heal_err=$(mktemp "${TMPDIR:-/tmp}/qdwin-heal.XXXXXXXX") || return 1
    # `|| heal_rc=$?`, not a bare call: callers may run with errexit, and a
    # bare nonzero return here would kill them in the very restart window
    # this block exists to ride out (and leak $heal_err).
    heal_rc=0
    qdwin_session_healthy 2>"$heal_err" || heal_rc=$?
    if [ "$heal_rc" -ne 0 ]; then
        # Replay the gate's own account only if we do NOT recover below; a
        # shell restart window is not an ERROR and must not print as one.
        [ "$heal_rc" -eq 2 ] && { cat "$heal_err" >&2; rm -f "$heal_err"; return 2; }
        # qdshell mid-restart (auto-restart after a crash test, or a
        # scenario's own restart) fails the unit check above for ~1-2s. The
        # capture client IS qdshell, so wait for its ctrl socket to answer
        # and re-run the gate; anything else falls through to VT recovery.
        if qdwin_wait_shell_ctrl "$QDWIN_CAPTURE_SHELL_WAIT_T" && qdwin_session_healthy; then
            echo "WARN: capture-after-shell-restart: qdshell was down at capture time and came back; this frame is taken AFTER the shell restart" >&2
        else
            cat "$heal_err" >&2
            rm -f "$heal_err"
            qdwin_recover_and_verify || return "$heal_rc"
        fi
    else
        cat "$heal_err" >&2
    fi
    rm -f "$heal_err"
    pid_before=$(qdwin_compositor_pid) || return 1

    # Connect as root: CtrlServer verifies SO_PEERCRED so the admin desktop
    # user cannot turn qdshell into a screenshot confused deputy. The qdshell
    # process itself writes a new 0600 file and atomically publishes it at the
    # requested runtime-dir path only after weston_capture reports complete.
    # The capture itself is retried once through the same recovery, because the
    # pre-capture gate above cannot cover the window that matters: in the
    # 19-wm-policy failure the seat died 1ms after the tile chord — i.e. AFTER
    # the gate passed and BEFORE/DURING this request. A takeaway here does not
    # fail fast, it makes the capture task sit unserviced in
    # pending_capture_list (nothing repaints while OFFSCREEN) until the timeout
    # expires. Recover, then ask once more.
    local attempt
    for attempt in 1 2; do
        local shell_t_arg=""
        [ "$QDWIN_CAPTURE_TIMEOUT_SCALE" -gt 1 ] && shell_t_arg=" $QDWIN_CAPTURE_SHELL_T_MS"
        # File capture, never `$( ... 2>&1 )`: see qdwin_vmx_merged. This is
        # the hottest vm-exec call in the suite (every screenshot), so it is
        # also the one most exposed to a virsh descendant holding the pipe.
        reply_raw=$(QDWIN_VMX_TIMEOUT="$QDWIN_CAPTURE_T" qdwin_vmx_merged \
            "printf 'capture Virtual-1 $guest$shell_t_arg\\n' | socat -T $QDWIN_CAPTURE_SOCAT_T -t $QDWIN_CAPTURE_SOCAT_T - UNIX-CONNECT:/run/user/1000/qdshell.sock") || :
        # SEPARATE TRANSPORT CHATTER FROM THE PROTOCOL REPLY BEFORE PARSING.
        # The capture is MERGED stdout+stderr, and the `case` below matches the
        # WHOLE string against the reply grammar -- so any line vm-exec writes
        # to its own stderr makes a perfectly good capture unparseable and
        # turns the scenario red for a transport reason. That is not
        # hypothetical: adding a success-path `[vm-exec] guest identity pinned`
        # line did exactly this, rejecting both attempts of a valid reply (sol,
        # todo/reviews/qci-A3-260917-sol-review.md finding 1). vm-exec's
        # periodic `[vm-exec] Waiting...` lines are the same hazard on a slow
        # capture. Diagnostics are KEPT, in $reply_diag, for the messages below.
        reply=$(printf '%s\n' "$reply_raw" | grep -v '^\[vm-exec\]' | grep -v '^[[:space:]]*$') || :
        reply_diag=$(printf '%s\n' "$reply_raw" | grep '^\[vm-exec\]') || :
        case "${reply:-}" in
            ok\ output=Virtual-1\ width=*\ height=*\ path="$guest") break ;;
            # v33 retained-frame fallback: the compositor served the LAST
            # COMPOSITED frame because no repaint could happen (seat away,
            # power off, repaint wedge). Valid image, stale evidence —
            # flagged below via the .meta sidecar.
            ok\ output=Virtual-1\ width=*\ height=*\ path="$guest"\ live=0\ age_ms=*) break ;;
        esac
        if [ "$attempt" = 1 ]; then
            # EMIT the retained transport diagnostics. They were captured into
            # $reply_diag and then never read, so filtering them out of the
            # protocol reply silently DESTROYED them: a transport that reported a
            # specific failure surfaced to the user as "capture command timed
            # out" (sol, qci-A4-260917-sol-review.md finding 2). Print them before
            # the retry, which overwrites both variables.
            [ -z "$reply_diag" ] || printf '%s\n' "$reply_diag" >&2
            rm -f "$host_tmp"
            # qdshell mid-restart: its ctrl socket refused or was absent. That
            # is a transient of the CAPTURE CHANNEL (the capture client IS
            # qdshell), not a property of what is on screen -- wait for the
            # respawned shell and ask once more. Loud, so the report can say
            # the frame was taken after the shell came back.
            if qdwin_capture_reply_shell_down "$reply"; then
                echo "NOTE: capture attempt 1 failed ($reply); qdshell's ctrl socket is down (shell restarting) — waiting up to ${QDWIN_CAPTURE_SHELL_WAIT_T}s for it" >&2
                if qdwin_wait_shell_ctrl "$QDWIN_CAPTURE_SHELL_WAIT_T"; then
                    echo "WARN: capture-after-shell-restart: qdshell's ctrl socket came back; retrying once — this frame is taken AFTER the shell restart, not at the moment originally requested" >&2
                    "$QDWIN_VM_EXEC" "$VMNAME" "rm -f '$guest'" >/dev/null 2>&1 || true
                    continue
                fi
                echo "ERROR: shell-capture-failed: qdshell's ctrl socket did not come back within ${QDWIN_CAPTURE_SHELL_WAIT_T}s ($reply)" >&2
                qdwin_capture_fail_cleanup "$guest"
                return 1
            fi
            echo "NOTE: capture attempt 1 failed (${reply:-no usable reply — timed out, or the capture exceeded its byte cap; any [vm-exec] lines above carry the transport account}); checking for a VT takeaway during the capture" >&2
            if qdwin_recover_and_verify; then
                "$QDWIN_VM_EXEC" "$VMNAME" "rm -f '$guest'" >/dev/null 2>&1 || true
                continue
            fi
        fi
        [ -z "$reply_diag" ] || printf '%s\n' "$reply_diag" >&2
        echo "ERROR: shell-capture-failed: ${reply:-no usable reply; see the [vm-exec] lines above for the transport account}" >&2
        qdwin_capture_fail_cleanup "$guest"
        return 1
    done
    # The reply dimensions are cross-checked against the decoded PNG below.
    reply_w=$(sed -n 's/.* width=\([0-9]*\) .*/\1/p' <<<"$reply")
    reply_h=$(sed -n 's/.* height=\([0-9]*\) .*/\1/p' <<<"$reply")

    # Guest-side ground truth BEFORE the copy: qemu-guest-agent stdout can be
    # silently truncated, and base64 that is cut on a 4-char quantum still
    # decodes cleanly. The copied host bytes must match this size + sha256.
    guest_meta=$(timeout "${QDWIN_CAPTURE_COPY_T}s" "$QDWIN_VM_EXEC" "$VMNAME" \
        "stat -c %s '$guest' && sha256sum '$guest' | cut -d' ' -f1") || {
        echo "ERROR: shell-capture-copy-failed: could not stat/hash $guest" >&2
        qdwin_capture_fail_cleanup "$guest"
        return 1
    }
    guest_size=$(sed -n 1p <<<"$guest_meta")
    guest_sha=$(sed -n 2p <<<"$guest_meta")

    b64=$(timeout "${QDWIN_CAPTURE_COPY_T}s" "$QDWIN_VM_EXEC" "$VMNAME" "base64 -w0 '$guest'") || {
        echo "ERROR: shell-capture-copy-failed: could not read $guest" >&2
        qdwin_capture_fail_cleanup "$guest"
        return 1
    }
    if ! printf '%s' "$b64" | base64 -d > "$host_tmp"; then
        echo "ERROR: shell-capture-copy-failed: invalid base64 payload" >&2
        rm -f "$host_tmp"
        qdwin_capture_fail_cleanup "$guest"
        return 1
    fi
    "$QDWIN_VM_EXEC" "$VMNAME" "rm -f '$guest'" >/dev/null 2>&1 || true

    host_size=$(stat -c %s "$host_tmp")
    host_sha=$(sha256sum "$host_tmp" | cut -d' ' -f1)
    if [ "$host_size" != "$guest_size" ] || [ "$host_sha" != "$guest_sha" ]; then
        echo "ERROR: shell-capture-copy-failed: guest/host mismatch" \
             "(size $guest_size vs $host_size, sha $guest_sha vs $host_sha)" >&2
        rm -f "$host_tmp"
        return 1
    fi

    # Full decode (not just a header probe): PIL verifies every chunk CRC and
    # the complete IDAT stream, then the decoded dimensions must agree with
    # the numeric dimensions qdshell reported for the capture buffer.
    dims=$(python3 -c 'import sys
from PIL import Image
with Image.open(sys.argv[1]) as im:
    im.verify()
with Image.open(sys.argv[1]) as im:
    im.load()
    print(im.width, im.height)' "$host_tmp" 2>&1) || {
        echo "ERROR: invalid-shell-capture-png: full decode failed: $dims" >&2
        rm -f "$host_tmp"
        return 1
    }
    read -r width height <<<"$dims"
    if [ "$width" != "$reply_w" ] || [ "$height" != "$reply_h" ]; then
        echo "ERROR: invalid-shell-capture-png: decoded ${width}x${height}" \
             "!= reported ${reply_w}x${reply_h}" >&2
        rm -f "$host_tmp"
        return 1
    fi
    # Identity stability: the compositor that produced this evidence must be
    # the same service MainPID that passed the pre-capture health gate. A
    # restart mid-capture means the pixels' provenance is unknown — refuse.
    pid_after=$(qdwin_compositor_pid) || { rm -f "$host_tmp"; return 1; }
    if [ "$pid_after" != "$pid_before" ]; then
        echo "ERROR: compositor-restarted-mid-capture:" \
             "MainPID $pid_before -> $pid_after" >&2
        rm -f "$host_tmp"
        return 1
    fi
    if ! mv "$host_tmp" "$out"; then
        echo "ERROR: shell-capture-publish-failed: could not move to $out" >&2
        rm -f "$host_tmp"
        return 1
    fi
    # v33 stale marking. Policy: a stale frame may support "the session was
    # showing X before the incident" but never a post-action assertion,
    # unless the action provably predates the retained frame (age_ms).
    # Scenario reports must quote the WARN.
    rm -f "$out.meta"
    case "$reply" in
        *" live=0 age_ms="*)
            local stale_fields
            stale_fields=$(sed -n 's/.* \(live=0 age_ms=[0-9]* msc=[0-9]*\)$/\1/p' <<<"$reply")
            printf '%s\n' "${stale_fields:-live=0}" > "$out.meta"
            echo "WARN: stale-capture: $out is the compositor's RETAINED last frame (${stale_fields:-live=0}), not a fresh repaint — not valid post-action evidence" >&2
            ;;
    esac
    # Record the capture in the gate's ledger BEFORE returning the path, so the
    # frame is attested the moment it becomes visible to the caller. A REFUSAL
    # (the ledger is bound to a different VM) fails the capture: returning an
    # unattested frame as if it were evidence is exactly what the contract
    # forbids, and a silent `|| true` here would restore that.
    if ! capture_attest_frame "$out" "$VMNAME"; then
        echo "ERROR: capture-attestation-refused: $out was not recorded as evidence for $VMNAME" >&2
        rm -f "$out"
        return 1
    fi
    echo "capture=Virtual-1 width=$width height=$height path=$out" >&2
    echo "$out"
}

# The service compositor's stable identity: qdwin-compositor.service MainPID
# (Type=simple, ExecStart=/usr/bin/weston — MainPID IS the compositor).
qdwin_compositor_pid() {
    local pid
    pid=$("$QDWIN_VM_EXEC" "$VMNAME" \
        "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show qdwin-compositor.service -p MainPID --value" \
        2>/dev/null | tr -cd '0-9')
    case "$pid" in ""|0)
        echo "ERROR: qdwin-compositor.service has no MainPID" >&2
        return 1 ;;
    esac
    printf '%s\n' "$pid"
}

# After a capture/copy failure, re-run the FULL session health check (units +
# socket + DRM master) so a qdshell/compositor death mid-capture is classified
# as an L1 environment failure in the log, not a generic capture error; then
# best-effort remove the guest-side temp path.
qdwin_capture_fail_cleanup() {
    local guest=$1
    qdwin_session_healthy >/dev/null || true
    "$QDWIN_VM_EXEC" "$VMNAME" "rm -f '$guest'" >/dev/null 2>&1 || true
}

# ----------------------------------------------------- session sanity
#
# Returns 0 if the qdwin user session is up: the admin wayland socket
# exists, the qdwin-compositor + qdshell user units are active, and the
# compositor owns a DRM-master file (directly or through its seatd broker).
# Mirrors the gui gate liveness probe (qdistro/ci/lib/gates/gui.sh) — the
# old qdshell ctrl-socket ("launcher" command) was removed when qdshell
# moved to Quickshell IPC, so probing it always failed. Use as the first
# step of any scenario.
qdwin_session_healthy() {
    qdwin_require_vm || return 2
    local vt_before
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "test -S /run/user/1000/wayland-1 && runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active --quiet qdwin-compositor.service && runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active --quiet qdshell.service" || {
        echo "ERROR: qdwin-session-unhealthy: socket or user unit unavailable" >&2
        return 1
    }
    # Read the active VT BEFORE the master check so qdwin_vt_stamp can refuse to
    # stamp when it changed underneath us (see there).
    vt_before=$("$QDWIN_VM_EXEC" "$VMNAME" \
        "cat /sys/class/tty/tty0/active 2>/dev/null || true" 2>/dev/null | tr -d '\r\n')
    qdwin_drm_master_ok || return $?
    qdwin_vt_stamp "$vt_before"
}

# Where the guest records the VT the compositor was proven healthy on.
QDWIN_VT_STAMP=${QDWIN_VT_STAMP:-/run/user/1000/qdwin-active-vt}

# Record the VT the compositor currently owns, and re-arm K_OFF on it.
#
# Only ever called right after qdwin_drm_master_ok succeeded. "Holds DRM master
# => the active VT is the compositor's" is NOT true of DRM in general (master is
# not VT-coupled), but it IS true under this stack: seatd is VT-bound and drops
# master synchronously on switch-away, and the kcmp gate in qdwin_drm_master_ok
# proves the master is the seatd-brokered one. The condition is what makes the
# stamp sound. Nothing else records the VT — weston starts with no --tty and
# admin has no logind seat, so the VT is simply whichever was current when seatd
# opened the client, and it appears in no unit, config, or log.
#
# The stamp is written only when the active VT is the SAME before and after the
# master check. Those are separate vm-exec round-trips, hundreds of ms apart, so
# a takeaway inside that window would otherwise stamp the FOREIGN VT — and
# qdwin_vt_recover would then faithfully "recover" into it and declare the wrong
# VT canonical.
#
# It also re-arms K_OFF on that VT. seatd installs K_OFF at session takeover,
# but getty@tty1 shares the VT in profiles that leave it enabled and its
# start-time TTY reset reverts the keyboard to K_UNICODE — at which point the
# kernel console interprets injected chords itself (the openSUSE keymap maps
# Super to Alt and Alt+Left to Decr_Console, so a tile chord becomes a console
# switch). Re-arming here immunizes existing golden images with no rebake, and
# the WARN doubles as detection of the getty-reset race. Best-effort throughout:
# nothing here may fail an otherwise healthy session.
qdwin_vt_stamp() {
    local before="$1"
    "$QDWIN_VM_EXEC" "$VMNAME" '
before='"'$before'"'
now=$(cat /sys/class/tty/tty0/active 2>/dev/null || true)
case "$now" in tty[0-9]*) ;; *) exit 0 ;; esac
if [ -n "$before" ] && [ "$before" != "$now" ]; then
    echo "vt-stamp: active VT changed $before -> $now during the health check; not stamping" >&2
    exit 0
fi
printf %s "$now" > '"'$QDWIN_VT_STAMP'"' 2>/dev/null || true
# Re-arm K_OFF so the kernel console cannot consume injected chords.
python3 - "$now" <<'"'"'PY'"'"'
import fcntl, os, sys, struct
KDGKBMODE, KDSKBMODE, K_OFF = 0x4B44, 0x4B45, 0x04
vt = sys.argv[1]
try:
    fd = os.open("/dev/" + vt, os.O_RDONLY | os.O_NOCTTY)
except OSError:
    sys.exit(0)
try:
    buf = fcntl.ioctl(fd, KDGKBMODE, struct.pack("i", 0))
    mode = struct.unpack("i", buf)[0]
    if mode != K_OFF:
        names = {0: "K_RAW", 1: "K_XLATE", 2: "K_MEDIUMRAW", 3: "K_UNICODE", 4: "K_OFF"}
        try:
            fcntl.ioctl(fd, KDSKBMODE, K_OFF)
            print("WARN: console keyboard on %s was %s, not K_OFF — injected chords "
                  "could VT-switch (kernel keymap: Super=Alt, Alt+Left=Decr_Console). "
                  "Re-armed K_OFF." % (vt, names.get(mode, mode)), file=sys.stderr)
        except OSError as e:
            print("WARN: console keyboard on %s is %s, not K_OFF, and re-arming "
                  "failed (%s) — injected chords may VT-switch."
                  % (vt, names.get(mode, mode), e), file=sys.stderr)
except OSError:
    pass
finally:
    os.close(fd)
PY
' >/dev/null || true
    return 0
}

# Recover a VT takeaway and wait for the session to come back.
#
# Returns 0 only if a switch happened AND health returned. VT_WAITACTIVE returns
# as soon as the VT is active, but everything that makes the session usable
# again is asynchronous after that: seatd re-enables the client, weston's
# handle_enable_seat -> session_notify re-acquires DRM master and re-enables
# libinput devices. A single immediate re-check therefore races that tail and
# would turn a recoverable event into ERROR on a loaded host — so poll.
QDWIN_VT_RECOVER_WAIT_S=${QDWIN_VT_RECOVER_WAIT_S:-6}
qdwin_recover_and_verify() {
    qdwin_vt_recover || return 1
    local deadline=$((SECONDS + QDWIN_VT_RECOVER_WAIT_S))
    while :; do
        if qdwin_session_healthy >/dev/null 2>&1; then
            echo "WARN: recovered-vt-takeaway: the compositor had lost its seat to a VT switch; switched back to the stamped VT and re-verified DRM master. This is an ENVIRONMENT event, not a product result. Re-activation also re-arms K_OFF, so the session is immune to the same chord afterwards." >&2
            return 0
        fi
        [ "$SECONDS" -ge "$deadline" ] && break
        sleep 0.3
    done
    echo "ERROR: vt-recover-insufficient: switched back to the stamped VT but the session did not become healthy within ${QDWIN_VT_RECOVER_WAIT_S}s" >&2
    return 1
}

# Undo an EXTERNAL VT takeaway by switching back to the stamped compositor VT.
#
# Cause-agnostic on purpose: weston.ini now sets vt-switching=false, which
# closes the Ctrl+Alt+Fn keyboard path, but a programmatic VT_ACTIVATE or a
# logind/seatd-initiated switch can still strand the compositor, and the trigger
# in the observed failures was never identified. Switching back re-enables the
# seat (seatd re-activates the client), weston re-acquires DRM master and
# reopens its input devices, and a capture becomes possible again.
#
# Returns 0 only when a switch was actually performed, so the caller retries
# exactly once and a "nothing to recover" case does not loop. Uses the VT
# ioctls directly rather than chvt(1) so no kbd package is required.
qdwin_vt_recover() {
    qdwin_require_vm || return 2
    "$QDWIN_VM_EXEC" "$VMNAME" '
want=$(cat '"'$QDWIN_VT_STAMP'"' 2>/dev/null || true)
now=$(cat /sys/class/tty/tty0/active 2>/dev/null || true)
case "$want" in
    tty[0-9]*) ;;
    *) echo "vt-recover: no stamped compositor VT; cannot recover" >&2; exit 1 ;;
esac
if [ "$want" = "$now" ]; then
    echo "vt-recover: still on the stamped VT $want — the seat was NOT taken by a VT switch; not a recoverable takeaway" >&2
    exit 1
fi
echo "vt-recover: active VT is ${now:-unknown}, switching back to $want" >&2
python3 - "${want#tty}" <<'"'"'PY'"'"'
import fcntl, os, sys
VT_ACTIVATE, VT_WAITACTIVE = 0x5606, 0x5607
n = int(sys.argv[1])
fd = os.open("/dev/tty0", os.O_WRONLY)
try:
    fcntl.ioctl(fd, VT_ACTIVATE, n)
    fcntl.ioctl(fd, VT_WAITACTIVE, n)
finally:
    os.close(fd)
PY
'
}

qdwin_drm_master_ok() {
    qdwin_require_vm || return 2
    "$QDWIN_VM_EXEC" "$VMNAME" '
# Resolve THE service compositor, not the oldest process named weston: a
# stray/leaked weston holding DRM master must not satisfy this gate while
# the actual qdwin-compositor.service process is something else.
pid=$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
    systemctl --user show qdwin-compositor.service -p MainPID --value)
case "$pid" in ""|0|*[!0-9]*)
    echo "ERROR: compositor-not-on-vt: qdwin-compositor.service has no MainPID" >&2
    exit 1 ;;
esac
comm=$(cat /proc/$pid/comm 2>/dev/null || true)
[ "$comm" = "weston" ] || {
    echo "ERROR: compositor-not-on-vt: unit MainPID $pid is ${comm:-gone}, not weston" >&2
    exit 1
}
# Direct DRM openers are recorded with weston tgid in debugfs. Under the
# VM seatd backend the master file description is opened by seatd and
# passed to weston over the seatd socket, so debugfs retains seatd as the
# opener. In that branch, do NOT accept any same-card seatd master row:
# prove with kcmp(2) that the compositor holds the SAME file description
# the master-row seatd process opened (kcmp KCMP_FILE == 0 <=> identical
# struct file), so an unrelated seatd master on the card cannot satisfy L1.
python3 - "$pid" <<'"'"'PY'"'"'
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
def card_fds(pid):
    fds = []
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                t = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if t.startswith("/dev/dri/card"):
                fds.append((int(fd), t))
    except OSError:
        pass
    return fds
wpid = int(sys.argv[1])
wfds = card_fds(wpid)
if not wfds:
    print("ERROR: compositor-not-on-vt: weston has no open /dev/dri/card*",
          file=sys.stderr)
    sys.exit(1)
for wfd, dev in wfds:
    minor = dev[len("/dev/dri/card"):]
    clients = f"/sys/kernel/debug/dri/{minor}/clients"
    try:
        rows = open(clients).read().splitlines()[1:]
    except OSError:
        continue
    for row in rows:
        f = row.split()
        if len(f) < 4 or f[3] != "y":
            continue
        comm, tgid = f[0], int(f[1])
        if tgid == wpid:
            sys.exit(0)          # weston opened the master directly
        if comm == "seatd":
            for sfd, sdev in card_fds(tgid):
                if sdev != dev:
                    continue
                # KCMP_FILE(0): rc 0 => same struct file (same description)
                if libc.syscall(312, tgid, wpid, 0, sfd, wfd) == 0:
                    sys.exit(0)
print("ERROR: compositor-not-on-vt: no DRM master file description held by "
      "the service weston (directly or via its seatd broker)",
      file=sys.stderr)
sys.exit(1)
PY
rc=$?
# On failure, say WHY the master is missing. The bare message above reads as
# "this compositor never had DRM master", but the common cause is the opposite:
# a healthy session that HELD master and then had the seat taken away by a VT
# switch (qdwin/tests/gui/19-wm-policy.md hit this — a switch to tty6 mid-chord
# deactivated the session, so the post-tile capture could not be taken, and the
# generic message sent the investigation looking for a compositor start-up
# fault). Distinguishing the two costs one journal grep and turns a forensic
# dig into a one-line diagnosis.
if [ "$rc" -ne 0 ]; then
    echo "--- drm-master diagnosis ---" >&2
    act=$(cat /sys/class/tty/tty0/active 2>/dev/null || true)
    [ -n "$act" ] && echo "active VT is now: $act" >&2
    jl=$(journalctl _UID=1000 --no-pager -n 500 2>/dev/null || true)
    if printf %s "$jl" | grep -qE "Disabling seat|deactivating session"; then
        echo "VERDICT: seat-taken-away, NOT compositor-never-had-master." >&2
        echo "  The compositor held an active session and then lost it" >&2
        echo "  (libseat Disabling seat / deactivating session below). A VT" >&2
        echo "  switch away from the compositor VT is the usual cause; check" >&2
        echo "  for a getty started on another VT at the same timestamp." >&2
        printf %s "$jl" | grep -E "Disabling seat|deactivating session" \
            | tail -3 | sed "s/^/  /" >&2
        systemctl --no-pager --no-legend list-units --state=active "getty@*" \
            2>/dev/null | sed "s/^/  active getty: /" >&2
    else
        echo "VERDICT: no seat-deactivation in the journal — the compositor" >&2
        echo "  most likely never acquired DRM master (start-up fault)." >&2
    fi
fi
exit $rc'
}

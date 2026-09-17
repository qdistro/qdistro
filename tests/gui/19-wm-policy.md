# 19 — window-manager policy live-apply (v25): capability gate + tile

**What**: validate the v25 window-manager-policy capability surface on a live
qdwin DRM session via the qdshell IPC contract — that the shell binds the
compositor at >= v25 and exposes `wmPolicy` + `keybindRegistration` true (what
flips the qdshell WindowManager settings tab from persist-only to live-apply),
plus one real visual proof that a tile resizes the live client. This is the
qdshell-driven half of the v25 surface; the direct-compositor functional proof
(FIFO-driven `set_wm_policy`/`request_tile`/`request_fullscreen`/`register_hotkey`
against `qdwin-bystander`) lives in its sibling `21-wm-policy-bystander.md`.

**Why**: v25 is what flips the qdshell WindowManager settings tab from
persist-only to live-apply (`CapabilityService.wmPolicy` /
`keybindRegistration`). The IPC capability read is the stable, deterministic
contract; journal strings from `CapabilityService` are diagnostic only.

## Environment

Standard qdwin GUI harness (`tests/gui/AGENTS.md`): a running libvirt domain
on `qemu:///session` with `qdwin-compositor.service` (weston + qdwin-shell.so)
and `qdshell.service` (qdshell). **The session is already fully provisioned by
the GUI gate** — the vendored libweston, qdwin-shell.so, qdshell, and the
qml-plugin are baked into the VM image and the user units are active before the
scenario runs. Do NOT build or deploy anything in-VM; just probe the live
session below. (If a precondition probe fails, that is an ERROR to report, not
a cue to provision.)

This scenario is deliberately lightweight: one deterministic IPC assert plus a
single visual judgment, so it fits the agent budget with room to spare. Write
your `status.txt` PASS as soon as the asserts below hold and STOP — do not run
extra diagnostics or screenshots past the one required tile capture.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdwin_session_healthy \
  || { echo "ERROR: qdwin/qdshell user session not up"; exit 1; }

# qs_ipc <method> [args...] — call a qdwin IPC method on the running qdshell
# instance. Same proven-working invocation as 16/17: `runuser -u admin --
# env … WAYLAND_DISPLAY=wayland-1 qs ipc -p PATH call qdwin …`, with a PID
# fallback if the -p path lookup can't find the instance.
QS_PATH=/usr/share/quickshell/qdshell
# CAPTURE THROUGH A FILE, NEVER THROUGH `$( ... 2>&1 )`. vm-exec bounds its own
# children's fd 1, but `2>&1` inside a command substitution hands fd 2 to the
# substitution's PIPE, and any virsh/jq descendant that outlives vm-exec and
# keeps that descriptor holds the pipe -- and therefore this call -- open
# forever. A read from a regular file reaches EOF at the current end of file
# however many writers still hold it open.
#
# THE FILE IS PER CALL AND UNLINKED BEFORE THE COMMAND RUNS. The earlier shape
# reused ONE file for the primary call and the PID fallback, which is the same
# isolation error vm-exec's own bounded_run fixed one level inward: a descendant
# of the first call that still holds fd 2 on that inode appends into the second
# call's capture, and here that can corrupt both the fallback decision and the
# IPC response this function returns. Each call now opens its own mktemp name
# and unlinks it before the command starts, so no later call can be handed an
# inode an earlier call's survivor still holds, and no capture is left NAMED
# while a command is running.
#
# The read is also ceilinged so a runaway command cannot pull unbounded output
# into a variable. This helper does not reap or size-bound a surviving writer;
# the only size bound on the (now nameless) inode is the RLIMIT_FSIZE vm-exec
# installs on its own children.
QD_IPC_CAP_BYTES=${QD_IPC_CAP_BYTES:-65536}
# Not a positive decimal integer => not a byte ceiling: `head -c -1` is
# "all but the last byte" and `head -c 00` reads nothing.
case "$QD_IPC_CAP_BYTES" in
    ''|*[!0-9]*|0|0*)
        echo "QD_IPC_CAP_BYTES must be a positive integer number of bytes, got '$QD_IPC_CAP_BYTES'" >&2
        exit 2 ;;
esac
qd_cap() {  # qd_cap <cmd...> -> runs <cmd> in the VM, prints its merged output
    local cf wfd rfd out="" rc=0 _qc_size=""
    cf=$(mktemp "${TMPDIR:-/tmp}/qd19-cap.XXXXXXXX") || return 125
    # Every setup step is CHECKED. Unchecked, a failing open or unlink let this
    # function run the command anyway and return its status, leaving the capture
    # NAMED for the whole run -- the opposite of what the unlink is for.
    # Reproduced by injecting a failing rm: it returned 0, printed COMMAND-RAN
    # and left the file (sol, qci-A-260917-sol-review.md section 3).
    exec {wfd}>"$cf" || { rm -f "$cf"; return 125; }
    exec {rfd}<"$cf" || { exec {wfd}>&-; rm -f "$cf"; return 125; }
    rm -f "$cf" || { exec {wfd}>&- {rfd}<&-; return 125; }
    # The command and every descendant get the capture FILE on fd 1 and fd 2;
    # the bookkeeping descriptors are closed for the child. Note that the
    # command substitution this function is normally called in never reaches
    # vm-exec: its pipe is only ever on THIS shell's fd 1.
    "$QDWIN_VM_EXEC" "$VMNAME" "$@" >&"$wfd" 2>&"$wfd" {wfd}>&- {rfd}<&- || rc=$?
    exec {wfd}>&-
    # A failed replay is a capture failure, not empty output (sol A3 f3).
    if ! out=$(head -c "$QD_IPC_CAP_BYTES" <&"$rfd"); then
        echo "capture replay FAILED; output unavailable, not empty" >&2
        exec {rfd}<&-
        return 125
    fi
    # Overflow must not be silent: these captures carry CONTROL information
    # (the `no running instance|No such` PID fallback, state/geometry tokens),
    # and a marker past the cap reads exactly like a marker that never
    # appeared -- so the fallback is skipped and the step reports success on a
    # reply it never fully saw (sol, qci-A2-260917-sol-review.md section 2).
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
    if ! _qc_size=$(stat -Lc %s "/proc/self/fd/$rfd" 2>/dev/null); then
        echo "could not stat the capture; completeness is UNKNOWN, not verified" >&2
        exec {rfd}<&-
        return 125
    fi
    if [ "$_qc_size" -gt "$QD_IPC_CAP_BYTES" ]; then
        echo "capture (${_qc_size} bytes) exceeded $QD_IPC_CAP_BYTES bytes; reply INCOMPLETE" >&2
        exec {rfd}<&-
        return 125
    fi
    exec {rfd}<&-
    printf '%s' "$out"
    return "$rc"
}
qs_ipc() {
    # A CAPTURE failure (125 from qd_cap: setup, overflow or a failed
    # replay) is not a guest answer and must not be printed as one -- it
    # reads as 'the command ran and said nothing', which is how the
    # `no running instance` PID fallback gets silently skipped (sol,
    # qci-A3-260917-sol-review.md finding 2).
    local out pid _qc=0
    out=$(qd_cap \
        "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
         qs ipc -p $QS_PATH call qdwin $*") || _qc=$?
    if printf '%s' "$out" | grep -qiE 'no running instance|No such'; then
        # Plain `$( ... )` with NO `2>&1`: vm-exec's fd 2 is this script's
        # stderr, not the substitution pipe, so a surviving descendant cannot
        # hold this call open. vm-exec's own children get a capture file on
        # fd 1, so the pipe's only holder is vm-exec itself.
        pid=$("$QDWIN_VM_EXEC" "$VMNAME" \
            "pgrep -u admin -f 'qs -p $QS_PATH' | while read p; do \
               grep -q dbus-run-session /proc/\$p/cmdline 2>/dev/null || { echo \$p; break; }; done")
        [ -n "$pid" ] || { printf '%s\n' "$out"; return 1; }
        out=$(qd_cap \
            "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
             qs ipc --pid $pid call qdwin $*") || _qc=$?
    fi
    if [ "$_qc" = 125 ]; then
        echo "qs_ipc: capture failed for '$*' (no usable reply)" >&2
        return 125
    fi
    printf '%s\n' "$out"
}
```

## Step 1 — readiness gate + capability assert (deterministic, load-bearing)

Poll the qdshell IPC capability probe until the shell reports a fully-bound
v25+ session, then assert the v25 capability flags. This is the same
readiness-gate + IPC-contract shape the migrated siblings (16/17/18) use; it is
the load-bearing assertion of this scenario.

```bash
CAPS=
for _ in $(seq 1 30); do
    CAPS=$(qs_ipc capabilities)
    ver=$(printf '%s' "$CAPS" | sed -nE 's/.*version=([0-9]+).*/\1/p')
    case "$CAPS" in
        *bound=true*) [ -n "$ver" ] && [ "$ver" -ge 25 ] && break ;;
    esac
    sleep 1
done
echo "capabilities: $CAPS"
```

**Assert (1.1):** `$CAPS` contains `bound=true` and `version=` >= 25
(the deployed build binds at v28). The shell reached a live qdwin binding.

**Assert (1.2):** `$CAPS` contains `wmPolicy=true` AND `keybindRegistration=true`
— the WindowManager settings tab is live-apply, not persist-only. HARD.

If `bound=true` never appears, the qdshell↔qdwin binding is not reachable —
record ERROR (precondition), not a product FAIL.

Before doing any visual assertion, take a quick screenshot and confirm it is
the qdwin graphical session, not a Linux tty/login screen. If the framebuffer
capture is on the wrong VT, record ERROR and stop; a tty screenshot cannot
prove or disprove tiling.

## Step 2 — one visual proof: a tile resizes the live client

Spawn a known test client, drive the default registered tile-left shortcut
(Super+Left), and confirm the real client (not just chrome) moved to the left
half. This is the single required visual judgment; capture exactly one
screenshot for it.

Tile is NOT an IPC method — the WM tiles ride the registered keyboard shortcut
(`keybindRegistration`): WindowManagerService registers `Super+Left → tile-left`
(qdshell default `windowManager.shortcutTileLeft`) once the shell binds at
>= v25, and dispatches it to `Qdwin.requestTileHandle(focusedHandle, 1)`. So we
focus the test window and send the real-keyboard chord `Super+Left` via
`qdwin_chord`.

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v qdistro-test-window >/dev/null' \
    || { echo "ERROR: qdistro-test-window not installed (cannot drive tile)"; exit 1; }
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[q]distro-test-window" 2>/dev/null; sleep 1' >/dev/null

CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
"$QDWIN_VM_EXEC" "$VMNAME" \
    "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title 'qd19-tile' \
     --width 400 --height 260 --color 0xff304050 >/tmp/qd19-tile.log 2>&1"
# Wait for the map itself rather than a fixed sleep.
HANDLE=
for _ in $(seq 1 20); do
    HANDLE=$("$QDWIN_VM_EXEC" "$VMNAME" \
      "journalctl _UID=1000 --after-cursor='$CURSOR' --no-pager | \
       grep -E 'qdwin: toplevel_added handle=[0-9]+ uid=1000 pid=[0-9]+ app_id=qdistro-test-window' | \
       tail -1 | sed -nE 's/.*handle=([0-9]+).*/\1/p'")
    [ -n "$HANDLE" ] && break
    sleep 0.5
done
[ -n "$HANDLE" ] || { echo "ERROR: test window never mapped (precondition)"; exit 1; }
# Brief settle for keyboard focus to land on the new toplevel (map-time
# activation has no journal line to wait on) before driving the chord.
sleep 1

# The newly-mapped window holds keyboard focus; drive the default registered
# tile-left shortcut. Super = Meta (qcode meta_l). Use qdwin_chord (real-keyboard
# sequence) — a modifier+key chord needs the modifier-release transition, see
# AGENTS.md "Why two key paths".
# Take a fresh cursor first: configure_extent also fires on map/inset paths,
# so the post-tile wait below must only see lines emitted after the chord.
TILE_CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
qdwin_chord meta_l -- left
# Deterministic post-action wait (no fixed sleep): the resize is in flight once
# the compositor logs the configure_extent it sent for this handle; the
# capture's own damage-repaint then guarantees fresh content.
for _ in $(seq 1 20); do
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "journalctl _UID=1000 --after-cursor='$TILE_CURSOR' --no-pager | \
         grep -q 'qdwin: configure_extent handle=$HANDLE '" && break
    sleep 0.5
done
qdwin_screenshot /tmp/19-tile-left.png
```

**Assert (2.1):** the journal shows `qdwin: tile handle=$HANDLE edge=left`
with the outer geometry at the left half of the output **work area** — the
output minus the top-bar exclusive zone, exactly like maximize (only
`request_fullscreen` covers the full output at `(0,0)`). On this fixed
1280x800 GUI profile the qdshell bar reserves 31px at the top, so the required
line is `outer=640x769 at (0,31)` (left half width 640; height 800−31=769;
origin y=31, below the bar). The screenshot should show the
`qd19-tile` window occupying the left half of the output (the client itself
resized — not just chrome moved). If the journal tile line is present but
the screenshot path is capturing the wrong VT/tty, record that as visual
evidence unavailable and keep Step 2 passing on the deterministic journal
proof. If the registered shortcut can't be driven (e.g. the user changed
`shortcutTileLeft` away from `Super+Left`), record SKIP/ERROR for Step 2
only — Step 1 is the mandatory deterministic gate.

### Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[q]distro-test-window" 2>/dev/null; true' >/dev/null
```

## Pass criteria

Step 1 (1.1 + 1.2) mandatory and deterministic — this is the v25 capability
contract. Step 2 (2.1) is the single visual proof; SKIP allowed only if no tile
path can be driven. Write `status.txt` PASS once 1.1 + 1.2 hold (and 2.1 passes
or is a justified SKIP) and STOP.

## Known-broken-if

- 1.2 `wmPolicy=false`/`keybindRegistration=false` while `bound=true
  version>=25`: the capability flip on bind didn't fire. Check
  `Services/Qdwin/Qdwin.qml`'s `onBoundChanged` sets
  `CapabilityService.setWmPolicy`/`setKeybindRegistration` on `shellVersion >= 25`.
- 1.1 never reaches `bound=true`: the qml-plugin never bound qdwin_shell_v1.
  Check `qs ipc list` shows the `qdwin` target and the compositor advertises
  the global. Record ERROR, not FAIL.
- 2.1 chrome moves but the client doesn't resize: `apply_inset → set_size`
  didn't reach the client — a real compositor defect (mirror of the headless
  `tests/host/13-wm-policy.md` resize assertion).

## Not covered here

The direct-compositor functional proof (bystander as shell: `set_wm_policy`
focus/placement/snap, `request_tile` left/right/restore, `request_fullscreen`
fill/restore, `register_hotkey`) is exercised in `21-wm-policy-bystander.md`.
Focus-follows-mouse retarget-delay and edge-snapping during an interactive drag
remain out of scope (timing-sensitive).

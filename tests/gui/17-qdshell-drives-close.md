# 17 — qdshell drives request_close via qdwin_shell_v1

**Acceptance criterion:** qdshell's `Qdwin.closeWindow(window)` (the
Q_INVOKABLE on the `QdwinBinding` exposed by the
`Qdistro.Qdwin` plugin) sends `qdwin_shell_v1.request_close(handle)`,
qdwin honors it, the target toplevel exits cleanly, and a
`toplevel_removed` event propagates back to the shell — closing the
loop end-to-end. Before workstream A, `Services/Qdwin/Qdwin.qml`'s
`closeWindow(window)` was a literal TODO stub.

## Prerequisites

Same as `16-qdshell-binding-protocol-events.md`. Additionally:

- A way to invoke `Qdwin.closeWindow(window)` from outside qs. We use
  the `IPCService` that qdshell already wires for testing
  (`Modules/IPC/...` exposes a generic command channel). Concretely,
  the agent runs:

  ```bash
  runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-1 \
    qs ipc -p /usr/share/quickshell/qdshell call qdwin closeWindow HANDLE
  ```

  inside the admin user session. Note the proven-working shape (mirrors
  `qdshell/tests/ui/runner.py`'s `ipc_vm`): `runuser -u admin -- env …`
  (NOT `runuser -l`, whose login shell scrubs the env), an explicit
  `WAYLAND_DISPLAY=wayland-1`, and `-p PATH` placed AFTER the `ipc`
  subcommand (`qs ipc -p PATH call …`), not before it.

Fail loudly if the IPC isn't available; do not skip.

Additionally:

- `qdistro-test-window` on the VM PATH (the same reliable test client
  the qdwin SMOKES use; it deterministically produces a qdwin toplevel
  with `app_id=qdistro-test-window`). This scenario uses it as the
  close target instead of `foot`, which is not installed on the VM.

## Setup

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdwin_session_healthy || { echo "FAIL: session not up"; exit 1; }

# qs_ipc <method> [args...] — call a qdwin IPC method on the running qdshell
# instance. Uses the proven-working invocation (runner.py ipc_vm shape):
# `runuser -u admin -- env … WAYLAND_DISPLAY=wayland-1 qs ipc -p PATH call …`.
# If the -p path-based lookup can't find the -p-launched instance, fall back
# to PID targeting (`qs ipc --pid PID call …`), resolving the qs child pid
# (the non-dbus-run-session process under admin).
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
    cf=$(mktemp "${TMPDIR:-/tmp}/qd17-cap.XXXXXXXX") || return 125
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

qs_ipc capabilities \
    | grep -q 'bound=true' \
    || { echo "FAIL: qs ipc bridge or qdwin binding not reachable"; exit 1; }

# PRECONDITION (infra): the test client must be installed. Absence is an
# ERROR (the scenario cannot be exercised), NOT a product FAIL.
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v qdistro-test-window >/dev/null' \
    || { echo "ERROR: qdistro-test-window not installed on VM (cannot drive close)"; exit 1; }

"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[q]distro-test-window" 2>/dev/null; sleep 1' >/dev/null
```

## Steps

### Step 1 — spawn a target toplevel

```bash
CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
# setsid -f detaches the client into its own session so it survives the
# vm_exec shell returning. Same launch pattern the qdwin smokes use.
"$QDWIN_VM_EXEC" "$VMNAME" \
    "setsid -f runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
     WAYLAND_DISPLAY=wayland-1 qdistro-test-window --title 'qd17-target' \
     --width 300 --height 180 --color 0xff304050 >/tmp/qd17-target.log 2>&1"
sleep 2
HANDLE=$("$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 --after-cursor='$CURSOR' --no-pager | \
   grep -E 'qdwin: toplevel_added handle=[0-9]+ uid=1000 pid=[0-9]+ app_id=qdistro-test-window' | tail -1 | \
   sed -nE 's/.*handle=([0-9]+).*/\1/p'")
[ -n "$HANDLE" ] || { echo "ERROR: no toplevel_added handle (test window never mapped)"; exit 1; }
```

### Step 2 — drive close via QML IPC

```bash
CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
qs_ipc closeWindow "$HANDLE"
sleep 1
```

**Assert (2.1):** a `qdwin: request_close handle=$HANDLE` log line
appears in the journal after `$CURSOR`. This proves the QML side
issued the `qdwin_shell_v1.request_close` request and the
compositor processed it.

**Assert (2.2):** within 2s of the above, `qdwin: toplevel_removed
handle=$HANDLE` appears in the journal — the test window exited
cleanly in response to xdg_toplevel.close.

**Assert (2.3):** `qdwin: seat_focus_changed seat=default
handle=4294967295` (or to a surviving sibling) appears within
500ms of `toplevel_removed`, confirming the focus-recovery idle
ran on the shell-driven close path the same way it would for a
user-initiated close.

### Step 3 — Verify no orphan in qdshell windows model

If the agent has visual access, screenshot the bar's
ActiveWindow widget. After the close:

**Assert (3.1):** the bar reflects "no active window" (no title
shown, or a placeholder). Mirrors the
`focusedWindowIndex === -1` state in the QML singleton.

### Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -f "[q]distro-test-window" 2>/dev/null; true' >/dev/null
```

## Pass criteria

2.1, 2.2, 2.3 mandatory. 3.1 soft.

## Known-broken-if

- 2.1 silent: the IPC call reached the QML side but
  `qdwinBinding.closeWindow` didn't fire the request. Check
  `Services/Qdwin/Qdwin.qml`'s `IpcHandler { target: "qdwin" }`
  and confirm `qs_ipc capabilities` reports `bound=true`.
- 2.2 fires but 2.1 silent: that's impossible — qdwin can't kill
  a toplevel without an originating request, unless the
  qdistro-test-window process itself exited on its own. Re-check
  $HANDLE.
- 2.3 silent: focus-recovery didn't run, or it ran but didn't
  emit `seat_focus_changed`. The latter would imply the binding
  unwound mid-request — check for `qdwin-binding: error:` lines
  in the journal between Step 2 and 3.

## Why agent-driven

The IPC mechanism (`qs ipc call qdwin ...`) is the supported
Quickshell test surface for this scenario. Step 3 (visual
confirmation of the bar's empty state) still benefits from
agent-side screenshot/OCR.

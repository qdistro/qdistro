# 20 — idle / DPMS live apply (v26): display power + ext-idle-notify

**What**: validate the v26 idle/DPMS path on a live qdwin DRM session — the
`set_display_power` request (DPMS all outputs off/on) and the capability gate
the qdshell Power tab's inactivity-action + display-off timers ride on (driven
by the standard `ext-idle-notify-v1`).

**Why**: v26 flips the Power tab's idle policy from persist-only to live
(`CapabilityService.idleDpms`). The shell owns the idle *timing*
(ext-idle-notify) and the inactivity *action* (suspend/lock — a session
decision); the compositor only enacts display power via `set_display_power`.
`CapabilityService.idleDpms` is true only when BOTH halves are present: a >= v26
shell bind (`set_display_power`) AND the ext-idle-notify client (an
`ext_idle_notifier_v1` + a `wl_seat` bound by the qml-plugin).

## Environment

Standard qdwin GUI harness (`tests/gui/AGENTS.md`): a running libvirt domain on
`qemu:///session` with `qdwin-compositor.service` (weston + qdwin-shell.so) and
`qdshell.service` (qdshell). **The session is already fully provisioned by the
GUI gate** — the vendored libweston, qdwin-shell.so, qdshell, and the qml-plugin
are baked into the VM image (built fresh from the host source tree for this run)
and the user units are active before the scenario runs. Do NOT build or deploy
anything in-VM; just probe the live session. (If a precondition probe fails,
that is an ERROR to report, not a cue to provision.)

> Note on service names: the legacy `noctalia-session.service` /
> `noctalia-shell.service` units were retired (2026-06-16). The deployed
> contract is `qdwin-compositor.service` + `qdshell.service` +
> `qdwin-session.target`, and capabilities are read via the qdshell IPC
> (`qs ipc call qdwin capabilities`), NOT by grepping journald for unit logs.

## Path A — capability gate (qdshell IPC, deterministic)

Read the idle/DPMS capability through the stable qdshell IPC contract, gated on
a fully-bound v26+ session.

```bash
source ${QDWIN_REPO}/tests/gui/qdwin-helpers.sh
qdwin_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdwin_session_healthy || { echo "ERROR: qdwin/qdshell user session not up"; exit 1; }

# qs_ipc <method> [args...] — call a qdwin IPC method on the running qdshell
# instance. Same proven-working invocation as 16/17/19 (`runuser -u admin --
# env … WAYLAND_DISPLAY=wayland-1 qs ipc -p PATH call qdwin …`), with a PID
# fallback if the -p path lookup can't find the instance.
QS_PATH=/usr/share/quickshell/qdshell
# CAPTURE THROUGH A FILE, NEVER THROUGH `$( ... 2>&1 )`. That shape hands
# vm-exec's fd 1 AND fd 2 to the substitution's pipe. vm-exec redirects its own
# children's fd 1 to an internal capture file, but fd 2 is inherited straight
# through to every virsh/jq descendant it starts; one that outlives vm-exec and
# keeps that descriptor holds the pipe -- and this call -- open long after the
# guest command is dead, and an outer `timeout` cannot help because the shell is
# blocked on the read. A read from a regular file reaches EOF at the current end
# of file however many writers still hold it open.
#
# The capture is per call and UNLINKED before the command starts, so a survivor
# of the primary call cannot append into the fallback call's capture (which
# would corrupt both the fallback decision and the returned IPC response), and
# no capture is left NAMED while a command runs. Same shape as bounded_run() in
# qdistro/scripts/vm/vm-exec.
# The read is ceilinged in BYTES, with `head -c`. It was `read -N` until
# 2026-09-17, which ceilings CHARACTERS: bash drops NUL bytes and they do not
# count, so a NUL-bearing capture was read PAST the ceiling. The comment here
# also claimed that could not hang, "because a read of a regular file returns
# at the current EOF however many writers still hold it open" -- that is WRONG,
# EOF is re-tested on every read, and a 256 KiB nominal replay was measured at
# 9.57s against a writer staying ahead of it.
# This adds no size bound of its own: the only limit on the nameless inode is
# the RLIMIT_FSIZE vm-exec installs on its own children.
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
    cf=$(mktemp "${TMPDIR:-/tmp}/qd20-cap.XXXXXXXX") || return 125
    # Every setup step is CHECKED. Unchecked, a failing open or unlink let this
    # function run the command anyway and return its status, leaving the capture
    # NAMED for the whole run -- the opposite of what the unlink is for.
    # Reproduced by injecting a failing rm: it returned 0, printed COMMAND-RAN
    # and left the file (sol, qci-A-260917-sol-review.md section 3).
    exec {wfd}>"$cf" || { rm -f "$cf"; return 125; }
    exec {rfd}<"$cf" || { exec {wfd}>&-; rm -f "$cf"; return 125; }
    rm -f "$cf" || { exec {wfd}>&- {rfd}<&-; return 125; }
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
        # Plain `$( ... )` with NO host stderr redirect: vm-exec's fd 2 is
        # this script's stderr, not the substitution pipe, so a surviving
        # descendant cannot hold this call open -- vm-exec's own children get a
        # capture file on fd 1, so the pipe's only holder is vm-exec itself.
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

# Readiness gate: poll until the shell reports a fully-bound v26+ session.
CAPS=
for _ in $(seq 1 30); do
    CAPS=$(qs_ipc capabilities)
    ver=$(printf '%s' "$CAPS" | sed -nE 's/.*version=([0-9]+).*/\1/p')
    case "$CAPS" in
        *bound=true*) [ -n "$ver" ] && [ "$ver" -ge 26 ] && break ;;
    esac
    sleep 1
done
echo "capabilities: $CAPS"
```

**Assert (A.0):** `$CAPS` contains `bound=true` and `version=` >= 26 (the
deployed build binds at v28). If `bound=true` never appears, the qdshell↔qdwin
binding is unreachable — record ERROR (precondition), not a product FAIL.

**Assert (A.1 — idle/DPMS capability, robust read):** the idle/DPMS capability
is live. Read it deterministically, preferring the IPC field and falling back
to the journal transition line:

```bash
# Preferred: the v26 IPC `idleDpms=` field (added to qdwin capabilities()).
# Present whenever the image was built from a qdshell that carries the field
# (the GUI golden is built fresh from host source for the run, so it normally
# is). If the field is absent (an older baked image predating it), fall back
# to the journal transition line emitted by CapabilityService on bind.
if printf '%s' "$CAPS" | grep -q 'idleDpms='; then
    printf '%s' "$CAPS" | grep -q 'idleDpms=true' \
        && echo "A.1 PASS (IPC idleDpms=true)" \
        || { echo "FAIL: idleDpms=false in IPC capabilities ($CAPS)"; exit 1; }
else
    # Fallback: restart qdshell for a clean bind, then poll the user journal
    # for the `idleDpms -> true` transition (module tag is the 14-char-padded
    # `CapabilityServ`). This line is emitted from Qdwin.qml's onBoundChanged /
    # onIdleNotifierAvailableChanged once both halves are present.
    echo "IPC idleDpms field absent (older image) — falling back to journal transition"
    CUR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
      --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")
    "$QDWIN_VM_EXEC" "$VMNAME" \
        "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
         systemctl --user restart qdshell.service"
    ok=
    for _ in $(seq 1 40); do
        "$QDWIN_VM_EXEC" "$VMNAME" \
          "journalctl _UID=1000 --after-cursor='$CUR' --no-pager 2>/dev/null" \
          | grep -q 'idleDpms -> true' && { ok=1; break; }
        sleep 1
    done
    [ -n "$ok" ] && echo "A.1 PASS (journal idleDpms -> true)" \
        || { echo "FAIL: no idleDpms=true (IPC field absent and no journal transition)"; exit 1; }
fi
```

This single read proves all three requirements held in the real session: a
>= v26 shell bind (`set_display_power`), `ext_idle_notifier_v1` bound, and a
`wl_seat` bound — i.e. the qml-plugin's ext-idle-notify client connected.

## Path B — compositor functional proof (bystander as shell)

Drive the compositor's `set_display_power` directly on the live DRM session,
independent of qdshell's init, using `qdwin-bystander` over its FIFO. Uses the
shared, self-healing take-over helpers in `tests/apps/qdwin-apps-helpers.sh`
(do NOT hand-roll the `systemctl stop` + bystander launch; the canonical helper
sets `XDG_RUNTIME_DIR=/run/user/1000`, `WAYLAND_DISPLAY` to the live socket, and
the FIFO at `/run/user/1000/qdwin-cmd.fifo`).

```bash
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
# Resolve VMNAME independently so Path B is runnable on its own (e.g. an agent
# debugging just the bystander proof), not only after Path A set it.
VMNAME="${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
qdwin_apps_set_vm "$VMNAME"

# PRECONDITION (infra): the bystander driver must be installed. Absence is an
# ERROR (cannot exercise the scenario), NOT a product FAIL. (The driver IS
# present on the baked image; a "no binary" reading is usually a stray
# `systemctl stop` erroring first because XDG_RUNTIME_DIR was unset — the
# helper exports it.)
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v qdwin-bystander >/dev/null' \
    || { echo "ERROR: qdwin-bystander not installed on VM (cannot drive DPMS)"; exit 1; }

# Take over the shell role with the bystander; restore qdshell on ANY exit.
# Arm the restore trap IMMEDIATELY after a successful takeover (before the
# session-health check) so a later failure never leaves the desktop headless.
qdwin_apps_become_shell || { echo "ERROR: could not take over shell role"; exit 1; }
trap 'qdwin_apps_restore_shell' EXIT
qdwin_apps_session_up   || { echo "ERROR: bystander session not healthy"; exit 1; }

CURSOR=$("$QDWIN_VM_EXEC" "$VMNAME" "journalctl _UID=1000 -n 1 \
  --show-cursor --no-pager 2>/dev/null | tail -1 | sed 's/^-- cursor: //'")

qdwin_apps_ctl displaypower 0    # DPMS all outputs off
sleep 0.5
qdwin_apps_ctl displaypower 1    # DPMS all outputs on
sleep 1

LOGS=$("$QDWIN_VM_EXEC" "$VMNAME" \
  "journalctl _UID=1000 --after-cursor='$CURSOR' --no-pager 2>/dev/null")
printf '%s\n' "$LOGS" | grep -E 'qdwin: set_display_power'
```

**Assert (B.1):** `qdwin: set_display_power on=0 (N output…)` then
`on=1 (N output…)` appear in the compositor journal since `$CURSOR` — the real
(virtual) output was power-cycled.
**Assert (B.2):** no `qdwin_shell_v1: … protocol error` between `$CURSOR` and
now (the bystander bound the shell role cleanly at >= v26).

## Cleanup

```bash
qdwin_apps_restore_shell   # also fires on the EXIT trap; idempotent — restarts qdshell.service
```

## Pass criteria

Path A (A.0 + A.1) mandatory and deterministic — this is the v26 capability
contract. Path B (B.1 + B.2) mandatory — the compositor functional proof. Write
`status.txt` PASS once both hold and STOP.

## Known-broken-if

- A.1 IPC `idleDpms=false` while `bound=true version>=26`: one half of the gate
  is missing. Check `Services/Qdwin/Qdwin.qml`'s `_refreshIdleDpmsCapability`
  (needs `bound && shellVersion >= 26 && idleNotifierAvailable`) — likely the
  ext-idle-notify client failed to bind `ext_idle_notifier_v1` + a `wl_seat`.
  Confirm with `wayland-info | grep -E 'qdwin_shell_v1|ext_idle_notifier_v1|wl_seat'`.
- A.1 fell back to the journal but no `idleDpms -> true`: the bind happened
  before `ext_idle_notifier_v1` arrived and the re-evaluation on
  `onIdleNotifierAvailableChanged` didn't fire — a real qdshell/binding defect.
- B.1 silent: `set_display_power` never reached the compositor. Confirm the
  bystander bound at >= v26 (B.2) and the FIFO command was accepted
  (`qdwin-bystander: cmd displaypower on=0` in `/tmp/bystander.log`).
- The desktop is left headless after the run: the EXIT trap /
  `qdwin_apps_restore_shell` didn't restart `qdshell.service`. Restart it
  manually before reporting.

## Not covered here

The full idle *trigger* (wait N minutes → `idled` → action / DPMS-off) is
timing-bound; the executable smoke `agent-idle-dpms-recovery-smoke.sh` covers
it live (short `power.displayOff*` / `inactivityTimeout*`, watch the
`PowerService` "idle policy armed" + "display-off idle -> DPMS off" lines, then
move the pointer to fire `resumed` → `set_display_power(1)`). Presentation mode
(`IdleInhibitorService`) suppressing both is verified there too.

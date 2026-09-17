# 10 — small-toolkit roundtrip: Tk, FLTK, Java Swing

**Acceptance criterion:** three "small toolkit" demo programs each
launch via XWayland, render their widgets correctly, and survive the
bystander's max/restore round-trip. The demos are
`tests/apps/demos/{tk-demo.py,fltk-demo.cxx,SwingDemo.java}` — checked
in to the repo next to this scenario. Each step stages the demo into
the VM via a private, free-port host HTTP server rooted at that
`demos/` directory (so `wget $DEMOS/<file>` fetches the tracked
source).

## Setup

```bash
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_apps_set_vm "${VMNAME}"

# qci supplies a durable per-run directory. Standalone runs get a unique
# fallback which is printed for retrieval; no host or guest path is shared
# with a concurrent invocation.
CASE_ARTIFACT_DIR=${QCI_GUI_ARTIFACT_DIR:-$(mktemp -d /tmp/qdwin-apps-10.XXXXXX)}
mkdir -p "$CASE_ARTIFACT_DIR"/{logs,screenshots}
CASE_TOKEN=$(basename "$CASE_ARTIFACT_DIR" | tr -cd 'A-Za-z0-9_.-')
CASE_GUEST_DIR=/tmp/qdwin-apps-10-$CASE_TOKEN
DEMOS_HTTP_PID=""
DEMOS_PORT_FILE=""
CASE_INFRA=0

# Arm cleanup before creating guest staging or launching any process. Preserve
# the scenario's original status, but always remove transient state and return
# the VM to its normal qdshell session. The artifact directory is evidence and
# intentionally survives cleanup.
qdwin_apps_10_kill_case() {
    [ -n "${CASE_GUEST_DIR:-}" ] || return 0
    "$QDWIN_VM_EXEC" "$VMNAME" "
kill_tracked() {
    pidfile=\$1; expected=\$2
    [ -r \"\$pidfile\" ] || return 0
    pid=\$(cat \"\$pidfile\")
    case \"\$pid\" in ''|*[!0-9]*) return 0;; esac
    [ -r \"/proc/\$pid/status\" ] || return 0
    uid=\$(awk '/^Uid:/{print \$2}' \"/proc/\$pid/status\")
    cmdline=\$(tr '\\0' ' ' < \"/proc/\$pid/cmdline\")
    [ \"\$uid\" = 1000 ] || { echo \"kept pid=\$pid: uid=\$uid expected=1000\"; return 0; }
    case \"\$cmdline\" in
        *\"\$expected\"*)
            kill -9 \"\$pid\" 2>/dev/null || true
            echo \"killed tracked pid=\$pid expected=\$expected cmdline=\$cmdline\"
            ;;
        *) echo \"kept tracked pid=\$pid: expected=\$expected cmdline=\$cmdline\";;
    esac
}
kill_tracked '$CASE_GUEST_DIR/tk.pid' '$CASE_GUEST_DIR/tk-demo.py'
kill_tracked '$CASE_GUEST_DIR/fltk.pid' '$CASE_GUEST_DIR/fltk-demo'
kill_tracked '$CASE_GUEST_DIR/swing.pid' 'java SwingDemo'
true
" >>"$CASE_ARTIFACT_DIR/logs/tracked-cleanup.log" 2>&1 || true
}

qdwin_apps_10_cleanup() {
    local status="${1:-0}"
    qdwin_apps_10_kill_case
    [ -n "${CASE_GUEST_DIR:-}" ] && \
        "$QDWIN_VM_EXEC" "$VMNAME" "rm -rf '$CASE_GUEST_DIR'" 2>/dev/null || true
    qdwin_apps_restore_shell || true
    [ -n "${DEMOS_PORT_FILE:-}" ] && rm -f "$DEMOS_PORT_FILE" || true
    [ -n "${DEMOS_HTTP_PID:-}" ] && kill "$DEMOS_HTTP_PID" 2>/dev/null || true
    DEMOS_HTTP_PID=""
    DEMOS_PORT_FILE=""
    trap - EXIT
    return "$status"
}
trap 'qdwin_apps_10_status=$?; qdwin_apps_10_cleanup "$qdwin_apps_10_status"; exit "$qdwin_apps_10_status"' EXIT

qdwin_apps_session_up || { echo "FAIL: bystander/weston not healthy"; exit 1; }
"$QDWIN_VM_EXEC" "$VMNAME" "install -d -o admin -g admin -m 700 '$CASE_GUEST_DIR'"
echo "scenario artifacts: $CASE_ARTIFACT_DIR"

# This GUI lane is defined for the baked 1280x800 mode. Gate that assumption
# explicitly rather than hard-coding geometry in a later assertion without
# proving the precondition.
qdwin_apps_screenshot "$CASE_ARTIFACT_DIR/screenshots/output-precondition.png"
if ! command -v identify >/dev/null 2>&1; then
    echo "INFRA: ImageMagick identify is required to verify the 1280x800 GUI CI output"
    exit 1
fi
CASE_OUTPUT_GEOMETRY=$(identify -format '%wx%h' "$CASE_ARTIFACT_DIR/screenshots/output-precondition.png" 2>/dev/null || true)
if [ "$CASE_OUTPUT_GEOMETRY" != 1280x800 ]; then
    echo "INFRA: scenario 10 requires GUI CI output 1280x800; observed ${CASE_OUTPUT_GEOMETRY:-unreadable}"
    exit 1
fi

# App-deps gate (opt-in): Tk/FLTK/Swing are heavy toolkit deps that are only
# baked into the QDWIN_APP_DEPS golden, not the lean GUI golden. Detect each
# toolkit's build/run prerequisite IN THE VM up front:
#   - Tk    : `python3 -c 'import tkinter'`   (python313-tk)
#   - FLTK  : `g++` + the FLTK dev headers    (fltk-devel, pulls gcc-c++)
#   - Swing : `javac`                          (java-25-openjdk-devel)
# If NONE are installed this is a lean golden -> clean SKIP (never ERROR/FAIL),
# exactly like the chromium/vlc scenarios. Mirrors tests/apps/AGENTS.md:
# "an app-specific package absent because the VM was not built with
#  QDWIN_APP_DEPS=1 -> clean SKIP: <app> not installed; qdwin app deps are
#  opt-in". Each present toolkit still runs; a per-step guard below skips any
# individual toolkit whose dep is missing (partial app-deps image) rather than
# erroring.
HAVE_TK=0;    "$QDWIN_VM_EXEC" "$VMNAME" "python3 -c 'import tkinter' 2>/dev/null" && HAVE_TK=1
HAVE_FLTK=0;  "$QDWIN_VM_EXEC" "$VMNAME" 'command -v g++ >/dev/null 2>&1 && test -e /usr/include/FL/Fl.H' && HAVE_FLTK=1
HAVE_SWING=0; "$QDWIN_VM_EXEC" "$VMNAME" 'command -v javac >/dev/null 2>&1' && HAVE_SWING=1
echo "toolkit deps: tk=$HAVE_TK fltk=$HAVE_FLTK swing=$HAVE_SWING"
if [ "$HAVE_TK" = 0 ] && [ "$HAVE_FLTK" = 0 ] && [ "$HAVE_SWING" = 0 ]; then
    echo "SKIP: no Tk/FLTK/Swing toolkits installed (python313-tk / fltk-devel / java-25-openjdk-devel); qdwin app deps are opt-in (rerun with QDWIN_APP_DEPS=1)"
    exit 0
fi

qdwin_apps_kill_all

# Locate the checked-in demo sources on the host. They live in demos/
# next to this scenario (tests/apps/demos), not under a compatibility shim.
DEMOS_DIR=""
for cand in "${QDWIN_REPO:-}/tests/apps/demos" \
            "$QDWIN_WORKSPACE/qdwin/tests/apps/demos"; do
    [ -f "$cand/tk-demo.py" ] && { DEMOS_DIR="$cand"; break; }
done
[ -n "$DEMOS_DIR" ] || { echo "FAIL: demo sources not found (looked for tests/apps/demos/tk-demo.py)"; exit 1; }

# Serve the demos over a PRIVATE, free-port HTTP server so parallel CI
# runs never collide the way the old fixed :8765 singleton did (see the
# vm-smoke port-collision note: 8765 + /tmp/qdistro-http.log were shared
# host singletons). Python binds port 0 — the kernel assigns a free port
# and we serve from that exact live socket, printing the bound port on
# stdout, so there is no bind-probe-then-close race window. Bind 0.0.0.0
# so the guest reaches us at 10.0.2.2 over SLIRP NAT.
DEMOS_PORT_FILE=$(mktemp "$CASE_ARTIFACT_DIR/demos-port.XXXXXX")
( cd "$DEMOS_DIR" && exec python3 -c '
import http.server, socketserver, sys
socketserver.TCPServer.allow_reuse_address = False
httpd = socketserver.TCPServer(("0.0.0.0", 0), http.server.SimpleHTTPRequestHandler)
sys.stdout.write(str(httpd.server_address[1]) + "\n"); sys.stdout.flush()
httpd.serve_forever()
' > "$DEMOS_PORT_FILE" 2>"$CASE_ARTIFACT_DIR/logs/demos-http.log" ) &
DEMOS_HTTP_PID=$!
DEMOS_PORT=""
for _ in $(seq 1 50); do
    DEMOS_PORT=$(head -1 "$DEMOS_PORT_FILE" 2>/dev/null | tr -dc '0-9')
    [ -n "$DEMOS_PORT" ] && break
    kill -0 "$DEMOS_HTTP_PID" 2>/dev/null || break   # server died before binding
    sleep 0.2
done
[ -n "$DEMOS_PORT" ] || { echo "FAIL: demo HTTP server failed to bind (see $CASE_ARTIFACT_DIR/logs/demos-http.log)"; exit 1; }
DEMOS=http://10.0.2.2:$DEMOS_PORT
echo "serving $DEMOS_DIR at $DEMOS (pid $DEMOS_HTTP_PID)"
```

## Steps

### Step 1 — Tk via Python

```bash
# Re-detect Tk inline so this guard is correct even if $HAVE_TK from Setup did
# not persist into this step's shell.
if ! "$QDWIN_VM_EXEC" "$VMNAME" "python3 -c 'import tkinter' 2>/dev/null"; then
    echo "SKIP step 1 (Tk): python313-tk not installed; qdwin app deps are opt-in"
else
TK_LOG_BOUNDARY=$(qdwin_apps_bystander_log_boundary) || exit 1
# CAPTURE THROUGH A FILE, NOT `... 2>&1 | tee`. A host pipeline hands vm-exec's
# fd 2 to the pipe, and the shell then waits for the READER's stdin to reach
# EOF -- which happens only when the LAST writer closes. vm-exec bounds its own
# children's fd 1 internally, but a virsh/jq descendant that outlives vm-exec
# still holds fd 2, so the step hangs after the guest command is long dead. The
# log file is the artifact we wanted anyway; read it back afterwards for the
# transcript.
#
# `|| :` DISCARDS the status here, and that is a deliberate policy choice, not
# an equivalence: the claim that it "keeps the old pipeline's status semantics"
# held only with pipefail off and a reader that always succeeded. With pipefail
# set the old pipeline could propagate vm-exec's failure, and a tee whose write
# failed could fail on its own. The step's verdict comes from the assertions
# below and from the log, never from this line's status.
qdwin_apps_launch tk "wget -qO '$CASE_GUEST_DIR/tk-demo.py' $DEMOS/tk-demo.py && printf '%s\\n' \$\$ > '$CASE_GUEST_DIR/tk.pid' && exec python3 '$CASE_GUEST_DIR/tk-demo.py'" \
    > "$CASE_ARTIFACT_DIR/logs/step1-launch.log" 2>&1 || :
head -c "${QDWIN_APPS_LOG_ECHO_BYTES:-65536}" "$CASE_ARTIFACT_DIR/logs/step1-launch.log"
sleep 6
qdwin_apps_screenshot "$CASE_ARTIFACT_DIR/screenshots/step1-tk.png"
# Preserve the demo's own stdout/stderr: the launch helper only returns
# the vm-exec exit, but the app writes to /tmp/tk.log INSIDE the VM.
# Pull it to the host so a failure (e.g. a Tk font error) is diagnosable
# instead of a silent black screenshot.
# File capture, not `2>&1 | tee` -- see the note in step 1's launch.
# The ECHO of that log is bounded in BYTES. `cat` was not a bounded reader:
# it runs to EOF of a file a surviving guest descendant may still be
# appending to, so one long line replays without limit (astra measured
# 1,048,576 bytes from a single-line 1 MiB capture, A-astra finding 5).
# NOTE the deliberate scope: unlike the qd_cap captures, these logs stay
# NAMED on purpose -- they are the scenario's artifacts, kept for triage --
# so this is a bounded replay of a persistent file, not the open/unlink
# shape. The full text is always in the artifact; only the echo is capped.
"$QDWIN_VM_EXEC" "$VMNAME" "cat /tmp/tk.log" > "$CASE_ARTIFACT_DIR/logs/step1-tk.log" 2>&1 || :
head -c "${QDWIN_APPS_LOG_ECHO_BYTES:-65536}" "$CASE_ARTIFACT_DIR/logs/step1-tk.log"

# ENV PREREQUISITE (not a qdwin bug): if the app log shows
# `failed to allocate font`, the VM template lacks a usable X11 bitmap
# font, so Tk cannot allocate its default font and renders a black/empty
# toplevel with no `toplevel_added`. Report this sub-case as an env
# prerequisite (INFRA), per tests/apps/AGENTS.md — NOT a compositor FAIL.
if grep -q 'failed to allocate font' "$CASE_ARTIFACT_DIR/logs/step1-tk.log" 2>/dev/null; then
    echo "INFRA: Tk font allocation failed in VM template (install xorg-x11-fonts / xorg-x11-fonts-core); Tk sub-case is an env prerequisite, not a qdwin bug"
    CASE_INFRA=1
    qdwin_apps_10_kill_case
else
    # A runnable Tk client must complete the protocol round-trip. This gate
    # comes after the exact prerequisite classification so missing fonts do
    # not get mislabeled as a compositor failure.
    qdwin_apps_assert_max_restore_last "$CASE_ARTIFACT_DIR/screenshots/step1-tk" \
        "$TK_LOG_BOUNDARY" tk "Tk on qdwin" || exit 1
    qdwin_apps_10_kill_case
fi
fi
```

**Assert (1.1):** if `HAVE_TK=1`, screenshot shows the Tk window: title "Tk on qdwin",
"Tkinter demo" header in bold, an entry field with "type here", and
a "Click me" button. Standard Tk theme (grey background, default
ttk widgets).

**Assert (1.2):** the max screenshot shows the same widgets in a window filling
the output, and the bystander evidence contains `cmd max handle=<N>`, state
`0x1`, and 1280x800 geometry for that handle.

**Assert (1.3):** the restore screenshot shows the complete Tk window back at
its floating size, surrounded by black compositor background, and the
bystander evidence contains `cmd restore handle=<N>`, state `0x0`, and the
pre-max geometry for the same handle. The black area outside the small,
centred window is expected when qdshell is not running; do not mistake that
background for a black or missing application window.

**Assert (1.4):** if instead `$CASE_ARTIFACT_DIR/logs/step1-tk.log` contains
`failed to allocate font`, classify the cause as `INFRA:` (missing VM font
package), continue collecting other toolkit evidence, then return a nonzero
overall scenario status after cleanup — see Setup/Known failure modes.

### Step 2 — FLTK

In-VM build of the fetched `fltk-demo.cxx`. The bake step is
idempotent.

```bash
if ! "$QDWIN_VM_EXEC" "$VMNAME" 'command -v g++ >/dev/null 2>&1 && test -e /usr/include/FL/Fl.H'; then
    echo "SKIP step 2 (FLTK): fltk-devel/g++ not installed; qdwin app deps are opt-in"
else
# File capture, not `2>&1 | tee` -- see the note in step 1's launch.
"$QDWIN_VM_EXEC" "$VMNAME" "wget -qO '$CASE_GUEST_DIR/fltk-demo.cxx' $DEMOS/fltk-demo.cxx && \
    g++ -o '$CASE_GUEST_DIR/fltk-demo' '$CASE_GUEST_DIR/fltk-demo.cxx' -lfltk" \
    > "$CASE_ARTIFACT_DIR/logs/step2-build.log" 2>&1 || :
tail -3 "$CASE_ARTIFACT_DIR/logs/step2-build.log"

FLTK_LOG_BOUNDARY=$(qdwin_apps_bystander_log_boundary) || exit 1
qdwin_apps_launch fltk "printf '%s\\n' \$\$ > '$CASE_GUEST_DIR/fltk.pid' && exec '$CASE_GUEST_DIR/fltk-demo'" \
    > "$CASE_ARTIFACT_DIR/logs/step2-launch.log" 2>&1 || :
head -c "${QDWIN_APPS_LOG_ECHO_BYTES:-65536}" "$CASE_ARTIFACT_DIR/logs/step2-launch.log"
sleep 4
qdwin_apps_screenshot "$CASE_ARTIFACT_DIR/screenshots/step2-fltk.png"
"$QDWIN_VM_EXEC" "$VMNAME" "cat /tmp/fltk.log" > "$CASE_ARTIFACT_DIR/logs/step2-fltk.log" 2>&1 || :
head -c "${QDWIN_APPS_LOG_ECHO_BYTES:-65536}" "$CASE_ARTIFACT_DIR/logs/step2-fltk.log"

qdwin_apps_assert_max_restore_last "$CASE_ARTIFACT_DIR/screenshots/step2-fltk" \
    "$FLTK_LOG_BOUNDARY" FLTK "FLTK on qdwin" || exit 1
qdwin_apps_10_kill_case
fi
```

**Assert (2.1):** if `HAVE_FLTK=1`, screenshot shows the FLTK window: title "FLTK on
qdwin", File / Edit menu bar, "FLTK demo" centred header, "Text:"
label with entry "type here", "Click me" button. Distinctive flat
FLTK widgets.

**Assert (2.2):** max fills the output and restore returns the complete FLTK
window to its floating size. The bystander evidence must show max state
`0x1`, restore state `0x0`, and the restored pre-max geometry for one handle.
Black surrounding the centred restored window is the expected bare-compositor
background, not a rendering failure.

### Step 3 — Java Swing

```bash
if ! "$QDWIN_VM_EXEC" "$VMNAME" 'command -v javac >/dev/null 2>&1'; then
    echo "SKIP step 3 (Swing): javac (java-25-openjdk-devel) not installed; qdwin app deps are opt-in"
else
# File capture, not `2>&1 | tee` -- see the note in step 1's launch.
"$QDWIN_VM_EXEC" "$VMNAME" "wget -qO '$CASE_GUEST_DIR/SwingDemo.java' $DEMOS/SwingDemo.java && \
    cd '$CASE_GUEST_DIR' && javac SwingDemo.java" \
    > "$CASE_ARTIFACT_DIR/logs/step3-build.log" 2>&1 || :
tail -3 "$CASE_ARTIFACT_DIR/logs/step3-build.log"

SWING_LOG_BOUNDARY=$(qdwin_apps_bystander_log_boundary) || exit 1
qdwin_apps_launch swing "cd '$CASE_GUEST_DIR' && printf '%s\\n' \$\$ > '$CASE_GUEST_DIR/swing.pid' && exec java SwingDemo" \
    > "$CASE_ARTIFACT_DIR/logs/step3-launch.log" 2>&1 || :
head -c "${QDWIN_APPS_LOG_ECHO_BYTES:-65536}" "$CASE_ARTIFACT_DIR/logs/step3-launch.log"
sleep 10
qdwin_apps_screenshot "$CASE_ARTIFACT_DIR/screenshots/step3-swing.png"
"$QDWIN_VM_EXEC" "$VMNAME" "cat /tmp/swing.log" > "$CASE_ARTIFACT_DIR/logs/step3-swing.log" 2>&1 || :
head -c "${QDWIN_APPS_LOG_ECHO_BYTES:-65536}" "$CASE_ARTIFACT_DIR/logs/step3-swing.log"

qdwin_apps_assert_max_restore_last "$CASE_ARTIFACT_DIR/screenshots/step3-swing" \
    "$SWING_LOG_BOUNDARY" SwingDemo "Swing on qdwin" || exit 1
qdwin_apps_10_kill_case
fi
```

**Assert (3.1):** if `HAVE_SWING=1`, screenshot shows the Swing window: title "Swing on
qdwin", File / Edit menu bar, "Swing demo" header, "type here" text
field, "Click me" button. Metal (Java default) look-and-feel.

**Assert (3.2):** max fills the output and restore returns the complete Swing
window to its floating size. The bystander evidence must show max state
`0x1`, restore state `0x0`, and the restored pre-max geometry for one handle.
Black surrounding the centred restored window is the expected bare-compositor
background, not a rendering failure.

## Cleanup

```bash
# Explicit success cleanup also disarms the EXIT trap. Any earlier exit takes
# the same cleanup path automatically while preserving its nonzero status.
qdwin_apps_10_status=0
if [ "${CASE_INFRA:-0}" != 0 ]; then
    echo "FAIL: scenario 10 had installed-toolkit infrastructure failures"
    qdwin_apps_10_status=1
fi
qdwin_apps_10_cleanup "$qdwin_apps_10_status"
exit "$qdwin_apps_10_status"
```

## Pass criteria

- Each INSTALLED toolkit (`HAVE_*=1`) renders a window with the named
  widgets, the bystander log shows `xwayland=1` for its toplevel, max state
  `0x1` fills the output, and restore state `0x0` returns the same complete
  window to its pre-max floating geometry.
- A toolkit whose dep is absent is a **clean SKIP**, never a FAIL/ERROR:
  Tk/FLTK/Swing are opt-in `QDWIN_APP_DEPS` packages, so on a lean golden
  the whole scenario short-circuits to `SKIP` in Setup, and on a partial
  app-deps image each missing toolkit's step reports `SKIP step N (...)`.
  Overall status is `PASS` if every installed toolkit passed, `SKIP` if
  none were installed.

## Known failure modes

- **`python3 -c "import tkinter"` fails** — VM is missing `python313-tk`.
  This is an opt-in app dep, so the Setup gate records `HAVE_TK=0` and Step 1
  reports `SKIP step 1 (Tk)` — it is NOT a FAIL. Install `python313-tk`
  (via `QDWIN_APP_DEPS=1`) to exercise it.
- **`_tkinter.TclError: failed to allocate font`** — the VM template
  has no usable X11 bitmap font, so Tk cannot allocate its default
  font and the window is black with no `toplevel_added`. This is an
  env/template prerequisite, not a qdwin compositor bug. Ensure
  `xorg-x11-fonts` (and `xorg-x11-fonts-core`) are installed in the
  template; if absent, report the Tk sub-case as
  `INFRA: Tk font allocation failed` per AGENTS.md. The cause remains
  infrastructure, but an installed toolkit that did not run makes the overall
  scenario nonzero after cleanup.
- **`g++` / FLTK headers not found** — VM missing `gcc-c++` / `fltk-devel`.
  The Setup gate records `HAVE_FLTK=0` and Step 2 reports `SKIP step 2 (FLTK)`
  — an opt-in app dep, not a FAIL. `fltk-devel` drags in `gcc-c++`.
- **`javac` not found** — VM has only `java-25-openjdk-headless` (no JDK),
  or no Java at all. The Setup gate records `HAVE_SWING=0` and Step 3 reports
  `SKIP step 3 (Swing)` — an opt-in app dep, not a FAIL. Install
  `java-25-openjdk-devel` (via `QDWIN_APP_DEPS=1`) to exercise Swing.

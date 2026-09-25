# 12 — single-window RDP sharing: subscribe + xfreerdp sees frames

<!-- qci:visual: none -->

**Acceptance criterion:** a v14+ shell client can `subscribe_view_stream`
on a live toplevel handle, qdwin spawns `qdistro-forward`, the
forward listens on the announced TCP port, and a real RDP client
(xfreerdp) completing TLS + RDP handshake against that port can
receive at least one frame from the bound toplevel.

This covers the deepest end-to-end §6.5 path that the existing
`compositor-shell.bats::s3c-e2e` test only partially exercises
(it asserts the port accepts + frames flow inside qdistro-forward,
not that a real RDP client successfully decodes them).

## Prerequisites

This scenario REQUIRES:
- `freerdp3` (or `freerdp2`) installed in the VM (`xfreerdp` or
  `xfreerdp3` on $PATH). The disposable VMs use QEMU user networking, so
  their guest-only RDP listener has no host-routable address.
- `qdistro-forward` installed on the VM (built via
  `qdistro/daemons/forward/` — present in the baseweed bake).
- `qdwin-bystander` v ≥ 2026-05-14 installed on the VM (its
  `--subscribe HANDLE` flag is what drives the request — see
  `qdwin/test-client/qdwin-bystander.c`).
- `[pipewire] num-outputs >= 1` in the VM's weston.ini, OR the
  §6.5 spike bake's pipewire-bake provisioning. Without a free
  pipewire output, qdwin emits `denied "no free pipewire output"`
  and the rest of the scenario short-circuits.

Fail loudly if VM `xfreerdp`, VM `qdistro-forward`, or `qdwin-bystander`
is missing; do not skip those. The one exception is the compositor journal
record `qdwin: subscribe_view_stream denied handle=$HANDLE peer_label="..." (no pw output)`
since the subscribe cursor, with that reason at the end of the line and
outside the quotes: Step 1 prints `SKIP:` naming that check and exits 77.
The wire string `no free pipewire output` is not itself a skip.
Any other missing approval (credentials absent without that denial, a real
product deny, a bad port) stays a loud FAIL. The *subject* app `foot`,
however, is part of the opt-in
`QDWIN_APP_DEPS` matrix: on a lean GUI golden (no `QDWIN_APP_DEPS=1`) it is
legitimately absent, so SKIP cleanly per the apps/AGENTS.md rule rather than
ERROR (see the Setup guard below).

## Setup

```bash
source ${QDWIN_REPO}/tests/apps/qdwin-apps-helpers.sh
qdwin_apps_set_vm "${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -1)}"
ACTIVE_SOCKET=$(qdwin_apps_active_socket)
[ -n "$ACTIVE_SOCKET" ] || { echo "FAIL: qdwin session not up"; exit 1; }

# VM: confirm xfreerdp exists. The RDP listener is guest-local under QEMU user
# networking, so the real client must run in this same disposable VM.
"$QDWIN_VM_EXEC" "$VMNAME" 'command -v xfreerdp >/dev/null 2>&1 || command -v xfreerdp3 >/dev/null 2>&1' \
    || { echo "FAIL: install freerdp3 in the VM"; exit 1; }

# VM: confirm qdistro-forward.
"$QDWIN_VM_EXEC" "$VMNAME" 'test -x /usr/bin/qdistro-forward' \
    || { echo "FAIL: qdistro-forward not installed on VM"; exit 1; }

# VM: confirm the lifecycle-probe option is baked into qdwin-bystander.
"$QDWIN_VM_EXEC" "$VMNAME" \
    '/usr/bin/qdwin-bystander --help 2>&1 | grep -q -- --ignore-torn-down' \
    || { echo "FAIL: deploy qdwin-bystander with --ignore-torn-down"; exit 1; }

# Subject app: foot is the toplevel shared over RDP and is part of the opt-in
# qdwin app-deps matrix. On a lean GUI golden (no QDWIN_APP_DEPS=1) it is
# legitimately absent — SKIP cleanly rather than ERROR, matching apps/05/07/08.
if ! "$QDWIN_VM_EXEC" "$VMNAME" 'command -v foot >/dev/null 2>&1'; then
    echo "SKIP: foot not installed; qdwin app deps are opt-in (rerun with QDWIN_APP_DEPS=1)"
    exit 0
fi

# Arm restoration before the first helper that can stop qdshell or replace the
# singleton shell client. The helper also self-restores on failure; the trap
# covers every later early exit from Setup/Steps.
trap 'qdwin_apps_restore_shell' EXIT
qdwin_apps_prepare_shell_probe || {
    echo "FAIL: could not reserve singleton shell role for RDP probe"; exit 1;
}
# Re-detect after takeover; never hard-code wayland-1 across compositor restarts.
ACTIVE_SOCKET=$(qdwin_apps_active_socket)
[ -n "$ACTIVE_SOCKET" ] || { echo "FAIL: qdwin stopped during shell takeover"; exit 1; }

"$QDWIN_VM_EXEC" "$VMNAME" 'pkill -u admin -x foot 2>/dev/null; sleep 1' >/dev/null

# Launch exactly one scenario bystander into the role reserved above. Pin its
# FIFO explicitly: a stale FIFO or a second shell client is a hard setup error.
"$QDWIN_VM_EXEC" "$VMNAME" \
    "rm -f /run/user/1000/qdwin-cmd.fifo /tmp/15-creds.env /tmp/15-bystander.err; \
     runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
       WAYLAND_DISPLAY=$ACTIVE_SOCKET \
       QDWIN_BYSTANDER_FIFO=/run/user/1000/qdwin-cmd.fifo \
       setsid -f /usr/bin/qdwin-bystander --ignore-torn-down \
       >/tmp/15-creds.env 2>/tmp/15-bystander.err; \
     for i in \$(seq 1 40); do \
       grep -q 'hello uid=' /tmp/15-bystander.err 2>/dev/null && break; sleep 0.1; \
     done; \
     ! grep -q 'shell role already claimed' /tmp/15-bystander.err; \
     test -p /run/user/1000/qdwin-cmd.fifo; \
     pgrep -u admin -x qdwin-bystander >/dev/null; \
     grep -q 'hello uid=' /tmp/15-bystander.err" || {
    echo "FAIL: RDP probe did not acquire singleton shell role"; exit 1;
}

# Spawn a foot terminal to share.
qdwin_apps_launch foot "foot sleep 600" || {
    echo "FAIL: could not launch foot subject"; exit 1;
}
sleep 2

HANDLE=$("$QDWIN_VM_EXEC" "$VMNAME" \
    "journalctl _UID=1000 --no-pager | grep 'qdwin: toplevel_added' \
     | tail -1 | sed -nE 's/.*handle=([0-9]+).*/\1/p'")
[ -n "$HANDLE" ] || { echo "FAIL: no toplevel handle"; exit 1; }
echo "subject toplevel handle=$HANDLE"
```

## Steps

### Step 1 — subscribe + capture the approved event

The shell-side request:

```c
qdwin_shell_v1_subscribe_view_stream(shell, HANDLE, "...", 0, 0, 0);
```

emits the `approved` event with `(pipewire_node_name, rdp_port,
rdp_cert_path, rdp_password)`. Drive this from the VM via
the probe bystander's canonical FIFO, which prints sh-sourceable KEY=value
lines on stdout when `approved` fires and keeps the wayland
connection open so the stream stays live:

```bash
SUBSCRIBE_CURSOR=$(qdwin_apps_journal_cursor) || {
  echo "FAIL: could not capture first subscribe journal cursor"; exit 1;
}
qdwin_apps_ctl "subscribe $HANDLE" || {
  echo "FAIL: bounded first subscribe FIFO write failed"; exit 1;
}
sleep 1
CREDS=
for _i in $(seq 1 30); do
  CREDS=$("$QDWIN_VM_EXEC" "$VMNAME" \
    'cat /tmp/15-creds.env 2>/dev/null' || true)
  printf '%s\n' "$CREDS" | grep -q '^RDP_PASSWORD=' && break
  sleep 0.2
done
# No free pipewire output is a bake limit, not a product deny. Only that
# journal denial since the subscribe cursor becomes a skip; every other
# missing-approval cause keeps the FAIL below it.
rdp_skip_if_no_pipewire_output() {
  # The compositor's no-output record is exactly
  #   qdwin: subscribe_view_stream denied handle=N peer_label="..." (no pw output)
  # with the reason outside the quotes and at the end of the line
  # (qdwin.c weston_log). A journalctl prefix may precede "qdwin:".
  # peer_label text, a different parenthetical reason, and a quoted
  # copy of the event must not skip.
  local denial record
  record="qdwin: subscribe_view_stream denied handle=${HANDLE} peer_label=\"[^\"]*\" \\(no pw output\\)\$"
  denial=$(qdwin_apps_log_since_cursor "$SUBSCRIBE_CURSOR" "$record" || true)
  if printf '%s\n' "$denial" | grep -Eq "$record"; then
    echo "SKIP: qdwin_apps_log_since_cursor since subscribe cursor shows subscribe_view_stream denied handle=${HANDLE} peer_label=\"...\" (no pw output) at end of record; bake has no pipewire output"
    exit 77   # bats convention for skip
  fi
  return 0
}
printf '%s\n' "$CREDS" | grep -q '^RDP_PASSWORD=' || {
  rdp_skip_if_no_pipewire_output
  echo "FAIL: approved credentials did not arrive within 6 seconds"; exit 1;
}
# qdwin-bystander emits only fixed KEY=value protocol fields. Import the
# complete snapshot after the bounded poll so password/port/node stay paired.
eval "$CREDS"

APPROVAL=$(qdwin_apps_log_since_cursor "$SUBSCRIBE_CURSOR" \
  "view_stream approved handle=$HANDLE" | tail -1)
FORWARD_PID=$(printf '%s\n' "$APPROVAL" | \
  sed -nE 's/.*forward_pid=([0-9]+).*/\1/p')
FIRST_PW_OUTPUT=$(printf '%s\n' "$APPROVAL" | \
  sed -nE 's/.* pw=([^ ]+).*/\1/p')
[ -n "$FORWARD_PID" ] && [ -n "$FIRST_PW_OUTPUT" ] || {
  rdp_skip_if_no_pipewire_output
  echo "FAIL: approved event lacks forward PID/PipeWire output: $APPROVAL"; exit 1;
}

# Variables now in scope: HANDLE, PIPEWIRE_NODE_NAME, RDP_PORT,
# RDP_CERT_PATH, RDP_PASSWORD. FORWARD_PID is not exposed via the
# protocol — derive from journal if needed.
echo "rdp_port=$RDP_PORT node=$PIPEWIRE_NODE_NAME"
[ "$RDP_PORT" -ge 1024 ] 2>/dev/null && [ "$RDP_PORT" -le 65535 ] || {
  echo "FAIL: approved event returned invalid RDP port: $RDP_PORT"; exit 1;
}
```

If qdwin-bystander is absent on the VM (older bake), fail with
"deploy qdwin-bystander v >= 2026-05-14"; do not skip.

**Assert (1.1):** the journal shows
`qdwin: view_stream approved handle=$HANDLE peer_label=... pw=...
output_pos=... rdp_port=<P> forward_pid=<P> ...` within 2s of the
subscribe request. (If instead the journal shows
`qdwin: subscribe_view_stream denied handle=$HANDLE ... (no pw
output)`, this bake has no pipewire output. That is **not** a
weston.ini `[pipewire] num-outputs` problem — that key is set. It
means the compositor was started without `pipewire-backend.so` in its
backend list, so no pipewire output ever initialised. The bash above
already checks that delta with `qdwin_apps_log_since_cursor` and prints
`SKIP:` and exits 77 only when the record ends in `(no pw output)` after
a closed `peer_label="..."`. Record that SKIP. A non-pipewire bake cannot
satisfy this test. Any other denial, a reason buried in `peer_label`, or
a missing approval with that record absent, stays FAIL.)

**Assert (1.2):** $RDP_PORT is a valid TCP port (1024..65535).

### Step 2 — VM-side TCP accept

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
    "timeout 3 bash -c 'echo > /dev/tcp/127.0.0.1/$RDP_PORT' && echo TCP_OPEN" \
    || { echo "FAIL: RDP port $RDP_PORT did not accept TCP"; exit 1; }
```

**Assert (2.1):** prints `TCP_OPEN` (qdistro-forward is listening
on the announced port and the kernel accepts a connection).

### Step 3 — guest-local full xfreerdp session

```bash
RDP_AUTH_CURSOR=$(qdwin_apps_journal_cursor) || {
  echo "FAIL: could not capture pre-xfreerdp journal cursor"; exit 1;
}
RDP_CLIENT_B64=$(base64 -w0 <<EOF
set -o pipefail
runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=$ACTIVE_SOCKET DISPLAY=:0 \\
  timeout 8 xfreerdp /v:127.0.0.1:$RDP_PORT /cert:ignore \\
  /u:test /p:$RDP_PASSWORD /size:640x480 +decorations -encryption \\
  > /tmp/15-xfreerdp.log 2>&1
rc=\$?
# A full client remains connected until the deliberate timeout. Any earlier
# exit is a handshake/auth/framebuffer failure and must stay red.
[ "\$rc" -eq 124 ] || { cat /tmp/15-xfreerdp.log; exit "\$rc"; }
EOF
)
"$QDWIN_VM_EXEC" "$VMNAME" "echo $RDP_CLIENT_B64 | base64 -d | bash" || {
  echo "FAIL: xfreerdp session driver failed"; exit 1;
}
AUTH_LOG=$(qdwin_apps_log_since_cursor "$RDP_AUTH_CURSOR" \
  "auth OK for user=test")
"$QDWIN_VM_EXEC" "$VMNAME" \
  "grep -q 'Local framebuffer format' /tmp/15-xfreerdp.log && \
   grep -q 'Remote framebuffer format' /tmp/15-xfreerdp.log" \
  && printf '%s\n' "$AUTH_LOG" | grep -q 'auth OK for user=test' || {
  echo "FAIL: connected session lacks auth/framebuffer evidence"; exit 1;
}
```

**Assert (3.1):** qdistro-forward's log shows `auth OK for user=test` and
xfreerdp's guest log shows both local and remote framebuffer initialization.
The client must remain connected until the deliberate timeout. This exercises
the shadow server's post-connect authentication path; `+auth-only` exits before
that path in FreeRDP 3.30 and is not a valid authentication test.

Failure modes that should fail this assert:

- Exit before the deliberate timeout → handshake, auth, or framebuffer setup failed.
- "TLS connection failed" → cert/port mismatch.
- "Failed to authenticate" → password mismatch (rotation race?).

### Step 5 — forwarder-death cleanup

```bash
TEARDOWN_CURSOR=$(qdwin_apps_journal_cursor) || {
  echo "FAIL: could not capture first teardown journal cursor"; exit 1;
}
"$QDWIN_VM_EXEC" "$VMNAME" \
    "kill $FORWARD_PID 2>/dev/null; sleep 1; \
     ! ps -p $FORWARD_PID >/dev/null 2>&1" || {
  echo "FAIL: forwarder $FORWARD_PID did not exit"; exit 1;
}
TEARDOWN_LOG=$(qdwin_apps_log_since_cursor "$TEARDOWN_CURSOR" \
  "view_stream_(torn_down|server_state_released) handle=$HANDLE")
TORN_COUNT=$(printf '%s\n' "$TEARDOWN_LOG" | \
  grep -cF "view_stream_torn_down handle=$HANDLE pid=$FORWARD_PID reason=\"forward exited\"" || true)
ALL_TORN_COUNT=$(printf '%s\n' "$TEARDOWN_LOG" | grep -c 'view_stream_torn_down' || true)
RELEASE_COUNT=$(printf '%s\n' "$TEARDOWN_LOG" | grep -c 'view_stream_server_state_released' || true)
[ "$TORN_COUNT" -eq 1 ] && [ "$ALL_TORN_COUNT" -eq 1 ] && [ "$RELEASE_COUNT" -eq 1 ] || {
  echo "FAIL: first teardown pid/reason count=$TORN_COUNT all_torn=$ALL_TORN_COUNT released=$RELEASE_COUNT"; exit 1;
}
"$QDWIN_VM_EXEC" "$VMNAME" \
  "pgrep -u admin -x qdwin-bystander >/dev/null && \
   grep -q 'retaining inert view_stream handle=$HANDLE' /tmp/15-bystander.err && \
   ! grep -q 'invalid object' /tmp/15-bystander.err" || {
  echo "FAIL: ignoring bystander died, missed tombstone, or hit protocol error"; exit 1;
}
```

**Assert (5.1):** qdistro-forward exits cleanly when killed.
qdwin should emit `qdwin: view_stream_torn_down handle=$HANDLE
pid=$FORWARD_PID` and exactly one
`qdwin: view_stream_server_state_released handle=$HANDLE` shortly after.
The deliberately non-cooperative bystander remains connected and reports
`retaining inert view_stream`; it must not report `invalid object`.

### Step 6 — ignored client cannot retain the output

The first protocol object is deliberately still alive. Subscribe to the same
handle again through the bystander's FIFO:

```bash
REUSE_CURSOR=$(qdwin_apps_journal_cursor) || {
  echo "FAIL: could not capture output-reuse journal cursor"; exit 1;
}
qdwin_apps_ctl "subscribe $HANDLE" || {
  echo "FAIL: bounded reuse subscribe FIFO write failed"; exit 1;
}
sleep 2
SECOND_APPROVAL=$(qdwin_apps_log_since_cursor "$REUSE_CURSOR" \
  "view_stream approved handle=$HANDLE" | tail -1)
SECOND_FORWARD_PID=$(printf '%s\n' "$SECOND_APPROVAL" | \
  sed -nE 's/.*forward_pid=([0-9]+).*/\1/p')
SECOND_PW_OUTPUT=$(printf '%s\n' "$SECOND_APPROVAL" | \
  sed -nE 's/.* pw=([^ ]+).*/\1/p')
[ -n "$SECOND_FORWARD_PID" ] && [ "$SECOND_FORWARD_PID" != "$FORWARD_PID" ] || {
  echo "FAIL: reuse approval missing a new forward PID: $SECOND_APPROVAL"; exit 1;
}
[ "$SECOND_PW_OUTPUT" = "$FIRST_PW_OUTPUT" ] || {
  echo "FAIL: output not reused: first=$FIRST_PW_OUTPUT second=$SECOND_PW_OUTPUT"; exit 1;
}

SECOND_TEARDOWN_CURSOR=$(qdwin_apps_journal_cursor) || {
  echo "FAIL: could not capture second teardown journal cursor"; exit 1;
}
"$QDWIN_VM_EXEC" "$VMNAME" "kill $SECOND_FORWARD_PID" || {
  echo "FAIL: could not kill second forwarder $SECOND_FORWARD_PID"; exit 1;
}
sleep 1
SECOND_TEARDOWN_LOG=$(qdwin_apps_log_since_cursor "$SECOND_TEARDOWN_CURSOR" \
  "view_stream_(torn_down|server_state_released) handle=$HANDLE")
SECOND_TORN_COUNT=$(printf '%s\n' "$SECOND_TEARDOWN_LOG" | \
  grep -cF "view_stream_torn_down handle=$HANDLE pid=$SECOND_FORWARD_PID reason=\"forward exited\"" || true)
SECOND_ALL_TORN_COUNT=$(printf '%s\n' "$SECOND_TEARDOWN_LOG" | grep -c 'view_stream_torn_down' || true)
SECOND_RELEASE_COUNT=$(printf '%s\n' "$SECOND_TEARDOWN_LOG" | grep -c 'view_stream_server_state_released' || true)
[ "$SECOND_TORN_COUNT" -eq 1 ] && [ "$SECOND_ALL_TORN_COUNT" -eq 1 ] && [ "$SECOND_RELEASE_COUNT" -eq 1 ] || {
  echo "FAIL: second teardown pid/reason count=$SECOND_TORN_COUNT all_torn=$SECOND_ALL_TORN_COUNT released=$SECOND_RELEASE_COUNT"; exit 1;
}
"$QDWIN_VM_EXEC" "$VMNAME" \
  "pgrep -u admin -x qdwin-bystander >/dev/null && \
   ! grep -q 'invalid object' /tmp/15-bystander.err" || {
  echo "FAIL: bystander died or hit protocol error after output reuse"; exit 1;
}
```

**Assert (6.1):** a second `view_stream approved` journal event names the same
PipeWire output as Step 1 and a new forwarder PID. This proves forwarder death
removed the old stream from the active list and made its output immediately
reusable even though the client retained the old protocol tombstone. Kill the
second forwarder and assert it too produces exactly one
`view_stream_server_state_released` line. The bystander remains alive with no
`invalid object`; disconnecting it in Cleanup frees both inert tombstones.

## Cleanup

```bash
"$QDWIN_VM_EXEC" "$VMNAME" \
    'pkill -u admin -x qdistro-forward 2>/dev/null; \
     pkill -u admin -x foot 2>/dev/null; true' >/dev/null
# restore_shell owns the probe-client exit so it can observe qdwin's exact
# shell-unbound boundary before starting qdshell.
qdwin_apps_restore_shell || {
  echo "FAIL: qdshell restoration did not reach compositor-bound state"; exit 1;
}
trap - EXIT
```

## Pass criteria

Asserts 1.1, 1.2, 2.1, 3.1, 5.1, and 6.1 pass. The original `s3c-e2e` test in
`compositor-shell.bats` proves the qdistro-forward → port-accepts
+ frames-flow path; THIS scenario adds the missing real-RDP-client
half (xfreerdp completing the TLS handshake and decoding frames).

## Known-broken-if

- 1.1 silent: subscribe path in qdwin's
  `qdwin_handle_subscribe_view_stream` is reaching the send but
  not logging — adjacent gap to the 2026-05-14 keybinding
  instrumentation fix.
- 2.1 connection refused: qdistro-forward never spawned, OR
  spawned and crashed. Check `journalctl -u user@1000` for
  forward.c crash signatures.
- 3.1 TLS failure: `rdp_cert_path` points at a non-existent or
  expired cert. Check `QDWIN_RDP_CERT` env on the compositor
  systemd unit; default is `/etc/qdwin/rdp-cert.pem` per
  forward.c.
- 3.1 auth failure: password from subscribe doesn't match.
  Rotation race — qdistro-forward may have generated a new
  password between subscribe and xfreerdp launch. Re-subscribe.
- 3.1 framebuffer failure: frames flow in qdistro-forward (s3c-e2e
  PASSes) but xfreerdp cannot initialize its framebuffers. Inspect the
  guest `/tmp/15-xfreerdp.log` with `wlog.level=debug`.

## Lifecycle coverage boundary

This scenario proves the forwarder-death terminal path, including ignored
clients and output reuse. Source-close and lock entry share the compositor's
single terminal transition and are covered by the lifecycle regression test;
they are not separately driven by this RDP scenario. Client `destroy` is a
cancellation (and sends no `torn_down` reply), not a simulated remote-subscriber
disconnect. There is currently no separate admin-revoke protocol request.

## History

Originally (pre-2026-05-14) this scenario could only run against
§6.5-baked VMs because it depended on
`/root/s3c-subscribe-extract.sh` to drive the request. As of
2026-05-14, `qdwin-bystander --subscribe <handle>` carries the
subscribe wire request and prints sh-sourceable credentials on
stdout when `approved` fires, so the scenario runs against any VM
that has `qdwin-bystander` + `qdistro-forward` installed and a
free pipewire output. The deterministic regression
`t_bystander_subscribe_sends_request` in
`qdistro/scripts/install/gui-regression-tests.sh` exercises the
wire path on the deny branch (the more common state on non-spike
VMs); the visual half of this scenario covers the approved branch
plus the real xfreerdp handshake.

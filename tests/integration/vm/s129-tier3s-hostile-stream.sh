#!/bin/bash
# s129-tier3s-hostile-stream.sh — GUEST driver (root) for
# phase7-tier3s-hostile-stream.bats. Phase B (ΔB9) hostile waypipe stream
# handling, tier3s/CONTRACT.md §"Security invariants" (blast radius):
# a malformed or hostile byte stream on EITHER end of a GUI launch's
# bridge — the sandbox-side waypipe channel sockets (exercised via
# pidfd_getfd on the runsc sentry's fds, the same bytes a hostile sandbox
# write puts on the wire toward the TRUSTED client parser) and the
# wayland-secctx listener (the client→compositor direction) — must
# only ever kill THAT connection. The compositor, qdshell and other GUI
# launches are untouched; no broad teardown, no host crash. The -o client
# unlinks $LAUNCHES/<tok>/link.sock at accept (single-attach), so the
# channel itself is no longer a connect target post-attach — asserted too.
#   - a second connect to the consumed link.sock refused;
#   - garbage onto the sandbox-side sockets (waypipe frames at the client
#     parser) and onto the bridge client's sockets (wayland requests at
#     qdwin on the tagged channel), via pidfd_getfd;
#   - garbage + a truncated frame + a flood on the launch's secctx
#     listener socket (the tagged channel into the compositor);
#   then: compositor MainPID unchanged + unit active; qdshell active; the
#   SECOND launch's record/bridge/toplevel all still live; and full
#   teardown of both launches is clean.
# Runs after tier3s-guest-setup.sh --gui. One PASS/FAIL line per check;
# `[s129] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s129
. "$(dirname "$0")/tier3s-guest-lib.sh"
SA=s129a; SB=s129b
GUISPAWN="qdistro.tier3s.spawn:weston-terminal/weston-terminal"

step "0. preconditions, silos"
is "probe PASS" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0
is "weston-terminal image loaded" "$(yes_no pm image exists localhost/qdistro/tier3s-weston-terminal:latest)" yes
is "admin compositor socket present" "$(yes_no test -S $ADMIN_RT/$GUI_DISPLAY)" yes
is "qdshell is up" "$(as_admin systemctl --user is-active qdshell.service 2>/dev/null)" active
is "profile is dev" "$(sed -n 's/^QDISTRO_PROFILE=//p' /etc/qdistro/profile | tail -1)" dev
assert_all_clear pre
for s in $SA $SB; do
    sm CreateTier3sSilo ssss "$s" weston-terminal "$s" none > /dev/null
    is "CreateTier3sSilo $s" "$(silo_state "$s")" Created
done
set_rules "allow:$GUISPAWN"
is "broker answers allow for the GUI spawn" "$(broker_check "$GUISPAWN")" allow

step "1. two GUI launches up"
TA=$(up_gui_silo "$SA"); TB=$(up_gui_silo "$SB")
[ -n "$TA" ] && [ -n "$TB" ] && pass "both GUI launches up ($TA, $TB)" \
    || fail "launches did not come up"
assert_gui_bridge_up "pre-attack/A" "$TA"
assert_gui_bridge_up "pre-attack/B" "$TB"
CPID_BEFORE=$(comp_pid)
is "compositor pid captured" "$(yes_no test -n "$CPID_BEFORE")" yes
LSOCK_A=$(secctx_listener "$TA")
is "A's secctx listener resolved" "$(yes_no test -n "$LSOCK_A" -a -S "$ADMIN_RT/$LSOCK_A")" yes

# The -o client unlinks link.sock once the sandbox's waypipe server attaches
# (single-attach by design): a reconnect attempt must be refused — prove it —
# and the live post-attach attack surfaces are the bridge's TWO protocol
# ends. There is no named in-sandbox wayland socket (waypipe server execs
# the workload over fd-passing) and /proc/pid/fd refuses sockets (ENXIO),
# so the stream attacks go through pidfd_getfd(2) as host root — the same
# bytes a hostile sandbox write or a compromised bridge client puts on
# these wires:
#  (a) dup the SANDBOX waypipe server's socket fds -> garbage travels
#      sandbox->client: malformed waypipe frames at the TRUSTED client's
#      parser (the surface the CONTRACT flags);
#  (b) dup the BRIDGE CLIENT's socket fds -> garbage travels client->peer:
#      wayland-request bytes at qdwin on the secctx-tagged channel, and
#      waypipe-frame bytes at the sandbox server. Either end may drop or
#      die — the verdicts below decide; only B must stay untouched.
is "single-attach: reconnect to A's consumed link.sock is refused" \
    "$(python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1])' "$LAUNCHES/$TA/link.sock" 2>/dev/null && echo accepted || echo refused)" refused
CTR_A=$(ctr_of "$SA")
# runsc systrap: the sandbox's sockets live in the sentry/gofer process,
# not the stub init — every host process whose cmdline carries the
# container id (plus the init pid itself) is a sandbox-side fd owner.
CPID_A=$(pm inspect --format '{{.State.Pid}}' "$CTR_A" 2>/dev/null)
SANDBOX_PIDS=$( { echo "$CPID_A"; pgrep -f "$CTR_A" 2>/dev/null; } | sort -u | grep -v '^$' )
BP_A=$(rec "$TA" bridge_client_pid)
is "attack targets resolved (sandbox host pids, bridge client pid)" \
    "$(yes_no test -n "$SANDBOX_PIDS" -a -n "$BP_A")" yes
host_hose() {   # host_hose <log> <pid>... — write garbage onto each pid's socket fds
    local log="$1"; shift
    python3 - "$@" > "$log" 2>&1 <<'PY'
import ctypes, glob, os, sys
libc = ctypes.CDLL(None, use_errno=True)
sent = 0
for a in sys.argv[1:]:
    pid = int(a)
    pidfd = libc.syscall(434, pid, 0)          # pidfd_open
    if pidfd < 0: continue
    for l in sorted(glob.glob("/proc/%d/fd/*" % pid)):
        try: t = os.readlink(l)
        except OSError: continue
        if not t.startswith("socket:"): continue
        n = libc.syscall(438, pidfd, int(os.path.basename(l)), 0)    # pidfd_getfd
        if n < 0: continue
        try: os.write(n, os.urandom(512)); sent += 1
        except OSError: pass
        os.close(n)
print("hose done sent=%d" % sent)
PY
}
# shellcheck disable=SC2086
host_hose "$WORK/hose-sandbox.log" $SANDBOX_PIDS
sed 's/^/    sandbox-hose: /' "$WORK/hose-sandbox.log"
is "sandbox->client hose wrote waypipe frames at the trusted parser" \
    "$(grep -c 'hose done sent=[1-9]' "$WORK/hose-sandbox.log")" 1
host_hose "$WORK/hose-client.log" "$BP_A"
sed 's/^/    client-hose: /' "$WORK/hose-client.log"
is "client->peer hose wrote onto the tagged bridge streams" \
    "$(grep -c 'hose done sent=[1-9]' "$WORK/hose-client.log")" 1

# the tagged channel into the compositor: garbage + truncated frame + flood
# on the secctx listener. A refused/absent socket mid-test is legitimate
# (the connection already died); the verdicts below decide.
python3 - "$ADMIN_RT/$LSOCK_A" > "$WORK/hose.log" 2>&1 <<'PY'
import os, socket, sys, time
listener = sys.argv[1]

def blast(path, payload, label):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(path)
        s.sendall(payload)
        time.sleep(0.05)
        s.close()
        print(f"{label}: sent {len(payload)} bytes to {path}")
    except OSError as e:
        print(f"{label}: connect/send failed ({e})")

blast(listener, os.urandom(4096), "listener-garbage")
blast(listener, b"\x01\x00\x00\x00\x01\x00\x0c\x00\x02\x00\x00\x00" + os.urandom(64),
      "listener-truncated-frame")
for i in range(20):
    blast(listener, os.urandom(128), f"listener-flood-{i}")
print("hose done")
PY
sed 's/^/    hose: /' "$WORK/hose.log"
wait_for 15 bash -c "grep -q 'hose done' '$WORK/hose.log'"
is "the listener hose ran to completion" "$(grep -c 'hose done' "$WORK/hose.log")" 1
sleep 2   # let any delayed connection teardown land before the verdicts

step "2. blast radius: only the attacked connection may die"
is "compositor still the same pid, unit active" \
    "$(comp_pid):$(as_admin systemctl --user is-active qdwin-compositor.service 2>/dev/null)" \
    "$CPID_BEFORE:active"
is "qdshell still active" \
    "$(as_admin systemctl --user is-active qdshell.service 2>/dev/null)" active
is "no compositor/qdshell crash in the journal since the attack" \
    "$(journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service _SYSTEMD_USER_UNIT=qdshell.service --no-pager -o cat --since '-30 sec' 2>/dev/null | grep -ciE 'segfault|panic|fatal|assertion.*failed')" 0

step "3. A's bridge: either dropped the bad connection or died — both in-contract"
BP_A=$(rec "$TA" bridge_client_pid); BS_A=$(rec "$TA" bridge_client_starttime)
if [ -n "$BP_A" ] && [ "$(starttime "$BP_A" 2>/dev/null)" = "$BS_A" ]; then
    pass "A's bridge client survived the garbage (waypipe dropped the bad stream)"
    is "A's bridge channel still established" "$(yes_no bridge_stream_live "$TA")" yes
else
    pass "A's bridge connection died under the hostile stream (pid ${BP_A:-?} gone)"
    is "A's toplevel is gone from qdshell (handle freed with the client)" \
        "$(qs_ipc tier3focus findSiloHandle "$SA" 2>/dev/null | head -1)" "HANDLE=-1"
fi

step "4. B's launch is completely untouched"
is "B's record still running" "$(rec "$TB" phase)" running
is "B's bridge client still live (starttime verified)" \
    "$(st=$(rec "$TB" bridge_client_starttime); p=$(rec "$TB" bridge_client_pid); [ -n "$p" ] && [ "$(starttime "$p" 2>/dev/null)" = "$st" ] && echo yes || echo no)" yes
is "B's bridge channel still established" "$(yes_no bridge_stream_live "$TB")" yes
is "B's toplevel still in the qdshell model" \
    "$(qs_ipc tier3focus findSiloHandle "$SB" 2>/dev/null | head -1 | grep -cv 'HANDLE=-1')" 1
is "B's container still running" "$(ctr_status "$(ctr_of "$SB")")" running

step "5. teardown: both launches come down clean"
for s in $SA $SB; do
    sm StopSilo si "$s" 10 > /dev/null
    is "StopSilo $s" "$(silo_state "$s")" Stopped
    wait_for 90 unit_down "$(unit_of "$s")"
done
assert_bridge_gone "cleanup/A" "$TA"; assert_launch_gone "cleanup/A" "$TA" "$(ctr_of "$SA")"
assert_bridge_gone "cleanup/B" "$TB"; assert_launch_gone "cleanup/B" "$TB" "$(ctr_of "$SB")"
for s in $SA $SB; do sm DeleteSilo s "$s" > /dev/null; is "DeleteSilo $s" "$(silo_state "$s")" absent; done
set_rules none
assert_all_clear end
finish

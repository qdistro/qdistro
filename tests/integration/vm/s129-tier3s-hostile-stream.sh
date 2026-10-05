#!/bin/bash
# s129-tier3s-hostile-stream.sh — GUEST driver (root) for
# phase7-tier3s-hostile-stream.bats. Phase B (ΔB9) hostile waypipe stream
# handling, tier3s/CONTRACT.md §"Security invariants" (blast radius):
# a malformed or hostile byte stream on EITHER end of a GUI launch's
# bridge must only ever kill THAT connection — the compositor, qdshell
# and other GUI launches are untouched; no broad teardown, no host
# crash. The -o client unlinks $LAUNCHES/<tok>/link.sock at accept
# (single-attach), so the channel itself is no longer a connect target
# post-attach — asserted too.
#   - a second connect to the consumed link.sock refused;
#   - garbage + a truncated frame + a 20-connection flood on the launch's
#     secctx listener, DELIVERED while the launch is alive (the delivery
#     count is asserted — refused blasts are a dead listener, not an
#     attack);
#   - attributed fd attacks via pidfd_getfd: the SANDBOX end of the
#     waypipe link (the sentry's channel fd, found by ss peer-inode
#     mapping and verified to belong to a runsc process — the same
#     bytes a hostile sandbox write puts on the wire toward the TRUSTED
#     client parser) and the bridge client's own socket fds (wayland
#     requests at qdwin on the tagged channel, waypipe frames at the
#     sandbox server);
#   then: compositor + qdshell MainPIDs UNCHANGED (a qdshell restart
#   would change its pid) and units active; the SECOND launch's
#   record/bridge/toplevel all still live; and full teardown of both
#   launches is clean.
# Runs after tier3s-guest-setup.sh --gui. One PASS/FAIL line per check;
# `[s129] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s129
. "$(dirname "$0")/tier3s-guest-lib.sh"
SA=s129a; SB=s129b
GUISPAWN="qdistro.tier3s.spawn:weston-terminal/weston-terminal"

step "0. preconditions, silos"
is "probe PASS (admin substrate)" "$(/usr/lib/qdistro/tier3s/probe.sh --user admin > /dev/null 2>&1; echo $?)" 0
is "weston-terminal image staged in admin's store" "$(yes_no pm image exists localhost/qdistro/tier3s-weston-terminal:latest)" yes
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
# Model A: provision qt3s-<silo> + per-silo image store for both silos
for s in $SA $SB; do
    if ensure_silo_image "$s" weston-terminal; then pass "$s: qt3s-$s provisioned; image in its store"
    else fail "$s: ensure_silo_image failed"; fi
done

step "1. two GUI launches up"
TA=$(up_gui_silo "$SA"); TB=$(up_gui_silo "$SB")
[ -n "$TA" ] && [ -n "$TB" ] && pass "both GUI launches up ($TA, $TB)" \
    || fail "launches did not come up"
assert_gui_bridge_up "pre-attack/A" "$TA"
assert_gui_bridge_up "pre-attack/B" "$TB"
CPID_BEFORE=$(comp_pid)
is "compositor pid captured" "$(yes_no test -n "$CPID_BEFORE")" yes
QS_BEFORE=$(as_admin systemctl --user show qdshell.service -p MainPID --value 2>/dev/null)
is "qdshell main pid captured (restart would change it)" "$(yes_no test -n "$QS_BEFORE" -a "$QS_BEFORE" != 0)" yes
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

# Attack 1 — the tagged channel into the compositor, while A is fully
# alive: garbage + a truncated frame + a 20-connection flood on A's
# secctx listener. Each blast is a NEW connection offering junk; the
# established tagged channel must be unaffected (or the tagged client
# may be disconnected — both are in-contract; B is the control).
python3 - "$ADMIN_RT/$LSOCK_A" > "$WORK/hose.log" 2>&1 <<'PY'
import os, socket, sys, time
listener = sys.argv[1]
sent = 0

def blast(path, payload, label):
    global sent
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(path)
        s.sendall(payload)
        sent += 1
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
print(f"hose done delivered={sent}")
PY
sed 's/^/    hose: /' "$WORK/hose.log"
wait_for 15 bash -c "grep -q 'hose done' '$WORK/hose.log'"
# Delivery matters, not just completion: ENOENT/ECONNREFUSED blasts are a
# dead listener, not a delivered attack. Require the majority landed.
is "listener attacks DELIVERED while A's listener was live (>=20 of 22)" \
    "$(yes_no test "$(grep -c ': sent ' "$WORK/hose.log")" -ge 20)" yes

step "2. listener blast radius (A still live): only junk connections may die"
is "compositor still the same pid, unit active after the listener blast" \
    "$(comp_pid):$(as_admin systemctl --user is-active qdwin-compositor.service 2>/dev/null)" \
    "$CPID_BEFORE:active"
is "qdshell still the SAME pid (no restart) and active" \
    "$(as_admin systemctl --user show qdshell.service -p MainPID --value 2>/dev/null):$(as_admin systemctl --user is-active qdshell.service 2>/dev/null)" \
    "$QS_BEFORE:active"
is "B's bridge channel still established after A's listener blast" \
    "$(yes_no bridge_stream_live "$TB")" yes
sleep 1   # let any delayed connection teardown land before the fd attacks

# Attack 2 — the bridge's two protocol ends, attributed precisely.
# No named in-sandbox wayland socket exists (waypipe fd-passes to the
# workload) and /proc/pid/fd refuses socket opens (ENXIO), so writes go
# through pidfd_getfd(2) as host root — but ONLY onto fds whose peer is
# identified. Topology (verified on this VM): the accepted link.sock
# channel lives in the runsc GOFER's network namespace — the waypipe
# client's accepted fd and the SENTRY's sandbox-end fd both appear in
# `nsenter -t <gofer> -n ss -xp` with `users:` naming their owners.
# A host-namespace ss does NOT see the channel at all (b19's failure:
# host-ns peer-mapping resolved nothing). The hose writes:
#  (a) the SENTRY's channel fd -> malformed waypipe frames travel
#      sandbox->client at the TRUSTED client parser (the CONTRACT's
#      flagged surface);
#  (b) the CLIENT's channel fd -> waypipe frames client->sandbox;
#  (c) the CLIENT's remaining ESTABLISHED socket -> wayland requests at
#      qdwin on the secctx-tagged channel.
# The link.sock LISTEN fd is skipped (unconnected; writing to it is not
# a stream attack) and NO unattributed socket is touched.
GP_A=$(gofer_pid_of "$TA")
BP_A=$(rec "$TA" bridge_client_pid)
SP_A=$(rec "$TA" sentry_pid)
is "bridge client pid resolved" "$(yes_no test -n "$BP_A")" yes
is "gofer pid resolved" "$(yes_no test -n "$GP_A")" yes
is "sentry pid resolved for peer attribution" "$(yes_no test -n "$SP_A")" yes
python3 - "$BP_A" "$GP_A" "$SP_A" > "$WORK/hose-channels.log" 2>&1 <<'PY'
import ctypes, glob, os, re, subprocess, sys
libc = ctypes.CDLL(None, use_errno=True)
client = int(sys.argv[1])
gofer = int(sys.argv[2])
sentry_pid = int(sys.argv[3] or 0)

def nsenter_ss(pid):
    try:
        return subprocess.run(["nsenter", "-t", str(pid), "-n", "ss", "-xpH"],
                              capture_output=True, text=True).stdout
    except OSError:
        return ""

def ss_rows(out, state):
    # rows: (local_inode, peer_inode, [(pid, fd), ...])
    rows = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 8 or cols[0] != "u_str" or cols[1] != state:
            continue
        try:
            li, pi = int(cols[5]), int(cols[7])
        except ValueError:
            continue
        users = [(int(a), int(b)) for a, b in
                 re.findall(r'pid=(\d+),fd=(\d+)', line)]
        rows.append((li, pi, users))
    return rows

# The channel pair lives in the gofer's netns: one ESTAB row's users:
# names the waypipe client (host end); its peer-inode names the sentry's
# sandbox end.
grows = ss_rows(nsenter_ss(gofer), "ESTAB")
chan_local = chan_peer = client_fd = None
for li, pi, users in grows:
    for p, f in users:
        if p == client:
            chan_local, chan_peer, client_fd = li, pi, f
            break
    if chan_local is not None:
        break
sentry_fd = None
if chan_peer is not None:
    for li, pi, users in grows:
        if li != chan_peer:
            continue
        # the peer row's users: lists every sentry THREAD pid sharing the
        # channel fd — prefer the launch-record's attested sentry pid,
        # else any pid whose exe is the runsc sandbox.
        fallback = None
        for p, f in users:
            try:
                exe = os.readlink("/proc/%d/exe" % p)
            except OSError:
                exe = ""
            if "runsc" not in exe:
                continue
            if p == sentry_pid:
                sentry_fd = (p, f)
                break
            fallback = fallback or (p, f)
        sentry_fd = sentry_fd or fallback
        break

# Client socket fds in the HOST netns: the listener (unlinked
# link.sock LISTEN, skipped — an unconnected socket is not a stream
# target) and the compositor-side wayland connection (the remaining
# ESTAB socket -> wayland requests at qdwin).
listen_inodes = set()
try:
    lout = subprocess.run(["ss", "-xlH"], capture_output=True, text=True).stdout
    for line in lout.splitlines():
        cols = line.split()
        if len(cols) >= 6 and cols[0] == "u_str" and cols[1] == "LISTEN":
            try:
                listen_inodes.add(int(cols[5]))
            except ValueError:
                pass
except OSError:
    pass

mine = {}
for l in sorted(glob.glob("/proc/%d/fd/*" % client)):
    try:
        t = os.readlink(l)
    except OSError:
        continue
    m = re.match(r"socket:\[(\d+)\]", t)
    if m:
        mine[int(os.path.basename(l))] = int(m.group(1))

def hose(pid, fd, label):
    pidfd = libc.syscall(434, pid, 0)          # pidfd_open
    if pidfd < 0:
        print(f"{label}: pidfd_open({pid}) failed"); return 0
    n = libc.syscall(438, pidfd, fd, 0)        # pidfd_getfd
    if n < 0:
        os.close(pidfd); print(f"{label}: pidfd_getfd({pid},{fd}) failed"); return 0
    try:
        wrote = os.write(n, os.urandom(512))
    except OSError as e:
        print(f"{label}: write onto pid {pid} fd {fd} failed: {e}")
        wrote = 0
    os.close(n); os.close(pidfd)
    print(f"{label}: wrote {wrote}B onto pid {pid} fd {fd}")
    return wrote

ends = 0
sent = 0
sandbox_bytes = 0
if sentry_fd is not None:
    sandbox_bytes = hose(sentry_fd[0], sentry_fd[1], "sandbox-end")
    sent += sandbox_bytes; ends += 1 if sandbox_bytes else 0
else:
    print("WARN: no sandbox-end channel fd identified")
for fd, ino in mine.items():
    if chan_local is not None and ino == chan_local:
        w = hose(client, fd, "client-channel")
    elif ino in listen_inodes:
        print(f"fd {fd} inode {ino}: the link.sock LISTENER — skipped (unconnected)")
        continue
    elif ino == chan_peer:
        print(f"fd {fd} inode {ino}: unexpected peer-side fd — skipped")
        continue
    else:
        w = hose(client, fd, "client-wayland-end")
    sent += w; ends += 1 if w else 0
print(f"hose done ends={ends} sent={sent} sandbox_end={'yes' if sentry_fd else 'no'} sandbox_bytes={sandbox_bytes}")
PY
sed 's/^/    channel-hose: /' "$WORK/hose-channels.log"
is "waypipe frames written onto the SANDBOX end of the link (at the trusted client parser)" \
    "$(yes_no test "$(sed -n 's/.*sandbox_bytes=\([0-9]*\).*/\1/p' "$WORK/hose-channels.log")" -ge 1)" yes
is "hose wrote onto at least two attributed bridge sockets" \
    "$(yes_no test "$(sed -n 's/^hose done ends=\([0-9]*\).*/\1/p' "$WORK/hose-channels.log")" -ge 2)" yes
sleep 2   # let any delayed connection teardown land before the verdicts

step "3. blast radius after the channel attacks: only A's bridge may die"
is "compositor still the same pid, unit active" \
    "$(comp_pid):$(as_admin systemctl --user is-active qdwin-compositor.service 2>/dev/null)" \
    "$CPID_BEFORE:active"
is "qdshell still the SAME pid (no restart) and active" \
    "$(as_admin systemctl --user show qdshell.service -p MainPID --value 2>/dev/null):$(as_admin systemctl --user is-active qdshell.service 2>/dev/null)" \
    "$QS_BEFORE:active"
is "no compositor/qdshell crash in the journal since the attack" \
    "$(journalctl _SYSTEMD_USER_UNIT=qdwin-compositor.service _SYSTEMD_USER_UNIT=qdshell.service --no-pager -o cat --since '-30 sec' 2>/dev/null | grep -ciE 'segfault|panic|fatal|assertion.*failed')" 0

step "4. A's bridge: either dropped the bad connection or died — both in-contract"
BP_A=$(rec "$TA" bridge_client_pid); BS_A=$(rec "$TA" bridge_client_starttime)
if [ -n "$BP_A" ] && [ "$(starttime "$BP_A" 2>/dev/null)" = "$BS_A" ]; then
    pass "A's bridge client survived the garbage (waypipe dropped the bad stream)"
    is "A's bridge channel still established" "$(yes_no bridge_stream_live "$TA")" yes
else
    pass "A's bridge connection died under the hostile stream (pid ${BP_A:-?} gone)"
    is "A's toplevel is gone from qdshell (handle freed with the client)" \
        "$(qs_ipc tier3focus findSiloHandle "$SA" 2>/dev/null | head -1)" "HANDLE=-1"
fi

step "5. B's launch is completely untouched"
is "B's record still running" "$(rec "$TB" phase)" running
is "B's bridge client still live (starttime verified)" \
    "$(st=$(rec "$TB" bridge_client_starttime); p=$(rec "$TB" bridge_client_pid); [ -n "$p" ] && [ "$(starttime "$p" 2>/dev/null)" = "$st" ] && echo yes || echo no)" yes
is "B's bridge channel still established" "$(yes_no bridge_stream_live "$TB")" yes
is "B's toplevel still in the qdshell model" \
    "$(qs_ipc tier3focus findSiloHandle "$SB" 2>/dev/null | head -1 | grep -cv 'HANDLE=-1')" 1
is "B's container still running" "$(ctr_status "$SB")" running

step "6. teardown: both launches come down clean"
for s in $SA $SB; do
    sm StopSilo si "$s" 10 > /dev/null
    is "StopSilo $s" "$(silo_state "$s")" Stopped
    wait_for 90 unit_down "$(unit_of "$s")"
done
assert_bridge_gone "cleanup/A" "$TA"; assert_launch_gone "cleanup/A" "$TA" "$SA"
assert_bridge_gone "cleanup/B" "$TB"; assert_launch_gone "cleanup/B" "$TB" "$SB"
for s in $SA $SB; do sm DeleteSilo s "$s" > /dev/null; is "DeleteSilo $s" "$(silo_state "$s")" absent; done
set_rules none
assert_all_clear end
finish

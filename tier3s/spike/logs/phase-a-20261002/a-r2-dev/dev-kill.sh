#!/bin/bash
# dev-kill.sh - GUEST (root), dev VM only. astra A r2 #2 / fable A r2 P3-1 on
# real systemd + cgroups: the installed qdistro-tier3s-cleanup against a
# podman that starts a helper in a NEW SESSION which ignores SIGTERM and holds
# the call's stdout/stderr, then hangs. The fake is bind-mounted over
# /usr/bin/podman in a PRIVATE mount namespace around each cleanup run only
# (unshare -m; `systemd-run --scope` execs in its caller's namespace), so the
# rest of the VM keeps the real podman. Records are synthetic.
set -u
P=0; F=0
pass() { echo "PASS: $*"; P=$((P + 1)); }
fail() { echo "FAIL: $*"; F=$((F + 1)); }
is() { if [ -n "$3" ] && [ "$2" = "$3" ]; then pass "$1 ($2)"; else fail "$1: got '$2', want '$3'"; fi; }
CLEANUP=/usr/libexec/qdistro/qdistro-tier3s-cleanup
CTL=/run/qdistro-tier3s-ctl
D=/var/tmp/t3s-r2-kill
rm -rf "$D"; install -d -m 0777 "$D"
alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }
callscopes() { systemctl list-units --all --plain --no-legend 'qdistro-t3s-call-*.scope' | grep -c .; }

echo "## 0. facts the r2 code relies on (systemd $(systemctl --version | head -1 | cut -d' ' -f2))"
out=$(systemctl show -p ActiveState --value qdistro-tier3s-0000000000000000000000000000dead.scope); rc=$?
is "show ActiveState of a never-loaded scope: completed, inactive" "$rc:$out" "0:inactive"
out=$(systemctl show -p ActiveState --value qdistro-session-manager.service); rc=$?
is "show ActiveState of a live unit" "$rc:$out" "0:active"
is "cgroup.kill exists (kernel >= 5.14)" "$([ -e /sys/fs/cgroup/system.slice/cgroup.kill ] && echo yes)" yes

cat > "$D/podman" <<'FAKE'
#!/bin/bash
# fake podman (dev VM only): a helper in a NEW session, ignoring SIGTERM,
# holding stdout/stderr; then hang
setsid bash -c 'trap "" TERM; exec sleep 600' &
echo "$!" > /var/tmp/t3s-r2-kill/escapee.pid
echo "$$" > /var/tmp/t3s-r2-kill/podman.pid
sleep 600
FAKE
chmod 0755 "$D/podman"
# fake_ns cmd...: cmd in a private mount namespace where /usr/bin/podman is the fake
FAKE_NS=(unshare -m --propagation private bash -c 'mount --bind "$1" /usr/bin/podman && shift && exec "$@"' _ "$D/podman")
fake_ns() { "${FAKE_NS[@]}" "$@"; }
is "the fake is visible only inside fake_ns" \
    "$(fake_ns cat /usr/bin/podman | grep -c 'fake podman'):$(grep -c 'fake podman' /usr/bin/podman)" "1:0"

mkrec() {   # mkrec <token>: a synthetic, valid, never-started record
    install -d -m 0700 "$CTL/$1"
    ( umask 077; printf '%s\n' schema=1 "token=$1" container=qdistro-tier3s-killtest \
        unit=qdistro-tier3s-silo@killtest.service "scope_unit=qdistro-tier3s-$1.scope" admin_uid=1000 \
        runsc_root=/run/qdistro-tier3s-runsc/1000 "per_launch_dir=/run/qdistro-tier3s/$1" phase=created \
        > "$CTL/$1/state" )
}
lockfree() { flock -n "$CTL/$1" true && echo yes || echo no; }
reset() { rm -f "$D/escapee.pid" "$D/podman.pid"; }

echo; echo "## 1. a call that times out: its new-session, TERM-ignoring helper dies with the call scope"
T1=11111111111111111111111111111111; mkrec $T1; reset
t0=$SECONDS
fake_ns $CLEANUP $T1 > "$D/c1.out" 2>&1 < /dev/null; rc=$?
took=$((SECONDS - t0)); sed 's/^/    /' "$D/c1.out"
is "cleanup fails the token (podman query failed, rc 4)" "$rc:$(grep -c 'podman query failed' "$D/c1.out")" "4:1"
is "it returned within the call bound + kill grace (<= 30 s)" "$([ "$took" -le 30 ] && echo yes || echo "no ($took s)")" yes
E=$(cat "$D/escapee.pid" 2>/dev/null); H=$(cat "$D/podman.pid" 2>/dev/null)
is "the fake podman started its new-session helper" "$([ -n "$E" ] && echo yes)" yes
sleep 1
is "the new-session TERM-ignoring helper is dead" "$(alive "$E" && echo alive || echo dead)" dead
is "the hung podman is dead" "$(alive "$H" && echo alive || echo dead)" dead
is "no call scope left" "$(callscopes)" 0
is "record preserved" "$([ -f "$CTL/$T1/state" ] && echo yes)" yes
is "token lock free" "$(lockfree $T1)" yes
is "no work dir left" "$(find "$CTL" -maxdepth 1 -name '.call-*' | grep -c .)" 0

echo; echo "## 2. SIGTERM to the cleanup mid-call: the call in flight dies at once"
reset
# a simple command in the background (not a function: that would fork a
# subshell and $! would not be the cleanup); unshare and bash exec into it
"${FAKE_NS[@]}" $CLEANUP $T1 > "$D/c2.out" 2>&1 < /dev/null &
cp=$!
for _ in $(seq 1 100); do [ -s "$D/escapee.pid" ] && break; sleep 0.1; done
E=$(cat "$D/escapee.pid" 2>/dev/null); H=$(cat "$D/podman.pid" 2>/dev/null)
sleep 0.5
is "the TERM goes to the cleanup itself" "$(tr '\0' ' ' < /proc/$cp/cmdline | cut -d' ' -f1-2)" "/bin/bash $CLEANUP"
t0=$SECONDS; kill -TERM $cp; wait $cp; rc=$?
sed 's/^/    /' "$D/c2.out"
is "cleanup exits 143 on TERM" "$rc" 143
sleep 1
is "the helper is dead right after" "$(alive "$E" && echo alive || echo dead)" dead
is "the hung podman is dead right after" "$(alive "$H" && echo alive || echo dead)" dead
is "no call scope left" "$(callscopes)" 0
is "record preserved, lock free" "$([ -f "$CTL/$T1/state" ] && echo yes):$(lockfree $T1)" "yes:yes"

echo; echo "## 3. the whole supervisor SIGKILLed (its unit's cgroup killed): systemd ends the call by RuntimeMaxSec"
reset
# the supervisor (cleanup + its timeout(1)) in a scope of its own, like a launch
# unit's ExecStop: killing that cgroup kills the supervisor, not the call scope
systemd-run --scope --unit=t3s-r2-supervisor --collect -q \
    unshare -m --propagation private bash -c 'mount --bind "$1" /usr/bin/podman && shift && exec "$@"' _ "$D/podman" \
    $CLEANUP $T1 > "$D/c3.out" 2>&1 < /dev/null &
for _ in $(seq 1 100); do [ -s "$D/escapee.pid" ] && break; sleep 0.1; done
E=$(cat "$D/escapee.pid" 2>/dev/null); H=$(cat "$D/podman.pid" 2>/dev/null)
sc=$(systemctl list-units --all --plain --no-legend 'qdistro-t3s-call-*.scope' | awk '{print $1}' | head -1)
echo "    call scope: $sc  RuntimeMaxUSec=$(systemctl show -p RuntimeMaxUSec --value "$sc") TimeoutStopUSec=$(systemctl show -p TimeoutStopUSec --value "$sc")"
start=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$sc")
echo "    supervisor scope procs: $(cat /sys/fs/cgroup/system.slice/t3s-r2-supervisor.scope/cgroup.procs | tr '\n' ' ')"
echo "    call scope procs: $(cat "/sys/fs/cgroup/system.slice/$sc/cgroup.procs" | tr '\n' ' ')"
systemctl kill --signal=KILL t3s-r2-supervisor.scope; sleep 1
is "the supervisor (cleanup + timeout) is gone" "$(systemctl show -p ActiveState --value t3s-r2-supervisor.scope)" inactive
is "nothing of the supervisor is left to act on the call" "$(cat /sys/fs/cgroup/system.slice/t3s-r2-supervisor.scope/cgroup.procs 2>/dev/null | grep -c .)" 0
for _ in $(seq 1 90); do alive "$E" || break; sleep 0.5; done
up=$(awk '{printf "%d", $1 * 1000000}' /proc/uptime)
age=$(( (up - start) / 1000000 ))
is "systemd killed the TERM-ignoring helper after the call scope's RuntimeMaxSec (26 s) + TimeoutStopSec (5 s)" \
    "$(alive "$E" && echo alive || { [ "$age" -ge 30 ] && [ "$age" -le 34 ] && echo "dead at ~31s" || echo "dead at ${age}s (outside 30..34)"; })" "dead at ~31s"
echo "    (scope age when the helper was found dead: ${age}s)"
journalctl -u "$sc" --no-pager -o cat | sed 's/^/    pid1: /'
is "systemd stopped it for its runtime limit" "$(journalctl -u "$sc" --no-pager -o cat | grep -ci 'runtime time limit')" 1
is "and the hung podman" "$(alive "$H" && echo alive || echo dead)" dead
sleep 1
is "no call scope left" "$(callscopes)" 0
is "record preserved, lock free" "$([ -f "$CTL/$T1/state" ] && echo yes):$(lockfree $T1)" "yes:yes"
echo "    (the SIGKILLed run's work dir: $(find "$CTL" -maxdepth 1 -name '.call-*' | tr '\n' ' '))"

echo; echo "## 4. --reap-stale sweeps the dead run's work dir; the real podman tears the record down"
is "real podman outside the namespace" "$(runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin XDG_RUNTIME_DIR=/run/user/1000 podman --version | cut -d' ' -f1-2)" "podman version"
$CLEANUP --reap-stale > "$D/c4.out" 2>&1 < /dev/null; rc=$?
sed 's/^/    /' "$D/c4.out"
is "no work dir left after --reap-stale" "$(find "$CTL" -maxdepth 1 -name '.call-*' | grep -c .)" 0
is "the record was torn down with the real podman" "$rc:$([ -e "$CTL/$T1" ] && echo left || echo gone)" "0:gone"
is "no call scope left" "$(callscopes)" 0
echo "[dev-kill] $P passes, $F failures"
[ "$F" -eq 0 ]

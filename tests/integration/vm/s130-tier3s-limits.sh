#!/bin/bash
# s130-tier3s-limits.sh — GUEST driver (root) for phase7-tier3s-limits.bats.
# Tier 3s Phase C (todo/paravirt 10): the owning scope's limits are enforced,
# not just set (tier3s/CONTRACT.md §3 D-A3b):
#   step 1  the live scope carries the contract's limit files with the
#           contract values, root-owned — an admin write to every one fails
#           (the selective delegation hands admin the dir and cgroup.procs,
#           never a limit file);
#   step 2  CPUQuota=200% throttles: 4 busy loops inside the sandbox grow the
#           scope's cpu.stat nr_throttled and throttled_usec;
#   step 3  TasksMax=1024 bounds the scope's host task count: an outside
#           admin process cannot inject itself into the scope (the v2
#           migration rule wants write on the common ancestor's
#           cgroup.procs, not just the delegated file), and a guest-side
#           fork bomb pushes the scope's pids.current to the ceiling —
#           the causal latch is the scope's own pids.events max counter
#           (the kernel's count of forks this cgroup's pids.max denied),
#           not the bomb's outcome, which is fatal to this sandbox;
#   step 4  MemoryMax=2G (MemorySwapMax=0) OOM-kills: a guest hog pushes
#           the scope's memory.current over the cap, and the scope's own
#           memory.events oom/oom_kill counters increment — the
#           attributable proof (a global OOM does not move them), not a
#           journal grep; the launch dies and the verified teardown runs;
#   step 5  a recursive cgroup.procs placement re-proof on the live silo —
#           a limit that does not apply to a straggler is not containment.
# Runs after tier3s-guest-setup.sh. Each check prints one PASS/FAIL line;
# `[s130] N passes, M failures`; exit 1 on any failure.
set -u
T3S_TAG=s130
. "$(dirname "$0")/tier3s-guest-lib.sh"
SA=s130a; SB=s130b; SC=s130c

step "0. preconditions (setup ran)"
out=$(/usr/lib/qdistro/tier3s/probe.sh --user admin 2>&1); rc=$?
is "probe PASS before the launches" "$rc:$(printf '%s\n' "$out" | grep -c '^RESULT PASS')" "0:1"
is "image present" "$(yes_no pm image exists "$IMAGE")" yes
is "broker allows the smoke spawn" "$(broker_check "$ACTION")" allow
assert_all_clear pre
for s in $SA $SB $SC; do
    sm CreateTier3sSilo ssss "$s" headless-smoke "$s" none > /dev/null
    is "CreateTier3sSilo $s" "$(silo_state "$s")" Created
done
set_argv "$SA=600" "$SB=600" "$SC=600" | sed 's/^/    /'
is "argv set with the manager restarted" "$(yes_no manager_up)" yes

# ---------------------------------------------------------------------------
step "1. live silo A: the scope's limit files, values, ownership; placement"
TA=$(up_silo $SA); CA=$(ctr_of $SA); UA=$(unit_of $SA)
if [ -n "$TA" ]; then pass "silo A $TA recorded running"; else fail "silo A did not come up"; finish; fi
cg="/sys/fs/cgroup$(rec "$TA" scope_cgroup)"
[ -d "$cg" ] || { fail "scope cgroup $cg is gone"; finish; }

# --- the contract's files carry the contract's values, root-owned
is "limits: memory.max = MemoryMax=2G" "$(cat "$cg/memory.max" 2>/dev/null)" 2147483648
is "limits: memory.swap.max = MemorySwapMax=0" "$(cat "$cg/memory.swap.max" 2>/dev/null)" 0
is "limits: pids.max = TasksMax=1024" "$(cat "$cg/pids.max" 2>/dev/null)" 1024
is "limits: cpu.max = CPUQuota=200%" "$(cat "$cg/cpu.max" 2>/dev/null)" "200000 100000"
# a valid higher value for each file — an invalid write would fail EINVAL
# and tell us nothing about the delegation (sol r1 P4). The errno is the
# evidence: only EACCES/EPERM proves the selective delegation holds.
declare -A RAISE=( [memory.max]=3221225472 [memory.swap.max]=1 [pids.max]=2048 [cpu.max]='300000 100000' )
for f in memory.max memory.swap.max pids.max cpu.max; do
    is "limits: $f is root's, not admin's" "$(stat -c %u "$cg/$f" 2>/dev/null)" 0
    werr=$(runuser -u admin -- bash -c "echo '${RAISE[$f]}' > '$cg/$f'" 2>&1); wrc=$?
    if [ "$wrc" -eq 0 ]; then
        fail "limits: admin wrote $f (selective delegation broken)"
    elif printf '%s' "$werr" | grep -qiE 'permission denied|EACCES|EPERM'; then
        pass "limits: admin write to $f fails with EACCES/EPERM (rc=$wrc)"
    else
        fail "limits: admin write to $f failed but not with a permission error: $werr (rc=$wrc) — the denial is not proven to be the delegation"
    fi
done
# and the delegation pieces that ARE admin's (the launch path depends on it):
# the dir, cgroup.procs, cgroup.subtree_control, cgroup.threads (D-A3b)
is "limits: scope dir itself is delegated to admin" "$(stat -c %u "$cg" 2>/dev/null)" 1000
is "limits: cgroup.procs delegated (admin-writable)" "$(yes_no runuser -u admin -- test -w "$cg/cgroup.procs")" yes

# --- placement re-proof: every class inside, no runsc-bundle process outside
classify() {   # classify <pid> -> class name (same classes as s120)
    local p="$1" exe a0
    exe=$(readlink "/proc/$p/exe" 2>/dev/null); a0=$(tr '\0' '\n' < "/proc/$p/cmdline" 2>/dev/null | head -1)
    case "$exe" in
        /usr/sbin/runuser) echo runuser ;;
        /usr/bin/podman) echo podman-cli ;;
        /usr/bin/conmon) echo conmon ;;
        /usr/libexec/qdistro/runsc/runsc) [ "$a0" = runsc-gofer ] && echo runsc-gofer || echo "runsc:$a0" ;;
        /usr/libexec/qdistro/runsc/gvisor-bin/gvisor_sentry)
            if [ "$p" = "$IPID" ] && [ "$a0" = runsc-sandbox ]; then echo runsc-sandbox
            elif [ -z "$a0" ]; then echo systrap-stub
            else echo "sentry:$a0"; fi ;;
        /usr/libexec/qdistro/runsc/gvisor-bin/runsc-fd-parking) echo runsc-fd-parking ;;
        *) echo "other:${exe:-?}" ;;
    esac
}
IPID=$(pm inspect --format '{{.State.Pid}}' "$CA")
declare -A n=()
for p in $(tree_procs "$cg"); do c=$(classify "$p"); n[$c]=$(( ${n[$c]:-0} + 1 )); done
for c in runuser podman-cli conmon runsc-gofer runsc-sandbox runsc-fd-parking systrap-stub; do
    if [ "${n[$c]:-0}" -ge 1 ]; then pass "placement[A]: $c x${n[$c]} inside the scope"
    else fail "placement[A]: no $c process inside the scope"; fi
done
outside=0
for p in $(runsc_pids); do tree_procs "$cg" | grep -qx "$p" || outside=$((outside + 1)); done
is "placement[A]: runsc-bundle processes outside the owning scope" "$outside" 0

# ---------------------------------------------------------------------------
step "2. CPUQuota=200% on silo A: in-sandbox burn grows cpu.stat throttling"
nr_throttled() { sed -n 's/^nr_throttled //p' "$cg/cpu.stat" 2>/dev/null; }
throttled_us() { sed -n 's/^throttled_usec //p' "$cg/cpu.stat" 2>/dev/null; }
nt0=$(nr_throttled); tu0=$(throttled_us)
is "cpu: counters readable before the burn" "$(yes_no test -n "$nt0$tu0")" yes
# 4 burners wanting ~400% of one core on a 200% cap; self-terminating via jobs
pm exec "$CA" sh -c '
    for i in 1 2 3 4; do ( while :; do :; done ) & done
    sleep 12
    kill $(jobs -p) 2>/dev/null; wait 2>/dev/null
    echo BURN_DONE' > "$WORK/burn.out" 2>&1 &
BURNPID=$!
peak_nr=0; end=$((SECONDS + 20))
while [ "$SECONDS" -lt "$end" ] && kill -0 "$BURNPID" 2>/dev/null; do
    n=$(nr_throttled); [ -n "$n" ] && [ "$n" -gt "$peak_nr" ] && peak_nr=$n
    sleep 0.5
done
wait "$BURNPID" 2>/dev/null
nt1=$(nr_throttled); tu1=$(throttled_us)
is "cpu: burn ran inside the sandbox" "$(grep -c BURN_DONE "$WORK/burn.out")" 1
if [ "${nt1:-0}" -gt "${nt0:-0}" ]; then
    pass "cpu: nr_throttled grew under a 400%-hungry load (${nt0:-?} -> $nt1, peak $peak_nr)"
else
    fail "cpu: nr_throttled flat (${nt0:-?} -> ${nt1:-?}) — CPUQuota not enforced"
fi
is "cpu: throttled_usec grew" "$([ "${tu1:-0}" -gt "${tu0:-0}" ] && echo yes)" yes

# ---------------------------------------------------------------------------
step "3. TasksMax=1024 on silo C: a guest fork bomb plateaus at pids.max"
TC=$(up_silo $SC); CC=$(ctr_of $SC)
if [ -n "$TC" ]; then pass "silo C $TC recorded running"; else fail "silo C did not come up"; finish; fi
cgt="/sys/fs/cgroup$(rec "$TC" scope_cgroup)"
pids0=$(cat "$cgt/pids.current"); pmax=$(cat "$cgt/pids.max")
is "tasks: C's scope really is at TasksMax=1024" "$pmax" 1024
pev() { sed -n 's/^max //p' "$cgt/pids.events" 2>/dev/null; }
pev0=$(pev); pev0=${pev0:-0}
info "pids.current before the bomb: $pids0 (limit $pmax), pids.events max=$pev0"
# cgroup v2 migration rule: moving a task into a cgroup needs write access
# on the LOWEST COMMON ANCESTOR's cgroup.procs too — for an admin process in
# user.slice writing into a system.slice scope that ancestor is the root
# cgroup, which is root's. So the delegated cgroup.procs lets admin move
# tasks WITHIN the scope subtree (what podman needs) but never inject an
# outside process into it. Assert that boundary first.
moved=$(runuser -u admin -- env -i PATH=/usr/bin:/bin bash -c \
    "echo \$\$ > '$cgt/cgroup.procs' 2>/dev/null && echo MOVED=yes || echo MOVED=no")
is "tasks: an outside admin process cannot inject itself into the scope" "$moved" MOVED=no
# 1500 guest tasks map to host tasks inside the Sentry/stubs; pids.max=1024
# makes the mapping hit the ceiling, and hitting it is fatal to this
# sandbox (the Sentry cannot survive a failed task create) — bounded
# either way, never over.
pm exec "$CC" sh -c '
    i=0; while [ "$i" -lt 1500 ]; do i=$((i+1)); sleep 20 & done
    j=0; for p in $(jobs -p); do j=$((j+1)); done
    echo "GUEST_JOBS=$j"; wait 2>/dev/null; echo GUEST_BOMB_DONE' > "$WORK/guestbomb.out" 2>&1 &
GBPID=$!
gpeak=0; evpeak=$pev0; end=$((SECONDS + 90))
while [ "$SECONDS" -lt "$end" ] && kill -0 "$GBPID" 2>/dev/null; do
    n=$(cat "$cgt/pids.current" 2>/dev/null || echo 0)
    [ "$n" -gt "$gpeak" ] && gpeak=$n
    e=$(pev); [ -n "$e" ] && [ "$e" -gt "$evpeak" ] && evpeak=$e
    sleep 0.25
done
wait "$GBPID" 2>/dev/null
head -5 "$WORK/guestbomb.out" | sed 's/^/    guest bomb: /'
gjobs=$(sed -n 's/^GUEST_JOBS=\([0-9]*\).*/\1/p' "$WORK/guestbomb.out" | tail -1)
info "guest bomb: host peak pids.current=$gpeak (limit $pmax), pids.events max $pev0 -> $evpeak, guest jobs=${gjobs:-died}, fork-error lines: $(grep -ci 'cannot fork\|resource temporarily' "$WORK/guestbomb.out")"
is "tasks: host pids.current never exceeded pids.max" "$(yes_no test "$gpeak" -le "$pmax")" yes
# the causal latch: pids.events max counts every fork this scope's pids.max
# denied — without a delta the bound is a coincidence, not enforcement
if [ "$evpeak" -gt "$pev0" ]; then
    pass "tasks: pids.events max grew — this scope's pids.max denied a fork ($pev0 -> $evpeak)"
else
    fail "tasks: pids.events max flat ($pev0) — the bound is not proven to be pids.max enforcement"
fi
# crossing the ceiling kills the sandbox OR the guest saw fork failures —
# either way all 1500 did not become host tasks and stay that way
if [ -n "$gjobs" ] && [ "$gjobs" -ge 1500 ] && [ "$(ctr_status "$CC")" = running ]; then
    fail "tasks: all 1500 guest tasks live and the sandbox is healthy — pids.max did not bite"
else
    pass "tasks: the bomb was bounded (guest jobs=${gjobs:-died}, ctr=$(ctr_status "$CC" 2>/dev/null || echo gone))"
fi
info "memory.events on C's scope after the bomb: $(tr '\n' ' ' 2>/dev/null < "$cgt/memory.events" || printf 'scope gone')"
sm StopSilo si $SC 10 > /dev/null; is "tasks teardown: StopSilo C rc" "$?" 0
assert_launch_gone tasks "$TC" "$CC"

# ---------------------------------------------------------------------------
step "4. MemoryMax=2G on silo B: a guest hog triggers the scope's OOM kill"
TB=$(up_silo $SB); CB=$(ctr_of $SB); UB=$(unit_of $SB)
if [ -n "$TB" ]; then pass "silo B $TB recorded running"; else fail "silo B did not come up"; finish; fi
cgb="/sys/fs/cgroup$(rec "$TB" scope_cgroup)"
# B's configured limits are the same contract values A showed — assert them
# on B so the OOM below is attributable to THIS scope's cap
is "memory: B's memory.max = 2G" "$(cat "$cgb/memory.max" 2>/dev/null)" 2147483648
is "memory: B's memory.swap.max = 0" "$(cat "$cgb/memory.swap.max" 2>/dev/null)" 0
mev() { sed -n "s/^$1 //p" "$cgb/memory.events" 2>/dev/null; }
k0=$(mev oom_kill); k0=${k0:-0}
o0=$(mev oom); o0=${o0:-0}
kcur=$(journal_cursor)
info "memory.events before the hog: $(tr '\n' ' ' < "$cgb/memory.events")"
# ~2.6 GiB in a shell variable (tr avoids bash's NUL stripping): the guest's
# memory is host memory of the Sentry/stubs inside the scope. timeout gets
# the real runuser argv — it cannot exec the pm() shell function.
timeout 240 runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin USER=admin LOGNAME=admin \
    XDG_RUNTIME_DIR=/run/user/1000 podman exec "$CB" \
    sh -c 'big=$(head -c 2600M /dev/zero | tr "\0" a); echo SURVIVED ${#big}' \
    > "$WORK/hog.out" 2>&1 &
HOGPID=$!
peak_k=0; peak_o=0; end=$((SECONDS + 240))
while [ "$SECONDS" -lt "$end" ]; do
    kill -0 "$HOGPID" 2>/dev/null || break
    k=$(mev oom_kill); [ -n "$k" ] && [ "$k" -gt "$peak_k" ] && peak_k=$k
    o=$(mev oom);      [ -n "$o" ] && [ "$o" -gt "$peak_o" ] && peak_o=$o
    [ "$peak_o" -gt "$o0" ] && break
    sleep 0.25
done
wait "$HOGPID" 2>/dev/null
# the kill frees the cgroup fast; latch the last read
k1=$(mev oom_kill); k1=${k1:-$peak_k}
o1=$(mev oom);      o1=${o1:-$peak_o}
printf '    hog: %s\n' "$(tail -3 "$WORK/hog.out" | tr '\n' '|')"
# memory.events oom counts OOMs where THIS cgroup's own limit was the
# trigger — a global OOM or another cgroup's OOM does not move it, so its
# delta is the attributable proof; oom_kill counts the victims (sol r1 P2).
# The counter can vanish with the scope before a poll latches it, so the
# fallback is a kernel OOM line naming B's own scope token — scoped, never
# a bare oom grep (a global OOM would not carry it).
kj=$(journalctl -k --after-cursor="$kcur" --no-pager -o cat 2>/dev/null \
    | grep -iE 'oom' | grep -c "$TB")
printf '    kernel oom lines naming %s: %s\n' "$TB" "$kj"
if [ "$o1" -gt "$o0" ] || [ "$peak_o" -gt "$o0" ]; then
    pass "memory: B's own memory.max triggered the OOM (oom $o0 -> $o1, peak $peak_o; oom_kill $k0 -> ${k1:-gone})"
elif [ "$kj" -gt 0 ]; then
    pass "memory: scope reaped before the counter latched; kernel OOM lines name B's scope ($kj)"
else
    fail "memory: B's memory.events oom stayed $o0 and no kernel OOM names its scope — not proven to be B's memory.max (oom_kill $k0 -> ${k1:-gone})"
fi
# whichever in-scope process died, the launch cannot survive its Sentry
wait_for 120 unit_down "$UB"
if unit_down "$UB"; then pass "memory: the hog's launch unit is down ($(systemctl show -p Result --value "$UB"))"
else fail "memory: launch unit still $(unit_state "$UB") after the OOM"; fi
assert_launch_gone memory "$TB" "$CB" 90
sm StopSilo si $SB 10 > /dev/null; is "memory: StopSilo B afterwards" "$(silo_state $SB)" Stopped

# ---------------------------------------------------------------------------
step "5. teardown of silo A + all clear"
sm StopSilo si $SA 10 > /dev/null; is "StopSilo A rc" "$?" 0
assert_launch_gone cpu "$TA" "$CA"
for s in $SA $SB $SC; do sm DeleteSilo s "$s" > /dev/null; is "DeleteSilo $s" "$(silo_state "$s")" absent; done
assert_all_clear end
finish

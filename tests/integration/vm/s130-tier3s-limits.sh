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
#   step 3  TasksMax=1024 bounds the scope's host task count: an admin helper
#           that moves ITSELF into the scope (delegated cgroup.procs) and
#           fork-bombs plateaus at pids.max with fork failures — and a
#           guest-side fork bomb never pushes the host scope past it
#           (gVisor runs guest tasks inside the Sentry: a guest fork bomb is
#           a Sentry memory problem — step 4 — while every host process the
#           sandbox spawns stays under pids.max);
#   step 4  MemoryMax=2G (MemorySwapMax=0) OOM-kills: a guest hog pushes the
#           scope's memory.current over the cap, memory.events oom_kill
#           increments, the launch dies and the verified teardown runs;
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
for f in memory.max memory.swap.max pids.max cpu.max; do
    is "limits: $f is root's, not admin's" "$(stat -c %u "$cg/$f" 2>/dev/null)" 0
    # an actual write attempt, not a -w guess (the dir being admin's could
    # fool a mode check; the open() verdict is what counts)
    if runuser -u admin -- bash -c "echo 1 > '$cg/$f'" 2>/dev/null; then
        fail "limits: admin wrote $f (selective delegation broken)"
    else
        pass "limits: admin write to $f fails (EACCES)"
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
step "3. TasksMax=1024 on silo C: fork bomb inside the scope plateaus at pids.max"
TC=$(up_silo $SC); CC=$(ctr_of $SC)
if [ -n "$TC" ]; then pass "silo C $TC recorded running"; else fail "silo C did not come up"; finish; fi
cgt="/sys/fs/cgroup$(rec "$TC" scope_cgroup)"
pids0=$(cat "$cgt/pids.current")
info "pids.current before the bomb: $pids0 (limit $(cat "$cgt/pids.max"))"
# An admin helper moves ITSELF into the scope (self-move needs only write
# access to the destination cgroup.procs — the delegated file — never the
# ancestor's) and fork-bombs. Pure builtins at the peak: a fork is what we
# are exhausting, so the counter reads cannot fork.
bomb() {
    runuser -u admin -- env -i PATH=/usr/bin:/bin bash -c '
        cg="'"$cgt"'"
        echo $$ > "$cg/cgroup.procs" 2>/dev/null || { echo MOVED=no; exit 9; }
        echo MOVED=yes
        i=0
        while [ "$i" -lt 1500 ]; do i=$((i+1)); sleep 20 & done
        j=0; for p in $(jobs -p); do j=$((j+1)); done
        read -r PC < "$cg/pids.current"
        echo "JOBS=$j PIDS=$PC"
        kill $(jobs -p) 2>/dev/null; wait 2>/dev/null
        echo BOMB_DONE'
}
bomb_out=$(bomb 2>&1); bomb_rc=$?
printf '%s\n' "$bomb_out" | sed 's/^/    bomb: /'
info "bomb rc=$bomb_rc (a nonzero rc is the fork failure path ending the run)"
is "tasks: admin helper moved itself into the scope" "$(printf '%s\n' "$bomb_out" | grep -c '^MOVED=yes')" 1
jobs_n=$(printf '%s\n' "$bomb_out" | sed -n 's/^JOBS=\([0-9]*\).*/\1/p')
pids_n=$(printf '%s\n' "$bomb_out" | sed -n 's/^JOBS=.* PIDS=\([0-9]*\)/\1/p')
if [ -n "$jobs_n" ] && [ "$jobs_n" -lt 1500 ]; then
    pass "tasks: the scope's fork bomb was bounded ($jobs_n of 1500 children spawned)"
else
    fail "tasks: fork bomb spawned all 1500 children — pids.max not enforced"
fi
is "tasks: in-scope pids.current stayed at or under pids.max" "$([ "${pids_n:-99999}" -le 1024 ] && echo yes || echo "no:$pids_n")" yes
is "tasks: guest-visible fork failure on stderr" \
    "$(printf '%s\n' "$bomb_out" | grep -ci 'cannot fork\|resource temporarily unavailable' | sed 's/[1-9][0-9]*/yes/')" yes
# the bomb's own children are killed by the helper; pids.current drops back
wait_for 30 bash -c "[ \"\$(cat '$cgt/pids.current' 2>/dev/null || echo 9999)\" -le $((pids0 + 10)) ]"
is "tasks: scope pids.current back near baseline after the bomb" \
    "$([ "$(cat "$cgt/pids.current" 2>/dev/null || echo 9999)" -le $((pids0 + 10)) ] && echo yes || cat "$cgt/pids.current")" yes
is "tasks: silo C survived the injected bomb" "$(ctr_status "$CC")" running
# guest-side bomb: whatever gVisor does with guest tasks internally, the
# host footprint stays under pids.max. Guest "Cannot fork" lines are logged
# as evidence either way (a gVisor-emulated task table is bounded by the
# Sentry's memory — step 4 — not by host clone()).
pm exec "$CC" sh -c '
    i=0; while [ "$i" -lt 1500 ]; do i=$((i+1)); sleep 15 & done
    j=0; for p in $(jobs -p); do j=$((j+1)); done
    echo "GUEST_JOBS=$j"; wait 2>/dev/null; echo GUEST_BOMB_DONE' > "$WORK/guestbomb.out" 2>&1 &
GBPID=$!
gpeak=0; end=$((SECONDS + 40))
while [ "$SECONDS" -lt "$end" ] && kill -0 "$GBPID" 2>/dev/null; do
    n=$(cat "$cgt/pids.current" 2>/dev/null || echo 0)
    [ "$n" -gt "$gpeak" ] && gpeak=$n
    sleep 0.5
done
wait "$GBPID" 2>/dev/null
printf '%s\n' "$(head -5 "$WORK/guestbomb.out")" | sed 's/^/    guest bomb: /'
info "guest bomb host-side peak pids.current: $gpeak (limit 1024)"
is "tasks: in-guest fork bomb: host scope pids.current never exceeded pids.max" \
    "$(yes_no test "$gpeak" -le 1024)" yes
info "in-guest bomb outcome: $(grep -o 'GUEST_JOBS=[0-9]*' "$WORK/guestbomb.out" | tail -1) $(grep -ci 'cannot fork\|resource temporarily' "$WORK/guestbomb.out") fork-error lines"
is "tasks: silo C survived the guest bomb" "$(ctr_status "$CC")" running
cur=$(journal_cursor)
sm StopSilo si $SC 10 > /dev/null; is "tasks teardown: StopSilo C rc" "$?" 0
assert_launch_gone tasks "$TC" "$CC"

# ---------------------------------------------------------------------------
step "4. MemoryMax=2G on silo B: a guest hog triggers the scope's OOM kill"
TB=$(up_silo $SB); CB=$(ctr_of $SB); UB=$(unit_of $SB)
if [ -n "$TB" ]; then pass "silo B $TB recorded running"; else fail "silo B did not come up"; finish; fi
cgb="/sys/fs/cgroup$(rec "$TB" scope_cgroup)"
okill() { sed -n 's/^oom_kill //p' "$cgb/memory.events" 2>/dev/null || true; }
k0=$(okill); k0=${k0:-0}
kcur=$(journal_cursor)
info "memory.events before the hog: $(tr '\n' ' ' < "$cgb/memory.events")"
# ~2.6 GiB in a shell variable (tr avoids bash's NUL stripping): the guest's
# memory is host memory of the Sentry/stubs inside the scope.
timeout 240 pm exec "$CB" sh -c 'big=$(head -c 2600M /dev/zero | tr "\0" a); echo SURVIVED ${#big}' \
    > "$WORK/hog.out" 2>&1 &
HOGPID=$!
peak_k=0; end=$((SECONDS + 240))
while [ "$SECONDS" -lt "$end" ]; do
    kill -0 "$HOGPID" 2>/dev/null || break
    k=$(okill); [ -n "$k" ] && [ "$k" -gt "$peak_k" ] && peak_k=$k
    [ "$peak_k" -gt "$k0" ] && break
    sleep 0.25
done
wait "$HOGPID" 2>/dev/null
# the kill frees the cgroup fast; latch the last read AND the kernel log
k1=$(okill); k1=${k1:-$peak_k}
kern_oom=$(journalctl -k --after-cursor="$kcur" --no-pager -o cat 2>/dev/null | grep -c 'oom_kill\|Out of memory\|oom-kill')
printf '    hog: %s\n' "$(tail -3 "$WORK/hog.out" | tr '\n' '|')"
printf '    kernel oom lines since launch: %s\n' "$kern_oom"
if [ "$k1" -gt "$k0" ] || [ "$peak_k" -gt "$k0" ] || [ "$kern_oom" -gt 0 ]; then
    pass "memory: scope OOM-killed the 2.6 GiB hog (oom_kill ${k0} -> ${k1:-gone}, peak $peak_k)"
else
    fail "memory: no OOM kill observed (oom_kill $k0, hog output above)"
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

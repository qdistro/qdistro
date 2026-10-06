#!/bin/bash
# tier3s/bench/bench-guest.sh — Phase E measurement driver, GUEST side
# (root; enforcing or permissive worker provisioned by tier3s-guest-setup.sh).
# Sourced lib does the launches; this file only measures and prints
# `MEAS <key> <value> <unit>` rows plus INFO/PASS/FAIL. A run emits one
# pass; run-bench.sh invokes it 3x and medians land in 04-measurements.md.
#
#   bench-guest.sh                 — full pass (all sections)
#   bench-guest.sh <section> ...   — named sections only
#   bench-guest.sh latency-up      — bring up a GUI window and stay up
#                                    (host-side input/frame probe drives it)
#   bench-guest.sh latency-down    — stop the latency silo
#
# Sections: env cold mem sys io bridge overhead (idle-CPU rides mem, teardown
# times ride cold). Repeatable on one VM: silo creation is idempotent.
set -u
T3S_TAG=bench
. "$(dirname "$0")/tier3s-guest-lib.sh"

GS=benchgui          # GUI silo (weston-terminal) for cold/mem/cpu/teardown
HS=benchhead         # headless silo (headless-smoke --hold)
FS=benchflood        # foot silo for the waypipe bridge-load sample
BW=/var/tmp/t3s-bench
IMGW=localhost/qdistro/tier3s-weston-terminal:latest
mkdir -p "$BW"

emit() { echo "MEAS $*"; }
ts_us() { echo $(( $(date +%s%N) / 1000 )); }
# pss_kb <pid>: PSS out of smaps_rollup (never RSS: sentry+stubs share backing)
pss_kb() { sed -n 's/^Pss:[[:space:]]*\([0-9]*\).*/\1/p' "/proc/$1/smaps_rollup" 2>/dev/null; }
sum_pss_kb() {
    local t=0 p v
    for p in "$@"; do v=$(pss_kb "$p"); [ -n "$v" ] && t=$((t + v)); done
    echo "$t"
}
cg_mem() { cat "/sys/fs/cgroup$1/memory.current" 2>/dev/null; }
cg_usage_us() { sed -n 's/^usage_usec //p' "/sys/fs/cgroup$1/cpu.stat" 2>/dev/null; }
proc_cg() { sed -n 's/^0:://p' "/proc/$1/cgroup" 2>/dev/null; }

# The perl microbench (perl is in the workload images; no gcc on-VM and no
# static libc on the host, so a C bench can't be staged). Every probe times
# ITSELF with Time::HiRes, so podman-exec/attach latency never pollutes it.
write_syscost() {
    cat > "$BW/syscost.pl" <<'PL'
use Time::HiRes qw(time);
my ($s, $f);
$s = time; syscall(39) for 1..100000;                       # getpid
printf "getpid_ms_per_100k=%.3f\n", (time - $s) * 1000;
$s = time; for (1..20000) { open $f, '<', '/dev/null' or die "open"; close $f }
printf "openclose_ms_per_20k=%.3f\n", (time - $s) * 1000;
$s = time; for (1..1000) { system('/bin/true') == 0 or die "forkexec" }
printf "forkexec_ms_per_1k=%.3f\n", (time - $s) * 1000;
PL
}

# --- section: environment ---------------------------------------------------
sec_env() {
    step "env"
    emit kernel "$(uname -r)" -
    emit nproc "$(nproc)" -
    emit mem_total_mb "$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)" MB
    emit selinux "$(getenforce 2>/dev/null || echo none)" -
    emit runsc_release "$(pin_value release)" -
    emit runsc_version "$(/usr/libexec/qdistro/runsc/runsc --version 2>/dev/null | head -1 | tr -s ' ')" -
    emit host_is_vm "$(systemd-detect-virt 2>/dev/null || echo bare)" -
    emit commit "$(cat "$(dirname "$0")/commit.txt" 2>/dev/null || echo unknown)" -
    emit utc "$(date -u +%FT%TZ)" -
}

# --- provisioning (idempotent across passes) ---------------------------------
mk_silo() {   # mk_silo <name> <workload>
    sm CreateTier3sSilo ssss "$1" "$2" "$1" none > /dev/null 2>&1
    case "$(silo_state "$1")" in Created|Stopped|Active|Frozen) return 0 ;; esac
    echo "mk_silo: $1 state=$(silo_state "$1")" >&2; return 1
}
provision() {
    step "provision silos (broker allow + images)"
    mk_silo "$GS" weston-terminal || fail "create $GS"
    mk_silo "$HS" headless-smoke || fail "create $HS"
    mk_silo "$FS" foot || fail "create $FS"
    set_rules "allow:qdistro.tier3s.spawn:weston-terminal/weston-terminal" \
              "allow:qdistro.tier3s.spawn:headless-smoke/$SMOKE_APP" \
              "allow:qdistro.tier3s.spawn:foot/foot"
    set_argv_json "$HS=[\"qdistro-tier3s-smoke\",\"--hold\",\"900\"]" \
                  "$FS=[\"foot\",\"-e\",\"/bin/sh\",\"-c\",\"head -c 52428800 /dev/urandom | base64; sleep 30\"]"
    ensure_silo_image "$GS" weston-terminal || fail "ensure_silo_image $GS"
    ensure_silo_image "$HS" headless-smoke || fail "ensure_silo_image $HS"
    ensure_silo_image "$FS" foot || fail "ensure_silo_image $FS"
}

# --- section: cold start to window ------------------------------------------
# t0 = just before the StartSilo call; t1 = journal realtime ts of the
# qdshell "[tier3s] toplevel observed" marker. Also records the synchronous
# StartSilo call-return time (Type=notify: returns once phase=running).
sec_cold() {
    step "cold start to window (tier3s GUI launch)"
    local i cur t0 tc mus
    for i in 1 2 3; do
        cur=$(journal_cursor)
        t0=$(ts_us)
        sm StartSilo s "$GS" > /dev/null || { fail "cold: StartSilo $i"; continue; }
        tc=$(ts_us)
        mus=""
        for _ in $(seq 1 480); do
            mus=$(journalctl _SYSTEMD_USER_UNIT=qdshell.service -o json \
                  --after-cursor="$cur" --no-pager 2>/dev/null | python3 -c '
import json, sys
for line in sys.stdin:
    j = json.loads(line)
    if "[tier3s] toplevel observed" in j.get("MESSAGE", ""):
        print(j["__REALTIME_TIMESTAMP"]); break')
            [ -n "$mus" ] && break
            sleep 0.25
        done
        if [ -n "$mus" ]; then
            emit cold_start_to_window_ms_$i $(( (mus - t0) / 1000 )) ms
            emit cold_startsilo_call_ms_$i $(( (tc - t0) / 1000 )) ms
        else
            fail "cold: no toplevel marker within 120s (attempt $i)"
        fi
        teardown_one "$GS"
    done
    # nearest product baseline: podman create+start of the same image under
    # the default runtime (NOT a product tier-2 podapp launch — labelled so)
    for i in 1 2 3; do
        pm rm -f t2cold >/dev/null 2>&1
        t0=$(ts_us)
        pm create --name t2cold --network none --entrypoint sleep \
            "$IMGW" 600 > /dev/null \
            && pm start t2cold > /dev/null
        tc=$(ts_us)
        [ "$(pm inspect --format '{{.State.Status}}' t2cold 2>/dev/null)" = running ] \
            || { fail "cold: t2 container did not start"; continue; }
        emit t2_podman_create_start_ms_$i $(( (tc - t0) / 1000 )) ms
        pm rm -f t2cold > /dev/null 2>&1
    done
}

# --- section: idle memory (+ idle CPU riding the open window) ---------------
sec_mem() {
    step "idle memory"
    local tok scope_cg procs scope_cur pss bridge_pss htok hcg
    tok=$(up_gui_silo "$GS")
    [ -n "$tok" ] || { fail "mem: GUI launch failed"; return; }
    sleep 15
    scope_cg=$(rec "$tok" scope_cgroup)
    procs=$(tree_procs "/sys/fs/cgroup$scope_cg")
    scope_cur=$(cg_mem "$scope_cg")
    pss=$(sum_pss_kb $procs)
    bridge_pss=$(pss_kb "$(rec "$tok" bridge_client_pid)")
    emit t3s_gui_scope_memory_current_mb $(( ${scope_cur:-0} / 1048576 )) MB
    emit t3s_gui_scope_pss_mb $(( pss / 1024 )) MB
    emit t3s_gui_bridge_client_pss_mb $(( ${bridge_pss:-0} / 1024 )) MB
    emit t3s_gui_scope_procs "$(printf '%s\n' "$procs" | grep -c .)" count
    htok=$(up_silo "$HS")
    if [ -n "$htok" ]; then
        sleep 10
        hcg=$(rec "$htok" scope_cgroup)
        procs=$(tree_procs "/sys/fs/cgroup$hcg")
        emit t3s_headless_scope_memory_current_mb $(( $(cg_mem "$hcg") / 1048576 )) MB
        emit t3s_headless_scope_pss_mb $(( $(sum_pss_kb $procs) / 1024 )) MB
        emit t3s_headless_scope_procs "$(printf '%s\n' "$procs" | grep -c .)" count
    else
        fail "mem: headless launch failed"
    fi
    pm rm -f t2mem >/dev/null 2>&1
    pm run -d --name t2mem --network none --entrypoint sleep "$IMGW" 600 > /dev/null
    sleep 5
    local t2pid t2cg
    t2pid=$(pm inspect --format '{{.State.Pid}}' t2mem 2>/dev/null)
    t2cg=$(proc_cg "$t2pid")
    if [ -n "$t2cg" ]; then
        emit t2_idle_memory_current_mb $(( $(cg_mem "$t2cg") / 1048576 )) MB
        emit t2_idle_pss_mb $(( $(sum_pss_kb $(tree_procs "/sys/fs/cgroup$t2cg")) / 1024 )) MB
    else
        fail "mem: t2 cgroup unresolved"
    fi
    # idle CPU over 60s with the window open (plan: repeat under
    # --systrap-disable-fast-path is not wired through the launch path; the
    # syscall section shows the per-syscall cost it would amortise)
    local a0 a1 b0 b1 h0 h1
    a0=$(cg_usage_us "$scope_cg"); b0=$(cg_usage_us "$t2cg"); h0=$(cg_usage_us "$hcg")
    sleep 60
    a1=$(cg_usage_us "$scope_cg"); b1=$(cg_usage_us "$t2cg"); h1=$(cg_usage_us "$hcg")
    emit t3s_gui_idle_cpu_pct "$(python3 -c "print(f'{($a1-$a0)/6e6*100:.2f}')")" pct_core
    emit t3s_headless_idle_cpu_pct "$(python3 -c "print(f'{($h1-$h0)/6e6*100:.2f}')")" pct_core
    emit t2_idle_cpu_pct "$(python3 -c "print(f'{($b1-$b0)/6e6*100:.2f}')")" pct_core
    pm rm -f t2mem > /dev/null 2>&1
    sm StopSilo si "$HS" 10 > /dev/null 2>&1
    teardown_one "$GS"
}

# --- section: syscall cost ---------------------------------------------------
# Dedicated containers on each runtime (the syscall path is runsc vs runc,
# not the launch machinery). Both run the same weston-terminal image for
# perl; the t3s one runs it through /usr/libexec/qdistro/tier3s-runsc from
# the GUI silo's own store (bench containers must not pollute silo state).
sec_sys() {
    step "syscall cost (perl self-timed loops)"
    write_syscost
    emit sys_host "$(perl "$BW/syscost.pl" | tr '\n' ' ')" -
    pm rm -f t2sys >/dev/null 2>&1
    pm run -d --name t2sys --network none --entrypoint sleep \
        -v "$BW:/bench:ro" "$IMGW" 600 > /dev/null
    emit sys_t2_runc "$(pm exec t2sys perl /bench/syscost.pl 2>/dev/null | tr '\n' ' ')" -
    pm rm -f t2sys > /dev/null 2>&1
    pm_s "$GS" rm -f sbcsys >/dev/null 2>&1
    pm_s "$GS" run -d --name sbcsys --network none --entrypoint sleep \
        --runtime /usr/libexec/qdistro/tier3s-runsc \
        --runtime-flag=host-uds=open \
        -v "$BW:/bench:ro" "$IMGW" 600 > /dev/null
    emit sys_t3s_systrap "$(pm_s "$GS" exec sbcsys perl /bench/syscost.pl 2>/dev/null | tr '\n' ' ')" -
    pm_s "$GS" rm -f sbcsys > /dev/null 2>&1
}

# --- section: file I/O -------------------------------------------------------
sec_io() {
    step "file I/O: tar xf ~200MB into a 1GiB tmpfs"
    local tree=/var/tmp/t3s-bench-tree tar=/var/tmp/t3s-bench-tree.tar
    rm -rf "$tree"; mkdir -p "$tree"
    for i in $(seq 1 200); do dd if=/dev/zero bs=1M count=1 of="$tree/f$i" 2>/dev/null; done
    tar cf "$tar" -C "$tree" .
    emit io_tree_mb $(( $(stat -c %s "$tar") / 1048576 )) MB
    mkdir -p /mnt/t3s-io && mount -t tmpfs -o size=1g none /mnt/t3s-io
    local t0 t1 i
    t0=$(ts_us); tar xf "$tar" -C /mnt/t3s-io; t1=$(ts_us)
    emit io_tar_host_ms $(( (t1 - t0) / 1000 )) ms
    umount /mnt/t3s-io
    for i in 1 2; do
        t0=$(ts_us)
        pm run --rm --name t2io --network none --entrypoint tar \
            --tmpfs /bench:size=1g -v "$tar:/bench.tar:ro" \
            "$IMGW" xf /bench.tar -C /bench > /dev/null
        t1=$(ts_us)
        emit io_tar_t2_ms_$i $(( (t1 - t0) / 1000 )) ms
    done
    for i in 1 2; do
        t0=$(ts_us)
        pm_s "$GS" run --rm --name sbcio$i --network none --entrypoint tar \
            --runtime /usr/libexec/qdistro/tier3s-runsc \
            --tmpfs /bench:size=1g -v "$tar:/bench.tar:ro" \
            "$IMGW" xf /bench.tar -C /bench > /dev/null 2>&1
        t1=$(ts_us)
        pm_s "$GS" rm -f sbcio$i > /dev/null 2>&1
        emit io_tar_t3s_ms_$i $(( (t1 - t0) / 1000 )) ms
    done
    rm -rf "$tree" "$tar"
}

# --- section: bridge memory under load ---------------------------------------
sec_bridge() {
    step "waypipe bridge PSS under shm load (foot flood)"
    local tok bp base=0 peak=0 v i
    tok=$(up_gui_silo "$FS")
    [ -n "$tok" ] || { fail "bridge: launch"; return; }
    bp=$(rec "$tok" bridge_client_pid)
    base=$(pss_kb "$bp")
    for i in $(seq 1 30); do
        v=$(pss_kb "$bp" 2>/dev/null) || break
        [ -n "$v" ] && [ "$v" -gt "$peak" ] && peak=$v
        sleep 0.4
    done
    emit bridge_client_pss_start_mb $(( ${base:-0} / 1024 )) MB
    emit bridge_client_pss_peak_mb $(( peak / 1024 )) MB
    teardown_one "$FS"
}

# --- section: per-silo overhead ----------------------------------------------
sec_overhead() {
    step "per-silo on-disk overhead"
    local uid
    uid=$(silo_uid "$GS")
    [ -n "$uid" ] || { fail "overhead: no uid for $GS"; return; }
    emit silo_store_mb "$(du -sm "$(getent passwd "$(silo_acct "$GS")" | cut -d: -f6)/.local/share/containers" 2>/dev/null | cut -f1)" MB
    emit silo_runsc_state_mb "$(du -sm "$RUNSC_BASE/$uid" 2>/dev/null | cut -f1)" MB
    emit silo_runtime_mb "$(du -sm "$RT_BASE/$uid" 2>/dev/null | cut -f1)" MB
}

# --- latency window modes (host run-bench.sh drives virsh send-key/screenshot)
sec_latency_up() {
    mk_silo "$GS" weston-terminal || return 1
    set_rules "allow:qdistro.tier3s.spawn:weston-terminal/weston-terminal"
    ensure_silo_image "$GS" weston-terminal || return 1
    local tok
    tok=$(up_gui_silo "$GS") || return 1
    echo "LATENCY-WINDOW-UP token=$tok silo=$GS"
}
sec_latency_down() { sm StopSilo si "$GS" 10 > /dev/null 2>&1; }

# teardown_one <silo>: time StopSilo -> scope down
teardown_one() {
    local s="$1" tok u t0
    u=$(unit_of "$s"); tok=$(token_of_unit "$u" | head -1)
    t0=$(ts_us)
    sm StopSilo si "$s" 10 > /dev/null 2>&1
    [ -n "$tok" ] && wait_for 60 unit_down "qdistro-tier3s-$tok.scope"
    emit "teardown_${s}_ms" $(( ($(ts_us) - t0) / 1000 )) ms
    [ -n "$tok" ] && rm -f "$WORK/$tok".{procs,cg,id}
}

main() {
    case "${1:-all}" in
        latency-up)   sec_latency_up; exit $? ;;
        latency-down) sec_latency_down; exit 0 ;;
    esac
    local secs="${*:-env cold mem sys io bridge overhead}"
    [ "$secs" = "all" ] && secs="env cold mem sys io bridge overhead"
    provision
    for s in $secs; do
        case "$s" in cpu|teardown|headless|latency-up|latency-down) continue ;; esac
        "sec_$s"
    done
    set_rules none
    finish
}
main "$@"

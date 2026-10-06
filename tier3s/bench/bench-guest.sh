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
IMGHEAD=localhost/qdistro/tier3s-headless-smoke:latest
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
mb_b() { awk -v b="${1:-0}" 'BEGIN{printf "%.1f", b/1048576}'; }   # bytes -> MB, 1 decimal
mb_kb() { awk -v k="${1:-0}" 'BEGIN{printf "%.1f", k/1024}'; }    # KiB -> MB
cg_mem() { cat "/sys/fs/cgroup$1/memory.current" 2>/dev/null; }
cg_usage_us() { sed -n 's/^usage_usec //p' "/sys/fs/cgroup$1/cpu.stat" 2>/dev/null; }
proc_cg() { sed -n 's/^0:://p' "/proc/$1/cgroup" 2>/dev/null; }

# Direct-runsc bench path. podman+runsc cannot exec into a live container
# (conmon exec is unsupported here) and a bare `podman run` dies at the gofer
# filestore outside the launch path, so the runsc column uses runsc directly
# on an OCI bundle built from an exported image rootfs. The pinned binary is
# labeled qdistro_tier3s_exec_t — exec'ing it transitions into
# qdistro_tier3s_t, which cannot read bench inputs. The bench copy is
# relabeled bin_t so it stays unconfined; the in-sandbox syscall path the
# measurement covers is identical (same pinned build, --platform=systrap).
RPLAIN=/var/tmp/runsc-plain/runsc
BUNDLE=$BW/bundle
ROOTFS=$BW/rootfs
RSHARE=$BW/share
HSACCT=; HSUID=

# t3s_spec <args-json> — regenerate the OCI spec. The seccomp object is the
# workload's PRODUCTION profile (podman would feed the same file via
# --security-opt), so the t3s numbers below are filtered-systrap, not the
# runsc default.
SECCOMP_JSON=/usr/lib/qdistro/tier3s/seccomp/headless-smoke.json
t3s_spec() {
    python3 - "$1" "$BUNDLE/config.json" "$SECCOMP_JSON" "$ROOTFS" "$RSHARE" <<'PY'
import json, sys
args, cfg, prof, rootfs, rshare = sys.argv[1:6]
p = json.load(open(prof))
spec = {"ociVersion": "1.0.0",
 "process": {"terminal": False, "user": {"uid": 0, "gid": 0},
   "args": json.loads(args), "env": ["PATH=/usr/local/bin:/usr/bin:/bin"], "cwd": "/"},
 "root": {"path": rootfs, "readonly": True},
 "hostname": "t3sbench",
 "mounts": [
   {"destination": "/proc", "type": "proc", "source": "proc"},
   {"destination": "/bench", "type": "bind", "source": rshare, "options": ["rbind", "ro"]},
   {"destination": "/w", "type": "tmpfs", "source": "tmpfs", "options": ["nosuid", "nodev", "size=768m"]}],
 "linux": {"namespaces": [{"type": t} for t in ("pid", "mount", "ipc", "uts")],
   "seccomp": {k: p[k] for k in ("defaultAction", "defaultErrnoRet", "archMap", "architectures", "syscalls") if k in p}}}
json.dump(spec, open(cfg, "w"))
PY
}

# t3s_run <id> <args-json> [extra runsc flags...]
t3s_run() {
    local id=$1 args=$2; shift 2
    t3s_spec "$args"
    runuser -u "$HSACCT" -- env -i PATH=/usr/bin:/bin \
        "$RPLAIN" "--root=$RUNSC_BASE/$HSUID" --ignore-cgroups \
        --platform=systrap --oci-seccomp --network=none --rootless "$@" \
        run --bundle "$BUNDLE" "$id" 2>&1
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
    # admin's user slice delegates only 'pids' by default: the tier-2
    # baseline containers (rootless podman) would get no memory.current /
    # cpu.stat. Delegate cpu+memory down the whole user@1000.service tree —
    # leaf writes fail harmlessly (no children); interior failures surface
    # below as a visible FAIL when the t2 container's cgroup lacks
    # memory.current.
    local d
    # ordered top-down: a level only delegates what its parent already has
    for d in /sys/fs/cgroup \
             /sys/fs/cgroup/user.slice \
             /sys/fs/cgroup/user.slice/user-1000.slice \
             /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service \
             /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/user.slice \
             /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice \
             /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/session.slice \
             /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/background.slice; do
        [ -d "$d" ] || continue
        echo "+cpu +memory" > "$d/cgroup.subtree_control" 2>/dev/null || :
    done
    mk_silo "$GS" weston-terminal || fail "create $GS"
    mk_silo "$HS" headless-smoke || fail "create $HS"
    mk_silo "$FS" foot || fail "create $FS"
    set_rules "allow:qdistro.tier3s.spawn:weston-terminal/weston-terminal" \
              "allow:qdistro.tier3s.spawn:headless-smoke/$SMOKE_APP" \
              "allow:qdistro.tier3s.spawn:foot/foot"
    set_argv_json "$HS=[\"qdistro-tier3s-smoke\",\"--hold\",\"900\"]" \
                  "$FS=[\"foot\",\"-e\",\"/bin/sh\",\"-c\",\"while true; do head -c 2097152 /dev/urandom | base64; done\"]"
    ensure_silo_image "$GS" weston-terminal || fail "ensure_silo_image $GS"
    ensure_silo_image "$HS" headless-smoke || fail "ensure_silo_image $HS"
    ensure_silo_image "$FS" foot || fail "ensure_silo_image $FS"
    # --- direct-runsc bench scaffolding (see RPLAIN comment) ---------------
    HSACCT=$(silo_acct "$HS"); HSUID=$(silo_uid "$HS")
    [ -n "$HSACCT" ] && [ -n "$HSUID" ] || fail "no acct/uid for $HS"
    if [ ! -x "$RPLAIN" ]; then
        rm -rf /var/tmp/runsc-plain
        mkdir -p /var/tmp/runsc-plain/gvisor-bin
        cp /usr/libexec/qdistro/runsc/runsc /var/tmp/runsc-plain/runsc \
            || fail "runsc copy"
        cp /usr/libexec/qdistro/runsc/gvisor-bin/* /var/tmp/runsc-plain/gvisor-bin/ \
            || fail "gvisor-bin copy"
        chcon -R -t bin_t /var/tmp/runsc-plain 2>/dev/null \
            || info "chcon bin_t failed (permissive worker still runs it)"
        chmod -R a+rX /var/tmp/runsc-plain
    fi
    # the syscall probe binary is compiled on the host and staged into $BW
    [ -x "$BW/syscost" ] || fail "syscost binary missing from $BW (run-bench.sh builds it)"
    mkdir -p "$RSHARE" "$BUNDLE" && chmod 755 "$RSHARE" "$BUNDLE"
    cp "$BW/syscost" "$RSHARE/syscost" && chmod 755 "$RSHARE/syscost"
    # the gofer drops .gvisor.filestore.* next to what it serves — every dir
    # it can reach must be writable by the uid runsc runs as (the silo)
    chown "$HSACCT:$HSACCT" "$RSHARE"
    if [ ! -d "$ROOTFS/usr" ]; then
        # export runs AS the silo uid — it cannot write into root's $BW, so
        # it lands in the silo rt dir; extraction runs as the silo too so the
        # whole tree is silo-owned (root-owned trees break the filestore)
        local rt="$RT_BASE/$HSUID"
        rm -f "$rt/bench-rootfs.tar"
        pm_s "$HS" rm -f benchrootfs > /dev/null 2>&1
        pm_s "$HS" create --name benchrootfs "$IMGHEAD" > /dev/null \
            || fail "rootfs create"
        pm_s "$HS" export benchrootfs -o "$rt/bench-rootfs.tar" \
            || fail "rootfs export"
        pm_s "$HS" rm benchrootfs > /dev/null 2>&1
        mkdir -p "$ROOTFS" && chown "$HSACCT:$HSACCT" "$ROOTFS" \
            && runuser -u "$HSACCT" -- tar xf "$rt/bench-rootfs.tar" -C "$ROOTFS" \
            || fail "rootfs extract"
        rm -f "$rt/bench-rootfs.tar"
    fi
    chmod 755 "$BW" "$ROOTFS"
}

# stop an Active silo left over from a crashed earlier pass (idempotent)
stop_quiet() {
    case "$(silo_state "$1")" in
        Active) sm StopSilo si "$1" 10 > /dev/null 2>&1; sleep 2 ;;
    esac
}

# --- section: cold start to window ------------------------------------------
# t0 = just before the StartSilo call; t1 = journal realtime ts of the
# qdshell "[tier3s] toplevel observed" marker. Also records the synchronous
# StartSilo call-return time (Type=notify: returns once phase=running).
sec_cold() {
    step "cold start to window (tier3s GUI launch)"
    stop_quiet "$GS"
    local i cur t0 tc mus
    for i in 1 2 3; do
        cur=$(journal_cursor)
        t0=$(ts_us)
        sm StartSilo s "$GS" > /dev/null || { fail "cold: StartSilo $i"; continue; }
        tc=$(ts_us)
        mus=""
        for _ in $(seq 1 960); do
            # -o short-unix: the marker's journal line carries ANSI colours,
            # which makes journald emit MESSAGE as a byte ARRAY in -o json
            # (unmatched); the unix format keeps epoch + text readable.
            mus=$(journalctl _SYSTEMD_USER_UNIT=qdshell.service -o short-unix \
                  --after-cursor="$cur" --no-pager 2>/dev/null \
                  | grep -a "toplevel observed" | head -1 \
                  | awk '{printf "%d", $1 * 1e6}')
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

# delegate_to <cgroup-rel-path>: enable +cpu +memory on the target cgroup's
# WHOLE ancestry, root first. Bottom-up can't work (a level only delegates
# what its parent already exposes) and PID1 rewrites subtree_control on unit
# events (transient-scope teardown can reset the root to 'pids'), so this is
# re-applied right before the t2 stats are read, not once at provision.
delegate_to() {
    local p="/sys/fs/cgroup$1" chain=()
    while [ -n "$p" ] && [ "$p" != /sys/fs ]; do
        chain=("$p" "${chain[@]}")
        p="${p%/*}"
    done
    for p in "${chain[@]}"; do
        [ -f "$p/cgroup.subtree_control" ] \
            && echo "+cpu +memory" > "$p/cgroup.subtree_control" 2>/dev/null || :
    done
}

# --- section: idle memory (+ idle CPU riding the open window) ---------------
sec_mem() {
    step "idle memory"
    stop_quiet "$GS"; stop_quiet "$HS"
    local tok scope_cg procs scope_cur pss bridge_pss htok hcg
    tok=$(up_gui_silo "$GS")
    [ -n "$tok" ] || { fail "mem: GUI launch failed"; return; }
    sleep 15
    scope_cg=$(rec "$tok" scope_cgroup)
    procs=$(tree_procs "/sys/fs/cgroup$scope_cg")
    scope_cur=$(cg_mem "$scope_cg")
    pss=$(sum_pss_kb $procs)
    bridge_pss=$(pss_kb "$(rec "$tok" bridge_client_pid)")
    emit t3s_gui_scope_memory_current_mb "$(mb_b "$scope_cur")" MB
    emit t3s_gui_scope_pss_mb "$(mb_kb "$pss")" MB
    emit t3s_gui_bridge_client_pss_mb "$(mb_kb "$bridge_pss")" MB
    emit t3s_gui_scope_procs "$(printf '%s\n' "$procs" | grep -c .)" count
    htok=$(up_silo "$HS")
    if [ -n "$htok" ]; then
        sleep 10
        hcg=$(rec "$htok" scope_cgroup)
        procs=$(tree_procs "/sys/fs/cgroup$hcg")
        emit t3s_headless_scope_memory_current_mb "$(mb_b "$(cg_mem "$hcg")")" MB
        emit t3s_headless_scope_pss_mb "$(mb_kb "$(sum_pss_kb $procs)")" MB
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
    [ -n "$t2cg" ] && delegate_to "$t2cg" && sleep 1
    local a0 a1 b0 b1 h0 h1
    if [ -n "$t2cg" ] && [ -n "$(cg_mem "$t2cg")" ]; then
        emit t2_idle_memory_current_mb "$(mb_b "$(cg_mem "$t2cg")")" MB
        local t2pss; t2pss=$(sum_pss_kb $(tree_procs "/sys/fs/cgroup$t2cg"))
        emit t2_idle_pss_mb "$(mb_kb "$t2pss")" MB
    else
        # report the delegation chain so a miss is diagnosable from the log
        local cgpath="/sys/fs/cgroup$t2cg" anc ctl=""
        while [ "$cgpath" != /sys/fs/cgroup ] && [ -n "$cgpath" ]; do
            [ -f "$cgpath/cgroup.subtree_control" ] \
                && ctl="$ctl ${cgpath#/sys/fs/cgroup}=$(cat "$cgpath/cgroup.subtree_control")"
            cgpath="${cgpath%/*}"
        done
        fail "mem: t2 cgroup has no memory controller (cg=${t2cg:-none}$ctl)"
    fi
    # idle CPU over 60s with the window open (plan: repeat under
    # --systrap-disable-fast-path is not wired through the launch path; the
    # syscall section shows the per-syscall cost it would amortise)
    a0=$(cg_usage_us "$scope_cg"); b0=$(cg_usage_us "$t2cg"); h0=$(cg_usage_us "$hcg")
    sleep 60
    a1=$(cg_usage_us "$scope_cg"); b1=$(cg_usage_us "$t2cg"); h1=$(cg_usage_us "$hcg")
    emit t3s_gui_idle_cpu_pct "$(python3 -c "print(f'{($a1-$a0)/60e6*100:.2f}')" 2>/dev/null || echo ERR)" pct_core
    emit t3s_headless_idle_cpu_pct "$(python3 -c "print(f'{($h1-$h0)/60e6*100:.2f}')" 2>/dev/null || echo ERR)" pct_core
    emit t2_idle_cpu_pct "$(python3 -c "print(f'{($b1-$b0)/60e6*100:.2f}')" 2>/dev/null || echo ERR)" pct_core
    pm rm -f t2mem > /dev/null 2>&1
    sm StopSilo si "$HS" 10 > /dev/null 2>&1
    teardown_one "$GS"
}

# --- section: syscall cost ---------------------------------------------------
# Same self-timed binary on all three paths: bare host, tier-2 runc
# container (bind-mounted into admin's podman), and direct runsc on the
# exported image rootfs (systrap, oci-seccomp, network=none — the sandbox
# internals are the same ones the launch path gets through the wrapper).
# syscost emits exactly one line of k=v fields; anything else is a probe
# failure and must not reach the MEAS stream.
valid_sysrow() { [ "${1##*getpid_ms_per_100k=}" != "$1" ]; }
sec_sys() {
    step "syscall cost (syscost binary, self-timed)"
    local out
    emit t2_runtime "$(pm info --format '{{.Host.OCIRuntime.Name}}' 2>/dev/null)" -
    out=$("$BW/syscost" 2>&1 | tr '\n' ' ')
    valid_sysrow "$out" || fail "sys host probe: $out"
    emit sys_host "$out" -
    # tier 2: the binary must be owned by admin so rootless podman's :z
    # relabel can lsetxattr it (root-owned files fail under enforcing)
    local ap=/home/admin/t3s-syscost
    install -m 0755 -o admin -g admin "$BW/syscost" "$ap"
    out=$(pm run --rm --name t2sys --network none \
        --security-opt label=disable \
        -v "$ap:/bench/syscost:ro,z" --entrypoint /bench/syscost "$IMGW" 2>&1 \
        | tr '\n' ' ')
    rm -f "$ap"
    valid_sysrow "$out" || fail "sys t2 probe: $out"
    emit sys_t2_runc "$out" -
    out=$(t3s_run t3sys '["/bench/syscost"]' | tr '\n' ' ')
    valid_sysrow "$out" || fail "sys t3s probe: $out"
    emit sys_t3s_systrap "$out" -
    # fast-path off shows the un-amortised trap cost (plan hypothesis)
    out=$(t3s_run t3sysnf '["/bench/syscost"]' --systrap-disable-fast-path | tr '\n' ' ')
    valid_sysrow "$out" || fail "sys t3s-nofastpath probe: $out"
    emit sys_t3s_systrap_nofastpath "$out" -
    t3s_run t3sys '["/bin/true"]' > /dev/null 2>&1 \
        || fail "direct runsc probe unhealthy — t3s columns above may be errors"
}

# --- section: file I/O -------------------------------------------------------
sec_io() {
    step "file I/O: tar xf ~200MB into a 1GiB tmpfs"
    local tree=/var/tmp/t3s-bench-tree tar=/var/tmp/t3s-bench-tree.tar
    rm -rf "$tree"; mkdir -p "$tree"
    for i in $(seq 1 200); do dd if=/dev/zero bs=1M count=1 of="$tree/f$i" 2>/dev/null; done
    tar cf "$tar" -C "$tree" .
    chmod 0644 "$tar"   # rootless podman binds it read-only into the container
    emit io_tree_mb $(( $(stat -c %s "$tar") / 1048576 )) MB
    # Same measurement boundary on all three paths: the workload itself
    # reports TAR_MS (extraction only, in-container clock) — container start /
    # spec-gen / teardown overhead is excluded everywhere. The host column is
    # timed the same way around a bare tar.
    tar_ms() {   # tar_ms <command-output> -> extraction ms or empty
        echo "$1" | sed -n 's/.*TAR_MS=\([0-9]*\).*/\1/p'
    }
    local out ms i
    mkdir -p /mnt/t3s-io && mount -t tmpfs -o size=1g none /mnt/t3s-io
    local t0 t1
    t0=$(ts_us); tar xf "$tar" -C /mnt/t3s-io; t1=$(ts_us)
    emit io_tar_host_ms $(( (t1 - t0) / 1000 )) ms
    umount /mnt/t3s-io
    # tier 2: admin-owned bind-mount so :z can relabel; the shell inside the
    # container prints TAR_MS for just the extraction
    local at=/home/admin/t3s-bench.tar t2probe
    install -m 0644 -o admin -g admin "$tar" "$at"
    t2probe='S=$(date +%s%3N); tar xf /bench.tar -C /bench && echo TAR_MS=$(( $(date +%s%3N) - S ))'
    for i in 1 2; do
        out=$(pm run --rm --name t2io$i --network none --entrypoint /bin/sh \
            --security-opt label=disable \
            --tmpfs /bench:size=1g -v "$at:/bench.tar:ro,z" \
            "$IMGW" -c "$t2probe" 2>&1)
        ms=$(tar_ms "$out")
        [ -n "$ms" ] || fail "t2 io run $i: $(echo "$out" | tail -2)"
        emit io_tar_t2_ms_$i "${ms:-0}" ms
    done
    rm -f "$at"
    # tier 3s: the tar is a bind-mounted host file → reads go through the
    # gofer (gofs); extraction writes the sandbox's tmpfs. Second variant
    # turns directfs off where the pin supports it.
    cp "$tar" "$RSHARE/bench.tar" && chmod 644 "$RSHARE/bench.tar"
    local t3probe='S=$(date +%s%3N); tar xf /bench/bench.tar -C /w && echo TAR_MS=$(( $(date +%s%3N) - S ))'
    for i in 1 2; do
        if ! out=$(t3s_run t3io$i "[\"/bin/sh\",\"-c\",\"$t3probe\"]" 2>&1); then
            fail "t3s io run $i: $(echo "$out" | tail -2)"; continue
        fi
        ms=$(tar_ms "$out")
        [ -n "$ms" ] || fail "t3s io run $i: no TAR_MS in $(echo "$out" | tail -2)"
        emit io_tar_t3s_ms_$i "${ms:-0}" ms
    done
    # directfs toggles the gofer bypass for bind mounts — probe support by
    # running it, emit only on success
    if out=$(t3s_run t3iodf "[\"/bin/sh\",\"-c\",\"$t3probe\"]" --directfs=false 2>&1) \
            && ms=$(tar_ms "$out") && [ -n "$ms" ]; then
        emit io_tar_t3s_directfs_off_ms "$ms" ms
    else
        info "runsc --directfs unsupported or refused: $(echo "$out" | tail -1)"
    fi
    rm -f "$RSHARE/bench.tar"; rm -rf "$tree" "$tar"
}

# --- section: bridge memory under load ---------------------------------------
sec_bridge() {
    step "waypipe bridge PSS under shm load (foot flood)"
    stop_quiet "$FS"
    local tok bp base=0 peak=0 v i
    tok=$(up_gui_silo "$FS")
    [ -n "$tok" ] || { fail "bridge: launch"; return; }
    bp=$(rec "$tok" bridge_client_pid)
    base=$(pss_kb "$bp")
    [ -n "$bp" ] && [ -n "$base" ] \
        || { fail "bridge: no client pid/PSS (bp=${bp:-none})"; return; }
    # rchar corroborates the flood crossed the bridge: the client's reads
    # on the waypipe socket are the (compressed) frame stream the in-sandbox
    # server pumps. Pixel bulk travels via the client's mmap'd shm rebuild,
    # not socket writes, so wchar stays small by design. PSS should stay
    # flat — waypipe streams damage, it does not buffer the file.
    local r0 r1
    r0=$(sed -n 's/^rchar: //p' "/proc/$bp/io" 2>/dev/null)
    for i in $(seq 1 30); do
        v=$(pss_kb "$bp" 2>/dev/null) || break
        [ -n "$v" ] && [ "$v" -gt "$peak" ] && peak=$v
        sleep 0.4
    done
    r1=$(sed -n 's/^rchar: //p' "/proc/$bp/io" 2>/dev/null)
    emit bridge_client_pss_start_mb "$(mb_kb "$base")" MB
    emit bridge_client_pss_peak_mb "$(mb_kb "$peak")" MB
    emit bridge_rchar_delta_kb $(( (${r1:-0} - ${r0:-0}) / 1024 )) KB
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

# teardown_one <silo>: time StopSilo -> scope down, then VERIFY it's down
teardown_one() {
    local s="$1" tok u t0 scg
    u=$(unit_of "$s"); tok=$(token_of_unit "$u" | head -1)
    scg=$(rec "$tok" scope_cgroup)
    t0=$(ts_us)
    sm StopSilo si "$s" 10 > /dev/null 2>&1
    [ -n "$tok" ] && wait_for 60 unit_down "qdistro-tier3s-$tok.scope"
    emit "teardown_${s}_ms" $(( ($(ts_us) - t0) / 1000 )) ms
    [ -n "$scg" ] && [ -d "/sys/fs/cgroup$scg" ] \
        && fail "teardown $s: scope cgroup $scg still present" || :
    case "$(silo_state "$s")" in Active) fail "teardown $s: still Active" ;; esac
    [ -n "$tok" ] && rm -f "$WORK/$tok".{procs,cg,id}
}

main() {
    case "${1:-all}" in
        latency-up)   sec_latency_up; T3S_DONE=1; exit $? ;;
        latency-down) sec_latency_down; T3S_DONE=1; exit 0 ;;
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

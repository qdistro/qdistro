#!/usr/bin/env bash
# qci module: mmnet gate (multi-machine VM smoke test)
#
# OPT-IN ONLY. gate_mmnet is reachable solely through `qci mmnet`; it is
# deliberately NOT part of gate_full, and its test does NOT live under
# tests/integration/vm/ (so gate_bats's *.bats glob never picks it up). The
# default single-machine lanes (qci, qci full, qci bats) never create the
# isolated segment, never boot a second VM, and never pay its RAM/boot cost.
#
# Isolated network: a QEMU point-to-point UDP socket segment over LOOPBACK
# (`<interface type='udp'>` with both endpoints on 127.0.0.1) — a rootless,
# host-confined Ethernet-in-UDP tunnel between exactly two VMs. No host bridge,
# no system libvirtd, no multicast route, no persistent libvirt network object.
# See scripts/vm/mmnet-config.sh for why UDP-over-loopback (not mcast).
#
# What it does:
#   1. Allocates a unique, locked port-pair seed (mmnet-alloc.sh) so a
#      concurrent sibling run can't share our isolated segment.
#   2. Load-aware gate: refuses to boot two ~4 GiB guests if host MemAvailable
#      is too low (a sibling `qci bats` pool may be running).
#   3. Clones TWO VMs from baseweed-baked, each with a SECOND NIC on the udp
#      segment (clone-mmnet.sh -> clone-baseweed.sh --extra-nic-xml). The boots
#      are STAGGERED (peer A waits for qga before peer B starts).
#   4. Over the qga CONTROL plane, brings up each guest's mmnet NIC with a
#      static /24 address, then asserts:
#        - peer A's route to peer B's IP egresses the mmnet NIC (proves the
#          second NIC, not some accidental other route, is the path),
#        - ICMP ping A->B succeeds,
#        - a TCP connect A->B (to a listener B opens) succeeds.
#      The ping/TCP BYTES traverse the udp NIC; qga is only orchestration, so
#      this validates a DATA-PLANE reachability that does NOT depend on qga.
#   5. ALWAYS destroys/undefines/removes both clones on EXIT (even on failure),
#      tracking exact names — never globbing by prefix. QD_MMNET_KEEP=1 keeps
#      them and prints the cleanup commands instead.
# shellcheck shell=bash

# Minimum host MemAvailable (GiB) to start the two-VM lane. Two baseweed clones
# are ~4 GiB each; with host headroom that's ~14 GiB. Override with
# QD_MMNET_MIN_FREE_GB.
_mmnet_min_free_gb() { printf '%s' "${QD_MMNET_MIN_FREE_GB:-14}"; }

# Run one guest command via vm-exec with BOTH of vm-exec's descriptors on a
# private capture file, then print (a bounded prefix of) what was written.
# Returns vm-exec's exit status.
#
# WHY. `x=$(vm-exec ... 2>&1)` hands vm-exec's fd 1 AND fd 2 to the
# substitution's pipe. vm-exec redirects its own children's fd 1 to an internal
# capture file, but fd 2 is inherited straight through to every virsh/jq
# descendant it starts; one that outlives vm-exec holds the pipe -- and the
# caller -- open long after the guest command is dead, and an outer `timeout`
# cannot help because the shell is blocked on the read. A read from a regular
# file reaches EOF at the current end of file however many writers still hold
# it open. The same applies when the `2>&1` is written inside a FUNCTION whose
# own stdout is a substitution pipe, which is what _mmnet_setup_guest did.
#
# The capture is per call and UNLINKED before the command starts (the shape of
# bounded_run() in scripts/vm/vm-exec), so a survivor of one call can never
# write into a later call's capture and nothing is left named while a command
# runs.
# The read is ceilinged in BYTES via `head -c`. It was `read -N` until
# 2026-09-17, which ceilings CHARACTERS: bash discards NUL bytes and they do
# not count, so a NUL-bearing capture was read PAST the ceiling -- 4 MiB of
# NULs cost 4,194,305 bytes and ~0.88s for a nominal 64.
#
# The comment here used to argue that this "cannot HANG, because a read of a
# regular file returns at the current EOF however many writers are still
# appending", citing a 0.00s measurement. That argument is WRONG and the
# measurement proved only that this particular reader caught its writer: EOF is
# tested at EACH read, not frozen when the replay begins, so a writer that
# stays ahead of the reader keeps it going, and a large pre-existing backlog
# alone makes a nominally tiny replay expensive. (sol,
# todo/reviews/qci-A-260917-sol-review.md section 1.) The byte ceiling is what
# bounds this, not the filesystem's EOF semantics.
#
# No size bound is added to the FILE here: the only limit on the nameless inode
# is the RLIMIT_FSIZE vm-exec installs on its own children.
# One visible side effect of the `$( )` replay, shared by every site that
# uses it: on a NUL-bearing capture bash writes `warning: command
# substitution: ignored null byte in input` to stderr, once per call.
# Harmless, and not a failure.
MMNET_CAP_BYTES=${QD_MMNET_CAP_BYTES:-262144}
case "$MMNET_CAP_BYTES" in
    ''|*[!0-9]*|0|0*)
        echo "mmnet: QD_MMNET_CAP_BYTES must be a positive integer number of bytes, got '$MMNET_CAP_BYTES'" >&2
        return 2 2>/dev/null || exit 2 ;;
esac
_mmnet_vmx_merged() {
    local vm=$1; shift
    local cf wfd rfd out="" rc=0 _mm_size=""
    cf=$(mktemp "${TMPDIR:-/tmp}/qci-mmnet-cap.XXXXXXXX") || return 125
    # Every step is checked: an unchecked failure here would leave the capture
    # NAMED while the command runs, which is exactly what this shape promises
    # not to do. 125 is this helper's INFRASTRUCTURE status by convention -- not
    # a reserved value, since a guest command can exit 125 too; the stderr line
    # is what tells them apart.
    exec {wfd}>"$cf" || { rm -f "$cf"; return 125; }
    exec {rfd}<"$cf" || { exec {wfd}>&-; rm -f "$cf"; return 125; }
    rm -f "$cf" || { exec {wfd}>&- {rfd}<&-; return 125; }
    "$VM_TOOLS/vm-exec" "$vm" "$@" >&"$wfd" 2>&"$wfd" {wfd}>&- {rfd}<&- || rc=$?
    exec {wfd}>&-
        # A FAILED replay is a capture failure, not empty output. `|| :` made
    # `head` returning nonzero indistinguishable from a command that printed
    # nothing, so an I/O error surfaced as rc=0 with an empty string -- sol
    # injected `head() { return 1; }` and got exactly that (A3 finding 3).
    if ! out=$(head -c "$MMNET_CAP_BYTES" <&"$rfd"); then
        echo "mmnet: capture replay FAILED; the output is unavailable, not empty" >&2
        exec {rfd}<&-
        return 125
    fi
    # Overflow is not silent. `MMNET-DEV=` is parsed out of this text at :214
    # and the TCP token is required at :278; a marker past the cap reads
    # exactly like a marker that never appeared, so a working setup would be
    # classified as a failure.
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
    if ! _mm_size=$(stat -Lc %s "/proc/self/fd/$rfd" 2>/dev/null); then
        echo "mmnet: could not stat the capture to check completeness; whether the control output is complete is UNKNOWN, not verified" >&2
        exec {rfd}<&-
        return 125
    elif [ "$_mm_size" -gt "$MMNET_CAP_BYTES" ]; then
        echo "mmnet: vm-exec output (${_mm_size} bytes) exceeded the ${MMNET_CAP_BYTES}-byte capture cap; the returned text is INCOMPLETE and any marker beyond it is lost. Raise QD_MMNET_CAP_BYTES, or set QD_MMNET_ALLOW_TRUNCATION=1 to accept a prefix." >&2
        if [ "${QD_MMNET_ALLOW_TRUNCATION:-0}" != 1 ]; then
            exec {rfd}<&-
            return 125
        fi
    fi
    exec {rfd}<&-
    printf '%s' "$out"
    return "$rc"
}

# Configure one guest's mmnet NIC over qga and verify the address landed. The
# NIC is identified by its MAC (mmnet_mac), found in the guest's `ip -o link`.
# Returns 0 on success; non-zero (with a diagnostic) otherwise.
_mmnet_setup_guest() {
    local vm=$1 peer=$2 seed=$3 ip mac vmx
    vmx="$VM_TOOLS/vm-exec"
    # shellcheck source=scripts/vm/mmnet-config.sh
    . "$VM_TOOLS/mmnet-config.sh"
    ip=$(mmnet_ip "$peer")
    mac=$(mmnet_mac "$peer" "$seed")
    local prefix="$MMNET_PREFIXLEN"
    # Find the guest NIC whose MAC matches, bring it up with the static addr.
    # All in one shell so qga sees a single command. `ip -o link` lists MAC on
    # the `link/ether` field; we match case-insensitively.
    _mmnet_vmx_merged "$vm" "set -e
        dev=\$(ip -o link | awk -v m='$mac' 'tolower(\$0) ~ tolower(m){print \$2}' | sed 's/://;s/@.*//' | head -1)
        [ -n \"\$dev\" ] || { echo 'MMNET-ERR: no NIC with mac $mac'; ip -o link; exit 1; }
        ip addr flush dev \"\$dev\" 2>/dev/null || true
        ip addr add $ip/$prefix dev \"\$dev\"
        ip link set \"\$dev\" up
        echo \"MMNET-DEV=\$dev MMNET-IP=$ip\""
}

gate_mmnet() {
    qci_assert_run_dir || return $?
    qci_assert_vm_tools mmnet || return $?

    local rc=$EXIT_OK seed="" vm_a="" vm_b="" t0 alloc="$VM_TOOLS/mmnet-alloc.sh"
    local clone="$VM_TOOLS/clone-mmnet.sh" log_dir="$RDIR/mmnet"
    mkdir -p "$log_dir"
    t0=$(date +%s)

    # shellcheck source=scripts/vm/mmnet-config.sh
    . "$VM_TOOLS/mmnet-config.sh"

    # 0. Tooling present.
    if [ ! -x "$clone" ] || [ ! -x "$alloc" ]; then
        record_blocked mmnet mmnet "$EXIT_PREFLIGHT" mmnet "clone-mmnet.sh / mmnet-alloc.sh missing or non-executable"
        return "$EXIT_PREFLIGHT"
    fi

    # 1. Load-aware gate: don't pile two heavy guests onto a loaded shared host.
    local avail_gb min_gb
    avail_gb=$(awk '/^MemAvailable:/{printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null)
    [ -n "$avail_gb" ] 2>/dev/null || avail_gb=0
    min_gb=$(_mmnet_min_free_gb)
    if [ "$avail_gb" -lt "$min_gb" ] && [ "${QD_MMNET_FORCE:-0}" != 1 ]; then
        record_blocked mmnet mmnet "$EXIT_VM_PROVISION" mmnet \
            "host too loaded for 2-VM lane: MemAvailable=${avail_gb}GiB < ${min_gb}GiB (set QD_MMNET_FORCE=1 to override)"
        log "mmnet: skipping — only ${avail_gb}GiB available (need ${min_gb}); a sibling lane may be running"
        return "$EXIT_VM_PROVISION"
    fi

    # 2. Allocate a unique locked segment seed for THIS run.
    seed=$(MMNET_OWNER_PID=$$ "$alloc" reserve) || {
        record_blocked mmnet mmnet "$EXIT_VM_PROVISION" mmnet "could not reserve a free segment seed"
        return "$EXIT_VM_PROVISION"
    }
    local pa pb
    pa=$(mmnet_local_port a "$seed"); pb=$(mmnet_local_port b "$seed")
    log "mmnet: isolated segment seed=$seed udp-loopback A:$pa<->B:$pb subnet=$MMNET_SUBNET_CIDR"
    kv mmnet_seed "$seed"; kv mmnet_udp_a "$pa"; kv mmnet_udp_b "$pb"

    # Strict cleanup: always reap exactly the VMs THIS run created (by name),
    # release the seed, on any exit path. QD_MMNET_KEEP=1 keeps them + prints the
    # manual cleanup commands. Tolerates partial setup (vm_b may be empty).
    _mmnet_cleanup() {
        local v
        if [ "${QD_MMNET_KEEP:-0}" = 1 ]; then
            log "mmnet: QD_MMNET_KEEP=1 — keeping VMs; clean up manually:"
            for v in "$vm_a" "$vm_b"; do
                [ -n "$v" ] || continue
                log "  ${VIRSH[*]} destroy $v; ${VIRSH[*]} undefine $v --nvram; rm -f ${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}/$v.qcow2"
            done
            log "  $alloc release $seed"
            return 0
        fi
        for v in "$vm_a" "$vm_b"; do
            [ -n "$v" ] || continue
            "${VIRSH[@]}" destroy "$v" >/dev/null 2>&1 || true
            "${VIRSH[@]}" undefine "$v" --nvram >/dev/null 2>&1 \
                || "${VIRSH[@]}" undefine "$v" >/dev/null 2>&1 || true
            rm -f "${QDWIN_IMG_DIR:-$HOME/.local/share/libvirt/images}/$v.qcow2" 2>/dev/null || true
            log "mmnet: reaped $v"
        done
        [ -n "$seed" ] && "$alloc" release "$seed" 2>/dev/null || true
    }
    trap _mmnet_cleanup RETURN

    # 3. Clone peer A, wait for it (clone-mmnet.sh blocks until qga is up via
    #    clone-baseweed.sh's vm-start-and-wait), THEN clone peer B — staggered so
    #    we never have two cold-booting guests competing for host CPU at once.
    log "mmnet: cloning peer A"
    vm_a=$(MMNET_SEED="$seed" "$clone" qci-mmnet-a a --from-baked 2>"$log_dir/clone-a.log" | grep -E '^qci-mmnet-a-' | tail -1) || true
    if [ -z "$vm_a" ]; then
        record_result mmnet clone-a fail "$EXIT_VM_PROVISION" vm_provision mmnet "$log_dir/clone-a.log" "peer A clone failed"
        return "$EXIT_VM_PROVISION"
    fi
    CREATED_VMS+=("$vm_a"); printf '%s\n' "$vm_a" >> "$RDIR/vm/created-vms.txt" 2>/dev/null || true
    kv mmnet_vm_a "$vm_a"; log "mmnet: peer A = $vm_a (up)"

    log "mmnet: cloning peer B"
    vm_b=$(MMNET_SEED="$seed" "$clone" qci-mmnet-b b --from-baked 2>"$log_dir/clone-b.log" | grep -E '^qci-mmnet-b-' | tail -1) || true
    if [ -z "$vm_b" ]; then
        record_result mmnet clone-b fail "$EXIT_VM_PROVISION" vm_provision mmnet "$log_dir/clone-b.log" "peer B clone failed"
        return "$EXIT_VM_PROVISION"
    fi
    CREATED_VMS+=("$vm_b"); printf '%s\n' "$vm_b" >> "$RDIR/vm/created-vms.txt" 2>/dev/null || true
    kv mmnet_vm_b "$vm_b"; log "mmnet: peer B = $vm_b (up)"

    # 4. Configure both guests' mmnet NICs over the qga control plane.
    local ip_a ip_b setup_a setup_b
    ip_a=$(mmnet_ip a); ip_b=$(mmnet_ip b)
    setup_a=$(_mmnet_setup_guest "$vm_a" a "$seed"); local sa=$?
    printf '%s\n' "$setup_a" > "$log_dir/setup-a.log"
    setup_b=$(_mmnet_setup_guest "$vm_b" b "$seed"); local sb=$?
    printf '%s\n' "$setup_b" > "$log_dir/setup-b.log"
    if [ "$sa" -ne 0 ] || [ "$sb" -ne 0 ]; then
        collect_vm_artifacts "$vm_a" mmnet-a; collect_vm_artifacts "$vm_b" mmnet-b
        # 125 from the capture helper is INFRASTRUCTURE (setup, byte-cap
        # overflow, failed replay) -- the guest's NIC was never judged. Saying
        # "NIC config failed" there blames the product for a harness fault
        # (sol, todo/reviews/qci-A3-260917-sol-review.md finding 2).
        if [ "$sa" = 125 ] || [ "$sb" = 125 ]; then
            record_result mmnet nic-setup fail "$EXIT_RUNNER" runner mmnet "$log_dir/setup-a.log" \
                "mmnet NIC config UNKNOWN: the host could not capture the setup output (A rc=$sa B rc=$sb); this is a harness capture failure, not a product result — see the qdwin-helpers/mmnet message on the runner's stderr"
            record_timing mmnet smoke 0 "$(( $(date +%s) - t0 ))" "$(( $(date +%s) - t0 ))" "$EXIT_RUNNER" "$vm_a,$vm_b"
            return "$EXIT_RUNNER"
        fi
        record_result mmnet nic-setup fail "$EXIT_SERVICE" service mmnet "$log_dir/setup-a.log" \
            "mmnet NIC config failed (A rc=$sa B rc=$sb)"
        record_timing mmnet smoke 0 "$(( $(date +%s) - t0 ))" "$(( $(date +%s) - t0 ))" "$EXIT_SERVICE" "$vm_a,$vm_b"
        return "$EXIT_SERVICE"
    fi
    log "mmnet: A=$setup_a B=$setup_b"

    # 5. Assert the route to peer B leaves via the mmnet NIC, then ICMP + TCP.
    local vmx="$VM_TOOLS/vm-exec" route_dev mmnet_dev tcp_out probe="$MMNET_PROBE_PORT"
    # The device peer A configured (parse MMNET-DEV= from its setup output).
    mmnet_dev=$(sed -n 's/.*MMNET-DEV=\([^ ]*\).*/\1/p' "$log_dir/setup-a.log" | head -1)
    route_dev=$("$vmx" "$vm_a" "ip -o route get $ip_b 2>/dev/null | sed -n 's/.*dev \\([^ ]*\\).*/\\1/p' | head -1" 2>/dev/null | tr -d '[:space:]')
    {
        echo "expected mmnet dev: $mmnet_dev"
        echo "route get $ip_b dev: $route_dev"
    } > "$log_dir/route.log"
    if [ -z "$route_dev" ] || [ "$route_dev" != "$mmnet_dev" ]; then
        collect_vm_artifacts "$vm_a" mmnet-a; collect_vm_artifacts "$vm_b" mmnet-b
        record_result mmnet route fail "$EXIT_SERVICE" service mmnet "$log_dir/route.log" \
            "route to $ip_b egresses '$route_dev', expected mmnet NIC '$mmnet_dev'"
        record_timing mmnet smoke 0 "$(( $(date +%s) - t0 ))" "$(( $(date +%s) - t0 ))" "$EXIT_SERVICE" "$vm_a,$vm_b"
        return "$EXIT_SERVICE"
    fi
    log "mmnet: route A->$ip_b via $route_dev (mmnet NIC) — data plane confirmed independent of qga"

    # ICMP A -> B.
    # CAPTURE THROUGH A FILE, NEVER THROUGH `$( ... 2>&1 )`. That shape hands
    # vm-exec's fd 1 AND fd 2 to the substitution's pipe. vm-exec redirects its
    # own children's fd 1 to an internal capture file, but fd 2 is inherited
    # straight through to every virsh/jq descendant it starts; a descendant that
    # outlives vm-exec and keeps that descriptor holds the pipe open, and bash
    # waits for the PIPE to close, not for vm-exec to exit. The call then hangs
    # after the ping is long dead, and an outer `timeout` on vm-exec does not
    # help because the shell is blocked on the read, not on the child. Writing
    # both descriptors to a regular file removes the dependency: a read from a
    # regular file reaches EOF at the current end-of-file whoever else still
    # holds it open. ping.log is the artifact the failure path records anyway,
    # so this costs nothing.
    local prc=0
    "$vmx" "$vm_a" "ping -c 3 -W 2 $ip_b" > "$log_dir/ping.log" 2>&1 || prc=$?
    if [ "$prc" -ne 0 ]; then
        collect_vm_artifacts "$vm_a" mmnet-a; collect_vm_artifacts "$vm_b" mmnet-b
        record_result mmnet ping fail "$EXIT_SERVICE" service mmnet "$log_dir/ping.log" "ping A->B ($ip_b) failed"
        record_timing mmnet smoke 0 "$(( $(date +%s) - t0 ))" "$(( $(date +%s) - t0 ))" "$EXIT_SERVICE" "$vm_a,$vm_b"
        return "$EXIT_SERVICE"
    fi
    record_result mmnet ping pass 0 pass mmnet "$log_dir/ping.log" "A($ip_a)->B($ip_b) ICMP ok over mmnet NIC $route_dev"

    # TCP A -> B: B opens a one-shot listener on $probe; A connects and reads a
    # token. Listener prefers nc/ncat but falls back to a portable python3
    # one-shot server (python3 is always present in baseweed) so the TCP proof
    # never depends on a netcat flavour the image may lack.
    local token="mmnet-ok-$seed"
    local py_listener="import socket,sys
s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(('0.0.0.0',$probe)); s.listen(1); s.settimeout(30)
c,_=s.accept(); c.sendall(b'$token'); c.close()"
    "$vmx" "$vm_b" "nohup sh -c '
        if command -v nc >/dev/null 2>&1; then printf %s \"$token\" | timeout 30 nc -l -p $probe 2>/dev/null;
        elif command -v ncat >/dev/null 2>&1; then printf %s \"$token\" | timeout 30 ncat -l $probe 2>/dev/null;
        else python3 -c \"$py_listener\" 2>/dev/null; fi' >/dev/null 2>&1 & echo listener-started" > "$log_dir/tcp-listener.log" 2>&1 || true
    sleep 2
    tcp_out=$(_mmnet_vmx_merged "$vm_a" "
        for i in 1 2 3 4 5; do
            r=\$( (timeout 5 nc $ip_b $probe 2>/dev/null) \
                || (timeout 5 ncat $ip_b $probe 2>/dev/null) \
                || (exec 3<>/dev/tcp/$ip_b/$probe && head -c 64 <&3) 2>/dev/null )
            [ -n \"\$r\" ] && { echo \"\$r\"; exit 0; }
            sleep 1
        done
        echo NO-TCP; exit 1"); local trc=$?
    printf '%s\n' "$tcp_out" > "$log_dir/tcp.log"
    if [ "$trc" -ne 0 ] || ! grep -qF "$token" <<<"$tcp_out"; then
        collect_vm_artifacts "$vm_a" mmnet-a; collect_vm_artifacts "$vm_b" mmnet-b
        # Same distinction as nic-setup above: a capture failure means the
        # token was never observed, not that it was absent.
        if [ "$trc" = 125 ]; then
            record_result mmnet tcp fail "$EXIT_RUNNER" runner mmnet "$log_dir/tcp.log" \
                "TCP A->B:$probe UNKNOWN: the host could not capture the probe output (rc=125); this is a harness capture failure, not a product result"
            record_timing mmnet smoke 0 "$(( $(date +%s) - t0 ))" "$(( $(date +%s) - t0 ))" "$EXIT_RUNNER" "$vm_a,$vm_b"
            return "$EXIT_RUNNER"
        fi
        record_result mmnet tcp fail "$EXIT_SERVICE" service mmnet "$log_dir/tcp.log" \
            "TCP A->B:$probe did not return token '$token' (got: ${tcp_out:0:120})"
        record_timing mmnet smoke 0 "$(( $(date +%s) - t0 ))" "$(( $(date +%s) - t0 ))" "$EXIT_SERVICE" "$vm_a,$vm_b"
        return "$EXIT_SERVICE"
    fi
    record_result mmnet tcp pass 0 pass mmnet "$log_dir/tcp.log" "A->B:$probe TCP token '$token' received over mmnet NIC"

    record_timing mmnet smoke 0 "$(( $(date +%s) - t0 ))" "$(( $(date +%s) - t0 ))" 0 "$vm_a,$vm_b"
    log "mmnet: SMOKE PASS — two VMs reached each other over the isolated udp-loopback segment"
    return "$rc"
}

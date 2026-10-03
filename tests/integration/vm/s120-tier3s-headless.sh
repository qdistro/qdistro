#!/bin/bash
# s120-tier3s-headless.sh — GUEST driver (root) for phase7-tier3s-headless.bats.
# Tier 3s (gVisor runsc) headless launch path, todo/paravirt 06 "Δ DONE bar":
#   item 1  every runtime process class sits in the recorded owning scope
#           (recursive cgroup.procs);
#   item 2  teardown paths: normal exit, plain `podman stop`, plain `podman rm
#           -f`, session-manager stop (StopSilo), and a forced runtime
#           stop/query failure while live (state root missing, then replaced):
#           an error, a preserved record and scope, no false "no container";
#           then recovery with the correct root and complete teardown;
#   item 3  two concurrent launches: tearing one down preserves the other;
#   item 4  runtime identity (ΔA9) and the runsc state-root policy (ΔA1), with
#           the missing/replaced-root negatives for a plain `podman stop`;
#   item 8  container posture from the emitted OCI spec AND the running
#           sandbox, the ΔA5 image assertions, the fchmodat2 chmod path and
#           each ΔA4 per-workload seccomp decision.
# Runs after tier3s-guest-setup.sh (installed tested commit, runsc
# provisioned, probe PASS, image loaded, broker allow rule). Each check prints
# one PASS/FAIL line; `[s120] N passes, M failures`; exit 1 on any failure.
#   s120-tier3s-headless.sh
set -u
T3S_TAG=s120
. "$(dirname "$0")/tier3s-guest-lib.sh"
SX=s120x; SA=s120a; SB=s120b

step "0. preconditions (setup ran)"
out=$(/usr/lib/qdistro/tier3s/probe.sh --user admin 2>&1); rc=$?
is "probe PASS before the launches" "$rc:$(printf '%s\n' "$out" | grep -c '^RESULT PASS')" "0:1"
is "image present" "$(yes_no pm image exists "$IMAGE")" yes
is "broker allows the smoke spawn" "$(broker_check "$ACTION")" allow
assert_all_clear pre
for s in $SX $SA $SB; do
    sm CreateTier3sSilo ssss "$s" headless-smoke "$s" none > /dev/null; is "CreateTier3sSilo $s" "$(silo_state "$s")" Created
done
set_argv "$SX=12" "$SA=600" "$SB=600" | sed 's/^/    /'
is "argv set with the manager restarted" "$(yes_no manager_up)" yes

# ---------------------------------------------------------------------------
step "1. teardown path: normal exit (workload ends by itself after 12 s)"
TX=$(up_silo $SX)
if [ -n "$TX" ]; then pass "normal-exit: launch $TX recorded running"; else fail "normal-exit: launch did not come up"; fi
UX=$(unit_of $SX)
wait_for 90 unit_down "$UX"
is "normal-exit: unit ended" "$(unit_state "$UX")" inactive
is "normal-exit: unit Result" "$(systemctl show -p Result --value "$UX")" success
is "normal-exit: spawn exit status" "$(systemctl show -p ExecMainStatus --value "$UX")" 0
is "normal-exit: workload ran to its end (no SIGTERM)" \
    "$(scope_log "$TX" | grep -c '^SMOKE done' | sed 's/[1-9][0-9]*/yes/'):$(scope_log "$TX" | grep -c '^SMOKE term')" "yes:0"
is "normal-exit: verified cleanup tore the launch down" \
    "$(journalctl -u "$UX" --no-pager -o cat | grep -c "qdistro-tier3s-cleanup: $TX: torn down (qdistro-tier3s-$SX)")" 1
assert_launch_gone normal-exit "$TX" "$(ctr_of $SX)"
sm StopSilo si $SX 10 > /dev/null; is "normal-exit: StopSilo afterwards" "$(silo_state $SX)" Stopped

# ---------------------------------------------------------------------------
step "2. live launch A: placement, identity, state root, record, posture"
TA=$(up_silo $SA); CA=$(ctr_of $SA); UA=$(unit_of $SA)
if [ -n "$TA" ]; then pass "launch A $TA recorded running"; else fail "launch A did not come up"; finish; fi
is "silo A Active" "$(silo_state $SA)" Active
is "Type=notify: StartSilo returned only once launch A was recorded running" "$(cat "$WORK/up-phase.$SA")" running
spid=$(rec "$TA" sentry_pid); cpid=$(rec "$TA" conmon_pid)
cg="/sys/fs/cgroup$(rec "$TA" scope_cgroup)"
read -r i_pid i_cpid i_id i_rt < <(pm inspect --format '{{.State.Pid}} {{.State.ConmonPid}} {{.Id}} {{.OCIRuntime}}' "$CA")

# --- DONE 1: placement, by recursive cgroup.procs of the recorded scope
classify() {   # classify <pid> -> class name
    local p="$1" exe a0
    exe=$(readlink "/proc/$p/exe" 2>/dev/null); a0=$(tr '\0' '\n' < "/proc/$p/cmdline" 2>/dev/null | head -1)
    case "$exe" in
        /usr/sbin/runuser) echo runuser ;;
        /usr/bin/podman) echo podman-cli ;;
        /usr/bin/conmon) echo conmon ;;
        /usr/libexec/qdistro/runsc/runsc) [ "$a0" = runsc-gofer ] && echo runsc-gofer || echo "runsc:$a0" ;;
        /usr/libexec/qdistro/runsc/gvisor-bin/gvisor_sentry)
            if [ "$p" = "$i_pid" ] && [ "$a0" = runsc-sandbox ]; then echo runsc-sandbox
            elif [ -z "$a0" ]; then echo systrap-stub
            else echo "sentry:$a0"; fi ;;
        /usr/libexec/qdistro/runsc/gvisor-bin/runsc-fd-parking) echo runsc-fd-parking ;;
        *) echo "other:${exe:-?}" ;;
    esac
}
placement() {   # placement <tag> <token> <state pid>
    local tag="$1" tok="$2" cgd="/sys/fs/cgroup$(rec "$2" scope_cgroup)" p c
    declare -A n=()
    for p in $(tree_procs "$cgd"); do c=$(i_pid="$3" classify "$p"); n[$c]=$(( ${n[$c]:-0} + 1 )); done
    for c in runuser podman-cli conmon runsc-gofer runsc-sandbox runsc-fd-parking systrap-stub; do
        if [ "${n[$c]:-0}" -ge 1 ]; then pass "placement[$tag]: $c x${n[$c]} inside $(rec "$tok" scope_unit)"
        else fail "placement[$tag]: no $c process inside the scope"; fi
    done
    for c in "${!n[@]}"; do
        case "$c" in runuser|podman-cli|conmon|runsc-*|systrap-stub) ;; *) info "placement[$tag]: also in the scope: $c x${n[$c]}" ;; esac
    done
}
placement A "$TA" "$i_pid"
outside=0; inside=0
for p in $(runsc_pids); do if tree_procs "$cg" | grep -qx "$p"; then inside=$((inside + 1)); else outside=$((outside + 1)); fi; done
is "placement[A]: runsc-bundle processes outside the owning scope (of $inside inside)" "$outside" 0
is "placement[A]: recorded sentry and conmon are in the scope" \
    "$(tree_procs "$cg" | grep -cx "$spid"):$(tree_procs "$cg" | grep -cx "$cpid")" "1:1"
# fable A r1 P3-5: every conmon of THIS container host-wide (by its full
# container id argument) and every podman CLI of THIS launch (by its token
# label argument) is inside the scope, not only "at least one inside"
declare -A own_in=([conmon]=0 [podman]=0) own_out=([conmon]=0 [podman]=0)
for p in /proc/[0-9]*; do
    case "$(readlink "$p/exe" 2>/dev/null)" in
        /usr/bin/conmon) c=conmon; tr '\0' '\n' < "$p/cmdline" 2>/dev/null | grep -qx "$i_id" || continue ;;
        /usr/bin/podman) c=podman; tr '\0' '\n' < "$p/cmdline" 2>/dev/null | grep -qx "qdistro_tier3s_token=$TA" || continue ;;
        *) continue ;;
    esac
    if tree_procs "$cg" | grep -qx "${p#/proc/}"; then own_in[$c]=$((own_in[$c] + 1)); else own_out[$c]=$((own_out[$c] + 1)); fi
done
for c in conmon podman; do
    is "placement[A]: every $c of this launch host-wide is inside the scope (inside:outside)" \
        "$([ "${own_in[$c]}" -ge 1 ] && echo some || echo none):${own_out[$c]}" "some:0"
done

# --- DONE 4 / ΔA9: identity (asserted), corroboration (INFO only)
is "identity: podman's selected runtime is the tier3s wrapper" "$i_rt" "$WRAPPER"
is "identity: State.Pid is the recorded sentry" "$i_pid" "$spid"
is "identity: State.ConmonPid is the recorded conmon" "$i_cpid" "$cpid"
is "identity: State.Pid exe sha512 = pin sidecar_gvisor_sentry_sha512" "$(exe_sha512 "$i_pid")" "$(pin_value sidecar_gvisor_sentry_sha512)"
gofer=""; for p in $(tree_procs "$cg"); do [ "$(i_pid=$i_pid classify "$p")" = runsc-gofer ] && gofer=$p; done
is "identity: gofer exe sha512 = pin runsc_sha512" "$(exe_sha512 "${gofer:-0}")" "$(pin_value runsc_sha512)"
n_app=0; for p in $(tree_procs "$cg"); do case "$(cat "/proc/$p/comm" 2>/dev/null)" in sleep|qdistro-tier3s-*) n_app=$((n_app + 1)) ;; esac; done
info "corroboration only: host processes in the scope named like the workload (sleep/qdistro-tier3s-smoke): $n_app"
info "corroboration only: in-sandbox $(scope_log "$TA" | grep -m1 '^SMOKE kernel=')"
info "corroboration only: in-sandbox $(scope_log "$TA" | grep -m1 '^SMOKE dmesg=')"

# --- DONE 4 / ΔA1: plain podman calls reach the sandbox through the wrapper root
is "state root: plain podman ps lists A running" "$(pm ps --filter "name=^$CA\$" --format '{{.State}}')" running
pm ps --sync > /dev/null 2>&1; rc=$?
is "state root: plain podman ps --sync (runtime state query) rc" "$rc" 0
is "state root: A still running after the sync" "$(ctr_status "$CA")" running
is "state root: runsc state for A is in $SROOT" "$(yes_no test -n "$(find "$SROOT" -mindepth 1 -name "*$i_id*" 2>/dev/null)")" yes

# --- ΔA8: the control record
is "record: dir and state are root 0700 / 0600" "$(stat -c '%U %a' "$CTL/$TA") / $(stat -c '%U %a' "$CTL/$TA/state")" "root 700 / root 600"
is "record: container" "$(rec "$TA" container)" "$CA"
is "record: token = container label" "$(rec "$TA" token)" "$(pm inspect --format '{{index .Config.Labels "qdistro_tier3s_token"}}' "$CA")"
is "record: container_id" "$(rec "$TA" container_id)" "$i_id"
is "record: unit" "$(rec "$TA" unit)" "$UA"
is "record: runsc_root" "$(rec "$TA" runsc_root)" "$SROOT"
is "record: scope_cgroup = systemd's cgroup of the scope" "$(rec "$TA" scope_cgroup)" "$(systemctl show -p ControlGroup --value "qdistro-tier3s-$TA.scope")"
is "record: sentry (pid, starttime)" "$(rec "$TA" sentry_pid) $(rec "$TA" sentry_starttime)" "$i_pid $(starttime "$i_pid")"
is "record: conmon (pid, starttime)" "$(rec "$TA" conmon_pid) $(rec "$TA" conmon_starttime)" "$i_cpid $(starttime "$i_cpid")"
is "record: per-launch dir is admin 0700 under the root parent" "$(stat -c '%u %a' "$LAUNCHES/$TA")" "1000 700"

# --- DONE 8: posture from the emitted OCI spec
spec=$(pm inspect --format '{{.OCIConfigPath}}' "$CA")
python3 - "$spec" /usr/lib/qdistro/tier3s/seccomp/headless-smoke.json <<'PY' > "$WORK/spec.txt"
import json, sys
c = json.load(open(sys.argv[1])); prof = json.load(open(sys.argv[2]))
p, lx = c["process"], c["linux"]
def out(k, v): print(f"{k}={v}")
out("user", f'{p["user"]["uid"]}:{p["user"]["gid"]}')
out("keepid", any(m["containerID"] == 1000 and m["hostID"] == 0 and m["size"] == 1 for m in lx.get("uidMappings", [])))
out("readonly", c["root"].get("readonly") is True)
out("selinux_process_label", p.get("selinuxLabel") or "none")
ann = c.get("annotations", {})
out("label_annotation", ann.get("io.podman.annotations.label", "?"))
out("seccomp_annotation", ann.get("io.podman.annotations.seccomp", "?"))
out("nnp", p.get("noNewPrivileges") is True)
caps = p.get("capabilities") or {}
out("caps_empty", all(not v for v in caps.values()))
out("netns_fresh", any(n["type"] == "network" and not n.get("path") for n in lx["namespaces"]))
tm = {m["destination"]: m["options"] for m in c["mounts"] if m["type"] == "tmpfs"}
for d in ("/run/user/1000", "/home/admin/.cache"):
    out(f"tmpfs{d}", "uid=1000" in tm.get(d, []) and "gid=1000" in tm.get(d, []))
s = lx.get("seccomp") or {}
spec_allow = {n for r in s.get("syscalls", []) if r["action"] == "SCMP_ACT_ALLOW" for n in r["names"]}
file_allow = {n for r in prof["syscalls"] if r["action"] == "SCMP_ACT_ALLOW" for n in r["names"]}
out("seccomp_default", f'{s.get("defaultAction")}/{prof["defaultAction"]}')
out("seccomp_allow_equal", spec_allow == file_allow)
out("seccomp_allow_diff", sorted(spec_allow ^ file_allow)[:12])
dec = prof.get("tier3sDecisions", {})
for call in ("fchmodat2", "llistxattr", "setfsuid", "setfsgid", "fadvise64", "link", "syslog"):
    d = dec.get(call)
    d = d.get("decision") if isinstance(d, dict) else d
    out(f"decision_{call}", f'{str(d).upper()}:{"ALLOW" if call in file_allow else "DENY"}:{"ALLOW" if call in spec_allow else "DENY"}')
PY
sp() { sed -n "s|^$1=||p" "$WORK/spec.txt"; }
sed 's/^/    spec: /' "$WORK/spec.txt"
is "posture/spec: process user (admin keep-id)" "$(sp user)" "1000:1000"
is "posture/spec: keep-id maps container 1000 to the admin (rootless userns 0)" "$(sp keepid)" True
is "posture/spec: read-only root" "$(sp readonly)" True
is "posture/spec: label=disable (annotation, no process label)" "$(sp label_annotation):$(sp selinux_process_label)" "disable:none"
is "posture/spec: no-new-privileges" "$(sp nnp)" True
is "posture/spec: no capabilities in any set" "$(sp caps_empty)" True
is "posture/spec: podman network mode" "$(pm inspect --format '{{.HostConfig.NetworkMode}}' "$CA")" none
is "posture/spec: fresh network namespace" "$(sp netns_fresh)" True
for p in "$gofer" "$i_pid"; do
    is "posture/runsc: network=none on $(tr '\0' '\n' < "/proc/$p/cmdline" | head -1) ($p)" \
        "$(tr '\0' '\n' < "/proc/$p/cmdline" | grep -cx -- '--network=none')" 1
done
is "posture/spec: selected per-workload seccomp file" "$(sp seccomp_annotation)" /usr/lib/qdistro/tier3s/seccomp/headless-smoke.json
is "posture/spec: seccomp default action (spec/file)" "$(sp seccomp_default)" "SCMP_ACT_ERRNO/SCMP_ACT_ERRNO"
is "posture/spec: seccomp allow set = the file's allow set" "$(sp seccomp_allow_equal)" True
is "posture/spec: tmpfs /run/user/1000 uid=1000,gid=1000" "$(sp tmpfs/run/user/1000)" True
is "posture/spec: tmpfs /home/admin/.cache uid=1000,gid=1000" "$(sp tmpfs/home/admin/.cache)" True
for call in fchmodat2:DENY llistxattr:ALLOW setfsuid:DENY setfsgid:DENY fadvise64:DENY link:DENY syslog:ALLOW; do
    is "ΔA4 decision ${call%%:*} (declared:profile:spec)" "$(sp "decision_${call%%:*}")" "${call##*:}:${call##*:}:${call##*:}"
done

# --- DONE 8 / ΔA5: the running sandbox (the smoke's own report from inside gVisor)
sm_line() { scope_log "$TA" | grep -m1 -F "SMOKE $1" | sed "s|^SMOKE $1||"; }
scope_log "$TA" | awk '!seen[$0]++' | sed 's/^/    sandbox: /'
pin=$(sed -n 's/^snapshot=\([0-9]\{8\}\)$/\1/p' /root/qdistro-src-t3s/snapshot.conf | head -1)
is "ΔA5: image snapshot label = snapshot.conf pin" "$(pm image inspect --format '{{index .Labels "org.qdistro.snapshot"}}' "$IMAGE")" "$pin"
is "ΔA5: /etc/qdistro/tier3s-image inside = pin" "$(sm_line snapshot=)" "$pin"
is "ΔA5/sandbox: uid 1000 (keep-id)" "$(sm_line id= | cut -d' ' -f1-2)" "uid=1000(admin) gid=1000(admin)"
is "ΔA5: passwd entry for uid 1000" "$(sm_line passwd=)" "admin:x:1000:1000:qdistro admin:/home/admin:/bin/bash"
is "ΔA5: HOME" "$(sm_line home=)" /home/admin
is "ΔA5: UTF-8 locale" "$(sm_line lang= | cut -d' ' -f1-2)" "C.UTF-8 charmap=UTF-8"
is "ΔA5: /run/user/1000 tmpfs owned 1000:1000 inside" "$(sm_line 'mount /run/user/1000=')" "1000:1000 700"
is "ΔA5: /home/admin/.cache tmpfs owned 1000:1000 inside" "$(sm_line 'mount /home/admin/.cache=')" "1000:1000 700"
is "sandbox: gVisor mount options carry uid=1000,gid=1000" \
    "$(sm_line 'mountopts /run/user/1000=' | grep -c 'uid=1000,gid=1000'):$(sm_line 'mountopts /home/admin/.cache=' | grep -c 'uid=1000,gid=1000')" "1:1"
is "sandbox: no capabilities" "$(sm_line caps=)" "inh:0000000000000000,prm:0000000000000000,eff:0000000000000000,bnd:0000000000000000,amb:0000000000000000"
is "sandbox: NoNewPrivs and seccomp filter mode" "$(sm_line nnp=)" "1 seccomp=2"
is "sandbox: root filesystem mounted ro" "$(sm_line rootfs=)" ro
is "sandbox: write to the (admin-owned) rootfs home fails EROFS" "$(sm_line 'rootfs_write ' | grep -c 'rc=1 .*Read-only file system')" 1
is "ΔA4 fchmodat2 path exercised: plain chmod works" "$(sm_line 'chmod ')" "rc=0 mode=600"
is "ΔA4 fchmodat2 path exercised: chmod -h (fchmodat2) is denied, mode unchanged" "$(sm_line 'chmod_nofollow ')" "rc=1 mode=600"
is "ΔA4 llistxattr ALLOW effective: ls -l clean" "$(sm_line 'ls_l ')" "rc=0 stderr_bytes=0"
is "ΔA4 syslog ALLOW effective: dmesg answers (gVisor banner)" "$(sm_line dmesg= | grep -c 'gVisor')" 1
is "sandbox: loopback is the only link" "$(sm_line routes= | sed 's/^.* links=//')" "lo,"
is "sandbox: no default route, every route on lo" \
    "$(sm_line route_table= | tr ';' '\n' | grep -c . | sed 's/^0$/none/;s/[1-9][0-9]*/some/'):$(sm_line route_table= | tr ';' '\n' | grep -c '^default'):$(sm_line route_table= | tr ';' '\n' | grep . | grep -vc ' dev lo')" "some:0:0"

# ---------------------------------------------------------------------------
step "3. two concurrent launches; StopSilo (session-manager stop) of A preserves B"
TB=$(up_silo $SB); CB=$(ctr_of $SB); UB=$(unit_of $SB)
if [ -n "$TB" ]; then pass "launch B $TB recorded running while A is live"; else fail "launch B did not come up"; finish; fi
is "two launches: distinct tokens and scopes" "$(yes_no test "$TA" != "$TB")" yes
is "two launches: both running" "$(ctr_status "$CA"):$(ctr_status "$CB")" "running:running"
read -r b_pid < <(pm inspect --format '{{.State.Pid}}' "$CB")
placement B "$TB" "$b_pid"
cgb="/sys/fs/cgroup$(rec "$TB" scope_cgroup)"
outside=0
for p in $(runsc_pids); do tree_procs "$cg" | grep -qx "$p" || tree_procs "$cgb" | grep -qx "$p" || outside=$((outside + 1)); done
is "two launches: runsc-bundle processes outside both scopes" "$outside" 0
cur=$(journal_cursor)
sm StopSilo si $SA 10 > /dev/null; rc=$?
is "session-manager stop: StopSilo A rc" "$rc" 0
is "session-manager stop: silo A Stopped" "$(silo_state $SA)" Stopped
is "session-manager stop: A ended gracefully (SIGTERM reached the workload)" "$(scope_log "$TA" | grep -c '^SMOKE term' | sed 's/[1-9][0-9]*/yes/')" yes
is "session-manager stop: verified cleanup tore A down" "$(unit_log "$UA" "$cur" | grep -c "qdistro-tier3s-cleanup: $TA: torn down")" 1
assert_launch_gone session-manager-stop "$TA" "$CA"
alive=0; total=0
while read -r p st; do total=$((total + 1)); [ "$(starttime "$p")" = "$st" ] && alive=$((alive + 1)); done < "$WORK/$TB.procs"
is "two launches: every process of B survived A's teardown ($total)" "$alive" "$total"
is "two launches: B unit, container, record, scope intact" \
    "$(unit_state "$UB"):$(ctr_status "$CB"):$(yes_no test -d "$CTL/$TB"):$(unit_state "qdistro-tier3s-$TB.scope")" "active:running:yes:active"
is "two launches: silo B still Active" "$(silo_state $SB)" Active

# ---------------------------------------------------------------------------
step "4. ΔA1 negatives on live B: plain podman stop with the state root missing, then replaced"
bs=$(rec "$TB" sentry_pid); bst=$(rec "$TB" sentry_starttime)
b_alive() { [ "$(starttime "$bs")" = "$bst" ]; }
preserved() {   # preserved <tag>: sandbox, record and scope all still there
    is "$1: sentry still alive" "$(yes_no b_alive)" yes
    is "$1: control record preserved" "$(yes_no test -f "$CTL/$TB/state")" yes
    is "$1: per-launch dir preserved" "$(yes_no test -d "$LAUNCHES/$TB")" yes
    is "$1: owning scope preserved" "$(unit_state "qdistro-tier3s-$TB.scope")" active
    is "$1: podman still has the container (no false absence)" "$(pm container exists "$CB"; echo $?)" 0
}
cur=$(journal_cursor)
mv "$SROOT" "$SROOT.s120-aside"
out=$(pm stop -t 3 "$CB" 2>&1); rc=$?
info "plain podman stop, root missing: rc=$rc: $(printf '%s' "$out" | tail -1)"
if [ "$rc" -ne 0 ]; then pass "missing root: plain podman stop fails visibly (rc=$rc)"; else fail "missing root: plain podman stop succeeded"; fi
is "missing root: no root minted by the stop" "$(yes_no test -e "$SROOT")" no
is "missing root: wrapper refusal in the journal (tier3s-runsc)" \
    "$(journalctl -t tier3s-runsc --after-cursor="$cur" --no-pager -o cat | grep -c "state root $SROOT is missing" | sed 's/[1-9][0-9]*/yes/')" yes
preserved "missing root (podman stop)"
install -d -o 1000 -g 1000 -m 0700 "$SROOT"
out=$(pm stop -t 3 "$CB" 2>&1); rc=$?
info "plain podman stop, empty replacement root: rc=$rc: $(printf '%s' "$out" | tail -1)"
if [ "$rc" -ne 0 ]; then pass "replaced root: plain podman stop fails visibly (rc=$rc)"; else fail "replaced root: plain podman stop succeeded"; fi
preserved "replaced root (podman stop)"

step "5. DONE 2: forced runtime failure through the cleanup itself, then recovery"
out=$("$CLEANUP" "$TB" 2>&1); rc=$?
printf '%s\n' "$out" | sed 's/^/    cleanup: /'
if [ "$rc" -ne 0 ]; then pass "replaced root: cleanup returns an error (rc=$rc)"; else fail "replaced root: cleanup returned 0"; fi
is "replaced root: no 'torn down' claimed" "$(printf '%s\n' "$out" | grep -c 'torn down')" 0
preserved "replaced root (cleanup)"
rmdir "$SROOT"
out=$("$CLEANUP" "$TB" 2>&1); rc=$?
printf '%s\n' "$out" | sed 's/^/    cleanup: /'
if [ "$rc" -ne 0 ]; then pass "missing root: cleanup returns an error (rc=$rc)"; else fail "missing root: cleanup returned 0"; fi
is "missing root: cleanup refuses to query or stop" "$(printf '%s\n' "$out" | grep -c 'refusing to query or stop')" 1
is "missing root: no 'torn down' and no 'no container' claimed" "$(printf '%s\n' "$out" | grep -ci 'torn down\|no container\|absent')" 0
preserved "missing root (cleanup)"
mv "$SROOT.s120-aside" "$SROOT"
is "recovery: original state root restored" "$(stat -c '%u %a' "$SROOT")" "1000 700"
cur=$(journal_cursor)
out=$("$CLEANUP" "$TB" 2>&1); rc=$?
printf '%s\n' "$out" | sed 's/^/    cleanup: /'
is "recovery: cleanup with the correct root rc" "$rc" 0
is "recovery: cleanup tore B down" "$(printf '%s\n' "$out" | grep -c "$TB: torn down ($CB)")" 1
assert_launch_gone recovery "$TB" "$CB"
wait_for 60 unit_down "$UB"
is "recovery: B's launch unit ended" "$(unit_state "$UB")" inactive
sm StopSilo si $SB 10 > /dev/null; is "recovery: StopSilo B afterwards" "$(silo_state $SB)" Stopped

# ---------------------------------------------------------------------------
step "6. teardown path: plain podman stop (as admin, no flags)"
TA=$(up_silo $SA)
if [ -n "$TA" ]; then pass "podman-stop: launch $TA up"; else fail "podman-stop: launch did not come up"; fi
out=$(pm stop -t 10 "$CA" 2>&1); rc=$?
is "podman-stop: plain podman stop rc" "$rc" 0
wait_for 60 unit_down "$UA"
is "podman-stop: launch unit ended cleanly" "$(unit_state "$UA"):$(systemctl show -p Result --value "$UA")" "inactive:success"
is "podman-stop: the workload got SIGTERM" "$(scope_log "$TA" | grep -c '^SMOKE term' | sed 's/[1-9][0-9]*/yes/')" yes
assert_launch_gone podman-stop "$TA" "$CA"
sm StopSilo si $SA 10 > /dev/null; is "podman-stop: StopSilo afterwards" "$(silo_state $SA)" Stopped

step "7. teardown path: plain podman rm -f (as admin, no flags)"
TA=$(up_silo $SA)
if [ -n "$TA" ]; then pass "podman-rm: launch $TA up"; else fail "podman-rm: launch did not come up"; fi
out=$(pm rm -f -t 10 "$CA" 2>&1); rc=$?
is "podman-rm: plain podman rm -f rc" "$rc" 0
wait_for 60 unit_down "$UA"
is "podman-rm: launch unit ended" "$(unit_state "$UA")" inactive
assert_launch_gone podman-rm "$TA" "$CA"
sm StopSilo si $SA 10 > /dev/null; is "podman-rm: StopSilo afterwards" "$(silo_state $SA)" Stopped

step "8. cleanup"
for s in $SX $SA $SB; do sm DeleteSilo s "$s" > /dev/null; is "DeleteSilo $s" "$(silo_state "$s")" absent; done
assert_all_clear end
finish

#!/bin/bash
# tier3s/spike/phase-a-feasibility.sh — GUEST side (root, dev test VM). The
# feasibility checks the Phase A decisions D-A1 (runsc state root) and D-A3b
# (owning scope + lifetime coupling) rest on (tier3s/CONTRACT.md). Not the
# A-iii acceptance drivers: no spawn script, no session manager, no seccomp
# profile, plain sleep workload. Asserting: each check prints
# `CHECK <name>: OK|BAD ...`; the step's exit status is its BAD count.
#   phase-a-feasibility.sh <step>     step = setup | a1 | a1neg | a3b | a3bkill
set -u
cd /root/qdistro-src || exit 99
BAD=0
ok()  { echo "CHECK $1: OK${2:+ ($2)}"; }
bad() { echo "CHECK $1: BAD${2:+ ($2)}"; BAD=$((BAD + 1)); }
is()  { if [ "$2" = "$3" ]; then ok "$1" "$2"; else bad "$1" "got '$2', want '$3'"; fi; }
finish() { echo "### BAD=$BAD"; exit "$BAD"; }
W=/usr/libexec/qdistro/tier3s-runsc
ROOTDIR=/run/qdistro-tier3s-runsc/1000
IMG=registry.opensuse.org/opensuse/tumbleweed:20260929
PIN=tier3s/RUNSC_RELEASE
as_admin() {
    runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin USER=admin LOGNAME=admin \
        XDG_RUNTIME_DIR=/run/user/1000 "$@"
}
RUN_FLAGS=(--security-opt label=disable --security-opt no-new-privileges --cap-drop=ALL
           --userns=keep-id --user 1000:1000 --read-only --network=none)
pm3s() { as_admin podman --runtime "$W" --runtime-flag=network=none "$@"; }
pinkey() {   # which RUNSC_RELEASE hash /proc/<pid>/exe matches
    local h; h=$(sha512sum < "/proc/$1/exe" 2>/dev/null | cut -d' ' -f1)
    awk -F= -v h="$h" '$1 ~ /_sha512$/ && $2 == h { print $1; f=1 } END { if (!f) print "-" }' "$PIN"
}
runsc_pids() {   # every host process whose exe is a file of the runsc bundle
    local p e
    for p in /proc/[0-9]*; do
        e=$(readlink "$p/exe" 2>/dev/null) || continue
        case "$e" in /usr/libexec/qdistro/runsc/*) echo "${p#/proc/}" ;; esac
    done
}
tree_procs() {   # recursive cgroup.procs of a /sys/fs/cgroup directory
    find "$1" -name cgroup.procs -exec cat {} + 2>/dev/null | sort -n
}
alive() { [ -d "/proc/$1" ]; }

case "${1:-}" in
setup)
    install -o root -g root -m 0644 tier3s/tmpfiles/qdistro-tier3s.conf /usr/lib/tmpfiles.d/qdistro-tier3s.conf
    systemd-tmpfiles --create /usr/lib/tmpfiles.d/qdistro-tier3s.conf; is tmpfiles-rc $? 0
    stat -c '%n %a %U:%G' /run/qdistro-tier3s-runsc $ROOTDIR /run/qdistro-tier3s-ctl /run/qdistro-tier3s
    is state-root "$(stat -c '%a %u:%g' $ROOTDIR)" "700 1000:1000"
    install -o root -g root -m 0755 tier3s/qdistro-tier3s-scope /usr/libexec/qdistro/qdistro-tier3s-scope
    tier3s/probe.sh --user admin; is probe-rc $? 0
    as_admin podman pull -q "$IMG"; is pull-rc $? 0
    as_admin podman image inspect --format '{{.Id}} {{.Digest}}' "$IMG"
    finish ;;
a1)
    echo "## D-A1: no --root anywhere; the wrapper derives the root from the host uid"
    pm3s run -d --name t3sf-a "${RUN_FLAGS[@]}" "$IMG" sleep 300; is run-rc $? 0
    sleep 2
    spid=$(as_admin podman inspect --format '{{.State.Pid}}' t3sf-a)
    id=$(as_admin podman inspect --format '{{.Id}}' t3sf-a)
    echo "State.Pid=$spid id=$id"; is sentry-exe-pin "$(pinkey "$spid")" sidecar_gvisor_sentry_sha512
    ls -la $ROOTDIR
    if ls $ROOTDIR | grep -q "$id"; then ok state-in-fixed-root; else bad state-in-fixed-root "no $id entry"; fi
    is runtime "$(as_admin podman inspect --format '{{.OCIRuntime}}' t3sf-a)" "$W"
    is plain-ps "$(as_admin podman ps --filter name=t3sf-a --format '{{.Names}} {{.State}}')" "t3sf-a running"
    as_admin podman stop -t 5 t3sf-a; is plain-stop-rc $? 0
    sleep 1; if alive "$spid"; then bad sentry-gone-after-plain-stop; else ok sentry-gone-after-plain-stop; fi
    as_admin podman rm t3sf-a; is plain-rm-rc $? 0
    if ls $ROOTDIR | grep -q "$id"; then bad root-clean-after-rm "$(ls $ROOTDIR)"; else ok root-clean-after-rm; fi
    is no-runsc-left "$(runsc_pids | wc -l)" 0
    finish ;;
a1neg)
    echo "## D-A1 negative: plain stop with the root moved aside / replaced must fail visibly, sandbox stays"
    pm3s run -d --name t3sf-b "${RUN_FLAGS[@]}" "$IMG" sleep 300; is run-rc $? 0
    sleep 2; spid=$(as_admin podman inspect --format '{{.State.Pid}}' t3sf-b)
    since=$(date +%s); sleep 1
    mv $ROOTDIR $ROOTDIR.aside
    out=$(as_admin podman stop -t 2 t3sf-b 2>&1); rc=$?
    echo "$out"
    if [ "$rc" -ne 0 ]; then ok missing-root-stop-fails "rc=$rc"; else bad missing-root-stop-fails rc=0; fi
    # podman hides the runtime's stderr; the wrapper's refusal is in the journal
    if journalctl -t tier3s-runsc --since "@$since" -o cat --no-pager | grep -F "state root $ROOTDIR is missing"; then
        ok missing-root-journal; else bad missing-root-journal; fi
    if alive "$spid"; then ok sentry-alive-after-failed-stop; else bad sentry-alive-after-failed-stop; fi
    is no-root-minted "$(ls -d $ROOTDIR 2>/dev/null | wc -l)" 0
    install -d -o 1000 -g 1000 -m 0700 $ROOTDIR
    out=$(as_admin podman stop -t 2 t3sf-b 2>&1); rc=$?
    echo "$out"
    if [ "$rc" -ne 0 ]; then ok replaced-root-stop-fails "rc=$rc"; else bad replaced-root-stop-fails rc=0; fi
    if alive "$spid"; then ok sentry-alive-after-replaced-stop; else bad sentry-alive-after-replaced-stop; fi
    rmdir $ROOTDIR && mv $ROOTDIR.aside $ROOTDIR
    as_admin podman ps -a --filter name=t3sf-b --format '{{.Names}} {{.State}}'
    as_admin podman stop -t 5 t3sf-b; is restored-stop-rc $? 0
    as_admin podman rm -f t3sf-b; is restored-rm-rc $? 0
    sleep 1; if alive "$spid"; then bad sentry-gone-after-restore; else ok sentry-gone-after-restore; fi
    is no-runsc-left "$(runsc_pids | wc -l)" 0
    finish ;;
a3b|a3bkill)
    TOK=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
    SC=qdistro-tier3s-$TOK.scope
    SVC=t3sf-launch-$1.service
    D=/var/tmp/t3sf; install -d -o root -g root -m 0755 $D
    cat > $D/launch.sh <<L
#!/bin/bash
systemd-run --scope --unit=$SC --collect -p Delegate=yes -p TasksMax=1024 -p MemoryMax=2G \
  -p BindsTo=$SVC -p Before=$SVC -- /usr/libexec/qdistro/qdistro-tier3s-scope enter $TOK 1000 -- \
  podman --runtime $W --runtime-flag=network=none --cgroup-manager=cgroupfs run --rm --name t3sf-c \
  ${RUN_FLAGS[*]} $IMG sleep 300 &
wait \$!
L
    cat > $D/post.sh <<P
#!/bin/bash
{ echo "ExecStopPost: SERVICE_RESULT=\$SERVICE_RESULT EXIT_CODE=\$EXIT_CODE scope=\$(systemctl is-active $SC)"
  echo "ExecStopPost: scope procs=\$(find /sys/fs/cgroup/system.slice/$SC -name cgroup.procs -exec cat {} + 2>/dev/null | wc -l)"; } >> $D/post-$1.log
P
    chmod 0755 $D/launch.sh $D/post.sh; rm -f $D/post-$1.log
    systemd-run --unit=$SVC -p ExecStopPost=$D/post.sh $D/launch.sh; is svc-start $? 0
    for _ in $(seq 60); do [ "$(as_admin podman inspect --format '{{.State.Status}}' t3sf-c 2>/dev/null)" = running ] && break; sleep 0.5; done
    sleep 1
    systemctl show -p ControlGroup -p BindsTo -p Before -p Delegate -p TasksMax -p MemoryMax $SC
    rel=$(systemctl show -p ControlGroup --value $SC); CG=/sys/fs/cgroup$rel
    is scope-active "$(systemctl is-active $SC)" active
    if [ -z "$rel" ]; then journalctl -u $SVC --no-pager | tail; bad scope-cgroup-known; finish; fi
    case "$(systemctl show -p BindsTo --value $SC)" in *$SVC*) ok scope-bindsto ;; *) bad scope-bindsto ;; esac
    case "$(systemctl show -p Before --value $SC)" in *$SVC*) ok scope-before ;; *) bad scope-before ;; esac
    stat -c '%n %U %a' $CG $CG/cgroup.procs $CG/cgroup.subtree_control $CG/cgroup.threads $CG/memory.max $CG/pids.max
    is delegated-dir "$(stat -c %u $CG)" 1000
    is limit-memory-root "$(stat -c %u $CG/memory.max)" 0
    is limit-pids-root "$(stat -c %u $CG/pids.max)" 0
    is memory-max "$(cat $CG/memory.max)" 2147483648
    if as_admin sh -c "echo max > $CG/memory.max" 2>/dev/null; then bad admin-cannot-raise-memory; else ok admin-cannot-raise-memory; fi
    if as_admin sh -c "echo max > $CG/pids.max" 2>/dev/null; then bad admin-cannot-raise-pids; else ok admin-cannot-raise-pids; fi
    spid=$(as_admin podman inspect --format '{{.State.Pid}}' t3sf-c)
    cpid=$(as_admin podman inspect --format '{{.State.ConmonPid}}' t3sf-c)
    in=$(tree_procs "$CG"); echo "scope subtree procs:"
    for p in $in; do printf '  %s %s %s %s\n' "$(grep -l "^$p$" $(find $CG -name cgroup.procs) | sed "s|$CG||")" "$p" "$(cat /proc/$p/comm)" "$(readlink /proc/$p/exe)"; done
    printf '%s\n' $in | grep -qx "$spid" && ok sentry-in-scope "$spid" || bad sentry-in-scope "$spid"
    printf '%s\n' $in | grep -qx "$cpid" && ok conmon-in-scope "$cpid" || bad conmon-in-scope "$cpid"
    out=0; nr=0
    for p in $(runsc_pids); do nr=$((nr+1)); printf '%s\n' $in | grep -qx "$p" || { out=$((out+1)); echo "OUTSIDE: $p $(cat /proc/$p/comm) $(cat /proc/$p/cgroup)"; }; done
    echo "runsc-bundle processes on the host: $nr"
    is runsc-procs-outside-scope "$out" 0
    for c in runsc-gofer runsc-sandbox runsc-fd-parking; do
        found=0; for p in $in; do [ "$(tr '\0' ' ' < /proc/$p/cmdline | cut -d' ' -f1)" = "$c" ] && found=1; done
        is "class-$c-in-scope" $found 1
    done
    stubs=0; for p in $in; do [ "$(readlink /proc/$p/exe)" = /usr/libexec/qdistro/runsc/gvisor-bin/gvisor_sentry ] && [ -z "$(tr -d '\0' < /proc/$p/cmdline)" ] && stubs=$((stubs+1)); done
    echo "systrap stubs in scope: $stubs"; [ "$stubs" -ge 1 ] && ok stubs-in-scope "$stubs" || bad stubs-in-scope 0
    for c in podman runuser conmon; do
        found=0; for p in $in; do [ "$(cat /proc/$p/comm)" = "$c" ] && found=1; done
        is "launcher-$c-in-scope" $found 1
    done
    if [ "$1" = a3b ]; then
        systemctl stop $SVC; is svc-stop $? 0
    else
        mp=$(systemctl show -p MainPID --value $SVC); echo "SIGKILL main pid $mp"; kill -9 "$mp"
    fi
    for _ in $(seq 60); do [ "$(systemctl is-active $SC)" != active ] && [ "$(systemctl is-active $SVC)" != active ] && [ "$(systemctl is-active $SVC)" != deactivating ] && break; sleep 0.5; done
    sleep 1
    cat $D/post-$1.log
    grep -q "scope=active" $D/post-$1.log && ok scope-alive-during-execstoppost || bad scope-alive-during-execstoppost
    is scope-gone-after-service "$(systemctl is-active $SC)" inactive
    is cgroup-gone "$(ls -d $CG 2>/dev/null | wc -l)" 0
    if alive "$spid"; then bad sentry-gone; else ok sentry-gone; fi
    is no-runsc-left "$(runsc_pids | wc -l)" 0
    echo "podman record afterwards (no cleanup ran):"; as_admin podman ps -a --filter name=t3sf-c --format '{{.Names}} {{.State}}'
    as_admin podman rm -f --ignore t3sf-c; is podman-rm-f-rc $? 0
    is podman-record-gone "$(as_admin podman ps -a -q --filter name=t3sf-c | wc -l)" 0
    ls -la $ROOTDIR
    systemctl reset-failed $SVC 2>/dev/null; true
    finish ;;
*) echo "usage: $0 setup|a1|a1neg|a3b|a3bkill"; exit 2 ;;
esac

#!/bin/bash
# In-VM driver (root) for tiered-isolation.bats:phase7-tier5-lifecycle-stress.
#
# Tier-5 concurrency and crash stress against REAL nested guests (decision
# record todo/review/tier5-decisions step 10: "genuine tier-5
# concurrency/crash stress ... Required scenarios must actually execute
# without skips"). The admin-run lifecycle (installed qdistro-tier5-spawn /
# qdistro-tier5-cleanup) is driven exactly as qdshell's VMApps drives it:
# `--vm <name> --policy-key <appId> -- <app>`.
#
#   A. simultaneous cold starts — two wrappers for the same app and guest
#      start at the same instant: exactly ONE creates the guest, the other
#      ADOPTS it; one domain, one qemu, one overlay; both publish their app.
#   C. different-app refusal — a third wrapper naming the same guest under
#      another appId is refused (exit 3) and leaves the running guest, its
#      qemu and overlay untouched.
#   B. shared-guest reuse — closing ONE of the two sessions leaves the guest
#      running for the other; closing the last one reaps domain, qemu and
#      overlay.
#   D. wrapper crash — SIGKILL of a wrapper: the root bridge sees EOF and
#      its waypipe client is gone; the orphaned guest is then reaped by the
#      admin cleanup tool (domain, qemu, overlay, sidecar).
#   E. qemu crash — SIGKILL of the guest's qemu: the wrapper notices, exits
#      and reaps the definition, overlay and its bridge chain.
#
# Every scenario asserts resource POSTCONDITIONS (process / domain / file
# absence), not "it didn't crash". There is no SKIP path: a missing
# precondition (base disk, kvm, compositor, installed tools) is a FAIL.
#
# Authentication is not this driver's subject (tier5-bridge-enforcing.bats
# owns it): the bridge's polkit action is granted to admin by the same
# temporary YES rule s45/s48/s49 use, removed on exit.
#
# Output: PASS/FAIL lines, `[s111] N passes, M failures`, exit 1 on failure.

# W_<tag>/R_<tag> are assigned by spawn_bg through printf -v.
# shellcheck disable=SC2154
set -u

PASSCOUNT=0
FAILCOUNT=0
pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
info() { echo "  info: $*"; }

ADMIN_UID=1000
RT=/run/user/$ADMIN_UID
SPAWN=/usr/local/bin/qdistro-tier5-spawn
CLEANUP=/usr/local/bin/qdistro-tier5-cleanup
BRIDGE=/usr/libexec/qdistro/qdistro-tier5-bridge
BASE=/var/lib/libvirt/images/qdistro-tier5-base.qcow2
IMG=/home/admin/.local/share/libvirt/images
LOGDIR=/run/qci-s111
T5_POLKIT_RULE=/etc/polkit-1/rules.d/10-qdistro-tier5-bridge-test.rules
TAG=s111-$$
VM1="$TAG-shared"     # scenarios A, C, B
VM2="$TAG-wcrash"     # scenario D
VM3="$TAG-qcrash"     # scenario E
KEY=s111/term         # the appId the guests are bound to
OTHER_KEY=s111/other  # a different app naming the same guest

as_admin() {
    runuser -u admin -- env XDG_RUNTIME_DIR=$RT HOME=/home/admin \
        PATH=/usr/local/bin:/usr/bin:/bin LIBVIRT_DEFAULT_URI=qemu:///session "$@"
}
vsh() { as_admin virsh "$@"; }
# wrapper_pid_under <pid> — spawn-tier5.sh's own pid below <pid>. A
# backgrounded `t5_as_admin`/`as_admin` call is a subshell running root
# runuser, whose child becomes admin and execs (env →) spawn-tier5.sh: the
# first admin-uid descendant IS the wrapper. Signals for the wrapper go to
# it, never to runuser: runuser forwards SIGTERM but SIGKILLs its child 2 s
# later ("Session terminated, killing shell"), and a SIGKILLed runuser
# forwards nothing.
wrapper_pid_under() {
    local p c _
    for _ in $(seq 1 100); do
        p=$1
        while c=$(pgrep -P "$p" 2>/dev/null | head -1) && [ -n "$c" ]; do
            [ "$(stat -c %u "/proc/$c" 2>/dev/null)" = 1000 ] && { echo "$c"; return 0; }
            p=$c
        done
        sleep 0.1
    done
    return 1
}

# --- precondition: every one is a FAIL, never a skip ------------------------
pre_ok=1
need() { if ! eval "$1"; then fail "precondition: $2"; pre_ok=0; fi; }
need '[ -f "$BASE" ]' "tier-5 base disk $BASE present (baked by build-baked-baseweed.sh)"
need '[ -e /dev/kvm ]' "/dev/kvm present (nested KVM)"
need 'command -v virsh >/dev/null' "virsh installed"
need '[ -x "$SPAWN" ] && [ -x "$CLEANUP" ]' "installed qdistro-tier5-spawn / qdistro-tier5-cleanup"
need '[ -x "$BRIDGE" ]' "installed tier-5 bridge"
need 'runuser -u admin -- test -S $RT/wayland-1' "admin compositor socket $RT/wayland-1"
need 'systemctl is-active -q qdistro-admin-broker.service' "admin broker active (the bridge's RegisterLaunch is mandatory)"
if [ "$pre_ok" != 1 ]; then
    echo "[s111] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi
pass "tier-5 stress preconditions present (base disk, kvm, installed tools, compositor)"

install -d -m 0755 /etc/polkit-1/rules.d
cat >"$T5_POLKIT_RULE" <<'RULE'
polkit.addRule(function(action, subject) {
    if (action.id == "org.qdistro.tier5.bridge" && subject.user == "admin") {
        return polkit.Result.YES;
    }
});
RULE
sleep 1  # polkitd reloads rules.d on change
install -d -m 0755 "$LOGDIR"
chown admin "$LOGDIR"

# --- inventory ----------------------------------------------------------------
qemu_pids() {   # qemu_pids <vm> — real qemu processes backing guest=<vm>
    local p c
    for p in $(pgrep -u admin -f "guest=$1," 2>/dev/null); do
        c=$(cat "/proc/$p/comm" 2>/dev/null) || continue
        case "$c" in qemu-system-*|qemu-kvm) echo "$p" ;; esac
    done
}
nqemu() { qemu_pids "$1" | grep -c . ; }
defined() { vsh list --all --name 2>/dev/null | grep -Fxq -- "$1"; }
overlay() { echo "$IMG/$1.qcow2"; }
# chain_pids — every live bridge-chain process (root bridge, its runuser,
# secctx-exec, waypipe — the client AND its per-connection children, whose
# argv does not say "client"), as "pid:starttime".
chain_pids() {
    local p exe cmd
    for p in /proc/[0-9]*; do
        exe=$(readlink "$p/exe" 2>/dev/null) || continue
        cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null) || continue
        case "$exe|$cmd" in
            *"|"*"$BRIDGE"*|/usr/bin/qdistro-secctx-exec"|"*|*/runuser"|"*qdistro-secctx-exec*) ;;
            /usr/bin/waypipe"|"*) ;;
            *) continue ;;
        esac
        echo "${p#/proc/}:$(awk '{print $22}' "$p/stat" 2>/dev/null)"
    done | sort
}
CHAIN_BASELINE=$(chain_pids)
new_chain() { comm -13 <(printf '%s\n' "$CHAIN_BASELINE") <(chain_pids) | grep . ; }
wait_for() {   # wait_for <secs> <cmd...>
    local end=$((SECONDS + $1)); shift
    while [ "$SECONDS" -lt "$end" ]; do "$@" && return 0; sleep 1; done
    "$@"
}

# spawn_bg <tag> <vm> <key> — a wrapper as admin, in the background. Sets
# W_<tag> (wrapper bash pid) and logs to $LOGDIR/<tag>.log.
spawn_bg() {
    local tag=$1 vm=$2 key=$3 rp w=""
    as_admin env TIER5_MEM_KIB=2097152 TIER5_QGA_TIMEOUT_SECS=300 \
        TIER5_IDLE_SHUTDOWN_SECS=0 TIER5_SHUTDOWN_METHOD=force \
        TIER5_DECISION_WAIT_SECS=300 \
        "$SPAWN" --vm "$vm" --policy-key "$key" -- weston-terminal \
        >"$LOGDIR/$tag.log" 2>&1 </dev/null &
    rp=$!
    w=$(wrapper_pid_under "$rp")
    printf -v "W_$tag" '%s' "${w:-0}"
    printf -v "R_$tag" '%s' "$rp"
}
wlog() { cat "$LOGDIR/$1.log" 2>/dev/null; }
published() { grep -q 'guest publisher pid=' "$LOGDIR/$1.log" 2>/dev/null; }
bridge_wp() { sed -n 's/.*bridge ready: waypipe client pid=\([0-9]*\).*/\1/p' "$LOGDIR/$1.log" | head -1; }
gone() { ! kill -0 "$1" 2>/dev/null; }
st_of() { local s; s=$(cat "/proc/$1/stat" 2>/dev/null) || return 1; s=${s##*) }; set -- $s; echo "${20}"; }
# session_tree <client pid> — "pid:starttime" of every process in the
# client's launch-chain process group (the bridge makes runuser its
# leader): runuser, secctx-exec, the waypipe client and the child waypipe
# forks per guest connection.
session_tree() {
    local pg p
    pg=$(ps -o pgid= -p "$1" 2>/dev/null | tr -d ' ')
    [ -n "$pg" ] || return 0
    for p in $(pgrep -g "$pg"); do echo "$p:$(st_of "$p")"; done
}
tree_gone() {   # tree_gone "<pid:start>..." — none of those processes lives
    local e
    for e in $1; do [ "$(st_of "${e%%:*}")" = "${e#*:}" ] && return 1; done
    return 0
}
# The create-lock barrier: the driver (root) holds hold-dir/create-<vm>
# exclusively, so every wrapper deciding to create that guest blocks on it.
HOLDD=$RT/qdistro-tier5
create_lock_waiters() {   # blocked flock waiters on create-<vm> (/proc/locks "->")
    local ino; ino=$(stat -c %i "$HOLDD/create-$1" 2>/dev/null) || { echo 0; return; }
    grep -cE -- "-> FLOCK .*:$ino " /proc/locks
}
create_lock_held() {   # a granted (non-waiter) flock on create-<vm>
    local ino; ino=$(stat -c %i "$HOLDD/create-$1" 2>/dev/null) || return 1
    grep -qE "^[0-9]+: FLOCK .*:$ino " /proc/locks
}
create_lock_contended() { [ "$(create_lock_waiters "$1")" -ge 2 ]; }

reap_everything() {
    local vm p
    for p in $(pgrep -u admin -f "spawn-tier5.sh --vm $TAG-" 2>/dev/null); do kill -KILL "$p" 2>/dev/null; done
    for vm in "$VM1" "$VM2" "$VM3"; do
        vsh destroy "$vm" >/dev/null 2>&1
        vsh undefine "$vm" >/dev/null 2>&1
        rm -f "$(overlay "$vm")" "$(overlay "$vm").appid"
    done
}
cleanup() {
    reap_everything
    rm -f "$T5_POLKIT_RULE"
}
trap cleanup EXIT INT TERM

# =============================================================================
# A. simultaneous cold starts of one app: one creator, one adopter
# =============================================================================
# Barrier: hold the create lock first, start both wrappers, and release it
# only once BOTH are blocked on it — they then contend for the create
# decision itself, not merely start close together. The lock file is
# created by admin (no root-owned file in admin's lock dir); flock -o keeps
# the lock with the flock process alone.
as_admin sh -c "mkdir -p -m 0700 '$HOLDD' && umask 077 && : >> '$HOLDD/create-$VM1'"
flock -o -x "$HOLDD/create-$VM1" sleep 900 &
BARRIER=$!
wait_for 10 create_lock_held "$VM1" || fail "A: barrier lock on $HOLDD/create-$VM1 not taken"
spawn_bg a1 "$VM1" "$KEY"
spawn_bg a2 "$VM1" "$KEY"
if wait_for 120 create_lock_contended "$VM1"; then
    pass "A: barrier — both wrappers blocked on the $VM1 create lock at once ($(create_lock_waiters "$VM1") waiters)"
else
    fail "A: barrier — wrappers never both waited on the create lock (waiters=$(create_lock_waiters "$VM1")): a1=[$(wlog a1 | tail -3 | tr '\n' ';')] a2=[$(wlog a2 | tail -3 | tr '\n' ';')]"
fi
kill "$BARRIER" 2>/dev/null; pkill -P "$BARRIER" -x sleep 2>/dev/null; wait "$BARRIER" 2>/dev/null
if wait_for 480 sh -c "grep -q 'guest publisher pid=' $LOGDIR/a1.log && grep -q 'guest publisher pid=' $LOGDIR/a2.log"; then
    pass "A: both simultaneous launches published their app"
else
    fail "A: not both published: a1=[$(wlog a1 | tail -5 | tr '\n' ';')] a2=[$(wlog a2 | tail -5 | tr '\n' ';')]"
fi
created=$( { wlog a1; wlog a2; } | grep -c "\[tier5\] domain $VM1 running (cid=")
adopted=$( { wlog a1; wlog a2; } | grep -c "\[tier5\] adopted running domain $VM1 ")
[ "$created" = 1 ] && [ "$adopted" = 1 ] \
    && pass "A: exactly one wrapper created $VM1 and the other adopted it (created=1 adopted=1)" \
    || fail "A: created=$created adopted=$adopted"
q=$(nqemu "$VM1")
[ "$q" = 1 ] && defined "$VM1" && [ -f "$(overlay "$VM1")" ] \
    && pass "A: one domain, one qemu, one overlay for $VM1" \
    || fail "A: qemu=$q defined=$(defined "$VM1" && echo y || echo n) overlay=$([ -f "$(overlay "$VM1")" ] && echo y || echo n)"
read -r _sapp _ < "$(overlay "$VM1").appid" 2>/dev/null || _sapp=""
[ "$_sapp" = "$KEY" ] && pass "A: overlay sidecar binds $VM1 to $KEY" || fail "A: sidecar names '$_sapp'"
WPA1=$(bridge_wp a1); WPA2=$(bridge_wp a2)
[ -n "$WPA1" ] && [ -n "$WPA2" ] && [ "$WPA1" != "$WPA2" ] && kill -0 "$WPA1" 2>/dev/null && kill -0 "$WPA2" 2>/dev/null \
    && pass "A: each session runs its own registered waypipe client ($WPA1, $WPA2)" \
    || fail "A: waypipe clients a1='$WPA1' a2='$WPA2'"
QPA=$(qemu_pids "$VM1" | head -1)

# =============================================================================
# C. a different app naming the same guest is refused; the guest is untouched
# =============================================================================
spawn_bg c1 "$VM1" "$OTHER_KEY"
wait_for 120 gone "$R_c1"
wait "$R_c1" 2>/dev/null; crc=$?
if [ "$crc" = 3 ] && wlog c1 | grep -q 'refusing cross-app adoption'; then
    pass "C: different-app launch refused (exit 3, 'refusing cross-app adoption')"
else
    fail "C: different-app launch rc=$crc log=[$(wlog c1 | tail -6 | tr '\n' ';')]"
fi
[ "$(qemu_pids "$VM1" | head -1)" = "$QPA" ] && defined "$VM1" && [ -f "$(overlay "$VM1")" ] \
    && pass "C: the running guest (qemu $QPA), its definition and overlay are untouched" \
    || fail "C: guest changed after the refused launch (qemu now '$(qemu_pids "$VM1" | tr '\n' ' ')')"
read -r _sapp _ < "$(overlay "$VM1").appid" 2>/dev/null || _sapp=""
[ "$_sapp" = "$KEY" ] && pass "C: sidecar still binds $VM1 to $KEY" || fail "C: sidecar now names '$_sapp'"
WPC=$(bridge_wp c1)
if [ -z "$WPC" ] || wait_for 15 gone "$WPC"; then
    pass "C: the refused launch left no waypipe client (${WPC:-none started})"
else
    fail "C: refused launch's waypipe client $WPC survives"
fi

# =============================================================================
# B. shared-guest reuse: closing one session keeps the guest for the other
# =============================================================================
TREE_A1=$(session_tree "$WPA1"); NC_A1=$(pgrep -P "$WPA1" | grep -c .)
[ "$NC_A1" -ge 1 ] && pass "B: the first session's waypipe client $WPA1 serves $NC_A1 guest connection child(ren)" \
    || fail "B: waypipe client $WPA1 has no connection child — the child-reaping check below would be vacuous"
kill -TERM "$W_a1" 2>/dev/null
wait_for 60 gone "$W_a1" && pass "B: first session closed (wrapper $W_a1 exited)" || fail "B: wrapper $W_a1 still running"
wlog a1 | grep -q "adopted by another session; leaving it running" \
    && pass "B: closing wrapper handed the guest to the remaining session" \
    || fail "B: no hand-over line: $(wlog a1 | tail -4 | tr '\n' ';')"
[ "$(qemu_pids "$VM1" | head -1)" = "$QPA" ] && defined "$VM1" && [ -f "$(overlay "$VM1")" ] \
    && pass "B: guest still running for the other session (qemu $QPA)" \
    || fail "B: guest lost while a session still used it"
if wait_for 15 tree_gone "$TREE_A1" && [ "$(qemu_pids "$VM1" | head -1)" = "$QPA" ]; then
    pass "B: every process of the closed session's chain is gone ($(wc -w <<<"$TREE_A1") incl. $NC_A1 waypipe connection child(ren)) while the shared guest (qemu $QPA) stays up"
else
    fail "B: closed session's chain survivors: $(for e in $TREE_A1; do [ "$(st_of "${e%%:*}")" = "${e#*:}" ] && echo "${e%%:*}"; done | tr '\n' ' ') guest qemu=$(qemu_pids "$VM1" | tr '\n' ' ')"
fi
wait_for 15 gone "$WPA1" && kill -0 "$WPA2" 2>/dev/null \
    && pass "B: the closed session's waypipe client is gone, the other's lives" \
    || fail "B: waypipe a1=$WPA1 alive=$(kill -0 "$WPA1" 2>/dev/null && echo y || echo n) a2=$WPA2 alive=$(kill -0 "$WPA2" 2>/dev/null && echo y || echo n)"
kill -TERM "$W_a2" 2>/dev/null
wait_for 90 gone "$W_a2" && pass "B: last session closed (wrapper $W_a2 exited)" || fail "B: wrapper $W_a2 still running"
reaped() {   # reaped <vm> — no definition, qemu, overlay or sidecar
    ! defined "$1" && [ "$(nqemu "$1")" = 0 ] \
        && ! [ -e "$(overlay "$1")" ] && ! [ -e "$(overlay "$1").appid" ]
}
if wait_for 30 reaped "$VM1"; then
    pass "B: last close reaped $VM1 (no domain, no qemu, no overlay, no sidecar)"
else
    fail "B: after the last close: defined=$(defined "$VM1" && echo y || echo n) qemu=$(nqemu "$VM1") overlay=$(ls "$(overlay "$VM1")"* 2>/dev/null | tr '\n' ' ')"
fi
wait_for 15 sh -c "! kill -0 $WPA2 2>/dev/null" && pass "B: no waypipe client survives the last close" \
    || fail "B: waypipe $WPA2 survives"

# =============================================================================
# D. wrapper crash: SIGKILL; the bridge chain goes, the cleanup tool reaps
# =============================================================================
spawn_bg d1 "$VM2" "$KEY"
if wait_for 480 published d1; then
    pass "D: launch up ($VM2)"
    WPD=$(bridge_wp d1)
    BRD=$(pgrep -f "$BRIDGE --vm $VM2 " | head -1)
    TREE_D=$(session_tree "$WPD"); NC_D=$(pgrep -P "$WPD" | grep -c .)
    [ "$NC_D" -ge 1 ] && pass "D: waypipe client $WPD serves $NC_D guest connection child(ren)" \
        || fail "D: waypipe client $WPD has no connection child — the child-reaping check below would be vacuous"
    kill -KILL "$W_d1" 2>/dev/null
    wait_for 20 gone "$WPD" && pass "D: wrapper SIGKILL — the bridge reaped its waypipe client $WPD" \
        || fail "D: waypipe client $WPD survived the wrapper's SIGKILL"
    if [ -n "$BRD" ] && wait_for 20 gone "$BRD"; then
        pass "D: the root bridge ($BRD) exited after its caller died"
    else
        fail "D: root bridge '${BRD:-not found}' still running"
    fi
    if wait_for 20 tree_gone "$TREE_D" && [ "$(nqemu "$VM2")" = 1 ]; then
        pass "D: every process of the crashed session's chain is gone ($(wc -w <<<"$TREE_D") incl. $NC_D waypipe connection child(ren)) while its orphaned guest stays up"
    else
        fail "D: chain survivors: $(for e in $TREE_D; do [ "$(st_of "${e%%:*}")" = "${e#*:}" ] && echo "${e%%:*}"; done | tr '\n' ' ') guest qemu count=$(nqemu "$VM2")"
    fi
    out=$(as_admin "$CLEANUP" "$VM2" 2>&1); rc=$?
    info "cleanup: $(tr '\n' ' ' <<<"$out")"
    if [ "$rc" = 0 ] && wait_for 30 reaped "$VM2"; then
        pass "D: qdistro-tier5-cleanup reaped the orphan (no domain, qemu, overlay, sidecar)"
    else
        fail "D: cleanup rc=$rc defined=$(defined "$VM2" && echo y || echo n) qemu=$(nqemu "$VM2") files=$(ls "$(overlay "$VM2")"* 2>/dev/null | tr '\n' ' ')"
    fi
else
    fail "D: launch never published: $(wlog d1 | tail -6 | tr '\n' ';')"
fi

# =============================================================================
# E. qemu crash: the wrapper notices, exits and reaps
# =============================================================================
spawn_bg e1 "$VM3" "$KEY"
if wait_for 480 published e1; then
    pass "E: launch up ($VM3)"
    WPE=$(bridge_wp e1)
    for p in $(qemu_pids "$VM3"); do kill -KILL "$p"; done
    wait_for 90 gone "$W_e1" && pass "E: qemu SIGKILL — the wrapper noticed and exited" \
        || fail "E: wrapper $W_e1 still running after the guest died"
    if wait_for 30 reaped "$VM3"; then
        pass "E: dead guest reaped (no domain, qemu, overlay, sidecar)"
    else
        fail "E: after the qemu crash: defined=$(defined "$VM3" && echo y || echo n) qemu=$(nqemu "$VM3") files=$(ls "$(overlay "$VM3")"* 2>/dev/null | tr '\n' ' ') log=[$(wlog e1 | tail -5 | tr '\n' ';')]"
    fi
    wait_for 15 gone "$WPE" && pass "E: its waypipe client $WPE is gone" || fail "E: waypipe $WPE survives"
else
    fail "E: launch never published: $(wlog e1 | tail -6 | tr '\n' ';')"
fi

# =============================================================================
# postconditions for the whole driver
# =============================================================================
no_new_chain() { [ -z "$(new_chain)" ]; }
wait_for 20 no_new_chain
left=$(new_chain)
if [ -z "$left" ]; then
    pass "no bridge, runuser, secctx-exec or waypipe client from this driver survives"
else
    fail "surviving bridge-chain processes: $(tr '\n' ' ' <<<"$left")"
fi
leftdom=$(vsh list --all --name 2>/dev/null | grep -F "$TAG-" | tr '\n' ' ')
leftq=$(pgrep -u admin -af "guest=$TAG-" 2>/dev/null | tr '\n' ' ')
leftf=$(ls "$IMG"/"$TAG"-* 2>/dev/null | tr '\n' ' ')
[ -z "$leftdom$leftq$leftf" ] && pass "no domain, qemu or overlay named $TAG-* remains" \
    || fail "leftovers: domains=[$leftdom] qemu=[$leftq] files=[$leftf]"
rootf=$(find "$IMG" "$RT/qdistro-tier5" -xdev \( -user root -o -group root \) -print 2>/dev/null | head -5)
[ -z "$rootf" ] && pass "no root-owned file in admin's image or lock directories" \
    || fail "root-owned lifecycle files: $rootf"
leg=""
for d in /run/qdistro-tier5 /run/qdistro-tier5-boot /var/lib/qdistro/tier5-owned; do [ -e "$d" ] && leg="$leg $d"; done
[ -z "$leg" ] && pass "no root-run tier-5 lifecycle state" || fail "legacy root tier-5 state:$leg"

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "§5 tier-5 concurrency/crash stress end-to-end (no leaked resources)"
    echo "[s111] $PASSCOUNT passes, 0 failures"
    exit 0
else
    echo "[s111] $PASSCOUNT passes, $FAILCOUNT failures"
    exit 1
fi

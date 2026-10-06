#!/bin/bash
# tier3s/spike/model-a-verify.sh — GUEST driver (root): end-to-end
# verification of the Phase C2 stage-2 model-A launch path on a baseweed VM
# that already has runsc provisioned (tier3s/provision-runsc.sh) and admin
# linger enabled.
#
# What the qci s12x lanes verify on a fully baked worker, run here against a
# minimally-installed stack (broker + resolver + session manager + tier3s
# block from install-session-manager.sh QDISTRO_TIER3S=1):
#
#   0  stack install from the staged tested commit (broker, templates,
#      session manager, tier3s), dev profile, daemons up;
#   1  silo row + allow rule; the qt3s-<silo> account does NOT exist yet;
#   2  first launch creates the account (useradd + auto subuid/subgid),
#      creates /run/qdistro-tier3s-{rt,runsc}/<silo uid>, then refuses on the
#      absent silo-store image; the published record is torn down;
#   3  per-silo image delivery: admin-built OCI archive `podman load`ed into
#      the qt3s store — invisible to admin's store;
#   4  StartSilo: record carries silo_uid/silo_user, conmon runs as the silo
#      uid, every runtime process sits in the owning scope, the container is
#      visible only from the silo's podman store, the smoke reaches --hold;
#   5  StopSilo: unit down, record gone, scope gone, container gone.
#
# One PASS/FAIL line per check; `[mA] N passes, M failures`; exit 1 on any.
set -u
SRC=${1:-/root/qdistro-src}
T3S_TAG=mA
. "$SRC/tests/integration/vm/tier3s-guest-lib.sh"
SILO=c2m
ACCT="qt3s-$SILO"

pm_s() {   # pm_s <podman args...> — podman as the silo account (model A store)
    local uid home
    uid=$(id -u "$ACCT" 2>/dev/null) || return 1
    home=$(getent passwd "$ACCT" | cut -d: -f6)
    runuser -u "$ACCT" -- env -i PATH=/usr/bin:/bin HOME="$home" \
        USER="$ACCT" LOGNAME="$ACCT" \
        XDG_RUNTIME_DIR="/run/qdistro-tier3s-rt/$uid" \
        CONTAINERS_CONF=/usr/lib/qdistro/tier3s/containers.conf podman "$@"
}
silo_uid() { id -u "$ACCT" 2>/dev/null; }

# Model-A variant of assert_all_clear: the container inventory lives in the
# SILO's podman store and the runsc state root is /run/.../<silo uid>, so the
# lib's admin-scoped checks would either QUERY-FAIL or look in the wrong
# store. Call only when the account exists (the empty-query oracles read a
# missing dir as nothing-found, matching assert_all_clear's pre-account use).
assert_ma_clear() {   # assert_ma_clear <tag>
    is "$1: control records" "$(records | wc -l)" 0
    is "$1: scopes" "$(qry systemctl list-units --all --plain --no-legend 'qdistro-tier3s-*.scope' | grep -c .)" 0
    is "$1: labelled containers (silo store)" \
        "$(qry pm_s ps -a --filter label=qdistro_tier3s_token --format '{{.Names}}' | grep -c .)" 0
    is "$1: runsc-bundle processes" "$(runsc_pids | wc -l)" 0
    is "$1: runsc state root holds no container state" \
        "$(qry find "/run/qdistro-tier3s-runsc/$(silo_uid)" -mindepth 1 ! -name null-netns | grep -c .)" 0
    is "$1: no cleanup call scope left" \
        "$(qry systemctl list-units --all --plain --no-legend 'qdistro-t3s-call-*.scope' | grep -c .)" 0
    "$CLEANUP" --reap-stale >/dev/null 2>&1 || :
}

step "0. install the tested stack"
info "kernel $(uname -r); $(podman --version); selinux $(getenforce 2>/dev/null)"
for d in broker templates snapshots session_manager tier3s agents scripts/install; do
    [ -e "$SRC/$d" ] || { fail "staged tree missing $d"; finish; }
done
out=$(bash "$SRC/scripts/install/install-broker-for-qdwin.sh" "$SRC/broker" 2>&1); rc=$?
is "install-broker rc" "$rc" 0; printf '%s\n' "$out" | tail -3 | sed 's/^/    /'
out=$(bash "$SRC/scripts/install/install-templates-for-vm.sh" "$SRC" 2>&1); rc=$?
is "install-templates rc" "$rc" 0; printf '%s\n' "$out" | tail -3 | sed 's/^/    /'
out=$(QDISTRO_TIER3S=1 bash "$SRC/scripts/install/install-session-manager.sh" "$SRC/session_manager" 2>&1); rc=$?
is "install-session-manager (QDISTRO_TIER3S=1) rc" "$rc" 0
printf '%s\n' "$out" | tail -4 | sed 's/^/    /'
# getent prints the group line on success — quiet it so yes_no reads the rc
is "qdistro-tier3s group installed" \
    "$(getent group qdistro-tier3s >/dev/null 2>&1 && echo yes || echo no)" yes
echo 'QDISTRO_PROFILE=dev' > /etc/qdistro/profile
systemctl daemon-reload
systemctl enable --now qdistro-admin-broker.service 2>&1 | tail -1 | sed 's/^/    /'
wait_for 30 bash -c 'busctl --system list --no-pager | grep -q "^org\.qdistro\.AdminBroker1 "' \
    && pass "broker owns its bus name" || fail "broker never owned org.qdistro.AdminBroker1"
systemctl restart qdistro-session-manager.service
wait_for 30 manager_up && pass "session manager owns its bus name" || fail "session manager never came up"
wait_for 45 bash -c 'busctl introspect org.qdistro.SessionManager1 /org/qdistro/SessionManager1 2>/dev/null | grep -q "^\.CreateTier3sSilo "' \
    && pass "manager serves CreateTier3sSilo" || fail "manager does not serve CreateTier3sSilo"
is "resolver on PATH" "$(command -v qdistro-resolve-binding | tr -d ' ' | grep -c .)" 1

# idempotent across reruns: drop a prior run's silo row, account, runtime
# dirs and state so the lazy-provisioning evidence is honest
step "0b. reset prior verify state"
systemctl stop qdistro-session-manager.service 2>/dev/null || true
if getent passwd "$ACCT" >/dev/null; then
    olduid=$(silo_uid)
    userdel -r "$ACCT" 2>/dev/null || { userdel "$ACCT" && rm -rf "/home/$ACCT"; }
    rm -rf "/run/qdistro-tier3s-rt/$olduid" "/run/qdistro-tier3s-runsc/$olduid"
fi
rm -f /etc/qdistro/silos.yaml
rm -rf "/var/lib/qdistro/silos/$SILO"
systemctl start qdistro-session-manager.service
wait_for 30 manager_up || { fail "manager not back after reset"; finish; }
pass "prior $ACCT state reset"
# no tier3s anything yet (the account does not exist — assert_ma_clear is
# only meaningful once it does)
is "install: control records" "$(records | wc -l)" 0
is "install: scopes" "$(qry systemctl list-units --all --plain --no-legend 'qdistro-tier3s-*.scope' | grep -c .)" 0
is "install: runsc-bundle processes" "$(runsc_pids | wc -l)" 0

step "1. silo row + allow rule; the account must not exist yet"
is "qt3s account absent before any launch" "$(yes_no getent passwd "$ACCT")" no
sm CreateTier3sSilo ssss "$SILO" headless-smoke "$SILO" none > /dev/null
is "CreateTier3sSilo $SILO" "$(silo_state "$SILO")" Created
set_argv "$SILO=600" | sed 's/^/    /'
is "argv set, manager back up" "$(yes_no manager_up)" yes
set_rules "allow:$ACTION"
is "broker allows the smoke spawn" "$(broker_check "$ACTION")" allow

step "2. first launch: lazy account provisioning, then image refusal"
cur=$(journal_cursor)
sm StartSilo s "$SILO" > /dev/null 2>&1 || true   # expected to fail: no image in the silo store
unit=$(unit_of "$SILO")
wait_for 60 unit_down "$unit" || true
ulog=$(unit_log "$unit" "$cur")
printf '%s\n' "$ulog" | grep -v pam_unix | tail -15 | sed 's/^/    unit: /'
printf '%s\n' "$ulog" | grep -q "is not in the silo's podman store" \
    && pass "first launch refused on the absent silo-store image" \
    || fail "first launch did not refuse on the absent image"
pw=$(getent passwd "$ACCT")
if [ -n "$pw" ]; then pass "spawn created $ACCT"; else fail "spawn did not create $ACCT"; finish; fi
SUID=$(silo_uid); SGID=$(id -g "$ACCT")
info "$ACCT: $pw"
[ "$SUID" -ge 1000 ] && [ "$SUID" != 1000 ] && pass "silo uid $SUID is a regular non-admin uid" \
    || fail "silo uid '$SUID' is not a regular non-admin uid"
is "silo GECOS marker" "$(printf '%s' "$pw" | cut -d: -f5)" "qdistro tier3s silo $SILO"
id -nG "$ACCT" 2>/dev/null | tr ' ' '\n' | grep -qx qdistro-tier3s \
    && pass "silo is a qdistro-tier3s member ($(id -nG "$ACCT"))" \
    || fail "silo is not a qdistro-tier3s member ($(id -nG "$ACCT" 2>/dev/null))"
srow=$(grep "^$ACCT:" /etc/subuid | head -1); grow=$(grep "^$ACCT:" /etc/subgid | head -1)
[ -n "$srow" ] && [ -n "$grow" ] && pass "subuid/subgid allocated ($srow / $grow)" \
    || fail "subuid/subgid missing for $ACCT"
for d in "/run/qdistro-tier3s-rt/$SUID" "/run/qdistro-tier3s-runsc/$SUID"; do
    is "spawn-created $d owner:mode" "$(stat -c '%u %a' "$d" 2>/dev/null)" "$SUID 700"
done
out=$(/usr/lib/qdistro/tier3s/probe.sh --user "$ACCT" 2>&1); rc=$?
printf '%s\n' "$out" | tail -6 | sed 's/^/    /'
is "probe as $ACCT rc" "$rc" 0
left=$(records | wc -l)
is "refused launch left no control records" "$left" 0

step "3. per-silo image delivery (admin builds, silo store loads)"
# admin cannot traverse /root: give the builder a world-readable copy of just
# what it reads (tier3s/ + the snapshot.conf pin beside it).
b=/var/tmp/t3s-build; rm -rf "$b"
install -d -m 0755 "$b" && cp -a "$SRC/tier3s" "$b/" && cp "$SRC/snapshot.conf" "$b/"
d=/var/tmp/t3s-img; rm -rf "$d"; install -d -o admin -m 0755 "$d"
out=$(as_admin bash "$b/tier3s/make-tier3s-image.sh" --oci-archive "$d" headless-smoke 2>&1); rc=$?
printf '%s\n' "$out" | tail -6 | sed 's/^/    /'
is "make-tier3s-image.sh rc" "$rc" 0
[ -f "$d/tier3s-headless-smoke.oci.tar" ] && chmod 0644 "$d/tier3s-headless-smoke.oci.tar" \
    || { fail "oci archive not produced"; finish; }
out=$(pm_s load -i "$d/tier3s-headless-smoke.oci.tar" 2>&1); rc=$?
printf '%s\n' "$out" | tail -2 | sed 's/^/    /'
is "podman load into the silo store rc" "$rc" 0
is "image exists in the silo store" "$(yes_no pm_s image exists "$IMAGE")" yes
# the build leaves the image in admin's store too — remove it there and the
# silo's copy must persist (the stores are independent, not a shared view)
pm rmi -f "$IMAGE" > /dev/null 2>&1
is "image rmi'd from admin's store" "$(yes_no pm image exists "$IMAGE")" no
is "image still exists in the silo store" "$(yes_no pm_s image exists "$IMAGE")" yes

step "4. live launch: identity, placement, record"
TOK=$(up_silo "$SILO")
if [ -n "$TOK" ]; then pass "launch up (token $TOK)"; else fail "launch did not come up"; finish; fi
is "silo Active" "$(silo_state "$SILO")" Active
is "record: admin_uid" "$(rec "$TOK" admin_uid)" 1000
is "record: silo_uid" "$(rec "$TOK" silo_uid)" "$SUID"
is "record: silo_user" "$(rec "$TOK" silo_user)" "$ACCT"
is "record: runsc_root" "$(rec "$TOK" runsc_root)" "/run/qdistro-tier3s-runsc/$SUID"
cpid=$(rec "$TOK" conmon_pid); spid=$(rec "$TOK" sentry_pid)
is "conmon runs as the silo uid" "$(stat -c %u "/proc/$cpid" 2>/dev/null)" "$SUID"
case "$(stat -c %u "/proc/$spid" 2>/dev/null)" in
    "$SUID") info "sentry runs as the silo uid (unexpected under this gVisor)" ;;
    *)       info "sentry host uid: $(stat -c %u "/proc/$spid" 2>/dev/null) (runsc subuid; containment is by scope)" ;;
esac
cg="/sys/fs/cgroup$(rec "$TOK" scope_cgroup)"
nproc=$(tree_procs "$cg" | wc -l)
[ "$nproc" -ge 4 ] && pass "scope $cg holds $nproc launch processes" \
    || fail "scope $cg holds only $nproc processes"
is "container running in the silo store" "$(pm_s inspect --format '{{.State.Status}}' "qdistro-tier3s-$SILO" 2>/dev/null)" running
is "container invisible to admin" "$(pm inspect --format '{{.State.Status}}' "qdistro-tier3s-$SILO" 2>/dev/null | grep -c .)" 0

step "5. StopSilo tears everything down"
sm StopSilo si "$SILO" 10 > /dev/null
is "silo Stopped" "$(silo_state "$SILO")" Stopped
is "unit down" "$(unit_state "$unit")" inactive
[ ! -e "$CTL/$TOK" ] && pass "control record gone" || fail "control record $TOK survived"
[ ! -d "$cg" ] && pass "scope cgroup gone" || fail "scope cgroup $cg survived"
is "container gone from the silo store" "$(yes_no pm_s container exists "qdistro-tier3s-$SILO")" no
assert_ma_clear teardown
finish

#!/bin/bash
# tier3s/spike/c2-silo-uid.sh — Phase C2 stage-1 spike, run as root INSIDE the
# dev VM. THROWAWAY. Answers the `03` Phase C step-3 prerequisite list:
#   1. subuid/subgid allocation per silo user
#   2. keep-id mappings under a silo podman caller (guest uid 1000 -> ?)
#   3. bridge-dir traversal for a silo-uid side (group dance)
#   4. runtime-dir / state ownership without a logind session
#   5. image store readability for a distinct uid (per-silo load vs
#      additionalimagestores under a different uid map)
# Every podman/runsc step as the silo uids goes through as_silo (scrubbed env).
# Nothing is asserted as product behavior; OBSERVE lines carry the evidence.
set -uo pipefail
. "$(dirname "$0")/c2-lib.sh"

say "0. environment"
uname -r; podman --version; "$RUNSC" --version 2>&1 | head -2
getenforce 2>/dev/null || true
obs "admin store images:"; as_admin podman images --format '{{.Repository}}:{{.Tag}} {{.Id}}' 2>&1 | head -5
obs "existing silo/group rows:"; getent group "$BRIDGE_GROUP"; getent passwd user1 user2 2>/dev/null; cat /etc/subuid /etc/subgid 2>/dev/null | head -10

# Idempotent re-runs: drop state a prior run may have left on the guest.
rm -f /home/$SILO_A/.config/containers/storage.conf /home/$SILO_B/.config/containers/storage.conf 2>/dev/null
rm -rf /var/lib/qdistro-tier3s-store /var/tmp/tier3s-spike

say "1. silo users + subuid/subgid allocation"
for u in "$SILO_A" "$SILO_B"; do
    if ! id "$u" >/dev/null 2>&1; then
        useradd -m -s /bin/bash "$u" && obs "useradd $u (no group)" || fail "useradd $u"
        usermod -L "$u" 2>/dev/null
    fi
    obs "$u: $(id "$u")"
done
# Does this Tumbleweed useradd auto-allocate subuids? (login.defs SUB_UID_*)
obs "subuid rows after plain useradd:"; grep -E "^($SILO_A|$SILO_B):" /etc/subuid /etc/subgid 2>/dev/null || echo "(none — useradd did not auto-allocate)"
for u in "$SILO_A" "$SILO_B"; do
    if ! grep -q "^$u:" /etc/subuid 2>/dev/null; then
        usermod --add-subuids 500000-565535 --add-subgids 500000-565535 "$u" \
            && obs "usermod --add-sub{u,g}ids 500000-565535 $u: rc=0" \
            || fail "usermod --add-subuids $u"
    fi
done
obs "subuid rows now:"; grep -E "^($SILO_A|$SILO_B):" /etc/subuid /etc/subgid
command -v getsubids >/dev/null && getsubids "$SILO_A" || true
command -v newuidmap >/dev/null || fail "newuidmap missing"
command -v newgidmap >/dev/null || fail "newgidmap missing"
ls -l "$(command -v newuidmap)" "$(command -v newgidmap)" 2>/dev/null
prep_silo_dirs "$SILO_A"; prep_silo_dirs "$SILO_B"
obs "podman unshare as $SILO_A (exercises newuidmap against /etc/subuid):"
as_silo "$SILO_A" podman unshare cat /proc/self/uid_map 2>&1 | head -5
obs "podman pull as $SILO_A into its OWN store (registry access is host-side):"
as_silo "$SILO_A" podman pull -q registry.opensuse.org/opensuse/busybox:latest 2>&1 | tail -3 \
    || as_silo "$SILO_A" podman pull -q docker.io/library/busybox:latest 2>&1 | tail -3
obs "silo store after pull:"; as_silo "$SILO_A" podman images --format '{{.Repository}}:{{.Tag}}' 2>&1 | head -4

say "2. keep-id under a silo podman caller"
IMG="$(as_silo "$SILO_A" podman images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | head -1)"
obs "image usable by $SILO_A: ${IMG:-<none — pull above failed>}"
obs "workload image visible to admin: $(as_admin podman images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | head -3 | tr '\n' ' ')"
# First measure what podman-as-silo does WITHOUT the image: pull failure mode
# tells us whether the store path itself is reachable.
SUID=$(id -u "$SILO_A")
RROOT=/run/qdistro-tier3s-runsc/$SUID
obs "silo runtime/runsc dirs:"; stat -c '%n %U:%G %a' "/run/qdistro-tier3s-rt/$SUID" "$RROOT"
obs "silo's /run/user/$SUID exists? $([ -d /run/user/$SUID ] && echo yes || echo no — no logind session)"
INNER='id; echo uid_map:; cat /proc/self/uid_map; echo gid_map:; cat /proc/self/gid_map; stat -c "stat %n %u:%g %a" /tmp /run/user/1000 2>&1; touch /run/user/1000/probe 2>&1 && echo wrote-probe; sleep 45'
if [ -n "$IMG" ]; then
    obs "B: launch under runsc as $SILO_A, keep-id --user 1000:1000"
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        --runtime-flag=root=$RROOT \
        run --rm --security-opt label=disable --security-opt no-new-privileges \
        --security-opt "seccomp=$SMOKE" --cap-drop=ALL \
        --userns=keep-id --user 1000:1000 --read-only \
        --tmpfs /tmp:size=64m --tmpfs /run/user/1000:rw,U,mode=0700 \
        --network=none \
        --name t3s-c2-keepid -d "$IMG" sh -c "$INNER" 2>&1
    sleep 5
    obs "container processes on the host (uid should be $SUID=$SILO_A, not $ADMIN_UID):"
    for p in $(pgrep -f 't3s-c2-keepid|runsc' 2>/dev/null | head -15); do
        [ -d /proc/$p ] && printf '  pid=%s uid=%s comm=%s exe=%s\n' "$p" \
            "$(awk '/^Uid:/{print $2}' /proc/$p/status)" "$(cat /proc/$p/comm)" \
            "$(readlink /proc/$p/exe)"
    done
    obs "container output (guest-side identity):"
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        --runtime-flag=root=$RROOT logs t3s-c2-keepid 2>&1 | head -15
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        --runtime-flag=root=$RROOT rm -f t3s-c2-keepid >/dev/null 2>&1
else
    obs "step 2 deferred: no image readable by $SILO_A yet — see step 5 result"
fi

say "3. bridge-dir traversal (group dance)"
# Admin-owned dir, group-traversable only; socket inside owned admin:<grp> 0660.
BDIR=$WORK/bridge-dance
rm -rf "$BDIR"; install -d -o "$ADMIN" -g "$BRIDGE_GROUP" -m 0710 "$BDIR" 2>/dev/null \
    || { obs "group $BRIDGE_GROUP absent — creating test group qdistro-t3s-c2"; \
         groupadd -f qdistro-t3s-c2; BRIDGE_GROUP=qdistro-t3s-c2; \
         install -d -o "$ADMIN" -g "$BRIDGE_GROUP" -m 0710 "$BDIR"; }
usermod -a -G "$BRIDGE_GROUP" "$SILO_A" && obs "$SILO_A added to $BRIDGE_GROUP: $(id "$SILO_A")"
obs "$SILO_B groups (control, not a member): $(id "$SILO_B")"
SOCK=$BDIR/bridge.sock
# A trivial unix listener as admin to stand in for the waypipe endpoint.
as_admin socat "UNIX-LISTEN:$SOCK,mode=660" SYSTEM:'echo pong' &
sleep 1
chgrp "$BRIDGE_GROUP" "$SOCK" 2>&1   # admin isn't a member; group fixup is a provisioning step
stat -c 'stat %n %U:%G %a' "$BDIR" "$SOCK" 2>&1
obs "$SILO_A (member) connect:"; as_silo "$SILO_A" socat - UNIX-CONNECT:"$SOCK" </dev/null 2>&1; echo "rc=$?"
obs "$SILO_B (non-member) connect:"; as_silo "$SILO_B" socat - UNIX-CONNECT:"$SOCK" </dev/null 2>&1; echo "rc=$?"
obs "$SILO_B (non-member) list dir:"; as_silo "$SILO_B" ls "$BDIR" 2>&1; echo "rc=$?"
obs "$SILO_A (member) list dir:"; as_silo "$SILO_A" ls "$BDIR" 2>&1; echo "rc=$?"
obs "admin (owner) list dir:"; as_admin ls "$BDIR" 2>&1; echo "rc=$?"
pkill -f "socat.*UNIX-LISTEN" 2>/dev/null; true

say "4. runtime-dir / state ownership"
# What does podman-as-silo need for XDG_RUNTIME_DIR, runroot, graphroot?
obs "silo home: $(stat -c '%n %U:%G %a' /home/$SILO_A 2>&1)"
obs "podman info as $SILO_A (storage section):"
as_silo "$SILO_A" podman info --format '{{.Store.GraphRoot}}|{{.Store.RunRoot}}|{{.Store.GraphDriverName}}' 2>&1 | head -5
obs "dirs after info:"; find /home/$SILO_A/.local/share/containers -maxdepth 2 2>/dev/null | head -8; \
    stat -c '%n %U:%G %a' /run/qdistro-tier3s-rt/$SUID/* 2>/dev/null | head -5
# State bind rule check: silo-owned host dir bind-mounted into keep-id
# container — guest uid 1000 sees it as uid 1000?
SD=$WORK/state-$SILO_A; rm -rf "$SD"; install -d -o "$SILO_A" -g "$SILO_A" -m 0700 "$SD"
if [ -n "$IMG" ]; then
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        --runtime-flag=root=$RROOT \
        run --rm --security-opt label=disable --userns=keep-id --user 1000:1000 \
        --network=none -v "$SD:/state" "$IMG" \
        sh -c 'stat -c "state %u:%g %a" /state; touch /state/x && echo wrote; id -u' 2>&1 | head -8
fi

say "5. image store readability for a distinct uid"
obs "admin home traversal for $SILO_A: $(as_silo "$SILO_A" ls /home/$ADMIN/.local/share/containers 2>&1 | head -1)"
obs "5a. per-silo store already exercised above: silo pulled + keeps its own images"
# (b) additionalimagestores: admin populates a rootless store at a shared
#     path; the silo lists it via its own storage.conf. THE mapping
#     question from `03`: store content created under admin's uid map must
#     be traversable under the silo's distinct uid map.
SHARED=/var/lib/qdistro-tier3s-store
rm -rf "$SHARED"; install -d -o "$ADMIN" -g "$ADMIN" -m 0755 "$SHARED"
# Populate as a ROOTLESS store (per-store lock files then have the store
# owner's ids — a root podman store's lock files are unreadable outright).
# Variant A: admin-owned store at a shared path — still a distinct uid map
# for the silo, which is what `03` wants proven.
as_admin podman --root "$SHARED" pull -q "${IMG:-registry.opensuse.org/opensuse/busybox:latest}" 2>&1 | tail -2
obs "shared store perms (admin-populated):"; find "$SHARED" -maxdepth 2 | head -6; du -sh "$SHARED"
chmod -R a+rX "$SHARED" 2>/dev/null   # traversal/read for other uids; locks still owner-only?
obs "after chmod -R a+rX:"; find "$SHARED/overlay-images" -maxdepth 1 2>/dev/null | head -5
stat -c '%n %U:%G %a' "$SHARED"/overlay-images/images.lock 2>&1
install -d -o "$SILO_A" -g "$SILO_A" -m 0700 /home/$SILO_A/.config /home/$SILO_A/.config/containers
printf '[storage]\ndriver="overlay"\n[storage.options]\nadditionalimagestores=["%s"]\n' "$SHARED" \
    > /home/$SILO_A/.config/containers/storage.conf
chown "$SILO_A:$SILO_A" /home/$SILO_A/.config/containers/storage.conf
obs "silo podman images WITH additionalimagestores=$SHARED:"
as_silo "$SILO_A" podman images --format '{{.Repository}}:{{.Tag}} {{.ReadOnly}}' 2>&1 | head -6
obs "and a run from the shared store (the real test — distinct uid map):"
as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
    --runtime-flag=root=$RROOT \
    run --rm --userns=keep-id --user 1000:1000 --network=none \
    "$(as_silo "$SILO_A" podman images --format '{{.Repository}}:{{.Tag}}' | grep tier3s | head -1)" \
    sh -c 'id; echo SHARED-STORE-RUN-OK' 2>&1 | head -8

say "6. summary of open answers"
echo "See OBSERVE lines above; findings get written into 13-phase-C2-progress.md"
echo "and the decided model into tier3s/CONTRACT.md."

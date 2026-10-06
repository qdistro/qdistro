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
# Keep $WORK/smoke.json (installed by c2-lib at source time); per-test dirs
# are rm -rf'd individually where they are created. The test users are
# DELETED so useradd + subuid allocation is observed fresh each run.
for u in "$SILO_A" "$SILO_B"; do
    id "$u" >/dev/null 2>&1 && userdel -rf "$u" 2>/dev/null
done
rm -rf /var/lib/qdistro-tier3s-store
rm -rf /run/qdistro-tier3s-rt /run/qdistro-tier3s-runsc

say "1. silo users + subuid/subgid allocation"
for u in "$SILO_A" "$SILO_B"; do
    useradd -m -s /bin/bash "$u" && obs "useradd $u rc=0" || fail "useradd $u"
    usermod -L "$u" 2>/dev/null
    obs "$u: $(id "$u" 2>&1)"
done
# Does this Tumbleweed useradd auto-allocate subuids? (login.defs SUB_UID_*)
obs "subuid rows after plain useradd:"; grep -E "^($SILO_A|$SILO_B):" /etc/subuid /etc/subgid 2>/dev/null || echo "(none — useradd did not auto-allocate)"
for u in "$SILO_A" "$SILO_B"; do
    # Check subuid and subgid independently; distinct fallback ranges.
    grep -q "^$u:" /etc/subuid 2>/dev/null || { usermod --add-subuids 500000-565535 "$u" && obs "usermod --add-subuids $u: rc=0" || fail "usermod --add-subuids $u"; }
    grep -q "^$u:" /etc/subgid 2>/dev/null || { usermod --add-subgids 500000-565535 "$u" && obs "usermod --add-subgids $u: rc=0" || fail "usermod --add-subgids $u"; }
done
obs "subuid rows now:"; grep -E "^($SILO_A|$SILO_B):" /etc/subuid /etc/subgid
command -v getsubids >/dev/null && getsubids "$SILO_A" || true
command -v newuidmap >/dev/null || fail "newuidmap missing"
command -v newgidmap >/dev/null || fail "newgidmap missing"
ls -l "$(command -v newuidmap)" "$(command -v newgidmap)" 2>/dev/null
getcap "$(command -v newuidmap)" "$(command -v newgidmap)" 2>/dev/null
prep_silo_dirs "$SILO_A"; prep_silo_dirs "$SILO_B"
obs "podman unshare as $SILO_A (full uid_map + gid_map):"
probe as_silo "$SILO_A" podman unshare sh -c 'cat /proc/self/uid_map; echo ---; cat /proc/self/gid_map'
obs "podman pull as $SILO_A into its OWN store (registry access is host-side):"
probe as_silo "$SILO_A" podman pull -q registry.opensuse.org/opensuse/busybox:latest \
    || probe as_silo "$SILO_A" podman pull -q docker.io/library/busybox:latest
obs "silo store after pull:"; probe as_silo "$SILO_A" podman images --format '{{.Repository}}:{{.Tag}}'

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
    # Wrapper enforcement probes in the silo context: caller --root must be
    # refused, and a missing per-uid root must refuse (no silent minting).
    obs "A: wrapper refuses caller-supplied --root (silo context):"
    probe as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        --runtime-flag=root=/tmp/t3s-evil-root \
        run --rm --security-opt label=disable --userns=keep-id --network=none \
        "$IMG" true
    obs "A-bis: same but doubled flag (parser-level rejection, distinct from wrapper refusal):"
    probe as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        --runtime-flag=--root=/tmp/t3s-evil-root \
        run --rm --security-opt label=disable --userns=keep-id --network=none \
        "$IMG" true
    obs "A2: wrapper refuses when per-uid root is missing:"
    rmdir "$RROOT"
    probe as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        run --rm --security-opt label=disable --userns=keep-id --network=none "$IMG" true
    install -d -m 0700 -o "$SILO_A" -g "$SILO_A" "$RROOT"   # restore
    obs "B: launch under runsc as $SILO_A, keep-id --user 1000:1000"
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        run --security-opt label=disable --security-opt no-new-privileges \
        --security-opt "seccomp=$SMOKE" --cap-drop=ALL \
        --userns=keep-id --user 1000:1000 --read-only \
        --tmpfs /tmp:size=64m --tmpfs /run/user/1000:rw,U,mode=0700 \
        --network=none \
        --name t3s-c2-keepid -d "$IMG" sh -c "$INNER" 2>&1
    sleep 5
    obs "container state (5s after create+start):"
    as_silo "$SILO_A" podman inspect --format '{{.State.Status}} exit={{.State.ExitCode}} err={{.State.Error}}' \
        t3s-c2-keepid 2>&1 | head -3
    obs "container processes on the host (uid should be $SUID=$SILO_A, not $ADMIN_UID):"
    for p in $(pgrep -f 't3s-c2-keepid|runsc' 2>/dev/null | head -15); do
        [ -d /proc/$p ] && printf '  pid=%s uid=%s comm=%s exe=%s\n' "$p" \
            "$(awk '/^Uid:/{print $2}' /proc/$p/status)" "$(cat /proc/$p/comm)" \
            "$(readlink /proc/$p/exe)"
    done
    obs "container output (guest-side identity):"
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        logs t3s-c2-keepid 2>&1 | head -15
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        rm -f t3s-c2-keepid >/dev/null 2>&1
    # Identity evidence needs a LIVE container; the hardened profile above
    # may itself deny fork — launch a minimal variant to isolate.
    obs "B2: minimal launch (no seccomp/cap-drop) for identity evidence"
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        run --security-opt label=disable --userns=keep-id --user 1000:1000 \
        --network=none --name t3s-c2-ident -d "$IMG" \
        sh -c 'id; echo uid_map:; cat /proc/self/uid_map; echo gid_map:; cat /proc/self/gid_map; sleep 30' 2>&1 | tail -3
    sleep 4
    as_silo "$SILO_A" podman inspect --format '{{.State.Status}} exit={{.State.ExitCode}}' t3s-c2-ident 2>/dev/null
    obs "ident container host procs:"
    for p in $(pgrep -f 't3s-c2-ident|runsc' 2>/dev/null | head -15); do
        [ -d /proc/$p ] && printf '  pid=%s uid=%s comm=%s exe=%s\n' "$p" \
            "$(awk '/^Uid:/{print $2}' /proc/$p/status)" "$(cat /proc/$p/comm)" \
            "$(readlink /proc/$p/exe)"
    done
    as_silo "$SILO_A" podman logs t3s-c2-ident 2>&1 | grep -v 'level=warning' | head -10
    as_silo "$SILO_A" podman rm -f t3s-c2-ident >/dev/null 2>&1
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

# --- sandbox-side connect: the real B-i path. The waypipe SERVER inside the
# sandbox connects OUT to the host bridge socket via host-uds=open. The
# connecting host-side identity is what matters; probe each gating model.
say "3b. sandbox->host connect through runsc (the real bridge path)"
SOCKIMG=docker.io/alpine/socat:latest
obs "pull socat image as $SILO_A:"; probe as_silo "$SILO_A" podman pull -q "$SOCKIMG"
cat > /root/peercred.py <<'PYEOF'
import socket,struct,os,sys
p=sys.argv[1]; mode=int(sys.argv[2],8)
s=socket.socket(socket.AF_UNIX)
if os.path.exists(p): os.unlink(p)
s.bind(p); os.chmod(p,mode); s.listen(2)
print("BOUND %s mode %o"%(p,mode),flush=True)
for i in range(2):
    c,_=s.accept()
    pid,uid,gid=struct.unpack("3i",c.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
    print("PEER%d pid=%d uid=%d gid=%d"%(i,pid,uid,gid),flush=True)
    c.send(b"pong\n"); c.close()
s.close()
PYEOF
sbx_connect() {   # $1=label $2=sockmode $3=sockowner $4..=extra podman args
    local label=$1 smode=$2 sown=$3; shift 3
    obs "$label:"
    timeout 40 python3 /root/peercred.py "$SOCK" "$smode" >/root/peer.out 2>&1 &
    sleep 1
    [ "$sown" != "-" ] && chown "$sown" "$SOCK"
    stat -c 'sock %n %U:%G %a' "$SOCK"
    probe as_silo "$SILO_A" podman --runtime "$WRAPPER" \
        --runtime-flag=network=none --runtime-flag=host-uds=open \
        run --rm --security-opt label=disable --userns=keep-id --network=none \
        -v "$BDIR:/bridge:ro" "$@" "$SOCKIMG" - UNIX-CONNECT:/bridge/bridge.sock
    sleep 1; cat /root/peer.out 2>/dev/null; wait 2>/dev/null; true
}
# control: world-open dir+socket — should always connect
chmod 0711 "$BDIR"; chown "$ADMIN:$ADMIN" "$BDIR"; setfacl -b "$BDIR" 2>/dev/null
sbx_connect "control: dir 0711, socket 0666" 0666 "-"
# group-guarded dir+socket (the tier-3 host-side dance model): silo is a
# member on the host, but the sandbox's creds are userns-mapped — expect
# the group to be unmappable and the connect denied.
chmod 0710 "$BDIR"; chown "$ADMIN:$BRIDGE_GROUP" "$BDIR"
sbx_connect "group dance: dir grp 0710, socket grp 0660 (member silo)" 0660 "$ADMIN:$BRIDGE_GROUP"
chmod 0711 "$BDIR"; chown "$ADMIN:$ADMIN" "$BDIR"   # dir opened: isolate the socket-level check
sbx_connect "same socket grp 0660 + open dir 0711, --group-add keep-groups" 0660 "$ADMIN:$BRIDGE_GROUP" --group-add keep-groups
# silo-owned socket — the model the sandbox path actually needs:
# guest-uid sees host uid 1001 as itself; socket 0600 silo-owned works.
sbx_connect "silo-owned socket 0600, dir admin 0711" 0600 "$SILO_A:$SILO_A"
# recorded evidence for the keep-groups failure: what gids does the guest
# actually hold, and what does the synthesized gid_map look like?
obs "keep-groups guest-side group set (is $BRIDGE_GROUP mapped in?):"
probe as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
    run --rm --security-opt label=disable --userns=keep-id --group-add keep-groups \
    --network=none "$IMG" sh -c 'id'


say "4. runtime-dir / state ownership"
# What does podman-as-silo need for XDG_RUNTIME_DIR, runroot, graphroot?
obs "silo home: $(stat -c '%n %U:%G %a' /home/$SILO_A 2>&1)"
obs "podman info as $SILO_A (storage section):"
as_silo "$SILO_A" podman info --format '{{.Store.GraphRoot}}|{{.Store.RunRoot}}|{{.Store.GraphDriverName}}' 2>&1 | head -5
obs "dirs after info:"; find /home/$SILO_A/.local/share/containers -maxdepth 2 2>/dev/null | head -8; \
    stat -c '%n %U:%G %a' /run/qdistro-tier3s-rt/$SUID/* 2>/dev/null | head -5
# State bind rule check: silo-owned host dir bind-mounted into a container.
# Each variant gets a FRESH silo-owned dir — :U and differing maps mutate
# host-side ownership, so reuse between variants corrupts the evidence.
mkstatedir() { rm -rf "$1"; install -d -o "$SILO_A" -g "$SILO_A" -m 0700 "$1"; }
if [ -n "$IMG" ]; then
    SD=$WORK/state-keepid1000; mkstatedir "$SD"
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        run --rm --security-opt label=disable --userns=keep-id --user 1000:1000 \
        --network=none -v "$SD:/state" "$IMG" \
        sh -c 'stat -c "state %u:%g %a" /state; touch /state/x && echo wrote; id -u' 2>&1 | head -8
    obs "same bind with :U (podman chowns to container user):"
    SD=$WORK/state-colonU; mkstatedir "$SD"
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        run --rm --security-opt label=disable --userns=keep-id --user 1000:1000 \
        --network=none -v "$SD:/state:U" "$IMG" \
        sh -c 'stat -c "state %u:%g %a" /state; touch /state/x && echo wrote; id -u' 2>&1 | head -8
    stat -c 'host-side %n %u:%g %a' "$SD" "$SD/x" 2>&1   # who did :U chown to?
    # keep-id keeps the SAME numeric uid: caller 1001 -> guest 1001. So a
    # guest-uid-1000 workload maps to a SUBUID unless an explicit uidmap
    # pins guest-1000 -> intermediate 0 (= the caller).
    obs "keep-id running AS guest 1001 (same-numeric model), fresh silo dir:"
    SD=$WORK/state-keepid1001; mkstatedir "$SD"
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        run --rm --security-opt label=disable --userns=keep-id \
        --network=none -v "$SD:/state" "$IMG" \
        sh -c 'id -u; stat -c "state %u:%g %a" /state; touch /state/y && echo wrote' 2>&1 | head -8
    obs "explicit uidmap guest-1000 -> caller (the 1000-preserving variant), fresh silo dir:"
    SD=$WORK/state-uidmap; mkstatedir "$SD"
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        run --rm --security-opt label=disable \
        --uidmap 0:1:1000 --uidmap 1000:0:1 --uidmap 1001:1001:64535 \
        --gidmap 0:1:1000 --gidmap 1000:0:1 --gidmap 1001:1001:64535 \
        --user 1000:1000 --network=none -v "$SD:/state" "$IMG" \
        sh -c 'id -u; stat -c "state %u:%g %a" /state; touch /state/z && echo wrote' 2>&1 | head -8
    obs "keep-id:uid=1000,gid=1000 (podman's own retarget spelling), fresh silo dir:"
    SD=$WORK/state-keepidretarget; mkstatedir "$SD"
    as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
        run --rm --security-opt label=disable \
        --userns=keep-id:uid=1000,gid=1000 --user 1000:1000 \
        --network=none -v "$SD:/state" "$IMG" \
        sh -c 'id -u; stat -c "state %u:%g %a" /state; touch /state/w && echo wrote' 2>&1 | head -8
    stat -c 'host-side %n %u:%g %a' "$SD" 2>&1
fi

say "5. image store readability for a distinct uid"
obs "admin home traversal for $SILO_A: $(as_silo "$SILO_A" ls /home/$ADMIN/.local/share/containers 2>&1 | head -1)"
obs "5a. per-silo store already exercised above: silo pulled + keeps its own images"
# Negative control: a ROOT-populated store's lock files are unreadable to
# the silo outright — record it this round, not just by provenance.
SHARED=/var/lib/qdistro-tier3s-store
rm -rf "$SHARED"; install -d -m 0755 "$SHARED"
obs "root-populated shared store (negative control):"
podman --root "$SHARED" pull -q registry.opensuse.org/opensuse/busybox:latest 2>&1 | tail -2
install -d -o "$SILO_A" -g "$SILO_A" -m 0700 /home/$SILO_A/.config /home/$SILO_A/.config/containers
printf '[storage]\ndriver="overlay"\n[storage.options]\nadditionalimagestores=["%s"]\n' "$SHARED" \
    > /home/$SILO_A/.config/containers/storage.conf
chown "$SILO_A:$SILO_A" /home/$SILO_A/.config/containers/storage.conf
obs "silo images vs root-populated store (expect lock EACCES):"
probe as_silo "$SILO_A" podman images --format '{{.Repository}}:{{.Tag}}'
# (b) additionalimagestores: admin populates a ROOTLESS store at a shared
#     path; the silo lists it via its own storage.conf. THE mapping
#     question from `03`: store content created under admin's uid map must
#     be traversable under the silo's distinct uid map.
rm -rf "$SHARED"; install -d -o "$ADMIN" -g "$ADMIN" -m 0755 "$SHARED"
obs "admin populates the shared (rootless) store:"
probe as_admin podman --root "$SHARED" pull -q "${IMG:-registry.opensuse.org/opensuse/busybox:latest}"
obs "shared store perms (admin-populated):"; find "$SHARED" -maxdepth 2 | head -6; du -sh "$SHARED"
chmod -R a+rX "$SHARED" 2>/dev/null   # traversal/read for other uids
stat -c '%n %U:%G %a' "$SHARED"/overlay-images/images.lock 2>&1
obs "silo podman images WITH additionalimagestores=$SHARED:"
probe as_silo "$SILO_A" podman images --format '{{.Repository}}:{{.Tag}} {{.ReadOnly}}'
# THE run test must be unambiguous: the ref must not exist in the silo's
# PRIVATE store, or "success" could come from the private copy.
obs "removing private copy so only the shared store has it:"
probe as_silo "$SILO_A" podman rmi "$IMG" 2>&1
probe as_silo "$SILO_A" podman images --format '{{.Repository}}:{{.Tag}} {{.ReadOnly}}'
SIMG=$(as_silo "$SILO_A" podman images --format '{{.Repository}}:{{.Tag}} {{.ReadOnly}}' 2>/dev/null | awk '$2=="true"{print $1; exit}')
obs "readonly image ref picked: ${SIMG:-<none>}"
obs "run from shared store --pull=never (private store cannot satisfy it):"
[ -n "$SIMG" ] && probe as_silo "$SILO_A" podman --runtime "$WRAPPER" --runtime-flag=network=none \
    run --rm --pull=never --security-opt label=disable --userns=keep-id --user 1000:1000 --network=none \
    "$SIMG" sh -c 'id; echo SHARED-STORE-RUN-OK'
# multi-reader: silo B lists the same shared store through its own conf
install -d -o "$SILO_B" -g "$SILO_B" -m 0700 /home/$SILO_B/.config /home/$SILO_B/.config/containers
printf '[storage]\ndriver="overlay"\n[storage.options]\nadditionalimagestores=["%s"]\n' "$SHARED" \
    > /home/$SILO_B/.config/containers/storage.conf
chown "$SILO_B:$SILO_B" /home/$SILO_B/.config/containers/storage.conf
obs "silo B (no private copy at all) images vs shared store:"
probe as_silo "$SILO_B" podman images --format '{{.Repository}}:{{.Tag}} {{.ReadOnly}}'
obs "silo B runs it too:"
[ -n "$SIMG" ] && probe as_silo "$SILO_B" podman --runtime "$WRAPPER" --runtime-flag=network=none \
    run --rm --pull=never --security-opt label=disable --userns=keep-id --user 1000:1000 --network=none \
    "$SIMG" sh -c 'echo SILO-B-SHARED-OK'

say "6. summary"
echo "FAILS=$FAILS"
echo "See OBSERVE lines above; findings get written into 13-phase-C2-progress.md"
echo "and the decided model into tier3s/CONTRACT.md."
[ "$FAILS" -eq 0 ]
